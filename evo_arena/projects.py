"""Explicit project identity, immutable starting points and derived workspace views."""
from __future__ import annotations

import uuid

from .store import canonical, digest, now
from .taskpacks import current_pack
from .versions import bounded_text


def create_project(store, registry, payload, key):
    if not isinstance(payload, dict) or set(payload) - {"name", "goal", "baseline", "target", "feedback_mode"}:
        raise ValueError("项目配置格式错误")
    bounded_text(key, "请求幂等键", 128, 1)
    for field, maximum in (("name", 80), ("goal", 1200), ("baseline", 100), ("target", 200)):
        bounded_text(payload.get(field), field, maximum, 1)
    mode = payload.get("feedback_mode", "development")
    if not isinstance(mode, str) or mode not in {"development", "none"}:
        raise ValueError("反馈方式无效")
    request = "project-" + digest(key)
    with store.lock, store.db:
        old = store.get("evo_project_request", request)
        if old:
            if old["payload_sha256"] != digest(payload):
                raise ValueError("同一请求不能更改项目")
            return store.get("evo_project", old["project"])
        baseline = store.get("evo_version", payload["baseline"])
        if not baseline or payload["target"] not in baseline["manifest"]["files"]:
            raise ValueError("请选择已有基线及其中的一个文件")
        registry.settings_for(baseline)
        pack = current_pack()
        project = {"id": "project-" + uuid.uuid4().hex[:16], "name": payload["name"],
                   "goal": payload["goal"], "target": payload["target"], "feedback_mode": mode,
                   "initial_version": baseline["id"], "current_version": baseline["id"],
                   "runner": baseline["manifest"]["runner"], "model": baseline["manifest"]["model"]["model"],
                   "task_pack": pack["id"], "revision": 0, "created": now()}
        for kind, ident, value in (("evo_project", project["id"], project),
                                   ("evo_project_request", request, {"project": project["id"], "payload_sha256": digest(payload)})):
            store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", (kind, ident, canonical(value), now()))
        if not store.get("evo_task_pack", pack["id"]):
            store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_task_pack", pack["id"], canonical(pack), now()))
    return project


def project_view(store, project):
    """Legacy implicit projects remain readable; no synthetic experiments or votes."""
    from .delivery import rollback_targets
    experiments = [e for e in store.list("evo_experiment") if e.get("project") == project["id"]]
    ids = {e["id"] for e in experiments}
    versions = {project["initial_version"], project["current_version"]}
    for e in experiments:
        versions.update(v for v in (e["baseline"], e.get("candidate")) if v)
    budgets = [b for ident in ids if (b := store.get("evo_budget", ident))]
    costs = {k: sum(b.get(k, 0) for b in budgets) for k in ("calls", "tokens", "unknown_usage_calls", "pending")}
    pack = project.get("task_pack") or (experiments[-1]["task_pack"] if experiments else None)
    initial = store.get("evo_version", project["initial_version"])
    return {**project, "goal": project.get("goal", "历史项目 · 目标未单独登记"),
            "runner": project.get("runner") or initial["manifest"]["runner"],
            "model": project.get("model") or initial["manifest"]["model"]["model"],
            "target": project.get("target") or (experiments[0].get("target") if experiments else None),
            "explicit_workspace": bool(project.get("task_pack") and project.get("target")),
            "task_pack": pack, "task_pack_current": pack == current_pack()["id"],
            "experiment_ids": [e["id"] for e in experiments], "version_ids": sorted(versions),
            "cost": costs, "rollback_targets": rollback_targets(store, project),
            "imported_from": next((r for r in store.list("evo_project_import") if r["project"] == project["id"]), None)}
