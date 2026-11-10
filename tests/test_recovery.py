import json
import sqlite3
import unittest
from unittest.mock import patch

import test_improvement as helpers
from evo_arena import execution_control as control
from evo_arena.experiment_budget import ExperimentBudget
from evo_arena.evolution import VersionExperiment
from evo_arena.evaluation import evaluate
from evo_arena.harness import TrialGateway
from evo_arena.improvement import finalize
from evo_arena.trial_resources import owns_container, reconcile
from evo_arena.web import Manager
from test_web import DummyThread


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = helpers.ImprovementTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def test_restart_after_confirmed_trial_skips_it_and_preserves_budget(self):
        f = self.f
        ex, bindings, _ = f.create()
        original = f.engine.save
        def crash(experiment, completed_run=None):
            original(experiment, completed_run)
            if completed_run and len(experiment["runs"]) == 2:
                control.recover(f.store)
                raise control.ExecutionStopped("owner died")
        with patch.object(f.engine, "save", crash):
            interrupted = f.execute(ex, bindings)
        first_ids = [r["id"] for r in interrupted["runs"]]
        deadline = f.store.get("evo_budget", ex["id"])["deadline"]
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(control.resume_reasons(f.store, interrupted), [])
        resumed, fresh = control.enqueue_resume(f.store, ex["id"], "resume-once")
        self.assertTrue(fresh)
        self.assertFalse(control.enqueue_resume(f.store, ex["id"], "resume-once")[1])
        result = f.execute(resumed, bindings)
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual([r["id"] for r in result["runs"][:2]], first_ids)
        self.assertEqual(result["budget"]["calls"], 32)
        self.assertEqual(result["budget"]["deadline"], deadline)
        self.assertEqual(len(f.store.list("evo_checkpoint")), 30)
        self.assertEqual(result["epoch"], 3)

    def test_captured_optimizer_output_survives_crash_before_experiment_reference(self):
        f = self.f
        for phase in ("diagnosis", "proposal"):
            with self.subTest(phase=phase):
                ex, bindings, _ = f.create(phase)
                original = f.store.create
                crashed = []
                def create(kind, ident, data):
                    value = original(kind, ident, data)
                    if kind == "evo_optimizer_output" and data["phase"] == phase and not crashed:
                        crashed.append(ident)
                        control.recover(f.store)
                        raise control.ExecutionStopped("crash after durable raw output")
                    return value
                with patch.object(f.store, "create", create):
                    result = f.execute(ex, bindings)
                self.assertEqual(result["status"], "interrupted")
                resumed, _ = control.enqueue_resume(f.store, ex["id"], phase)
                result = f.execute(resumed, bindings)
                self.assertEqual(result["budget"]["calls"], 32)
                self.assertEqual(len(result["optimizer_calls"]), 2)
                self.assertIn(crashed[0], [a["id"] for a in result["optimizer_attempts"]])
                # Keep the next subcase on the same initial recommendation.
                project = f.store.get("evo_project", result["project"])
                project.update(current_version=f.baseline["id"])
                f.store.update("evo_project", project["id"], project)

    def test_pending_model_call_becomes_unknown_and_cannot_be_replayed(self):
        f = self.f
        ex, _, _ = f.create()
        control.acquire(f.store, ex)
        budget = ExperimentBudget(f.store, ex["id"], ex)
        budget.start(ex["limits"])
        call = budget.claim(None, "diagnosis", "hash")
        control.recover(f.store)
        self.assertEqual(f.store.get("evo_call", call)["status"], "unknown")
        self.assertEqual(budget.public()["unknown_usage_calls"], 1)
        self.assertEqual(budget.public()["calls"], 1)
        with self.assertRaises(ValueError): control.enqueue_resume(f.store, ex["id"], "retry")
        with self.assertRaises(control.ExecutionStopped): budget.claim(None, "diagnosis", "hash")

    def test_known_charge_without_raw_output_is_not_reissued(self):
        f = self.f
        ex, _, _ = f.create()
        control.acquire(f.store, ex)
        budget = ExperimentBudget(f.store, ex["id"], ex); budget.start(ex["limits"])
        call = budget.claim(None, "proposal", "hash"); budget.settle(call, {"total_tokens": 9})
        control.recover(f.store)
        reasons = control.resume_reasons(f.store, f.store.get("evo_experiment", ex["id"]))
        self.assertTrue(any("结果尚未落盘" in r for r in reasons))
        with self.assertRaises(ValueError): control.enqueue_resume(f.store, ex["id"], "retry")

    def test_old_epoch_cannot_save_or_promote_after_recovery(self):
        f = self.f
        ex, bindings, _ = f.create()
        with patch("evo_arena.improvement.finalize"):
            old = f.execute(ex, bindings)
        control.recover(f.store)
        resumed, _ = control.enqueue_resume(f.store, ex["id"], "resume")
        control.acquire(f.store, resumed)
        with self.assertRaises(control.ExecutionStopped): f.engine.save(old)
        with self.assertRaises(control.ExecutionStopped): finalize(f.store, old)
        self.assertEqual(f.store.list("evo_decision"), [])
        self.assertEqual(f.store.get("evo_project", ex["project"])["revision"], 0)

    def test_cancel_blocks_new_model_and_business_actions_and_keeps_known_usage(self):
        f = self.f
        ex, bindings, _ = f.create()
        original = f.engine.run_row
        def cancel_after_trial(*args):
            run = original(*args)
            control.request_cancel(f.store, ex["id"])
            control.request_cancel(f.store, ex["id"])
            return run
        with patch.object(f.engine, "run_row", cancel_after_trial):
            result = f.execute(ex, bindings)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["decision"]["reason"], "cancelled")
        self.assertEqual(result["budget"]["calls"], 1)
        budget = ExperimentBudget(f.store, ex["id"], ex)
        gateway = TrialGateway(f.settings, lambda *args: None, experiment_budget=budget)
        response = gateway.dispatch({"route": "submit"}, f.store.root)
        self.assertEqual(response[0], 409)
        self.assertEqual(gateway.writes, 0)
        self.assertEqual(control.request_cancel(f.store, ex["id"])["status"], "cancelled")
        with self.assertRaises(ValueError): control.enqueue_resume(f.store, ex["id"], "resume")

    def test_unconfirmed_trial_and_changed_runtime_refuse_resume(self):
        f = self.f
        ex, _, _ = f.create(); control.acquire(f.store, ex)
        ex["runs"].append({"id": "tr-unknown", "status": "running"})
        f.engine.save(ex); control.recover(f.store)
        saved = f.store.get("evo_experiment", ex["id"])
        self.assertTrue(any("未确认 Trial" in r for r in control.resume_reasons(f.store, saved)))
        with patch("evo_arena.execution_control.protocol_hash", return_value="changed"):
            self.assertTrue(any("代码已变化" in r for r in control.resume_reasons(f.store, saved)))
        with self.assertRaises(sqlite3.IntegrityError): f.store.update("evo_intent", ex["id"], {})

    def test_cancellation_survives_restart_and_settles_inflight_known_usage(self):
        f = self.f
        ex, bindings, _ = f.create()
        class InflightTrial:
            def __init__(self, *args): pass
            def execute(self, run, root, original, record, version, case, budget):
                call = budget.claim(run["id"], "execution", "inflight")
                control.request_cancel(f.store, ex["id"])
                budget.settle(call, {"total_tokens": 17})
                budget.check_control()
        with patch.object(f, "runner", return_value=InflightTrial):
            result = f.execute(ex, bindings)
        self.assertEqual((result["status"], result["budget"]["tokens"], result["budget"]["pending"]), ("cancelled", 17, 0))
        second, _, _ = f.create("cancel-before-restart")
        control.acquire(f.store, second)
        control.request_cancel(f.store, second["id"])
        control.recover(f.store)
        self.assertEqual(f.store.get("evo_experiment", second["id"])["status"], "cancelled")
        with self.assertRaises(ValueError): control.enqueue_resume(f.store, second["id"], "cannot-resume")

    def test_expired_absolute_deadline_is_not_extended_by_resume(self):
        f = self.f
        ex, _, _ = f.create(); control.acquire(f.store, ex)
        budget = ExperimentBudget(f.store, ex["id"], ex); budget.start(ex["limits"])
        deadline = budget.public()["deadline"]
        control.recover(f.store)
        with patch("evo_arena.experiment_budget.time.time", return_value=deadline + 1):
            with self.assertRaises(ValueError): control.enqueue_resume(f.store, ex["id"], "expired")
        self.assertEqual(budget.public()["deadline"], deadline)

    def test_cancellation_between_last_result_and_terminal_commit_wins(self):
        f = self.f
        f.engine = VersionExperiment(f.store, f.registry, f.harbor, "test-image")
        candidate = f.registry.derive(f.baseline, "Manual", "policy.md", "Inspect the state and verify the final outcome.")
        ex, settings, _ = f.engine.create({"baseline": f.baseline["id"], "candidate": candidate["id"], "mode": "smoke"}, "late-cancel")
        cancelled = []
        def final_evaluation(plan, runs):
            if ex["status"] == "completed" and not cancelled:
                cancelled.append(True)
                control.request_cancel(f.store, ex["id"])
            return evaluate(plan, runs)
        with patch("evo_arena.evolution.evaluate", final_evaluation), patch.object(f.engine, "prepare", f.fake_prepare), \
             patch("evo_arena.evolution.HarborTrial", f.runner()):
            f.engine.execute(ex, settings)
        result = f.store.get("evo_experiment", ex["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(f.store.get("evo_control", ex["id"])["state"], "cancelled")
        self.assertEqual(len(f.store.list("evo_checkpoint")), 6)

    def test_duplicate_resume_does_not_start_another_worker(self):
        f = self.f
        ex, _, _ = f.create(); control.recover(f.store)
        manager = Manager(f.store, f.settings, "image")
        with patch("evo_arena.web.threading.Thread", DummyThread):
            first, code = manager.resume_evolution(f.engine, ex["id"], "resume")
            self.assertEqual(code, 202)
            again, code = manager.resume_evolution(f.engine, ex["id"], "resume")
            self.assertEqual((code, again["id"]), (200, first["id"]))
            self.assertEqual(manager.resume_evolution(f.engine, ex["id"], "another")[1], 409)

    def test_cleanup_only_removes_exact_trial_mount_owners(self):
        f = self.f
        resource = {"id": "tr-owned", "experiment": "ex-test", "workspace": str(f.store.root / "trials" / "tr-owned"),
                    "state": "declared", "harbor": "/fake/harbor", "jobs_dir": str(f.store.root / "jobs"), "job": "job-unique"}
        f.store.create("evo_resource", resource["id"], resource)
        ours = {"Id": "own-id", "Mounts": [{"Type": "bind", "Source": resource["workspace"] + "/ledger"}]}
        other = {"Id": "other-id", "Mounts": [{"Type": "bind", "Source": str(f.store.root / "trials" / "other" / "ledger")}]}
        self.assertTrue(owns_container(resource, ours)); self.assertFalse(owns_container(resource, other))
        removed = []
        def shell(*args):
            if args[:2] == ("ps", "-axo"): return ""
            if args == ("docker", "ps", "-aq"): return "other-id" if removed else "own-id other-id"
            if args[:2] == ("docker", "inspect"):
                return json.dumps([x for x in (ours, other) if x["Id"] in args[2:]])
            if args[:3] == ("docker", "rm", "-f"): removed.append(args[3]); return args[3]
            raise AssertionError(args)
        with patch("evo_arena.trial_resources.shell", shell):
            result = reconcile(f.store, resource)
        self.assertEqual(result["state"], "cleaned")
        self.assertEqual(removed, ["own-id"])


if __name__ == "__main__": unittest.main()
