"""Automatic host checks and a pinned Harbor install inside the current virtualenv."""
from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HARBOR_VERSION = "0.22.0"


def harbor_path(configured=None):
    candidates = [configured, Path(sys.executable).parent / "harbor", shutil.which("harbor")]
    return next((Path(p) for p in candidates if p and Path(p).is_file()), None)


def install_command():
    # Never pip-install into the user's system Python or choose an arbitrary latest version.
    if sys.prefix == sys.base_prefix:
        return None
    if uv := shutil.which("uv"):
        return [uv, "pip", "install", "--python", sys.executable, f"harbor=={HARBOR_VERSION}"]
    if importlib.util.find_spec("pip"):
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", f"harbor=={HARBOR_VERSION}"]
    return None


def install_harbor(root, cancel, on_progress=None):
    command = install_command()
    if not command:
        return "请使用项目虚拟环境，并安装 uv 或 pip；可运行 uv sync --frozen --extra harbor"
    log_path = root / "harbor-install.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    env = {k: v for k, v in os.environ.items() if not k.endswith(("API_KEY", "TOKEN"))}
    with os.fdopen(fd, "w") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        started = last_update = time.monotonic()
        deadline = started + 600
        try:
            while process.poll() is None:
                if cancel.wait(.2) or time.monotonic() > deadline:
                    return "已停止安装" if cancel.is_set() else "Harbor 安装超时，请检查软件源连接"
                if on_progress and time.monotonic() - last_update >= 2:
                    last_update = time.monotonic()
                    on_progress(round(last_update - started))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    return None if process.returncode == 0 else "Harbor 自动安装失败，请检查软件源连接或运行 uv sync --frozen --extra harbor"


def check_environment(root, harbor, cancel, models_ready, *, repair=False, on_update=None):
    checks = []

    def publish(phase):
        result = {"checks": list(checks), "ready": bool(checks) and all(c["ok"] for c in checks),
                  "model_calls": 0, "phase": phase}
        if on_update:
            on_update(result)
        return result

    for name, command, hint in (
        ("Docker", ["docker", "info", "--format", "{{.ServerVersion}}"], "请安装并启动 Docker，确认 docker info 可用"),
        ("Git", ["git", "--version"], "请安装 Git，并确保 git 命令可用"),
    ):
        if cancel.is_set():
            return harbor, publish("已停止检查")
        publish("检查 " + name)
        try:
            detail = subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL, timeout=12).strip()
            checks.append({"name": name, "ok": bool(detail), "detail": detail[:160] or hint})
        except (OSError, subprocess.SubprocessError):
            checks.append({"name": name, "ok": False, "detail": hint})
    harbor = harbor_path(harbor)
    install_error = None
    if not harbor and repair and all(c["ok"] for c in checks) and not cancel.is_set():
        publish("自动安装 Harbor " + HARBOR_VERSION)
        try:
            install_error = install_harbor(root, cancel,
                lambda seconds: publish(f"自动安装 Harbor {HARBOR_VERSION} · {seconds} 秒"))
        except (OSError, subprocess.SubprocessError):
            install_error = "Harbor 安装进程无法启动，请检查 uv/pip 与项目目录权限"
        harbor = harbor_path()
    publish("检查 Harbor")
    try:
        if not harbor:
            raise FileNotFoundError()
        value = subprocess.check_output([str(harbor), "--version"], text=True,
                                        stderr=subprocess.DEVNULL, timeout=12).strip()
        checks.append({"name": "Harbor", "ok": bool(value), "detail": value[:160]})
    except (OSError, subprocess.SubprocessError):
        checks.append({"name": "Harbor", "ok": False,
                       "detail": install_error or "Harbor 未就绪；开始评测时会尝试在项目虚拟环境安装"})
    checks.append({"name": "模型配置", "ok": models_ready, "detail": "已配置；未调用模型" if models_ready else "请先在设置中添加模型连接"})
    return harbor, publish("环境就绪" if all(c["ok"] for c in checks) else "需要处理环境")
