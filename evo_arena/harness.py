from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from .provider import Budget, BudgetExceeded, NoRedirect, ProviderError
from .store import now


FIXTURES = Path(__file__).with_name("fixtures") / "harness"
BASE = "4cb37be2d024049b76b2392696ea94ca9acce376"
FIX = "3f29065e740a1d9d0b05308b26b28ce41034fc62"
FILES = ("scripts/smoke-provider-recovery-ui.mjs", "scripts/smoke-session-lifecycle-ui.mjs")
TASKS = {
    "process-cleanup": {
        "title": "测试结束后清理后台进程",
        "category": "真实维护任务",
        "prompt": "桌面应用的两条冒烟测试结束后，后台服务有时仍在运行，占着端口。请修改 /work/scripts 内的两份原始脚本，清理测试启动的进程树，包括直接启动的进程已经退出但后代仍在运行的情况。保留原有测试流程和日志。直接修改文件，并简要说明如何验证。",
    },
    "lost-receipt": {
        "title": "下单回执超时后的安全恢复",
        "category": "运行时故障场景",
        "prompt": "使用终端工具 arena-order 提交一笔订单。arena-order submit 发出提交，arena-order status 查询可信的订单记录。即使工具返回异常，也要确保服务最终只写入一笔订单，并在回答里简要报告实际状态。",
    },
}
RUNNERS = {"dsh": ("DeepSeek Harness", "ArenaDSH"), "pi": ("Pi", "ArenaPi")}
TIME_LIMIT = 240
MAX_CALLS = 12
MAX_TOKENS = 180_000
ACTIVE = {"queued", "preparing", "running"}
LIMITS = {"seconds": TIME_LIMIT, "calls": MAX_CALLS, "tokens": MAX_TOKENS}


def command(*args: str, timeout: int = 20) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout.strip()


def snapshot(repo: Path, revision: str) -> dict[str, str]:
    return {path: subprocess.check_output(["git", "-C", str(repo), "show", f"{revision}:{path}"],
                                          timeout=12).decode() for path in FILES}


