"""Independent Harbor evaluations: task set × Harness × model, without an Evo parent."""
from __future__ import annotations

import json
import math
import os
import platform
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .datasets import DatasetCatalog
from .harness import TrialGateway, read_text
from .provider import Budget
from .store import canonical, digest, now
from .benchmark_progress import attempt_root, snapshot
from .benchmark_retry import DEFAULT_RETRIES, BACKOFF_SECONDS, retry_reason
from .benchmark_runtime import check_environment

ADAPTERS = {"pi": "BenchmarkPi", "dsh": "BenchmarkDSH"}
ACTIVE = {"queued", "preparing", "running", "cancelling", "retrying"}
DETAIL_FIELDS = {"trace", "verifier_output", "harbor_log", "installation_log", "agent_output", "progress"}
GRADED = {"passed", "failed", "scored"}
RETRYABLE = {"error", "not_run", "cancelled"}


def recovery_view(run):
    pending = [t for t in run["trials"] if t["status"] in RETRYABLE]
    retry = sum(t["status"] == "error" or bool(t.get("started")) for t in pending)
    return {"eligible": run["status"] in {"incomplete", "interrupted", "cancelled"} and bool(pending),
            "retry_count": retry, "unrun_count": len(pending) - retry,
            "remaining": len(pending), "preserved": sum(t["status"] in GRADED for t in run["trials"])}


class ModelGateway(TrialGateway):
    on_stage = None

    def model(self, raw):
        if self.failure:
            return super().model(raw)
        if self.on_stage:
            self.on_stage("model")
        try:
            self.record("模型请求", f"第 {self.budget.calls + 1} 次请求，等待模型响应")
            return super().model(raw)
        finally:
            if self.on_stage:
                self.on_stage("agent")

    def dispatch(self, data, ledger):
        if data.get("route") not in {"model", "models"}:
            return self.error(404, "Not found")
        return super().dispatch(data, ledger)


