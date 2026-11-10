"""Host-owned Trial workspaces and narrowly attributed process/container cleanup."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time

from .store import now


def shell(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=15).stdout.strip()


def owned_processes(resource):
    result = []
    # No command strings are logged or returned to the browser: they can contain
    # the per-Trial verifier token. Identity is a unique, persisted job intent.
    for line in shell("ps", "-axo", "pid=,lstart=,command=").splitlines():
        parts = line.split(None, 6)
        if len(parts) != 7:
            continue
        pid, started, command = int(parts[0]), " ".join(parts[1:6]), parts[6]
        try:
            words = shlex.split(command)
        except ValueError:
            continue
        def option(name):
            return words[words.index(name) + 1] if name in words and words.index(name) + 1 < len(words) else None
        if (resource["harbor"] in words[:3] and option("--job-name") == resource["job"]
                and option("--jobs-dir") == resource["jobs_dir"]):
            result.append((pid, started))
    return result


def owns_container(resource, value):
    paths = {str(Path(resource["workspace"]) / name) for name in ("spool", "ledger", "version")}
    return any(m.get("Type") == "bind" and m.get("Source") in paths for m in value.get("Mounts", []))


def owned_containers(resource):
    ids = shell("docker", "ps", "-aq").split()
    if not ids:
        return []
    values = json.loads(shell("docker", "inspect", *ids))
    return [v["Id"] for v in values if owns_container(resource, v)]


def reconcile(store, resource):
    """Stop only the unique job process group and exact host-mount owners."""
    resource = dict(resource)
    try:
        processes = owned_processes(resource)
        for pid, started in processes:
            # A reused PID must not authorize a signal, even within this scan.
            if (pid, started) in owned_processes(resource) and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGINT)
        until = time.monotonic() + 3
        while processes and time.monotonic() < until:
            processes = owned_processes(resource)
            if processes:
                time.sleep(.1)
        for pid, started in processes:
            if (pid, started) in owned_processes(resource) and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
        removed = []
        for ident in owned_containers(resource):
            # Re-inspect exact immutable ID immediately before removing it.
            if owns_container(resource, json.loads(shell("docker", "inspect", ident))[0]):
                shell("docker", "rm", "-f", ident)
                removed.append(ident)
        resource.update(state="unresolved" if owned_processes(resource) or owned_containers(resource) else "cleaned",
                        reconciled=now(), removed_containers=removed)
        resource.pop("error", None)
    except (OSError, ValueError, subprocess.SubprocessError):
        resource.update(state="unresolved", error="资源对账未完成；不能继续实验", reconciled=now())
    store.update("evo_resource", resource["id"], resource)
    return resource


def reconcile_experiment(store, ident):
    return [reconcile(store, r) for r in store.list("evo_resource")
            if r["experiment"] == ident and r["state"] != "cleaned"]


@contextmanager
def workspace(budget, run, root, job, harbor):
    if not budget or budget.experiment is None:
        with tempfile.TemporaryDirectory(prefix="evoarena-relay-") as temporary:
            yield Path(temporary)
        return
    budget.check_control()
    folder = root / "trials" / run["id"]
    folder.mkdir(parents=True, mode=0o700, exist_ok=False)
    resource = {"id": run["id"], "experiment": budget.ident, "epoch": budget.experiment["epoch"],
                "workspace": str(folder.resolve()), "job": job, "jobs_dir": str((root / "jobs").resolve()),
                "harbor": str(harbor.resolve()), "state": "declared", "created": now()}
    budget.store.create("evo_resource", run["id"], resource)
    try:
        yield folder
    finally:
        # Subprocess exit/reaping is inside this context. Restart uses the same
        # persisted ownership intent even if it died before saving a child PID.
        reconcile(budget.store, resource)
