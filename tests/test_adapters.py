"""Adapter contract checks against the optional, pinned Harbor distribution."""
import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from evo_arena.cli import find_harbor


class HarborDiscoveryTests(unittest.TestCase):
    def test_explicit_then_environment_then_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            local = root / "harbor"
            external = root / "external-harbor"
            with patch("evo_arena.cli.sys.executable", str(root / "python")), patch("evo_arena.cli.shutil.which", return_value=str(external)):
                self.assertEqual(find_harbor(), external)
                local.touch(); local.chmod(0o700)
                self.assertEqual(find_harbor(), local)
                self.assertEqual(find_harbor(external), external)
            with patch("evo_arena.cli.sys.executable", str(root / "absent" / "python")), patch("evo_arena.cli.shutil.which", return_value=None):
                self.assertIsNone(find_harbor())


@unittest.skipUnless(importlib.util.find_spec("harbor"), "Install the harbor extra for adapter contract tests")
class HarborAdapterTests(unittest.TestCase):
    def test_both_adapters_bind_only_to_trial_relay_and_disable_ambient_skills(self):
        from evo_arena.harbor_agents import ArenaDSH, ArenaPi
        with tempfile.TemporaryDirectory() as folder, patch.dict("os.environ", {"EVO_VERSION_FILE": ""}):
            for adapter in (ArenaDSH, ArenaPi):
                agent = adapter(logs_dir=Path(folder), model_name="arena/vendor/model", extra_env={
                    "ARENA_TRIAL_URL": "http://127.0.0.1:18765", "ARENA_TRIAL_TOKEN": "trial-only-token"})
                agent.exec_as_agent = AsyncMock()
                agent._upload_config_text = AsyncMock()
                asyncio.run(agent.run("Read the order's status; don't duplicate it.", object(), object()))
                uploads = {c.kwargs["filename"]: c.kwargs["content"] for c in agent._upload_config_text.await_args_list}
                self.assertNotIn("trial-only-token", json.dumps(uploads))
                invocation = agent.exec_as_agent.await_args_list[-1]
                self.assertEqual(invocation.kwargs["env"]["ARENA_TRIAL_TOKEN"], "trial-only-token")
                self.assertNotIn("OPENAI_API_KEY", invocation.kwargs["env"])
                if adapter is ArenaDSH:
                    config = yaml.safe_load(uploads["settings.yaml"])
                    provider = config["llm-pi-ai"]["providers"]["arena"]
                    self.assertEqual(provider["baseURL"], "http://127.0.0.1:18765/v1")
                    self.assertEqual(provider["apiKeyEnv"], "ARENA_TRIAL_TOKEN")
                    self.assertEqual(provider["models"][0]["id"], "vendor/model")
                    self.assertTrue(all(p["disabled"] for p in yaml.safe_load(uploads["cordis.patch.yml"])))
                    self.assertIn("--profile headless", invocation.args[1])
                else:
                    provider = json.loads(uploads["models.json"])["providers"]["arena"]
                    self.assertEqual(provider["baseUrl"], "http://127.0.0.1:18765/v1")
                    self.assertEqual(provider["models"][0]["id"], "vendor/model")
                    for flag in ("--no-skills", "--no-context-files", "--no-extensions", "--no-session"):
                        self.assertIn(flag, invocation.args[1])

    def test_execution_end_marker_is_written_even_on_adapter_failure(self):
        from evo_arena.harbor_agents import ArenaDSH, ArenaPi
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "ended"
            for adapter in (ArenaDSH, ArenaPi):
                agent = adapter(logs_dir=Path(folder), model_name="arena/model")
                with patch.dict("os.environ", {"EVO_TRIAL_END_FILE": str(marker)}), patch(
                        "evo_arena.harbor_agents.load_version", AsyncMock(side_effect=ValueError("bad version"))):
                    with self.assertRaisesRegex(ValueError, "bad version"):
                        asyncio.run(agent.run("task", object(), object()))
                    self.assertEqual(marker.read_text(), "ended")
                    marker.unlink()
