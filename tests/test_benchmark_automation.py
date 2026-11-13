import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from evo_arena.benchmark_retry import retry_reason
from evo_arena.benchmark_runtime import check_environment, install_command, install_harbor
from evo_arena.benchmarks import recover_benchmarks, trial_result
import test_benchmarks as fixtures


class AutomaticEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BenchmarkTests()
        self.fixture.setUp()
        self.engine, self.store = self.fixture.engine, self.fixture.store
        self.run, self.credentials = self.engine.create({**self.fixture.payload, "max_auto_retries": 2}, "auto-fixture")

    def tearDown(self):
        self.fixture.tearDown()

    def execute(self, action, wait=None):
        with patch.object(self.engine, "run_trial", side_effect=action) as trial, patch(
                "evo_arena.benchmarks.subprocess.check_output", return_value="0.22.0"), patch.object(
                self.engine.cancel, "wait", side_effect=wait or (lambda delay: False)) as waiting:
            self.engine.execute(self.run, self.credentials)
        return trial, waiting

    def test_transient_error_recovers_automatically_then_continues_remaining_tasks(self):
        def action(run, trial, root, settings):
            trial.update(usage={"calls": 1, "tokens": 50}, seconds=2,
                         trace=[{"title": "Attempt", "body": str(trial["attempt"])}])
            if trial["id"] == "trial-0001" and trial["attempt"] == 1:
                trial.update(status="error", provider_failure={"category": "response_failed"})
                trial["usage"]["unknown_usage_calls"] = 1
            else:
                trial["status"] = "passed"
        def wait(delay):
            pending = self.engine.run_view(self.run["id"])["trials"][0]
            self.assertEqual(pending["status"], "retrying")
            self.assertEqual(pending["progress"]["stage"], "retrying")
            self.assertEqual(pending["auto_retries"], 1)
            self.assertEqual(delay, 10)
            return False
        trial, waiting = self.execute(action, wait)
        self.assertEqual(trial.call_count, 3)
        self.assertEqual(waiting.call_count, 1)
        self.assertEqual(self.run["status"], "completed")
        self.assertEqual(self.run["trials"][0]["attempt"], 2)
        old = self.engine.trial(self.run["id"], "trial-0001", 1)
        self.assertEqual(old["provider_failure"]["category"], "response_failed")
        self.assertEqual(old["trace"][0]["body"], "1")
        self.assertEqual(self.run["summary"][0]["tokens"], 100)
        self.assertEqual(self.run["summary"][0]["unknown_usage_calls"], 1)

    def test_persistent_transient_error_is_bounded_and_retains_every_attempt(self):
        def fail(run, trial, root, settings):
            trial.update(status="error", provider_failure={"category": "http_error", "http_status": 503},
                         usage={"calls": 1, "tokens": 50}, trace=[])
        trial, waiting = self.execute(fail)
        self.assertEqual(trial.call_count, 3)
        self.assertEqual([c.args[0] for c in waiting.call_args_list], [10, 30])
        self.assertEqual(self.run["status"], "incomplete")
        self.assertEqual(self.run["trials"][1]["status"], "not_run")
        self.assertEqual(len(self.store.list("benchmark_attempt")), 2)
        self.assertEqual(self.run["summary"][0]["calls"], 3)

    def test_stop_during_backoff_prevents_another_model_or_task_dispatch(self):
        def fail(run, trial, root, settings):
            trial.update(status="error", provider_failure={"category": "transport_error"})
        def stop(delay):
            self.engine.cancel.set()
            return True
        trial, _ = self.execute(fail, stop)
        self.assertEqual(trial.call_count, 1)
        self.assertEqual(self.run["status"], "cancelled")
        self.assertEqual([t["status"] for t in self.run["trials"]], ["cancelled", "cancelled"])
        self.assertEqual(self.engine.trial(self.run["id"], "trial-0001", 1)["status"], "error")

    def test_restart_during_backoff_preserves_archive_without_dispatch(self):
        trial = self.run["trials"][0]
        trial.update(status="error", provider_failure={"category": "response_failed"})
        self.engine.save_trial(self.run, trial)
        self.engine.save(self.run)
        self.engine.queue_auto_retry(self.run, trial, "fixture")
        recover_benchmarks(self.store)
        run = self.store.get("benchmark_run", self.run["id"])
        self.assertEqual(run["status"], "interrupted")
        self.assertEqual(run["trials"][0]["status"], "not_run")
        self.assertEqual(self.engine.trial(run["id"], trial["id"], 1)["status"], "error")

    def test_bad_environment_blocks_dispatch_with_actionable_checks(self):
        def check(command, **kwargs):
            if command[0] == "docker":
                raise subprocess.CalledProcessError(1, command)
            return "0.22.0"
        with patch("evo_arena.benchmark_runtime.subprocess.check_output", side_effect=check), patch.object(self.engine, "run_trial") as trial:
            self.engine.execute(self.run, self.credentials)
        trial.assert_not_called()
        self.assertEqual(self.run["status"], "incomplete")
        self.assertIn("启动 Docker", self.run["error"])
        self.assertTrue(all(t["status"] == "not_run" for t in self.run["trials"]))

    def test_installation_failure_is_rebuilt_but_a_valid_zero_score_is_not_retried(self):
        def action(run, trial, root, settings):
            if trial["id"] == "trial-0001" and trial["attempt"] == 1:
                trial.update(status="error", failure_stage="setup", exception_type="RuntimeError", usage={"calls": 0, "tokens": 0})
            else:
                trial.update(status="failed", usage={"calls": 1, "tokens": 20})
        trial, _ = self.execute(action)
        self.assertEqual(trial.call_count, 3)
        self.assertEqual(self.run["status"], "completed")
        self.assertEqual([s["pass_rate"] for s in self.run["summary"]], [0, 0])

    def test_retry_configuration_is_strict_and_snapshotted(self):
        for value in (-1, 6, True, "2"):
            with self.assertRaises(ValueError):
                self.engine.plan({**self.fixture.payload, "max_auto_retries": value})
        payload = {k: v for k, v in self.fixture.payload.items() if k != "max_auto_retries"}
        run, _ = self.engine.create(payload, "default-auto")
        self.assertEqual(run["automation"]["max_retries"], 2)
        self.assertEqual(run["automation"]["backoff_seconds"][:2], [10, 30])

    def test_failure_classification_does_not_guess_at_credentials_budget_or_score(self):
        from evo_arena.response_protocol import decode_response, public_summary
        response = {"status": "failed", "error": {"code": "insufficient_quota", "message": "private-text"}}
        _, summary = decode_response(json.dumps(response).encode(), "application/json")
        public = public_summary(summary)
        self.assertEqual(public["provider_code"], "insufficient_quota")
        self.assertNotIn("private-text", json.dumps(public))
        self.assertIsNone(retry_reason({"status": "error", "provider_failure": public}))
        for code in (400, 401, 402, 403, 404, 422):
            self.assertIsNone(retry_reason({"status": "error", "provider_failure": {"category": "http_error", "http_status": code}}))
        for category in ("missing_usage", "invalid_response", "response_incomplete", "cancelled", "task_ended", "job_timeout"):
            self.assertIsNone(retry_reason({"status": "error", "provider_failure": {"category": category}}))
        for status in ("passed", "failed", "scored", "cancelled"):
            self.assertIsNone(retry_reason({"status": status, "provider_failure": {"category": "response_failed"}}))
        self.assertIsNone(retry_reason({"status": "error", "budget_exhausted": True, "provider_failure": {"category": "timeout"}}))
        self.assertIsNone(retry_reason({"status": "error", "failure_stage": "agent"}))
        for code in (408, 429, 500, 502, 503, 504):
            self.assertTrue(retry_reason({"status": "error", "provider_failure": {"category": "http_error", "http_status": code}}))

    def test_harbor_result_identifies_setup_failure_before_any_agent_work(self):
        path = self.fixture.root / "result.json"
        value = {"exception_info": {"exception_type": "RuntimeError"}, "agent_setup": {"started_at": "fixture"}}
        path.write_text(json.dumps(value))
        result = trial_result(path)
        self.assertEqual(result["failure_stage"], "setup")
        self.assertTrue(retry_reason(result))
        value["agent_execution"] = {"started_at": "fixture"}
        path.write_text(json.dumps(value))
        self.assertIsNone(retry_reason(trial_result(path)))


