import copy
import unittest

from manual_feedback_study import selected_version


class StudySelectionTests(unittest.TestCase):
    def test_only_complete_quality_decisions_can_select_a_version(self):
        ex = {"status": "completed", "candidate": "child", "baseline": "initial", "initial_version": "initial",
              "evaluation": {"complete": True}, "budget": {"pending": 0, "unknown_usage_calls": 0},
              "decision": {"status": "accepted", "reason": "improved"}}
        self.assertEqual(selected_version(ex, "initial"), "child")
        for reason in ("unchanged", "regression"):
            ex["decision"] = {"status": "rejected", "reason": reason}
            self.assertEqual(selected_version(ex, "initial"), "initial")
        for changes in ({"status": "incomplete"}, {"evaluation": {"complete": False}},
                        {"decision": {"status": "rejected", "reason": "invalid_candidate"}},
                        {"budget": {"unknown_usage_calls": 1}}, {"budget": {"pending": 1}},
                        {"initial_version": "different"}, {"candidate": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                selected_version({**copy.deepcopy(ex), **changes}, "initial")
