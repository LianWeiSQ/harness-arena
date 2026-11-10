import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo_arena.config import Settings
from evo_arena.experiment_budget import ExperimentBudget
from evo_arena.improvement import ImprovementExperiment, finalize
from evo_arena.models import ModelProfiles
from evo_arena.optimizer import BudgetedOptimizer, evidence_bundle, validate_diagnosis, validate_proposal
from evo_arena.provider import ProviderError
from evo_arena.store import Store
from evo_arena.taskpacks import current_pack, make_plan
from evo_arena.versions import VersionRegistry


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.settings = Settings("https://private-endpoint.invalid/v1", "model", "private-key")
        self.models = ModelProfiles(self.store.root, self.settings)
        self.registry = VersionRegistry(self.store, self.models)
        self.baseline = self.registry.register({"name": "Baseline", "runner": "pi", "policy": "Follow the task carefully.",
            "skills": [{"name": "orders", "description": "Business workflow", "body": "Read the current state."}]})
        self.harbor = self.store.root / "harbor"; self.harbor.touch()
        self.engine = ImprovementExperiment(self.store, self.registry, self.harbor, "test-image")
        self.payload = {"mode": "improve", "baseline": self.baseline["id"], "target": "policy.md"}
        self.inputs = []
        self.behavior = "improve"

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def create(self, key="one"):
        return self.engine.create(self.payload, key)

    def fake_prepare(self, ex, selected):
        ex["status"] = "running"; self.engine.save(ex)
        return self.store.root, self.store.get("evo_task_pack", ex["task_pack"])

    def optimizer(self, settings, ledger):
        test = self

        class Optimizer:
            def complete(self, phase, instructions, payload):
                test.inputs.append((phase, payload))
                ident = ledger.claim(None, phase, "test-hash")
                ledger.settle(ident, {"total_tokens": 20})
                if phase == "diagnosis":
                    result = {"observations": [{"id": "obs-1", "summary": "A reported outcome needs a final state check.",
                               "evidence": [payload["evidence"][0]["id"]]}], "hypothesis": {
                        "summary": "A final check may improve reliability.", "observation_ids": ["obs-1"], "target": payload["target"],
                        "expected_effect": "Fewer unchecked outcomes", "validation": "Use paired held-out evaluation"}}
                    if test.behavior == "bad_diagnosis" or test.behavior == "repair" and "correction" not in payload:
                        result["observations"][0]["evidence"] = ["UNKNOWN_REFERENCE"]
                else:
                    result = {"name": "Generated", "content": payload["parent_file"] + "\nVerify the final state.",
                              "rationale": "Check outcomes, without claiming improvement."}
                    if test.behavior == "invalid": result["model"] = "different-model"
                return json.dumps(result), {"call_id": ident, "total_tokens": 20}
        return Optimizer()

    def runner(self):
        test = self

        class Trial:
            def __init__(self, *args): pass

            def execute(self, run, root, original, record, version, case, budget):
                call = budget.claim(run["id"], "execution", "test-hash")
                budget.settle(call, {"total_tokens": 10})
                success = True
                if run.get("phase") == "validation":
                    if test.behavior in {"improve", "regress", "stale"} and run["role"] == "baseline" and case["id"] == "receipt-validation-1":
                        success = False
                    if test.behavior == "regress" and run["role"] == "candidate" and case["id"] == "workflow-validation-2":
                        success = False
                run.update(status="passed" if success else "failed", checks=[{"name": "Business goal", "passed": success, "critical": False}],
                    verifier_mode="separate", verifier_attested=True, execution_frozen=True, task_checksum=case["checksum"],
                    input_evidence={"registration_verified": True}, usage={"calls": 1, "tokens": 10, "unknown_usage_calls": 0})
                record("业务工具", "DEVELOPMENT_ONLY" if case["split"] == "development" else "VALIDATION_CANARY")
                if test.behavior == "stale" and case["id"] == "workflow-validation-2":
                    project = test.store.list("evo_project")[0]
                    project["revision"] += 1
                    test.store.update("evo_project", project["id"], project)
        return Trial

    def execute(self, ex, bindings):
        with patch.object(self.engine, "prepare", self.fake_prepare), patch.object(self.engine, "optimizer_factory", self.optimizer), \
             patch("evo_arena.evolution.HarborTrial", self.runner()):
            self.engine.execute(ex, bindings)
        return self.store.get("evo_experiment", ex["id"])

    def test_end_to_end_accepted_candidate_and_complete_budget(self):
        ex, bindings, _ = self.create()
        result = self.execute(ex, bindings)
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual(len(result["runs"]), 30)
        self.assertEqual(result["budget"]["calls"], 32)
        self.assertEqual(result["budget"]["tokens"], 340)
        project = self.store.get("evo_project", ex["project"])
        self.assertEqual(project["current_version"], result["candidate"])
        self.assertEqual(project["initial_version"], self.baseline["id"])
        self.assertEqual(project["revision"], 1)
        self.assertEqual(finalize(self.store, result)["decision"], result["decision"])
        self.assertEqual(len(self.store.events(project["id"])), 1)
        for kind, ident in (("evo_decision", result["decision"]["id"]), ("evo_diagnosis", result["id"]), ("evo_proposal", result["id"])):
            with self.assertRaises(sqlite3.IntegrityError): self.store.update(kind, ident, {})

    def test_optimizer_never_receives_validation_truth_or_credentials(self):
        ex, bindings, _ = self.create(); result = self.execute(ex, bindings)
        text = json.dumps(self.inputs)
        for forbidden in ("VALIDATION_CANARY", "private-key", "private-endpoint.invalid", "reject_before_commit", "stock_conflict"):
            self.assertNotIn(forbidden, text)
        self.assertIn("DEVELOPMENT_ONLY", text)
        bundle = result["evidence_bundle"]
        value = json.loads(json.dumps(result["diagnosis"]))
        value["observations"][0]["evidence"] = ["made-up-event"]
        with self.assertRaises(ValueError): validate_diagnosis(value, bundle)
        with self.assertRaises(ValueError):
            evidence_bundle(self.baseline, current_pack(), result["runs"], "policy.md")

    def test_no_feedback_control_keeps_budget_and_trials_but_withholds_execution_evidence(self):
        ordinary, _, _ = self.create("feedback")
        self.payload["feedback_mode"] = "none"
        ex, bindings, _ = self.create("control")
        result = self.execute(ex, bindings)
        self.assertEqual(result["decision"]["status"], "accepted")
        self.assertEqual(len(result["runs"]), 30)
        self.assertEqual(result["budget"]["calls"], 32)
        self.assertEqual(ex["limits"], ordinary["limits"])
        self.assertEqual(ex["optimizer_policy"], ordinary["optimizer_policy"])
        self.assertEqual(ex["development_plan"], ordinary["development_plan"])
        self.assertEqual(ex["validation_template"], ordinary["validation_template"])
        self.assertEqual(result["evidence_bundle"]["schema"], "public-prior-v1")
        text = json.dumps(self.inputs)
        for forbidden in ("DEVELOPMENT_ONLY", "VALIDATION_CANARY", "private-key", "private-endpoint.invalid", "seconds", "tokenUsage"):
            self.assertNotIn(forbidden, text)
        for run in result["runs"]:
            self.assertNotIn(run["id"], text)
        self.assertNotEqual(ex["project"], ordinary["project"])
        self.assertEqual(self.store.get("evo_project", ordinary["project"])["current_version"], self.baseline["id"])
        self.assertEqual(self.store.get("evo_project", ex["project"])["current_version"], result["candidate"])
        self.payload["project"] = ordinary["project"]
        with self.assertRaisesRegex(ValueError, "独立项目"):
            self.create("bad-cross-arm-project")

    def test_feedback_mode_is_frozen_and_validated(self):
        from evo_arena import execution_control as control
        for value in ([], "all", None):
            self.payload["feedback_mode"] = value
            with self.assertRaises(ValueError): self.create("invalid-mode")
        self.payload["feedback_mode"] = "none"
        ex, _, _ = self.create("frozen-mode")
        ex["feedback_mode"] = "development"
        self.store.update("evo_experiment", ex["id"], ex)
        with self.assertRaises(control.ExecutionStopped): control.acquire(self.store, ex)

    def test_no_gain_regression_and_invalid_candidate_leave_recommendation_unchanged(self):
        for behavior in ("unchanged", "regress", "invalid"):
            with self.subTest(behavior=behavior):
                self.behavior = behavior
                ex, bindings, _ = self.create(behavior); result = self.execute(ex, bindings)
                self.assertEqual(result["decision"]["status"], "rejected")
                self.assertEqual(self.store.get("evo_project", ex["project"])["current_version"], self.baseline["id"])
                if behavior == "invalid":
                    self.assertIsNone(result["candidate"])
                    self.assertEqual(len(result["runs"]), 6)

    def test_stale_revision_cannot_apply_even_when_quality_improved(self):
        self.behavior = "stale"
        ex, bindings, _ = self.create(); result = self.execute(ex, bindings)
        self.assertEqual(result["decision"]["status"], "not_applied")
        self.assertEqual(result["decision"]["reason"], "project_changed")
        self.assertEqual(self.store.get("evo_project", ex["project"])["current_version"], self.baseline["id"])

    def test_decision_failure_rolls_back_pointer_and_event_together(self):
        ex, bindings, _ = self.create()
        with patch("evo_arena.improvement.finalize"):
            result = self.execute(ex, bindings)
        self.store.db.executescript("CREATE TRIGGER reject_decision BEFORE INSERT ON objects WHEN NEW.kind='evo_decision' BEGIN SELECT RAISE(ABORT,'test'); END;")
        with self.assertRaises(sqlite3.IntegrityError): finalize(self.store, result)
        self.assertEqual(self.store.get("evo_project", ex["project"])["current_version"], self.baseline["id"])
        self.assertEqual(self.store.list("evo_decision"), [])
        self.assertEqual(self.store.events(ex["project"]), [])

    def test_protocol_tamper_cannot_promote(self):
        ex, bindings, _ = self.create()
        with patch("evo_arena.improvement.finalize"):
            result = self.execute(ex, bindings)
        result["plan"]["quality_gate"] = "ignore regression"
        self.engine.save(result)
        decision = finalize(self.store, result)["decision"]
        self.assertEqual(decision["reason"], "protocol_changed")
        self.assertEqual(decision["status"], "insufficient_evidence")

    def test_single_asset_derivation_preserves_all_other_bytes(self):
        before = self.baseline["manifest"]["files"]
        target = "skills/orders/SKILL.md"
        child = self.registry.derive(self.baseline, "Skill candidate", target, before[target] + "Confirm final state.\n")
        self.assertEqual(child["manifest"]["files"]["policy.md"], before["policy.md"])
        self.assertEqual(child["manifest"]["model"], self.baseline["manifest"]["model"])
        for path, content in (("../escape", "text"), (target, "malformed skill"), ("policy.md", before["policy.md"]),
                              (target, before[target].replace('name: "orders"', 'name: orders'))):
            with self.assertRaises(ValueError): self.registry.derive(self.baseline, "Invalid", path, content)
        with self.assertRaises(ValueError): validate_proposal({"name": "x", "content": "y", "rationale": "z", "files": {}})

    def test_low_budget_stops_without_generating_candidate(self):
        self.payload["limits"] = {"calls": 2, "tokens": 100, "seconds": 100}
        ex, bindings, _ = self.create(); result = self.execute(ex, bindings)
        self.assertEqual(result["decision"]["reason"], "budget_exhausted")
        self.assertIsNone(result["candidate"])
        self.assertEqual(result["budget"]["calls"], 2)
        self.assertEqual(self.inputs, [])

    def test_optimizer_failure_usage_is_durable_and_not_zero(self):
        ledger = ExperimentBudget(self.store, "optimizer")
        ledger.start({"calls": 2, "tokens": 100, "seconds": 100})
        def broken(client, *args, **kwargs):
            client.budget.claim()
            raise ProviderError("transport failed")
        with patch("evo_arena.optimizer.ResponsesClient.complete", broken), self.assertRaises(ProviderError):
            BudgetedOptimizer(self.settings, ledger).complete("diagnosis", "instructions", {})
        self.assertEqual(ledger.public()["calls"], 1)
        self.assertEqual(ledger.public()["unknown_usage_calls"], 1)
        self.assertEqual(ledger.public()["pending"], 0)

    def test_invalid_diagnosis_is_preserved_and_repair_is_bounded(self):
        self.behavior = "bad_diagnosis"
        ex, bindings, _ = self.create(); result = self.execute(ex, bindings)
        self.assertEqual(result["decision"]["reason"], "invalid_diagnosis")
        self.assertIsNone(result["candidate"])
        self.assertEqual(len(result["optimizer_calls"]), 2)
        self.assertEqual(result["budget"]["calls"], 8)
        for item in result["optimizer_attempts"]:
            raw = self.store.get("evo_optimizer_output", item["id"])
            self.assertIn("UNKNOWN_REFERENCE", raw["raw_output"])
            with self.assertRaises(sqlite3.IntegrityError): self.store.update("evo_optimizer_output", item["id"], {})
        self.behavior = "repair"
        ex, bindings, _ = self.create("repair"); result = self.execute(ex, bindings)
        self.assertIsNotNone(result["candidate"])
        self.assertEqual([x["status"] for x in result["optimizer_attempts"][:2]], ["invalid", "valid"])
        self.assertEqual(result["budget"]["calls"], 33)

    def test_failed_create_rolls_back_project_pack_and_experiment(self):
        self.store.db.executescript("CREATE TRIGGER fail_improvement_request BEFORE INSERT ON objects WHEN NEW.kind='evo_request' BEGIN SELECT RAISE(ABORT,'test'); END;")
        with self.assertRaises(sqlite3.IntegrityError): self.create()
        for kind in ("evo_project", "evo_task_pack", "evo_experiment", "evo_request"):
            self.assertEqual(self.store.list(kind), [])

    def test_evidence_aliases_resolve_to_real_events_and_results(self):
        ex, bindings, _ = self.create(); result = self.execute(ex, bindings)
        runs = {r["id"]: r for r in result["runs"]}
        for evidence in result["evidence_bundle"]["evidence"]:
            source = evidence["source"]; run = runs[source["run_id"]]
            self.assertEqual(run["split"], "development")
            if source["event"] == "result": self.assertEqual(evidence["value"]["checks"], run["checks"])
            else: self.assertEqual(evidence["value"], run["trace"][source["event"]]["text"])


if __name__ == "__main__": unittest.main()
