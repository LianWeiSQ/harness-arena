import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo_arena.benchmarks import BenchmarkEngine, build_job_config, recover_benchmarks, summarize, trial_result
from evo_arena.config import Settings
from evo_arena.datasets import DatasetCatalog, TB21
from evo_arena.models import ModelProfiles
from evo_arena.store import Store
from evo_arena.web import Manager


@unittest.skipUnless(importlib.util.find_spec("harbor"), "Install the harbor extra")
class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "data")
        self.models = ModelProfiles(self.store.root, Settings("https://example.invalid/v1", "test-model", "secret-test"))
        self.catalog = DatasetCatalog(self.store)
        self.engine = BenchmarkEngine(self.store, self.models, Path(__file__))
        task = self.root / "input" / "task-one"
        (task / "environment").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "task.toml").write_text('version = "1.0"\n[agent]\ntimeout_sec=900\n[verifier]\ntimeout_sec=120\n[environment]\ncpus=1\nmemory_mb=1024\n')
        (task / "instruction.md").write_text("Test fixture; never a production benchmark.")
        (task / "environment" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
        (task / "tests" / "test.sh").write_text("#!/bin/sh\necho 1 > /logs/verifier/reward.txt\n")
        self.task = task
        self.dataset = self.catalog.import_local({"name": "Fixture", "path": str(task.parent)})
        self.payload = {"dataset": self.dataset["id"], "tasks": ["task-one"],
                        "entries": [{"runner": "pi", "model_profile": "startup"},
                                    {"runner": "dsh", "model_profile": "startup"}]}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_catalog_has_only_official_placeholder_and_explicit_imports(self):
        rows = self.catalog.list()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], TB21["id"])
        self.assertEqual(rows[0]["status"], "not_installed")
        self.assertNotIn("business-tools", json.dumps(rows))
        self.assertEqual(self.dataset["tasks"][0]["seconds"], 900)

    def test_import_snapshots_content_and_rejects_tampering(self):
        self.task.joinpath("instruction.md").write_text("Original edited after import")
        path = self.catalog.task_path(self.dataset, "task-one")
        self.assertIn("Test fixture", path.joinpath("instruction.md").read_text())
        path.joinpath("tests/test.sh").write_text("modified")
        with self.assertRaisesRegex(ValueError, "快照"):
            self.catalog.task_path(self.dataset, "task-one")

    def test_import_rejects_links_and_invalid_tasks(self):
        (self.task / "escape").symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "链接"):
            self.catalog.import_local({"name": "link", "path": str(self.task)})
        (self.task / "escape").unlink()
        (self.task / "tests/test.sh").unlink()
        with self.assertRaisesRegex(ValueError, "无效"):
            self.catalog.import_local({"name": "missing verifier", "path": str(self.task)})

    def test_plan_needs_real_tasks_and_unique_combinations_and_no_calls(self):
        with patch("evo_arena.harness.TrialGateway.model", side_effect=AssertionError("Model must not be called")):
            run, _ = self.engine.create(self.payload, "create")
        self.assertEqual(len(run["trials"]), 2)
        self.assertEqual(run["trials"][0]["status"], "queued")
        self.assertNotIn("secret-test", json.dumps(run))
        self.assertNotIn("example.invalid", json.dumps(run))
        for change in ({"tasks": []}, {"tasks": ["../escape"]},
                       {"entries": [self.payload["entries"][0]] * 2}, {"repeats": True}):
            with self.assertRaises(ValueError):
                self.engine.plan({**self.payload, **change})

    def test_idempotent_start_and_global_exclusion(self):
        class Worker:
            def __init__(self, **kwargs): pass
            def start(self): pass
            def is_alive(self): return True
        manager = Manager(self.store, None, "")
        with patch("evo_arena.web.threading.Thread", Worker):
            first, code = manager.start_benchmark(self.engine, self.payload, "once")
            self.assertEqual(code, 202)
            same, code = manager.start_benchmark(self.engine, self.payload, "once")
            self.assertEqual(code, 200)
            self.assertEqual(first["id"], same["id"])
            self.assertEqual(manager.start_benchmark(self.engine, self.payload, "other")[1], 409)
            self.assertEqual(manager.start_benchmark(self.engine, {**self.payload, "repeats": 2}, "once")[1], 409)

    def test_harbor_config_keeps_verifier_timeouts_and_never_mounts_solutions(self):
        from harbor.models.job.config import JobConfig
        cfg = build_job_config(self.task, self.root, {"id": "trial", "runner": "pi", "model": "test"}, self.root / "spool")
        config = JobConfig.model_validate(cfg)
        self.assertEqual(config.n_concurrent_trials, 1)
        self.assertEqual(config.retry.max_retries, 0)
        self.assertEqual(config.tasks[0].path, self.task)
        self.assertFalse(config.verifier.disable)
        self.assertIsNone(config.agents[0].override_timeout_sec)
        self.assertEqual(config.agents[0].override_setup_timeout_sec, 1800)
        self.assertEqual([m["target"] for m in config.environment.mounts], ["/arena-relay"])

    def test_infrastructure_and_continuous_rewards_not_misreported(self):
        target = self.root / "result.json"
        def result(rewards, **kwargs):
            target.write_text(json.dumps({"verifier_result": {"rewards": rewards}, **kwargs}))
            return trial_result(target)
        self.assertEqual(result({"reward": 1})["status"], "passed")
        self.assertEqual(result({"reward": 0})["status"], "failed")
        self.assertEqual(result({"reward": 1}, exception_info={"exception_type": "AgentError"})["status"], "error")
        self.assertEqual(result(None)["status"], "error")
        self.assertEqual(result({"quality": .5})["status"], "scored")
        self.assertEqual(result({"reward": True})["status"], "error")
        self.assertEqual(trial_result(target, "provider unavailable")["status"], "error")

    def test_restart_never_replays_or_publishes_incomplete_pass_rate(self):
        run, _ = self.engine.create(self.payload, "recovery")
        run["status"] = "running"
        run["trials"][0]["status"] = "passed"
        run["trials"][1]["status"] = "running"
        self.engine.save(run)
        recover_benchmarks(self.store)
        saved = self.store.get("benchmark_run", run["id"])
        self.assertEqual(saved["status"], "interrupted")
        self.assertEqual(saved["summary"][0]["pass_rate"], 1)
        self.assertIsNone(saved["summary"][1]["pass_rate"])
        self.assertEqual(saved["summary"][1]["failed"], 0)

    def test_cancellation_and_provider_failure_do_not_dispatch_remaining_trials(self):
        run, credentials = self.engine.create(self.payload, "stop")
        def fail(run, trial, root, settings):
            trial.update(status="error", provider_failure={"category": "http_error"})
        with patch.object(self.engine, "run_trial", side_effect=fail) as execute, patch(
                "evo_arena.benchmarks.subprocess.check_output", return_value="0.22.0"):
            self.engine.execute(run, credentials)
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(run["trials"][1]["status"], "not_run")
        self.assertEqual(run["status"], "incomplete")

    def test_trial_command_relay_and_result_contract_without_execution(self):
        run, credentials = self.engine.create(self.payload, "contract")
        trial = run["trials"][0]
        root = self.root / "trial-contract"
        root.mkdir()
        captured = {}

        class Process:
            returncode = 0

            def __init__(self, args, **kwargs):
                captured.update(args=args, **kwargs)
                config = json.loads(Path(args[3]).read_text())
                self.config = config
                out = Path(config["jobs_dir"]) / config["job_name"] / "task-one__fixture"
                (out / "verifier").mkdir(parents=True)
                (out / "verifier/test-stdout.txt").write_text("fixture verifier")
                (out / "result.json").write_text(json.dumps({"trial_name": "task-one__fixture",
                    "verifier_result": {"rewards": {"reward": 1}}, "task_checksum": "fixture-checksum"}))

            def poll(self): return 0

        with patch("evo_arena.benchmarks.subprocess.Popen", Process), patch(
                "evo_arena.harness.TrialGateway.model", side_effect=AssertionError("No model calls")):
            self.engine.run_trial(run, trial, root, credentials[trial["entry"]])
        self.assertEqual(trial["status"], "passed")
        self.assertEqual(trial["verifier_output"], "fixture verifier")
        self.assertEqual(trial["usage"]["calls"], 0)
        self.assertEqual(captured["env"]["ARENA_TRIAL_URL"], "http://127.0.0.1:18765")
        self.assertNotIn("secret-test", repr(captured))
        self.assertNotIn("OPENAI_API_KEY", captured["env"])

    def test_user_stop_is_not_reported_as_a_provider_outage(self):
        run, credentials = self.engine.create(self.payload, "user-stop")
        def stop(run, trial, root, settings):
            self.engine.cancel.set()
            trial.update(status="cancelled", provider_failure={"category": "cancelled"})
        with patch.object(self.engine, "run_trial", side_effect=stop), patch(
                "evo_arena.benchmarks.subprocess.check_output", return_value="0.22.0"):
            self.engine.execute(run, credentials)
        self.assertEqual(run["status"], "cancelled")
        self.assertNotIn("error", run)
        self.assertEqual(run["trials"][1]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
