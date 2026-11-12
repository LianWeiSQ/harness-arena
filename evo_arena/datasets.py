"""Versioned Harbor task sources. No synthetic tasks in the active catalog."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from .store import digest, now

TB21 = {
    "id": "terminal-bench-2.1",
    "name": "Terminal-Bench 2.1",
    "package": "terminal-bench/terminal-bench-2-1",
    "repository": "https://github.com/harbor-framework/terminal-bench-2-1.git",
    "revision": "12b7565679ce5006230b820666353673a133db93",
    "url": "https://hub.harborframework.com/datasets/terminal-bench/terminal-bench-2-1/latest",
    "kind": "official", "expected_tasks": 89,
}


def harbor_task(path):
    try:
        from harbor.models.task.task import Task
    except ImportError:
        raise ValueError("请安装 Harbor：uv sync --frozen --extra harbor") from None
    try:
        return Task(path)
    except Exception as exc:
        raise ValueError(f"无效的 Harbor 任务：{path.name} ({type(exc).__name__})") from None


def tree_digest(path):
    """Hash bytes and relative names; reject links and special files before copy."""
    hashes, total = {}, 0
    for item in sorted(path.rglob("*")):
        if ".git" in item.relative_to(path).parts:
            continue
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            raise ValueError("任务目录不能包含符号链接或特殊文件")
        if item.is_file():
            total += item.stat().st_size
            if total > 1_000_000_000 or len(hashes) >= 20000:
                raise ValueError("任务目录超过 1 GB 或 20000 个文件")
            with item.open("rb") as stream:
                hashes[item.relative_to(path).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest(hashes)


def describe_task(path):
    task = harbor_task(path)
    config = task.config.model_dump(mode="json")
    metadata, environment = config.get("metadata") or {}, config.get("environment") or {}
    return {
        "id": path.name, "name": task.name,
        "description": (config.get("task") or {}).get("description") or task.instruction[:240],
        "category": metadata.get("category", "custom"),
        "difficulty": metadata.get("difficulty", "—"),
        "seconds": (config.get("agent") or {}).get("timeout_sec", 900),
        "cpus": environment.get("cpus"), "memory_mb": environment.get("memory_mb"),
        "gpus": environment.get("gpus", 0), "image": environment.get("docker_image"),
        "sha256": tree_digest(path),
    }


class DatasetCatalog:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "datasets"
        self.root.mkdir(exist_ok=True)

    def list(self):
        rows = self.store.list("benchmark_dataset")
        if not any(row["id"] == TB21["id"] for row in rows):
            rows.insert(0, {**TB21, "status": "not_installed", "tasks": []})
        return sorted(rows, key=lambda row: row["kind"] != "official")

    def get(self, ident):
        row = self.store.get("benchmark_dataset", ident)
        if not row:
            raise ValueError("请先接入评测集")
        return row

    def sync_terminal(self):
        checkout = self.root / ("tb21-" + TB21["revision"][:12])
        if not checkout.exists():
            temporary = Path(tempfile.mkdtemp(prefix="download-", dir=self.root))
            try:
                env = dict(os.environ)
                # Git does not read macOS system proxy settings on its own.
                for scheme, proxy in urllib.request.getproxies().items():
                    if scheme in {"http", "https"}:
                        env.setdefault(scheme + "_proxy", proxy)
                for args in (["init", "--quiet", str(temporary)],
                             ["-C", str(temporary), "fetch", "--depth", "1", TB21["repository"], TB21["revision"]],
                             ["-C", str(temporary), "checkout", "--quiet", "--detach", "FETCH_HEAD"]):
                    subprocess.run(["git", *args], check=True, capture_output=True, timeout=300, env=env)
                temporary.rename(checkout)
            except (OSError, subprocess.SubprocessError):
                shutil.rmtree(temporary, ignore_errors=True)
                raise ValueError("官方任务下载失败，请检查 GitHub 连接后重试") from None
        revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain"], text=True)
        if revision != TB21["revision"] or dirty:
            raise ValueError("官方任务缓存被修改，请恢复固定来源后重试")
        tasks_root = checkout / "tasks"
        tasks = [describe_task(path.parent) for path in sorted(tasks_root.glob("*/task.toml"))]
        if len(tasks) != TB21["expected_tasks"]:
            raise ValueError("官方任务数量与固定版本不符")
        data = {**TB21, "path": str(tasks_root), "tasks": tasks, "status": "ready",
                "sha256": digest(tasks), "installed": now()}
        old = self.store.get("benchmark_dataset", data["id"])
        if old:
            if old["sha256"] != data["sha256"]:
                raise ValueError("同一官方版本的内容发生变化")
            return old
        return self.store.create("benchmark_dataset", data["id"], data)

    def import_local(self, payload):
        if set(payload) != {"name", "path"} or not all(isinstance(v, str) and v.strip() for v in payload.values()):
            raise ValueError("请填写名称和本机 Harbor 任务目录")
        if len(payload["name"]) > 100:
            raise ValueError("名称最多 100 个字符")
        source = Path(payload["path"]).expanduser().resolve()
        if not source.is_dir() or source == Path("/"):
            raise ValueError("任务目录不存在")
        paths = [source] if (source / "task.toml").is_file() else [p.parent for p in sorted(source.glob("*/task.toml"))]
        if not paths or len(paths) > 500:
            raise ValueError("目录应包含 1–500 个完整 Harbor 任务")
        tasks = [describe_task(path) for path in paths]
        fingerprint = digest(tasks)
        ident = "custom-" + fingerprint[:16]
        old = self.store.get("benchmark_dataset", ident)
        if old:
            return old
        target = self.root / ident
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        try:
            for path, task in zip(paths, tasks):
                shutil.copytree(path, target / path.name, ignore=shutil.ignore_patterns(".git"))
                if tree_digest(target / path.name) != task["sha256"]:
                    raise ValueError("导入时源文件变化，请重试")
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return self.store.create("benchmark_dataset", ident, {
            "id": ident, "name": payload["name"].strip(), "kind": "custom", "status": "ready",
            "path": str(target), "tasks": tasks, "sha256": fingerprint, "installed": now(),
        })

    def task_path(self, dataset, task_id):
        row = next((t for t in dataset["tasks"] if t["id"] == task_id), None)
        if not row:
            raise ValueError("任务不属于所选评测集")
        path = Path(dataset["path"]) / task_id
        if path.resolve().parent != Path(dataset["path"]).resolve() or tree_digest(path) != row["sha256"]:
            raise ValueError("任务文件与接入时的快照不符，请重新导入")
        return path
