import copy
import json
import tempfile
import unittest
from pathlib import Path

from evo_arena.dashboard import dashboard_view
from evo_arena.store import Store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.counter = 0

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def run_record(self):
        self.counter += 1
        entries = [{"id": "pi", "runner": "pi", "model": "snapshot-model", "label": "Local"},
                   {"id": "dsh", "runner": "dsh", "model": "snapshot-model", "label": "Local"}]
        return {"id": f"bench-{self.counter:012d}", "created": f"2026-11-13T12:00:{self.counter:02d}+00:00",
                "status": "completed", "dataset": {"id": "fixture", "name": "Fixture", "sha256": "snapshot",
                "tasks": [{"id": t, "category": "coding"} for t in ("a", "b", "c")]},
                "selected_tasks": ["a", "b"], "repeats": 1, "entries": entries,
                "trials": [{"id": f"trial-{i:04d}", "entry": entry["id"], "task": task, "repeat": 1,
                    "status": "passed", "seconds": 10+i, "usage": {"calls": 1, "tokens": 100},
                    "agent_info": {"version": "fixture"}} for i, (entry, task) in enumerate(
                        ((e,t) for e in entries for t in ("a", "b")), 1)]}

    def save(self, run):
        self.store.create("benchmark_run", run["id"], run)

    def test_empty_dashboard_never_invents_scores(self):
        self.assertEqual(dashboard_view(self.store), {"runs": [], "active_runs": [], "run": None,
                         "entries": [], "tasks": [], "evaluation_count": 0})

    def test_tied_ranks_subset_coverage_and_snapshot_models(self):
        run = self.run_record(); self.save(run)
        view = dashboard_view(self.store)
        self.assertEqual([(e["score"], e["rank"]) for e in view["entries"]], [(100, 1), (100, 1)])
        self.assertTrue(view["run"]["is_subset"])
        self.assertEqual((view["run"]["task_count"], view["run"]["dataset_size"]), (2, 3))
        self.assertEqual(view["entries"][0]["model"], "snapshot-model")
        self.assertEqual(view["entries"][0]["tokens"], 200)
        self.assertEqual(view["entries"][0]["mean_seconds"], 13.5)
        self.assertEqual(self.store.get("benchmark_run", run["id"]), run)

    def test_failures_are_zero_but_errors_or_unrun_trials_are_unscored(self):
        for status, score in (("failed", 0), ("error", None), ("not_run", None), ("cancelled", None), ("running", None)):
            with self.subTest(status=status):
                run = self.run_record()
                for t in run["trials"]: t["status"] = status
                self.save(run)
                rows = dashboard_view(self.store, run["id"])["entries"]
                self.assertTrue(all(e["score"] == score for e in rows))
                self.assertTrue(all(e["rank"] == (1 if score == 0 else None) for e in rows))

    def test_missing_or_duplicate_trials_cannot_produce_full_scores(self):
        run = self.run_record(); run["trials"].pop(); self.save(run)
        self.assertIsNone(next(e for e in dashboard_view(self.store)["entries"] if e["id"] == "dsh")["score"])
        duplicate = self.run_record(); duplicate["trials"][1] = copy.deepcopy(duplicate["trials"][0]); self.save(duplicate)
        self.assertIsNone(next(e for e in dashboard_view(self.store)["entries"] if e["id"] == "pi")["score"])

    def test_separate_runs_datasets_and_models_are_never_merged(self):
        original = self.run_record(); self.save(original)
        latest = self.run_record(); latest["status"] = "running"
        latest["dataset"]["name"] = "Different tasks"; latest["entries"][0]["model"] = "another-model"
        latest["trials"][0]["status"] = "running"; self.save(latest)
        view = dashboard_view(self.store)
        self.assertEqual(view["run"]["id"], original["id"])
        self.assertEqual(view["active_runs"][0]["id"], latest["id"])
        self.assertEqual(view["evaluation_count"], 2)
        view = dashboard_view(self.store, latest["id"])
        pi = next(e for e in view["entries"] if e["id"] == "pi")
        self.assertIsNone(pi["score"])
        self.assertEqual(pi["model"], "another-model")

    def test_unknown_usage_is_not_reported_as_complete_and_custom_rewards_not_normalized(self):
        run = self.run_record(); run["trials"][0]["usage"]["unknown_usage_calls"] = 1
        for t in run["trials"][2:]:
            t.update(status="scored", rewards={"quality": .7})
        self.save(run); view = dashboard_view(self.store)
        pi = next(e for e in view["entries"] if e["id"] == "pi")
        dsh = next(e for e in view["entries"] if e["id"] == "dsh")
        self.assertFalse(pi["usage_known"]); self.assertEqual(pi["tokens"], 200)
        self.assertIsNone(dsh["score"])
        cell = next(c for c in view["tasks"][0]["cells"] if c["entry"] == "dsh")
        self.assertEqual(cell["rewards"], [{"quality": .7}])

    def test_repeats_use_all_trials_and_keep_stable_evidence_links(self):
        run = self.run_record(); run["repeats"] = 2
        repeated = copy.deepcopy(run["trials"])
        for t in repeated: t.update(repeat=2, id=t["id"]+"-repeat", status="failed")
        run["trials"] += repeated; self.save(run)
        view = dashboard_view(self.store)
        self.assertTrue(all(e["score"] == 50 and e["total"] == 4 for e in view["entries"]))
        self.assertEqual(view["tasks"][0]["cells"][0]["status"], "mixed")
        self.assertEqual(len(view["tasks"][0]["cells"][0]["trial_ids"]), 2)

    def test_only_public_score_fields_and_unknown_run_rejected(self):
        run = self.run_record(); run["dataset"]["path"] = "/private-path"
        run["entries"][0].update(api_key="private-key", base_url="private-gateway")
        run["trials"][0]["trace"] = ["private-trace"]
        self.save(run)
        self.assertNotIn("private-", json.dumps(dashboard_view(self.store)))
        with self.assertRaises(ValueError): dashboard_view(self.store, "missing-run")


if __name__ == "__main__": unittest.main()
