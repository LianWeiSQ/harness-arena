import json
import tempfile
import unittest
from pathlib import Path

from evo_arena.harness import HarnessMatch


class HarnessResultTests(unittest.TestCase):
    def test_contest_limits_score_only_when_harbor_returned_a_valid_reward(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.json"
            for exception, budget_stop, reward, provider_failure, expected in (
                ("UnknownApiError", True, 1, None, "passed"),
                ("UnknownApiError", False, 1, None, "error"),
                ("UnknownApiError", True, None, None, "error"),
                ("UnknownApiError", True, 1, "模型供应商 HTTP 502", "error"),
                ("AgentTimeoutError", False, 0, None, "failed"),
            ):
                with self.subTest(exception=exception, budget_stop=budget_stop, reward=reward,
                                  provider_failure=provider_failure):
                    path.write_text(json.dumps({
                        "trial_name": "test", "task_checksum": "test",
                        "exception_info": {"exception_type": exception},
                        "verifier_result": {"rewards": {"reward": reward}},
                    }))
                    run = {"task": "lost-receipt", "usage": {"calls": 1}}
                    if budget_stop:
                        run["stop_reason"] = "达到模型请求或 Token 预算"
                    HarnessMatch.load_result(run, path, {}, provider_failure)
                    self.assertEqual(run["status"], expected)
