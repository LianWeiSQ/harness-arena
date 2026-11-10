import copy
import unittest

from evo_arena.comparison import compare_match


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.match = {"status": "completed", "model": "same-model", "runners": ["dsh", "pi"], "runs": [
            {"task": "one", "runner": "dsh", "status": "passed", "task_checksum": "same",
             "seconds": 100, "usage": {"tokens": 200}},
            {"task": "one", "runner": "pi", "status": "passed", "task_checksum": "same",
             "seconds": 75, "usage": {"tokens": 100}},
        ]}

    def test_tied_quality_and_measured_efficiency_are_separate(self):
        result = compare_match(self.match, ["one"])
        self.assertEqual(result["outcome"], "draw")
        self.assertIsNone(result["winner"])
        self.assertEqual(result["metrics"]["seconds"]["reduction_pct"], 25)
        self.assertEqual(result["metrics"]["tokens"]["reduction_pct"], 50)
        self.assertEqual(result["metrics"]["seconds"]["lower"], "pi")

    def test_fast_failed_task_does_not_gain_an_efficiency_advantage(self):
        self.match["runs"][1].update(status="failed", seconds=1)
        result = compare_match(self.match, ["one"])
        self.assertEqual(result["winner"], "dsh")
        self.assertEqual(result["points"], {"dsh": 1, "pi": 0})
        self.assertEqual(result["metrics"]["seconds"]["tasks"], 0)

    def test_errors_missing_runs_and_mismatched_tasks_cannot_decide_a_duel(self):
        for change in ({"status": "error"}, {"task_checksum": "different"}, {"model": "other-model"}):
            with self.subTest(change=change):
                match = copy.deepcopy(self.match)
                match["runs"][1].update(change)
                result = compare_match(match, ["one"])
                self.assertEqual(result["valid_tasks"], 0)
                self.assertIsNone(result["winner"])
                self.assertEqual(result["metrics"]["tokens"]["tasks"], 0)
        self.match["runs"].pop()
        self.assertEqual(compare_match(self.match, ["one"])["valid_tasks"], 0)

    def test_single_runs_and_unknown_usage_have_no_invented_comparison(self):
        self.match["runs"][0]["usage"]["unknown_usage_calls"] = 1
        result = compare_match(self.match, ["one"])
        self.assertEqual(result["metrics"]["tokens"]["tasks"], 0)
        self.assertEqual(result["metrics"]["seconds"]["tasks"], 1)
        self.match["runners"] = ["pi"]
        result = compare_match(self.match, ["one"])
        self.assertEqual(result["outcome"], "single")
        self.assertEqual(result["metrics"]["seconds"]["tasks"], 0)
