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


class ModelSelectionTests(unittest.TestCase):
    def test_private_profiles_survive_restart_and_blank_key_preserves_credential(self):
        from evo_arena.models import ModelProfiles
        with tempfile.TemporaryDirectory() as folder:
            profiles = ModelProfiles(Path(folder), None)
            result = profiles.save({"name": "Test", "model": "model-a", "base_url": "https://example.invalid/v1",
                                    "api_key": "private-test-key", "default": True})
            self.assertNotIn("private-test-key", json.dumps(result))
            self.assertEqual(profiles.path.stat().st_mode & 0o777, 0o600)
            restarted = ModelProfiles(Path(folder), None)
            restarted.save({"id": result["id"], "name": "Renamed", "model": "model-b",
                            "base_url": "https://example.invalid/v1", "api_key": ""})
            settings, ident, label = restarted.resolve()
            self.assertEqual((settings.api_key, settings.model, label), ("private-test-key", "model-b", "Renamed"))
            restarted.remove(ident)
            self.assertEqual(restarted.public(), {"default": None, "profiles": []})

    def test_selected_roster_and_model_are_frozen_and_retries_do_not_launch_again(self):
        from unittest.mock import patch
        from evo_arena.config import Settings
        from evo_arena.models import ModelProfiles
        from evo_arena.store import Store
        from evo_arena.web import Manager
        from test_web import DummyThread
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            harbor = root / "harbor"
            harbor.touch()
            store = Store(root)
            self.addCleanup(store.close)
            original = Settings("https://old.invalid/v1", "old-model", "old-key")
            models = ModelProfiles(root, original)
            saved = models.save({"name": "Selected", "model": "chosen-model", "base_url": "https://new.invalid/v1",
                                 "api_key": "new-key"})
            harness = HarnessMatch(store, original, root, harbor, "test-image")
            manager = Manager(store, original, "test-image")
            payload = {"runners": ["pi"], "model_profile": saved["id"]}
            with patch("evo_arena.web.threading.Thread", side_effect=DummyThread) as thread:
                match, code = manager.start_harness(harness, "selection-key", payload, models)
                self.assertEqual(code, 202)
                self.assertEqual(match["runners"], ["pi"])
                self.assertEqual(match["model"], "chosen-model")
                self.assertNotIn("new-key", json.dumps(match))
                worker = thread.call_args.kwargs["target"].__self__
                self.assertEqual(worker.settings.api_key, "new-key")
                models.save({"id": saved["id"], "name": "Changed", "model": "changed-model",
                             "base_url": "https://new.invalid/v1", "api_key": "changed-key"})
                self.assertEqual(worker.settings.api_key, "new-key")
                again, code = manager.start_harness(harness, "selection-key", payload, models)
                self.assertEqual((again["id"], code), (match["id"], 200))
                _, code = manager.start_harness(harness, "selection-key", {**payload, "runners": ["dsh"]}, models)
                self.assertEqual(code, 409)
                self.assertEqual(thread.call_count, 1)
            with self.assertRaises(ValueError):
                harness.create(["pi", "pi"])
            with self.assertRaises(ValueError):
                harness.create(["missing"])