def write_files(root: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def read_text(path: Path, limit: int = 200_000) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    with path.open("rb") as handle:
        return handle.read(limit).decode(errors="replace")


class TrialGateway:
    """Forward one trial's model calls and keep the order ledger outside the sandbox."""
    def __init__(self, settings, record):
        self.settings, self.record = settings, record
        self.budget = Budget(MAX_CALLS, MAX_TOKENS)
        self.opener = urllib.request.build_opener(NoRedirect())
        self.failure = None
        self.budget_exhausted = False
        self.outputs = set()
        self.writes = 0
        self.checked_after_fault = False
        self.verifier_token = secrets.token_urlsafe()

    def model(self, raw: bytes) -> tuple[int, str, str]:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("model") != self.settings.model:
                raise ValueError()
            if any(not isinstance(tool, dict) or tool.get("type") != "function" for tool in (data.get("tools") or [])):
                raise ValueError()
            data["store"] = False
            data["max_output_tokens"] = min(8192, int(data.get("max_output_tokens") or 8192))
            self.budget.claim()
        except BudgetExceeded:
            self.budget_exhausted = True
            return self.error(429, "达到模型请求或 Token 预算")
        except (ValueError, TypeError):
            return self.error(400, "Unsupported model request")
        for item in data.get("input", []) if isinstance(data.get("input"), list) else []:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                call_id = str(item.get("call_id", ""))
                if call_id not in self.outputs:
                    self.outputs.add(call_id)
                    self.record("工具返回", str(item.get("output", ""))[:2600])
        req = urllib.request.Request(self.settings.base_url + "/responses",
            data=json.dumps(data).encode(), method="POST",
            headers={"Authorization": "Bearer " + self.settings.api_key, "Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=75) as response:
                content_type = response.headers.get("Content-Type", "application/json")
                result = response.read(6_000_001).decode()
            if len(result) > 6_000_000:
                raise ValueError()
            if "text/event-stream" in content_type:
                records = [json.loads(line[5:]) for line in result.splitlines()
                           if line.startswith("data:") and line[5:].strip() not in {"", "[DONE]"}]
                completed = next((r["response"] for r in reversed(records)
                                  if r.get("type") == "response.completed"), {})
            else:
                completed = json.loads(result)
            self.budget.record(completed.get("usage") or {})
            for item in completed.get("output", []):
                if item.get("type") == "function_call":
                    self.record("调用 " + str(item.get("name", "tool")), str(item.get("arguments", ""))[:2600])
                elif item.get("type") == "message":
                    for part in item.get("content", []):
                        if part.get("type") == "output_text" and part.get("text"):
                            self.record("Agent 输出", part["text"][:2600])
            return 200, content_type, result
        except urllib.error.HTTPError as error:
            self.failure = f"模型供应商 HTTP {error.code}"
        except (OSError, ValueError, ProviderError, KeyError, TypeError):
            self.failure = "模型响应异常或用量未报告"
        self.budget.unknown_usage_calls = max(1, self.budget.unknown_usage_calls)
        return self.error(502, self.failure)

    @staticmethod
    def error(status: int, message: str) -> tuple[int, str, str]:
        return status, "application/json", json.dumps({"error": {"message": message}})

    def write_ledger(self, folder):
        payload = json.dumps({"writes": self.writes, "checkedAfterFault": self.checked_after_fault})
        signature = hmac.new(self.verifier_token.encode(), payload.encode(), hashlib.sha256).hexdigest()
        atomic_json(folder / "receipt.json", {"payload": payload, "signature": signature})

    def dispatch(self, data, ledger):
        route = data.get("route")
        if route == "models":
            return 200, "application/json", json.dumps({"object": "list",
                "data": [{"id": self.settings.model, "object": "model"}]})
        if route == "model":
            body = data.get("body")
            return self.model(body.encode()) if isinstance(body, str) else self.error(400, "Invalid request")
        if route == "submit":
            self.writes += 1
            self.write_ledger(ledger)
            if self.writes == 1:
                self.record("故障注入", "订单已写入，工具收到 HTTP 504，模拟回执超时。")
                return self.error(504, "Order response timed out. Outcome unknown.")
            self.record("外部账本", f"再次提交，累计写入 {self.writes} 笔。")
        elif route == "status":
            self.checked_after_fault |= self.writes > 0
            self.write_ledger(ledger)
            self.record("外部账本", f"查询状态，当前有 {self.writes} 笔订单。")
        else:
            return self.error(404, "Not found")
        return 200, "application/json", json.dumps({"ordersWritten": self.writes})

    def drain(self, spool, ledger):
        for request in spool.glob("*.request"):
            if not re.fullmatch(r"[0-9a-f-]{36}\.request", request.name):
                continue
            try:
                fd = os.open(request, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise ValueError()
                    data = json.loads(handle.read(4_000_001))
                if not isinstance(data, dict):
                    raise ValueError()
            except (OSError, ValueError):
                reply = self.error(400, "Invalid relay request")
            else:
                reply = self.dispatch(data, ledger)
            request.unlink(missing_ok=True)
            status, content_type, body = reply
            atomic_json(request.with_suffix(".reply"), {"status": status, "type": content_type, "body": body})


def atomic_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(data, handle)
        temporary = Path(handle.name)
    temporary.chmod(0o644)
    temporary.replace(path)


def materialize_tasks(root: Path, image: str, original: dict[str, str]) -> None:
    for key, task in TASKS.items():
        path = root / key
        maintenance = key == "process-cleanup"
        artifacts = json.dumps(["/work/" + file for file in FILES]) if maintenance else '["/arena-ledger/receipt.json"]'
        (path / "environment").mkdir(parents=True, exist_ok=True)
        (path / "tests").mkdir(exist_ok=True)
        config = f"""version = "1.0"
artifacts = {artifacts}

[task]
name = "evoarena/{key}"
description = {json.dumps(task["title"], ensure_ascii=False)}

[agent]
timeout_sec = {TIME_LIMIT}
user = "node"

[environment]
cpus = 2
memory_mb = 1024

[verifier]
timeout_sec = 45
environment_mode = "separate"
include_logs = ["test-stdout.txt"]

[verifier.environment]
cpus = 1
memory_mb = 512
"""
        files = {
            "task.toml": config, "instruction.md": task["prompt"] + "\n",
            "environment/Dockerfile": f"FROM {image}\nWORKDIR /work\nRUN chown node:node /work\n",
            "tests/Dockerfile": f"FROM {image}\nCOPY . /tests\nRUN chmod +x /tests/test.sh\n",
        }
        # Docker Desktop may lack Harbor's egress-control sidecar kernel support.
        # Enforce offline operation directly in both Compose projects.
        offline = "services:\n  main:\n    network_mode: none\n"
        files["environment/docker-compose.yaml"] = offline
        files["tests/docker-compose.yaml"] = offline
        if maintenance:
            files["environment/Dockerfile"] += (
                "COPY --chown=node:node scripts /work/scripts\n"
                "RUN su -s /bin/sh node -c 'git init -q && git add scripts && git -c user.name=Arena -c user.email=arena@local commit -qm baseline'\n"
            )
            files.update({"environment/" + name: content for name, content in original.items()})
            files["tests/verify.mjs"] = (FIXTURES / "verify.mjs").read_text()
            files["tests/test.sh"] = """#!/bin/sh
set -eu
node /tests/verify.mjs /work/scripts > /tmp/checks.json
node -e 'const fs=require("node:fs");const checks=JSON.parse(fs.readFileSync("/tmp/checks.json"));fs.writeFileSync("/logs/verifier/reward.json",JSON.stringify({reward:Number(checks.length===4&&checks.every(c=>c.passed))}));console.log(JSON.stringify({checks}))'
"""
        else:
            files["tests/verify.cjs"] = (FIXTURES / "verify-order.cjs").read_text()
            files["tests/test.sh"] = "#!/bin/sh\nset -eu\nnode /tests/verify.cjs\n"
        write_files(path, files)


class HarnessMatch:
    def __init__(self, store, settings, repo, harbor, image):
        self.store, self.settings, self.repo, self.harbor, self.image = store, settings, repo, harbor, image

    @property
    def ready(self):
        return bool(self.settings and self.repo and self.repo.is_dir() and self.harbor and self.harbor.is_file())

    def overview(self):
        return {"tasks": TASKS, "base": BASE, "fix": FIX, "runners": {k: v[0] for k, v in RUNNERS.items()},
                "model": self.settings.model if self.settings else None, "ready": self.ready,
                "limits": LIMITS,
                "matches": [{k: v for k, v in match.items() if k != "runs"}
                            for match in self.store.list("harness_match")]}

    def create(self):
        ident = "hm-" + uuid.uuid4().hex[:12]
        return self.store.create("harness_match", ident, {"id": ident, "model": self.settings.model,
            "created": now(), "status": "queued", "base": BASE, "fix": FIX, "runs": [],
            "engine": "harbor", "mode": "arena", "runners": list(RUNNERS), "limits": LIMITS})

    def execute(self, match):
        try:
            match["status"] = "preparing"
            self.save(match)
            match["harbor_version"] = command(str(self.harbor), "--version")
            match["image"] = command("docker", "image", "inspect", self.image, "--format", "{{.Id}}")
            original = snapshot(self.repo, BASE)
            root = self.store.root / "harbor" / match["id"]
            materialize_tasks(root / "tasks", self.image, original)
            match["status"] = "running"
            self.save(match)
            for task in TASKS:
                for runner in match["runners"]:
                    run = {"runner": runner, "task": task, "status": "running", "trace": [],
                           "checks": [], "patch": "", "model": match["model"]}
                    match["runs"].append(run)
                    self.save(match)
                    try:
                        self.run_trial(match, run, root, original)
                    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                        run.update(status="error", error="Harbor 执行异常 · " + type(error).__name__)
                    self.save(match)
            match["status"] = "completed" if all(r["status"] in {"passed", "failed"} for r in match["runs"]) else "incomplete"
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            match.update(status="incomplete", error="赛场准备失败 · " + type(error).__name__)
        finally:
            match["finished"] = now()
            self.save(match)

    def save(self, match):
        self.store.update("harness_match", match["id"], match)

    def run_trial(self, match, run, root, original):
        def record(label, message):
            run["trace"].append({"label": label, "text": message, "time": now()})
            self.save(match)

        task, runner = run["task"], run["runner"]
        job = f"{task}-{runner}"
        jobs = root / "jobs"
        jobs.mkdir(exist_ok=True)
        run["job"] = job
        gateway = TrialGateway(self.settings, record)
        worker_root = self.harbor.parent.parent.parent
        python_path = os.pathsep.join([str(Path(__file__).resolve().parent.parent), str(worker_root)])
        with tempfile.TemporaryDirectory(prefix="evoarena-relay-") as temporary, (root / f"{job}.log").open("w") as logs:
            spool, ledger = Path(temporary) / "spool", Path(temporary) / "ledger"
            spool.mkdir()
            spool.chmod(0o777)
            ledger.mkdir(mode=0o755)
            gateway.write_ledger(ledger)
            mounts = json.dumps([
                {"type": "bind", "source": str(spool), "target": "/arena-relay"},
                {"type": "bind", "source": str(ledger), "target": "/arena-ledger", "read_only": True},
            ])
            env = {**os.environ, "PYTHONPATH": python_path, "ARENA_TRIAL_URL": "http://127.0.0.1:18765",
                   "ARENA_TRIAL_TOKEN": "arena", "HARBOR_TELEMETRY": "off"}
            for name in ("ARENA_API_KEY", "OPENAI_API_KEY", "MAAS_API_KEY", "ANTHROPIC_API_KEY"):
                env.pop(name, None)
            args = [str(self.harbor), "run", "--path", str(root / "tasks" / task), "--jobs-dir", str(jobs),
                    "--job-name", job, "--agent", "evo_arena.harbor_agents:" + RUNNERS[runner][1],
                    "--model", "arena/" + match["model"], "--n-concurrent", "1", "--max-retries", "0",
                    "--mounts", mounts, "--yes"]
            if task == "lost-receipt":
                args += ["--ve", "ARENA_VERIFY_TOKEN=" + gateway.verifier_token]
            record("Harbor 启动", f"{RUNNERS[runner][0]} · {TASKS[task]['title']}")
            with subprocess.Popen(args, env=env, cwd=worker_root, stdout=logs, stderr=subprocess.STDOUT,
                                  start_new_session=True) as process:
                deadline = time.monotonic() + TIME_LIMIT + 150
                try:
                    while process.poll() is None and time.monotonic() < deadline:
                        gateway.drain(spool, ledger)
                        time.sleep(.10)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGINT)
                        try:
                            process.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                run["usage"] = gateway.budget.public()
                if gateway.budget_exhausted:
                    run["stop_reason"] = "达到模型请求或 Token 预算"
        results = list((jobs / job).glob("*/result.json"))
        if len(results) != 1:
            run.update(status="error", error="Harbor 未生成单题结果")
            return
        self.load_result(run, results[0], original, gateway.failure)

    @staticmethod
    def load_result(run, path, original, provider_failure):
        data = json.loads(path.read_text())
        trial = path.parent
        timing = data.get("agent_execution") or {}
        if timing.get("started_at") and timing.get("finished_at"):
            run["seconds"] = round((datetime.fromisoformat(timing["finished_at"]) -
                                    datetime.fromisoformat(timing["started_at"])).total_seconds(), 2)
        run["trial"] = data["trial_name"]
        run["task_checksum"] = data["task_checksum"]
        run["runtime_version"] = (data.get("agent_info") or {}).get("version")
        run["verifier_mode"] = data.get("verifier_environment_mode")
        run["atif_available"] = (trial / "agent" / "trajectory.json").is_file()
        if run["task"] == "process-cleanup":
            for name, source in original.items():
                artifact = trial / "artifacts" / "work" / name
                if not artifact.is_relative_to(trial / "artifacts") or not artifact.resolve().is_relative_to(trial.resolve()):
                    continue
                candidate = read_text(artifact)
                if artifact.is_file() and candidate != source:
                    run["patch"] += "".join(difflib.unified_diff(source.splitlines(keepends=True),
                        candidate.splitlines(keepends=True), "a/" + name, "b/" + name))
        output = read_text(trial / "verifier" / "test-stdout.txt", 8000)
        for line in reversed(output.splitlines()):
            if line.startswith("{"):
                evidence = json.loads(line)
                run["checks"] = evidence.get("checks", [])
                if "ledger" in evidence:
                    run["ledger"] = evidence["ledger"]
                break
        exception = data.get("exception_info") or {}
        reward = ((data.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        exception_type = exception.get("exception_type")
        if exception_type == "AgentTimeoutError":
            run["stop_reason"] = "达到执行时限"
        # Judge the frozen artifact when Harbor still ran the verifier after a contest limit.
        expected_exit = exception_type == "AgentTimeoutError" or (
            run.get("stop_reason") and exception_type in {"NonZeroAgentExitCodeError", "UnknownApiError"}
        )
        if provider_failure or (exception and not expected_exit) or reward not in (0, 1) or not run["usage"]["calls"]:
            run.update(status="error", error=provider_failure or exception_type or
                       ("未完成真实模型调用" if not run["usage"]["calls"] else "缺少独立验收结果"))
        else:
            if exception_type:
                run["agent_exit"] = exception_type
            run.update(status="passed" if reward == 1 else "failed", reward=reward)
