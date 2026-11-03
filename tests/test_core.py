import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from evo_arena.arena import SeasonRunner, admission
from evo_arena.provider import Budget, BudgetExceeded
from evo_arena.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_versions_are_immutable_and_survive_reopen(self):
        version = self.store.version("a", "A sufficiently long policy")
        self.assertEqual(version, self.store.version("a", "A sufficiently long policy"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update("version", version["id"], {**version, "policy": "changed"})
        self.store.close()
        self.store = Store(Path(self.temp.name))
        self.assertEqual(version, self.store.get("version", version["id"]))

    def test_promotion_is_atomic_idempotent_and_fenced(self):
        parent = self.store.version("a", "A sufficiently long policy")
        child = self.store.version("b", "An improved sufficiently long policy", parent["id"])
        self.store.initialize_seats("season", [parent["id"]])
        decision = {"id": "d-1", "status": "accepted"}
        self.assertTrue(self.store.promote("season", 0, parent["id"], 0, child["id"], decision))
        self.assertFalse(self.store.promote("season", 0, parent["id"], 0, child["id"], decision))
        with self.assertRaises(ValueError):
            self.store.promote("season", 0, parent["id"], 0, parent["id"], {"id": "stale"})
        self.assertIsNone(self.store.get("decision", "stale"))
        self.assertEqual(self.store.seats("season")[0]["revision"], 1)
        self.assertEqual(len(self.store.events("season")), 1)


def trial(passed, seed=73, status="completed"):
    return {"passed": passed, "seed": seed, "status": status, "phase": "admission"}


class AdmissionTests(unittest.TestCase):
    def test_equal_scores_are_rejected(self):
        self.assertEqual(admission([trial(True)], [trial(True)])["status"], "rejected")

    def test_invalid_coverage_is_pending(self):
        self.assertEqual(admission([trial(True)], [trial(None, status="interrupted")])["status"], "pending")

    def test_regression_cannot_be_offset_by_other_wins(self):
        result = admission([trial(True), trial(False, 74), trial(False, 75)],
                           [trial(False), trial(True, 74), trial(True, 75)])
        self.assertEqual(result["reason"], "task_success_regression")

    def test_mismatched_instances_refused(self):
        with self.assertRaises(ValueError):
            admission([trial(False)], [trial(True, seed=99)])

    def test_strict_success_gain(self):
        self.assertEqual(admission([trial(False)], [trial(True)])["status"], "accepted")


class FakeScenario:
    def __init__(self, root, run_id, seed, **kwargs):
        self.fixed = False
        self.port = str(18100 + seed % 100)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def calibrate(self):
        return {"broken_passed": False, "healthy_passed": True}

    def tool(self, action):
        if action["action"] == "inspect":
            return {"port": self.port}
        if action["action"] == "patch_config":
            self.fixed = action["upstream"].endswith(":" + self.port)
        return {"ok": True}

    def verify(self):
        return {"passed": self.fixed, "tests": [{"name": "business", "passed": self.fixed}]}


class FakeClient:
    def __init__(self):
        self.budget = Budget(100, 10000)

    def complete(self, instructions, payload, max_output=500):
        self.budget.claim()
        self.budget.record({"total_tokens": 1})
        if "策略进化器" in instructions:
            result = {"name": "Child", "policy": "improved-policy: inspect and repair using actual observations", "hypothesis": "Use actual service port"}
        elif "improved-policy" not in instructions:
            result = {"action": "submit"}
        elif not payload["history"]:
            result = {"action": "inspect"}
        elif len(payload["history"]) == 1:
            port = payload["history"][0]["result"]["port"]
            result = {"action": "patch_config", "upstream": "http://127.0.0.1:" + port}
        else:
            result = {"action": "submit"}
        return json.dumps(result), {"total_tokens": 1}


class WorkflowTests(unittest.TestCase):
    def test_complete_evolution_and_sealed_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.addCleanup(store.close)
            runner = SeasonRunner(store, FakeClient(), scenario_factory=FakeScenario,
                                  image_builder=lambda: {"image_id": "test-only"})
            season = runner.execute(runner.create(2, 2))
            self.assertEqual(season["status"], "completed")
            decision = store.list("decision")[0]
            self.assertEqual(decision["status"], "accepted")
            self.assertIn(decision["child"], [s["version"] for s in store.seats(season["id"])])
            child = store.get("version", decision["child"])
            child_runs = [r for r in store.list("run") if r["version"] == child["id"]]
            self.assertTrue(all(r["effective_policy_sha256"] for r in child_runs))
            self.assertEqual({r["seed"] for r in child_runs}, {41, 73, 97})
            events = store.events(season["id"])
            decision_seq = next(e["seq"] for e in events if e["kind"] == "child.promoted")
            report_seq = next(e["seq"] for e in events if e["kind"] == "report.started")
            self.assertLess(decision_seq, report_seq)
            self.assertEqual(len(season["final_report"]), 3)

    def test_budget_prevents_more_requests(self):
        budget = Budget(1, 100)
        budget.claim()
        with self.assertRaises(BudgetExceeded):
            budget.claim()
        self.assertEqual(budget.calls, 1)

    def test_custom_strategies_are_snapshotted_and_duplicates_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.addCleanup(store.close)
            a = store.version("Player A", "My own strategy inspects before repair")
            b = store.version("Player B", "My second strategy verifies the evidence")
            runner = SeasonRunner(store, FakeClient())
            season = runner.create(rounds=2, version_ids=[a["id"], b["id"]])
            self.assertEqual(season["initial_versions"], [a["id"], b["id"]])
            with self.assertRaises(ValueError):
                runner.create(version_ids=[a["id"], a["id"]])
            self.assertEqual(len(store.list("season")), 1)

    def test_cancel_during_model_request_prevents_next_tool_side_effect(self):
        cancel = threading.Event()
        tool_actions = []

        class Client(FakeClient):
            def complete(self, *args, **kwargs):
                result = super().complete(*args, **kwargs)
                cancel.set()
                return result

        class Scenario(FakeScenario):
            def tool(self, action):
                tool_actions.append(action)
                return super().tool(action)

        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            self.addCleanup(store.close)
            runner = SeasonRunner(store, Client(), cancel, scenario_factory=Scenario,
                                  image_builder=lambda: {"image_id": "test-only"})
            season = runner.execute(runner.create())
            self.assertEqual(season["status"], "canceled")
            self.assertEqual(tool_actions, [])
            self.assertEqual(store.list("run")[0]["usage"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
