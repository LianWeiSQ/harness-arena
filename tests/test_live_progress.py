import asyncio
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from evo_arena.benchmark_progress import snapshot, tail
from evo_arena.benchmarks import ModelGateway
from evo_arena.config import Settings
from evo_arena.models import ModelProfiles
from evo_arena.store import now


class ModelEditingTests(unittest.TestCase):
    def test_startup_edit_persists_without_leaking_or_reentering_key(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Settings("https://example.invalid/v1", "model-a", "private-inherited-key")
            models = ModelProfiles(Path(folder), source)
            self.assertTrue(models.public()["profiles"][0]["editable"])
            running_settings, _, _ = models.resolve("startup")
            saved = models.save({"id": "startup", "name": "Edited", "model": "model-b",
                "base_url": source.base_url, "api_key": "", "default": True})
            self.assertNotIn(source.api_key, json.dumps(saved))
            restarted = ModelProfiles(Path(folder), source)
            settings, ident, name = restarted.resolve()
            self.assertEqual((ident, name, settings.model), ("startup", "Edited", "model-b"))
            self.assertEqual(settings.api_key, source.api_key)
            self.assertEqual(len(restarted.public()["profiles"]), 1)
            self.assertEqual(restarted.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(running_settings.model, "model-a")
            self.assertEqual(source.model, "model-a")
            with self.assertRaises(ValueError): restarted.remove("startup")

    def test_startup_without_a_source_cannot_invent_a_credential(self):
        with tempfile.TemporaryDirectory() as folder:
            models = ModelProfiles(Path(folder), None)
            with self.assertRaises(ValueError):
                models.save({"id": "startup", "name": "Missing", "model": "m", "base_url": "https://example.invalid", "api_key": ""})


class LiveProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run = {"id": "bench-0123456789ab", "platform": {"engine_arch": "aarch64"}}
        self.trial = {"id": "trial-0001", "status": "running", "stage": "environment", "started": now(), "heartbeat_at": now()}
        self.job = self.root / "benchmarks" / self.run["id"] / "jobs" / self.trial["id"]
        self.agent = self.job / "task__fixture" / "agent"
        (self.agent / "setup").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_setup_overrides_generic_phase_and_streams_newest_logs(self):
        (self.agent / "setup-progress.json").write_text(json.dumps({"stage": "node", "started_at": now(), "guest_arch": "x86_64"}))
        (self.agent / "setup/install.log").write_text("initial\n" + "x" * 30000 + "\nDownloaded 43 MB\n")
        (self.job / "job.log").write_text("Harbor preparing\n")
        result = snapshot(self.root, self.run, self.trial, include_logs=True)
        self.assertEqual(result["progress"]["label"], "下载 Node.js")
        self.assertEqual(result["progress"]["guest_arch"], "amd64")
        self.assertEqual(result["progress"]["host_arch"], "arm64")
        self.assertIn("43 MB", result["installation_log"])
        self.assertNotIn("initial", result["installation_log"])
        self.assertIn("preparing", result["harbor_log"])
        (self.agent / "setup/install.log").write_text("npm installing\n")
        self.assertIn("npm installing", snapshot(self.root, self.run, self.trial, True)["installation_log"])
        self.assertNotIn("installation_log", snapshot(self.root, self.run, self.trial))

    def test_model_phase_precedes_old_setup_marker_and_final_status_is_not_running(self):
        (self.agent / "setup-progress.json").write_text(json.dumps({"stage": "harness", "started_at": now()}))
        trial = {**self.trial, "stage": "model", "stage_started": now()}
        self.assertEqual(snapshot(self.root, self.run, trial)["progress"]["stage"], "model")
        trial.update(status="error", finished=now())
        self.assertEqual(snapshot(self.root, self.run, trial)["progress"]["stage"], "finished")

    def test_live_tool_partial_output_is_bounded_and_survives_a_partial_event(self):
        content = "old output\n" + "x" * 20000 + "\nDownloading R packages"
        event = {"type": "tool_execution_update", "toolName": "bash",
                 "partialResult": {"content": [{"type": "text", "text": content}]}}
        (self.agent / "pi.jsonl").write_text(json.dumps(event) + '\n{"type":')
        result = snapshot(self.root, self.run, {**self.trial, "runner": "pi"}, True)
        self.assertIn("Downloading R packages", result["agent_output"])
        self.assertNotIn("old output", result["agent_output"])
        self.assertNotIn("agent_output", snapshot(self.root, self.run, self.trial))
        (self.agent / "dsh-output.log").write_text("DSH tool running\n")
        result = snapshot(self.root, self.run, {**self.trial, "runner": "dsh"}, True)
        self.assertIn("DSH tool running", result["agent_output"])

    def test_model_stage_survives_tool_evidence_and_resets_after_errors(self):
        from unittest.mock import patch
        stage = []
        gateway = ModelGateway(Settings("https://example.invalid/v1", "test", "secret"), lambda *args: None)
        gateway.on_stage = stage.append
        def forward(raw):
            gateway.record("工具返回", "prior tool output")
            self.assertEqual(stage[-1], "model")
            raise TimeoutError()
        with patch("evo_arena.harness.TrialGateway.model", side_effect=forward):
            with self.assertRaises(TimeoutError): gateway.model(b"{}")
        self.assertEqual(stage, ["model", "agent"])

    def test_reader_ignores_links_fifos_and_malformed_partial_json(self):
        secret = self.root / "secret.txt"; secret.write_text("private test data")
        (self.agent / "setup/install.log").symlink_to(secret)
        (self.agent / "setup-progress.json").write_text('{"stage":')
        self.assertEqual(snapshot(self.root, self.run, self.trial, True)["installation_log"], "")
        (self.agent / "setup-progress.json").write_text('{"stage":[],"guest_arch":{}}')
        self.assertEqual(snapshot(self.root, self.run, self.trial)["progress"]["stage"], "environment")
        pipe = self.root / "pipe"; os.mkfifo(pipe)
        self.assertEqual(tail(pipe), "")
        normal = self.root / "normal"; normal.write_text("Authorization: Bearer private-key\rnew line")
        self.assertNotIn("private-key", tail(normal))

    def test_installer_publishes_stages_and_logs_without_running_agent(self):
        try:
            from evo_arena.benchmark_agents import install_runtime
        except ImportError:
            self.skipTest("Install Harbor extra")
        agent = SimpleNamespace(logs_dir=self.agent, ensure_system_dependencies=AsyncMock(),
            exec_as_root=AsyncMock(return_value=SimpleNamespace(stdout="x86_64\n")))
        environment = SimpleNamespace(upload_file=AsyncMock())
        asyncio.run(install_runtime(agent, environment, "pi"))
        state = json.loads((self.agent / "setup-progress.json").read_text())
        self.assertEqual((state["stage"], state["guest_arch"]), ("ready", "x86_64"))
        commands = [call.args[1] for call in agent.exec_as_root.await_args_list]
        self.assertTrue(any("--progress-bar" in c and "tee -a" in c for c in commands))
        self.assertTrue(any("--loglevel=http" in c and "tee -a" in c for c in commands))

    def test_log_pipelines_preserve_installer_and_harness_failures(self):
        try:
            from evo_arena.benchmark_agents import BenchmarkDSH
        except ImportError:
            self.skipTest("Install Harbor extra")
        agent = BenchmarkDSH(logs_dir=self.agent, model_name="arena/test")
        log = self.root / "captured.log"

        async def execute(command, **kwargs):
            # Fake binaries, real shell pipeline; never apt/npm/network or a model.
            command = command.replace("/logs/agent/setup/install.log", shlex.quote(str(log)))
            command = command.replace("/logs/agent/dsh-output.log", shlex.quote(str(log)))
            output = subprocess.run(["bash", "-c", "apt-get() { echo install-failed; return 7; }; "
                                     "dsh() { echo harness-failed; return 9; }; " + command],
                                    text=True, capture_output=True, timeout=5)
            return SimpleNamespace(return_code=output.returncode, stdout=output.stdout, stderr=output.stderr)

        environment = SimpleNamespace(exec=execute)
        with self.assertRaises(RuntimeError):
            asyncio.run(agent.exec_as_root(environment, "apt-get update && apt-get install -y curl"))
        self.assertIn("install-failed", log.read_text())
        with self.assertRaises(RuntimeError):
            asyncio.run(agent.exec_as_agent(environment, "dsh --profile headless fixture"))
        self.assertIn("harness-failed", log.read_text())

    def test_runtime_detection_reuses_exact_versions_without_network_or_npm(self):
        try:
            from evo_arena.benchmark_agents import install_runtime
        except ImportError:
            self.skipTest("Install Harbor extra")
        prefix = self.root / "runtime"
        (prefix / "bin").mkdir(parents=True)
        for name, version in (("node", "v22.22.0"), ("pi", "0.82.1")):
            target = prefix / "bin" / name
            target.write_text("#!/bin/sh\necho " + version + "\n")
            target.chmod(0o700)
        async def execute(environment, command, **kwargs):
            command = command.replace("/opt/evo-agent", str(prefix)).replace("/usr/local/lib", str(self.root / "lib"))
            command = command.replace("/logs/agent/setup", str(self.agent / "setup"))
            result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c",
                "curl() { echo unexpected-download; return 9; }; npm() { echo unexpected-npm; return 9; }; " + command],
                text=True, capture_output=True, timeout=5)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return result
        agent = SimpleNamespace(logs_dir=self.agent, ensure_system_dependencies=AsyncMock(), exec_as_root=execute)
        asyncio.run(install_runtime(agent, SimpleNamespace(upload_file=AsyncMock()), "pi"))
        log = (self.agent / "setup/install.log").read_text()
        self.assertIn("already installed", log)
        self.assertIn("pi 0.82.1 ready", log)
        self.assertNotIn("unexpected", log)


if __name__ == "__main__":
    unittest.main()