def trial_result(path, failure=None, budget_exhausted=False):
    """Keep missing verification and infrastructure failures out of the failure count."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"status": "error", "error": "Harbor 未生成有效的单题结果"}
    rewards = (data.get("verifier_result") or {}).get("rewards")
    exception = data.get("exception_info")
    result = {"rewards": rewards, "trial_name": data.get("trial_name"),
              "task_checksum": data.get("task_checksum"), "agent_info": data.get("agent_info"),
              "verifier_output": read_text(path.parent / "verifier" / "test-stdout.txt", 24000),
              "result_path": str(path)}
    if failure or budget_exhausted or exception:
        result.update(status="error", error=failure or ("单题预算耗尽" if budget_exhausted else
                      "Harbor: " + str(exception.get("exception_type", "execution_error"))[:160]))
        if exception:
            result["exception_type"] = str(exception.get("exception_type", "execution_error"))[:160]
            result["failure_stage"] = ("verifier" if (data.get("verifier") or {}).get("started_at") else
                "agent" if (data.get("agent_execution") or {}).get("started_at") else
                "setup" if (data.get("agent_setup") or {}).get("started_at") else "environment")
    elif (isinstance(rewards, dict) and type(rewards.get("reward")) in (float, int)
          and math.isfinite(rewards["reward"]) and rewards["reward"] in (0, 1)):
        result.update(status="passed" if rewards["reward"] == 1 else "failed")
    elif isinstance(rewards, dict) and rewards and all(
            type(v) in (int, float) and math.isfinite(v) for v in rewards.values()):
        # Custom multidimensional/continuous rewards are displayed, never invented as pass/fail.
        result.update(status="scored")
    else:
        result.update(status="error", error="验收器没有返回有效得分")
    return result


def summarize(run):
    rows = []
    for entry in run["entries"]:
        trials = [t for t in run["trials"] if t["entry"] == entry["id"]]
        counts = {status: sum(t["status"] == status for t in trials)
                  for status in ("passed", "failed", "scored", "error", "cancelled", "queued", "running", "retrying")}
        graded = counts["passed"] + counts["failed"]
        complete = len(trials) == graded
        attempts = [a for t in trials for a in [*t.get("attempts", []), t]]
        rows.append({**entry, **counts, "total": len(trials),
                     "pass_rate": counts["passed"] / len(trials) if complete and trials else None,
                     "retries": sum(len(t.get("attempts", [])) for t in trials),
                     "calls": sum(t.get("usage", {}).get("calls", 0) for t in attempts),
                     "tokens": sum(t.get("usage", {}).get("tokens", 0) for t in attempts),
                     "unconfirmed_usage_trials": sum(bool(t.get("usage_unconfirmed")) for t in attempts),
                     "unknown_usage_calls": sum(t.get("usage", {}).get("unknown_usage_calls", 0) for t in attempts)})
    return rows


class BenchmarkEngine:
    def __init__(self, store, models, harbor):
        self.store, self.models = store, models
        self.harbor = Path(harbor) if harbor else None
        self.catalog = DatasetCatalog(store)
        self.cancel = threading.Event()

    def overview(self):
        runs = [{k: v for k, v in run.items() if k != "trials"} for run in self.store.list("benchmark_run")]
        return {"datasets": self.catalog.list(), "runs": runs,
                "runners": {"pi": "Pi · 0.82.1", "dsh": "DSH · 0.1.2-rc.1"},
                "automation": {"preflight": True, "install_runtime": True, "default_retries": DEFAULT_RETRIES},
                "installation": self.store.get("benchmark_sync", "terminal-bench-2.1")}

    def preflight(self, *, repair=False, on_update=None):
        harbor, result = check_environment(self.store.root, self.harbor,
            self.cancel if repair else threading.Event(),
            bool(self.models.public()["profiles"]), repair=repair, on_update=on_update)
        if repair:
            self.harbor = harbor
        return result

    def plan(self, payload):
        allowed = {"dataset", "tasks", "entries", "repeats", "max_calls", "max_tokens", "max_auto_retries"}
        if set(payload) - allowed:
            raise ValueError("评测配置包含未知字段")
        dataset = self.catalog.get(payload.get("dataset"))
        selected = payload.get("tasks")
        if not isinstance(selected, list) or not selected or not all(isinstance(t, str) for t in selected):
            raise ValueError("请明确选择要执行的任务")
        if len(set(selected)) != len(selected):
            raise ValueError("任务不能重复选择")
        for task in selected:
            self.catalog.task_path(dataset, task)
        repeats, calls, tokens = payload.get("repeats", 1), payload.get("max_calls", 128), payload.get("max_tokens", 1000000)
        for value, low, high in ((repeats, 1, 5), (calls, 1, 1000), (tokens, 1000, 10000000),
                                 (payload.get("max_auto_retries", DEFAULT_RETRIES), 0, 5)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("重复次数、自动重试次数或单题预算超出范围")
        entries = payload.get("entries")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
            raise ValueError("请选择 1–8 个 Harness × 模型组合")
        resolved, credentials, identities = [], {}, set()
        for index, entry in enumerate(entries):
            if (not isinstance(entry, dict) or set(entry) != {"runner", "model_profile"}
                    or entry["runner"] not in ADAPTERS or not isinstance(entry["model_profile"], str)):
                raise ValueError("Harness 或模型配置无效")
            settings, profile, label = self.models.resolve(entry["model_profile"])
            identity = entry["runner"], settings.model, settings.base_url
            if identity in identities:
                raise ValueError("不能重复选择相同 Harness 与模型连接")
            identities.add(identity)
            ident = "entry-" + str(index + 1)
            resolved.append({"id": ident, "runner": entry["runner"], "model_profile": profile,
                             "model": settings.model, "label": label,
                             "connection_fingerprint": digest([settings.base_url.rstrip("/"), settings.model])})
            credentials[ident] = settings
        return dataset, selected, resolved, credentials, repeats, calls, tokens

    def create(self, payload, key):
        if not key or len(key) > 200:
            raise ValueError("缺少有效的幂等请求标识")
        dataset, selected, entries, credentials, repeats, calls, tokens = self.plan(payload)
        ident = "bench-" + uuid.uuid4().hex[:12]
        trials = [{"id": f"trial-{i + 1:04d}", "task": task, "entry": entry["id"],
                   "runner": entry["runner"], "model": entry["model"], "repeat": repeat,
                   "status": "queued", "attempt": 1, "trace": []}
                  for i, (task, entry, repeat) in enumerate(
                      (task, entry, repeat) for repeat in range(1, repeats + 1)
                      for task in selected for entry in entries)]
        run = {"id": ident, "status": "queued", "created": now(), "dataset": dataset,
               "selected_tasks": selected, "entries": entries, "repeats": repeats, "trials": trials,
               "limits": {"calls_per_trial": calls, "tokens_per_trial": tokens},
               "automation": {"max_retries": payload.get("max_auto_retries", DEFAULT_RETRIES),
                              "backoff_seconds": list(BACKOFF_SECONDS)},
               "protocol": "official-task-timeouts; sequential; classified-infrastructure-retry; bounded-model-relay"}
        run["summary"] = summarize(run)
        self.store.create("benchmark_run", ident, run)
        self.store.create("benchmark_request", digest(key), {"run": ident, "payload_sha256": digest(payload)})
        self.cancel.clear()
        return run, credentials

    def reset_trial(self, run, trial):
        """Prepare fresh work and write intents; caller commits them with the run."""
        writes, previous = [], list(trial.get("attempts", []))
        attempt = trial.get("attempt", 1)
        if trial["status"] == "error" or trial.get("started"):
            evidence = self.trial(run["id"], trial["id"])
            evidence.pop("attempt_options", None)
            evidence.pop("attempts", None)
            evidence["attempt"] = attempt
            writes.append(("benchmark_attempt", f"{run['id']}/{trial['id']}/{attempt}", evidence))
            previous.append({k: v for k, v in evidence.items() if k not in DETAIL_FIELDS})
            attempt += 1
        replacement = {k: trial[k] for k in ("id", "task", "entry", "runner", "model", "repeat")}
        replacement.update(status="queued", attempt=attempt, attempts=previous, trace=[])
        trial.clear()
        trial.update(replacement)
        writes.append(("benchmark_trial", f"{run['id']}/{trial['id']}", trial))
        return writes

    def write_retry(self, run, writes):
        # Called under the Store lock and a single SQLite transaction.
        for kind, ident, value in writes:
            sql = "INSERT INTO objects VALUES(?,?,?,?)"
            if kind == "benchmark_trial":
                sql += " ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data"
            self.store.db.execute(sql, (kind, ident, canonical(value), now()))
        run["updated_at"], run["summary"] = now(), summarize(run)
        self.store.db.execute("UPDATE objects SET data=? WHERE kind='benchmark_run' AND id=?", (canonical(run), run["id"]))

    def queue_auto_retry(self, run, trial, reason):
        retry = trial.get("auto_retries", 0) + 1
        delay = run["automation"]["backoff_seconds"][retry - 1]
        with self.store.lock, self.store.db:
            writes = self.reset_trial(run, trial)
            trial.update(status="retrying", auto_retries=retry, retry_reason=reason,
                         retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
            self.write_retry(run, writes)
        return delay

    def resume(self, run_id, key):
        """Enqueue only unresolved slots; preserve every paid attempt atomically."""
        if not key or len(key) > 200:
            raise ValueError("缺少有效的幂等请求标识")
        with self.store.lock, self.store.db:
            run = self.store.get("benchmark_run", run_id)
            if not run or not recovery_view(run)["eligible"]:
                raise ValueError("这场评测没有可继续的异常或未执行任务")
            pending = [t for t in run["trials"] if t["status"] in RETRYABLE]
            credentials = {}
            for task in {t["task"] for t in pending}:
                self.catalog.task_path(run["dataset"], task)
            for entry in run["entries"]:
                if entry["id"] not in {t["entry"] for t in pending}:
                    continue
                settings, _, _ = self.models.resolve(entry["model_profile"])
                fingerprint = digest([settings.base_url.rstrip("/"), settings.model])
                if (settings.model != entry["model"] or
                        entry.get("connection_fingerprint", fingerprint) != fingerprint):
                    raise ValueError("模型或 API 地址已改变，请恢复原连接后继续，或新建评测")
                entry.setdefault("connection_fingerprint", fingerprint)
                credentials[entry["id"]] = settings
            writes = []
            for trial in pending:
                writes.extend(self.reset_trial(run, trial))
            run.pop("error", None)
            run.pop("finished", None)
            # A legacy record opts into the new default only when explicitly continued.
            run.setdefault("automation", {"max_retries": DEFAULT_RETRIES, "backoff_seconds": list(BACKOFF_SECONDS)})
            run["protocol"] = "official-task-timeouts; sequential; classified-infrastructure-retry; bounded-model-relay"
            run.update(status="queued", updated_at=now())
            run.setdefault("resumptions", []).append({"queued_at": now(), "trials": [t["id"] for t in pending]})
            run["summary"] = summarize(run)
            # One SQLite transaction: an interrupted enqueue cannot lose the old evidence
            # or leave a committed retry without its idempotency record.
            self.write_retry(run, writes)
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)",
                ("benchmark_resume_request", digest(key), canonical({"run": run_id}), now()))
        self.cancel.clear()
        return run, credentials

    def save(self, run):
        run["updated_at"] = now()
        run["summary"] = summarize(run)
        # Polling 89 × N trials must not serialize every trajectory on each model call.
        compact = {**run, "trials": [{k: v for k, v in trial.items() if k not in DETAIL_FIELDS}
                                     for trial in run["trials"]]}
        self.store.update("benchmark_run", run["id"], compact)

    def save_trial(self, run, trial):
        ident = run["id"] + "/" + trial["id"]
        if self.store.get("benchmark_trial", ident):
            self.store.update("benchmark_trial", ident, trial)
        else:
            self.store.create("benchmark_trial", ident, trial)

    def trial(self, run_id, trial_id, attempt=None):
        run = self.store.get("benchmark_run", run_id)
        row = next((t for t in (run or {}).get("trials", []) if t["id"] == trial_id), None)
        if not row:
            return None
        options = [{"attempt": t.get("attempt", 1), "status": t["status"]} for t in [*row.get("attempts", []), row]]
        if attempt is not None and str(attempt) != str(row.get("attempt", 1)):
            if str(attempt) not in {str(t["attempt"]) for t in options}:
                return None
            evidence = self.store.get("benchmark_attempt", f"{run_id}/{trial_id}/{attempt}")
            if not evidence:
                return None
        else:
            evidence = {**(self.store.get("benchmark_trial", run_id + "/" + trial_id) or {}), **row}
        return {**snapshot(self.store.root, run, evidence, include_logs=True), "attempt_options": options}

    def run_view(self, run_id):
        run = self.store.get("benchmark_run", run_id)
        if run:
            run["trials"] = [snapshot(self.store.root, run, trial) for trial in run["trials"]]
            run["summary"] = summarize(run)
            run["recovery"] = recovery_view(run)
        return run

    def execute(self, run, credentials):
        root = self.store.root / "benchmarks" / run["id"]
        try:
            root.mkdir(parents=True, exist_ok=True)
            run.update(status="preparing", last_started=now())
            run.setdefault("started", now())
            def progress(value):
                run["preflight"] = value
                self.save(run)
            readiness = self.preflight(repair=True, on_update=progress)
            if self.cancel.is_set():
                run["status"] = "cancelled"
                return
            if not readiness["ready"]:
                run.update(status="incomplete", error="；".join(c["detail"] for c in readiness["checks"] if not c["ok"]))
                return
            run["status"] = "running"
            run["platform"] = {"host_arch": platform.machine()}
            try:
                run["platform"]["engine_arch"] = subprocess.check_output(
                    ["docker", "version", "--format", "{{.Server.Arch}}"], text=True,
                    stderr=subprocess.DEVNULL, timeout=8).strip()
            except (OSError, subprocess.SubprocessError):
                pass
            run["harbor_version"] = subprocess.check_output([str(self.harbor), "--version"], text=True).strip()
            self.save(run)
            for trial in run["trials"]:
                if trial["status"] != "queued":
                    continue
                if self.cancel.is_set():
                    break
                while not self.cancel.is_set():
                    self.run_trial(run, trial, root, credentials[trial["entry"]])
                    self.save_trial(run, trial)
                    self.save(run)
                    for key in DETAIL_FIELDS:
                        trial.pop(key, None)
                    reason = retry_reason(trial)
                    retries = (run.get("automation") or {}).get("max_retries", 0)
                    if not reason or trial.get("auto_retries", 0) >= retries or self.cancel.is_set():
                        break
                    delay = self.queue_auto_retry(run, trial, reason)
                    if self.cancel.wait(delay):
                        break
                    trial.update(status="queued")
                    trial.pop("retry_at", None)
                if self.cancel.is_set():
                    break
                if trial.get("provider_failure") or trial.get("budget_exhausted"):
                    run["error"] = ("单题预算耗尽，需要处理" if trial.get("budget_exhausted") else
                                    "自动重试后模型仍异常，需要处理" if trial.get("auto_retries") else
                                    "模型调用异常，需要处理")
                    break
            run["status"] = "cancelled" if self.cancel.is_set() else (
                "completed" if all(t["status"] in {"passed", "failed", "scored"} for t in run["trials"]) else "incomplete")
        except Exception as exc:
            run.update(status="interrupted", error="执行中断：" + type(exc).__name__)
        finally:
            for trial in run["trials"]:
                if trial["status"] == "running":
                    trial.update(status="error", error="执行中断，结果未知", usage_unconfirmed=True)
                    self.save_trial(run, trial)
                elif trial["status"] in {"queued", "retrying"}:
                    trial["status"] = "cancelled" if self.cancel.is_set() else "not_run"
            run["finished"] = now()
            self.save(run)

    def run_trial(self, run, trial, root, settings):
        path = self.catalog.task_path(run["dataset"], trial["task"])
        root = attempt_root(root, trial)
        trial.update(status="running", phase="准备任务环境", stage="environment", started=now(), heartbeat_at=now())
        work = root / trial["id"]
        spool, ledger = work / "spool", work / "ledger"
        spool.mkdir(parents=True)
        spool.chmod(0o777)
        ledger.mkdir()

        def record(title, body):
            trial["trace"].append({"time": now(), "title": title, "body": body})
            trial["trace"] = trial["trace"][-200:]
            trial["usage"] = gateway.budget.public()
            trial["heartbeat_at"] = now()
            self.save_trial(run, trial)
            self.save(run)

        def set_stage(stage):
            trial.update(stage=stage, stage_started=now(), heartbeat_at=now(),
                         phase="等待模型响应" if stage == "model" else "Agent 执行", usage=gateway.budget.public())
            self.save(run)

        gateway = ModelGateway(settings, record)
        gateway.on_stage = set_stage
        gateway.budget = Budget(run["limits"]["calls_per_trial"], run["limits"]["tokens_per_trial"])
        gateway.end_marker = work / "agent-ended"
        config = build_job_config(path, root, trial, spool)
        config_path = work / "job.json"
        config_path.write_text(json.dumps(config))
        env = {k: v for k, v in os.environ.items() if not k.endswith(("API_KEY", "TOKEN"))}
        env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent), HARBOR_TELEMETRY="off",
                   ARENA_TRIAL_URL="http://127.0.0.1:18765", ARENA_TRIAL_TOKEN="arena",
                   EVO_TRIAL_END_FILE=str(gateway.end_marker))
        env.pop("EVO_VERSION_FILE", None)
        from .datasets import harbor_task
        task_config = harbor_task(path).config
        timeout = (task_config.agent.timeout_sec + task_config.verifier.timeout_sec
                   + task_config.environment.build_timeout_sec + 1980)
        started = time.monotonic()
        last_saved = started
        self.save(run)
        with (work / "harbor.log").open("w") as log:
            process = subprocess.Popen([str(self.harbor), "run", "--config", str(config_path), "--yes"],
                                       cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            gateway.stop_check = lambda: ("cancelled" if self.cancel.is_set() else
                "task_ended" if process.poll() is not None else
                "job_timeout" if time.monotonic() - started > timeout else None)
            try:
                while process.poll() is None:
                    if self.cancel.is_set() or time.monotonic() - started > timeout:
                        break
                    gateway.drain(spool, ledger)
                    if time.monotonic() - last_saved >= 5:
                        trial["heartbeat_at"] = now()
                        trial["usage"] = gateway.budget.public()
                        if gateway.end_marker.exists():
                            trial["phase"] = "验收中"
                            if trial.get("stage") != "verifier":
                                trial.update(stage="verifier", stage_started=now())
                        elif gateway.budget.calls:
                            trial["phase"] = "Agent 执行"
                        self.save(run)
                        last_saved = time.monotonic()
                    time.sleep(.1)
            finally:
                try:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                finally:
                    trial["usage"] = gateway.budget.public()
        trial.update(seconds=round(time.monotonic() - started, 2), usage=gateway.budget.public(),
                     finished=now(), harbor_exit_code=process.returncode,
                     harbor_log=read_text(work / "harbor.log", 16000))
        if gateway.failure_detail:
            trial["provider_failure"] = gateway.failure_detail
        if gateway.budget_exhausted:
            trial["budget_exhausted"] = True
        files = list((root / "jobs" / trial["id"]).glob("*/result.json"))
        if self.cancel.is_set():
            trial.update(status="cancelled")
        elif len(files) != 1:
            trial.update(status="error", error="Harbor 没有生成唯一的单题结果，请查看执行日志")
        else:
            trial.update(trial_result(files[0], gateway.failure, gateway.budget_exhausted))


def build_job_config(task_path, root, trial, spool):
    return {"job_name": trial["id"], "jobs_dir": str(root / "jobs"), "n_attempts": 1,
            "n_concurrent_trials": 1, "retry": {"max_retries": 0},
            "environment": {"type": "docker", "delete": True,
                            "mounts": [{"type": "bind", "source": str(spool), "target": "/arena-relay"}]},
            "agents": [{"import_path": "evo_arena.benchmark_agents:" + ADAPTERS[trial["runner"]],
                        "model_name": "arena/" + trial["model"], "override_setup_timeout_sec": 1800}],
            "tasks": [{"path": str(task_path)}]}


def recover_benchmarks(store):
    for run in store.list("benchmark_run"):
        if run["status"] in ACTIVE:
            run.update(status="interrupted", finished=now(), error="服务重启，未自动重放评测")
            for trial in run["trials"]:
                if trial["status"] in {"running", "queued", "retrying"}:
                    if trial["status"] == "running":
                        trial["usage_unconfirmed"] = True
                    trial["status"] = "error" if trial["status"] == "running" else "not_run"
            run["summary"] = summarize(run)
            store.update("benchmark_run", run["id"], run)
    state = store.get("benchmark_sync", "terminal-bench-2.1")
    if state and state["status"] == "running":
        state.update(status="error", error="下载被中断，可重新接入")
        store.update("benchmark_sync", state["id"], state)
