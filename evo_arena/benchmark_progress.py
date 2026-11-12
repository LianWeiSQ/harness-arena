"""Bounded, read-only live views of Harbor jobs, including their preparation phase."""
from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path

LABELS = {"environment": "准备任务环境", "dependencies": "安装系统依赖", "node": "下载 Node.js",
          "harness": "安装运行器", "ready": "运行器已就绪", "agent": "Agent 执行",
          "model": "等待模型响应", "verifier": "运行验收器", "queued": "等待前一组合完成",
          "finished": "执行结束"}
ACTIVE = {"running", "cancelling", "preparing"}


def tail(path, limit=12000):
    """Do not follow sandbox-created links or block on FIFOs; return the newest output."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return ""
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - limit))
            text = stream.read(limit).decode(errors="replace")
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).replace("\r", "\n")
        return re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[redacted]", text)
    except OSError:
        return ""


def epoch(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def arch(value):
    if not isinstance(value, str):
        return None
    return {"aarch64": "arm64", "x86_64": "amd64"}.get(value, value)


def pi_output(path):
    """Pi emits cumulative partial tool output, including long-running shell commands."""
    for line in reversed(tail(path, 512000).splitlines()):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") not in {"tool_execution_update", "tool_execution_end", "tool_execution_start"}:
            continue
        result = event.get("partialResult") or event.get("result") or {}
        content = result.get("content", []) if isinstance(result, dict) else []
        output = "\n".join(str(item.get("text", "")) for item in content
                           if isinstance(item, dict) and item.get("type") == "text") if isinstance(content, list) else ""
        if not output and isinstance(event.get("args"), dict):
            output = str(event["args"].get("command", ""))
        label = "工具完成" if event["type"] == "tool_execution_end" else "工具执行中"
        return label + " · " + str(event.get("toolName", "")) + "\n" + output[-16000:]
    return ""


def snapshot(data_root, run, trial, include_logs=False):
    result = dict(trial)
    if not re.fullmatch(r"bench-[a-f0-9]{12}", run["id"]) or not re.fullmatch(r"trial-\d{4,}", trial["id"]):
        return result
    root = data_root / "benchmarks" / run["id"]
    job = root / "jobs" / trial["id"]
    work = root / trial["id"]
    # Only the host-owned direct job children may contribute live files.
    trials = [p for p in job.glob("*") if p.is_dir() and not p.is_symlink()]
    state, live_files = {}, [work / "harbor.log", job / "job.log"]
    install_files, verifier_files, agent_files = [], [], []
    for folder in trials:
        agent = folder / "agent"
        if agent.is_symlink() or folder.joinpath("verifier").is_symlink():
            continue
        if not (agent / "setup").is_symlink():
            install_files.append(agent / "setup" / "install.log")
        live_files.append(folder / "trial.log")
        live_files.append(agent / "pi.jsonl")
        live_files.append(agent / "dsh-output.log")
        agent_files.append(agent)
        verifier_files.append(folder / "verifier" / "test-stdout.txt")
        try:
            candidate = json.loads(tail(agent / "setup-progress.json", 4096))
            if (isinstance(candidate, dict) and isinstance(candidate.get("stage"), str)
                    and candidate["stage"] in LABELS):
                state = candidate
        except ValueError:
            pass
    times = []
    for path in [*live_files, *install_files, *verifier_files]:
        try:
            if not path.is_symlink() and path.is_file():
                times.append(path.stat().st_mtime)
        except OSError:
            pass
    current = time.time()
    start, end = epoch(trial.get("started")), epoch(trial.get("finished"))
    controller = trial.get("stage")
    stage = controller if controller in {"agent", "model", "verifier"} else state.get("stage", controller or "environment")
    stage_at = (trial.get("stage_started") if stage == controller else state.get("started_at")) or trial.get("started")
    if trial["status"] == "queued":
        stage, stage_at = "queued", None
    elif trial["status"] not in ACTIVE:
        stage = "finished"
    elif (work / "agent-ended").is_file():
        stage = "verifier"
    elif (trial.get("usage") or {}).get("calls", 0) and stage not in {"agent", "model"}:
        stage = "agent"
    heartbeat = epoch(trial.get("heartbeat_at"))
    activity = max(times) if times else start
    result["progress"] = {"stage": stage, "label": LABELS.get(stage, stage),
        "started_at": trial.get("started"), "stage_started_at": stage_at,
        "elapsed_seconds": max(0, round((end or current) - start)) if start else trial.get("seconds"),
        "stage_elapsed_seconds": max(0, round((end or current) - epoch(stage_at))) if epoch(stage_at) else None,
        "heartbeat_age_seconds": max(0, round(current - heartbeat)) if heartbeat else None,
        "log_age_seconds": max(0, round(current - activity)) if activity else None,
        "guest_arch": arch(state.get("guest_arch")),
        "host_arch": arch((run.get("platform") or {}).get("engine_arch") or (run.get("platform") or {}).get("host_arch")),
        "checked_at": datetime.fromtimestamp(current).astimezone().isoformat()}
    if include_logs:
        sections = []
        # The job log includes Harbor's preparation events; stdout can be empty until exit.
        for label, path in [("Harbor", job / "job.log"), ("进程输出", work / "harbor.log")]:
            content = tail(path)
            if content:
                sections.append(label + "\n" + content)
        if sections:
            result["harbor_log"] = "\n\n".join(sections)
        result["installation_log"] = "\n".join(filter(None, (tail(p) for p in install_files)))
        result["agent_output"] = "\n".join(filter(None, (
            pi_output(p / "pi.jsonl") if trial.get("runner") == "pi" else tail(p / "dsh-output.log", 16000)
            for p in agent_files)))
        verifier = "\n".join(filter(None, (tail(p, 24000) for p in verifier_files)))
        if verifier:
            result["verifier_output"] = verifier
    return result
