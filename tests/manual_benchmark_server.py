"""Browser fixture on 8797. Temporary database, no model calls or task execution."""
import sys
import time
import json
from pathlib import Path
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
from test_benchmarks import BenchmarkTests
from evo_arena.benchmarks import BenchmarkEngine
from evo_arena.benchmark_progress import attempt_root
from evo_arena.web import serve
from evo_arena.store import now

fixture = BenchmarkTests()
fixture.setUp()
if "--retry" in sys.argv:
    run, _ = fixture.engine.create({**fixture.payload, "repeats": 2}, "retry-browser-fixture")
    run.update(status="incomplete", error="模型调用异常，可重试并继续")
    for trial, status in zip(run["trials"], ["passed", "failed", "error", "not_run"]):
        trial["status"] = status
        if status != "not_run":
            trial.update(started=now(), finished=now(), seconds=2,
                         usage={"calls": 1, "tokens": 50},
                         trace=[{"title": "Previous attempt", "body": "Original fixture evidence"}])
        fixture.engine.save_trial(run, trial)
    fixture.engine.save(run)


def fake_trial(self, run, trial, root, settings):
    root = attempt_root(root, trial)
    trial.update(status="running", started=now(), stage="environment")
    run["platform"] = {"engine_arch": "arm64"}
    self.save(run)
    if trial["runner"] == "pi":
        agent = root / "jobs" / trial["id"] / "task__fixture" / "agent"
        (agent / "setup").mkdir(parents=True)
        (agent / "setup-progress.json").write_text(json.dumps({"stage": "node", "started_at": now(), "guest_arch": "amd64"}))
        (agent / "setup/install.log").write_text("Fixture download: 10%\n")
        time.sleep(5)
        (agent / "setup/install.log").write_text("Fixture download: 100%\n")
        (agent / "pi.jsonl").write_text(json.dumps({"type": "tool_execution_update", "toolName": "bash",
            "partialResult": {"content": [{"type": "text", "text": "Fixture tool in progress"}]}}) + "\n")
        trial.update(stage="agent", stage_started=now())
        self.save(run)
        time.sleep(7)
    trial.update(status="passed" if trial["runner"] == "pi" else "failed", seconds=2,
                 finished=now(),
                 usage={"calls": 2, "tokens": 100, "unknown_usage_calls": 0},
                 verifier_output="Controlled browser fixture. Not a benchmark result.",
                 trace=[{"title": "Fixture", "body": "No model was called."}])


try:
    with patch.object(BenchmarkEngine, "run_trial", fake_trial), patch(
            "evo_arena.benchmarks.subprocess.check_output", return_value="0.22.0"):
        serve(fixture.store, fixture.models.inherited, port=8797, harbor_bin=Path(sys.executable))
finally:
    fixture.tearDown()