class RuntimePreparationTests(unittest.TestCase):
    def test_install_is_pinned_and_never_targets_system_python(self):
        with patch("evo_arena.benchmark_runtime.sys.prefix", "/system"), patch("evo_arena.benchmark_runtime.sys.base_prefix", "/system"):
            self.assertIsNone(install_command())
        with patch("evo_arena.benchmark_runtime.sys.prefix", "/project/.venv"), patch("evo_arena.benchmark_runtime.shutil.which", return_value="/bin/uv"):
            command = install_command()
            self.assertIn("--python", command)
            self.assertEqual(command[-1], "harbor==0.22.0")

    def test_read_only_preflight_never_installs_but_execute_can_repair_missing_harbor(self):
        with tempfile.TemporaryDirectory() as folder, patch("evo_arena.benchmark_runtime.subprocess.check_output", return_value="ready"), patch(
                "evo_arena.benchmark_runtime.harbor_path", return_value=None), patch("evo_arena.benchmark_runtime.install_harbor") as install:
            _, result = check_environment(Path(folder), None, threading.Event(), True)
            install.assert_not_called()
            self.assertFalse(result["ready"])
        with tempfile.TemporaryDirectory() as folder, patch("evo_arena.benchmark_runtime.subprocess.check_output", return_value="ready"), patch(
                "evo_arena.benchmark_runtime.harbor_path", side_effect=[None, Path('/fixture/harbor')]), patch(
                "evo_arena.benchmark_runtime.install_harbor", return_value=None) as install:
            steps = []
            harbor, result = check_environment(Path(folder), None, threading.Event(), True, repair=True, on_update=steps.append)
            install.assert_called_once()
            self.assertEqual(harbor, Path('/fixture/harbor'))
            self.assertTrue(result["ready"])
            self.assertEqual(result["model_calls"], 0)
            self.assertTrue(any('自动安装 Harbor' in step['phase'] for step in steps))

    def test_cancel_stops_installer_process_group_without_waiting_for_network(self):
        with tempfile.TemporaryDirectory() as folder, patch("evo_arena.benchmark_runtime.install_command", return_value=['fixture']), patch(
                "evo_arena.benchmark_runtime.subprocess.Popen") as launch, patch("evo_arena.benchmark_runtime.os.killpg") as kill:
            process = launch.return_value
            process.pid = 1234
            process.poll.return_value = None
            stop = threading.Event(); stop.set()
            self.assertEqual(install_harbor(Path(folder), stop), "已停止安装")
            kill.assert_called_once()
            process.wait.assert_called_once()
            self.assertEqual((Path(folder) / 'harbor-install.log').stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
