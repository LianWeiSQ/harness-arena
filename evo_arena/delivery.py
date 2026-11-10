"""Portable version assets, bounded report exports and audited recommendation rollback."""
from __future__ import annotations

import io
import json
import re
import stat
import zipfile
import zlib

from .store import canonical, digest, now
from .versions import CAPABILITIES, SCHEMA, bounded_text, file_hashes, sha256, validate_manifest, verify_version


BUNDLE_SCHEMA = "evo-portable-version-v1"


def version_metadata(version):
    import yaml
    metadata = {}
    for path, content in version["manifest"]["files"].items():
        if path != "policy.md":
            header = content.removeprefix("---\n").split("\n---\n", 1)[0]
            value = yaml.safe_load(header)
            if isinstance(value, dict):
                metadata[path] = {k: value.get(k, "") for k in ("name", "description")}
    return {**version, "skills_meta": metadata}


def export_bundle(version):
    manifest = verify_version(version)
    package = {"schema": BUNDLE_SCHEMA, "name": version["name"],
        "source": {"version_id": version["id"], "manifest_sha256": version["manifest_sha256"], "parent": version["parent"]},
        "runner": manifest["runner"], "runtime_version": manifest["runtime_version"],
        "model_name": manifest["model"]["model"], "files": file_hashes(manifest)}
    package["sha256"] = digest(package)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in {"package.json": canonical(package), **manifest["files"]}.items():
            info = zipfile.ZipInfo(path, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, content.encode(), compress_type=zipfile.ZIP_DEFLATED)
    return stream.getvalue()


def read_bundle(data):
    if not isinstance(data, bytes) or len(data) > 100000:
        raise ValueError("版本包不能超过 100 KB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (not 2 <= len(entries) <= 6 or len({e.filename for e in entries}) != len(entries)
                    or sum(e.file_size for e in entries) > 75000
                    or any(e.flag_bits & 1 or stat.S_ISLNK(e.external_attr >> 16)
                           or e.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} for e in entries)):
                raise ValueError()
            # Never extract an archive. Only decode bounded, explicitly named assets.
            files = {e.filename: archive.read(e).decode("utf-8") for e in entries}
            package = json.loads(files.pop("package.json"))
            if (set(package) != {"schema", "name", "source", "runner", "runtime_version", "model_name", "files", "sha256"}
                    or package["schema"] != BUNDLE_SCHEMA or package["sha256"] != digest({k: v for k, v in package.items() if k != "sha256"})
                    or package["files"] != {p: sha256(c) for p, c in files.items()}):
                raise ValueError()
            bounded_text(package["name"], "版本名称", 80, 1)
            bounded_text(package["model_name"], "模型名称", 256, 1)
            source = package["source"]
            if (not isinstance(source, dict) or set(source) != {"version_id", "manifest_sha256", "parent"}
                    or not re.fullmatch(r"av-[a-f0-9]{20}", source["version_id"])
                    or not re.fullmatch(r"[a-f0-9]{64}", source["manifest_sha256"])
                    or source["parent"] is not None and not re.fullmatch(r"av-[a-f0-9]{20}", source["parent"])):
                raise ValueError()
            validate_manifest({"schema": SCHEMA, "runner": package["runner"], "runtime_version": package["runtime_version"],
                "model": {"profile": "unbound", "model": package["model_name"], "base_url": "https://unbound.invalid"}, "files": files})
            bounded_text(files["policy.md"], "执行策略", 8000)
            import yaml
            for path, content in files.items():
                if path == "policy.md":
                    continue
                if not content.startswith("---\n"):
                    raise ValueError()
                header, body = content[4:].split("\n---\n", 1)
                try:
                    metadata = yaml.safe_load(header)
                except yaml.YAMLError:
                    raise ValueError() from None
                if not isinstance(metadata, dict) or set(metadata) != {"name", "description"} or metadata["name"] != path.split("/")[1]:
                    raise ValueError()
                bounded_text(metadata["description"], "Skill 描述", 600, 1)
                bounded_text(body.strip(), "Skill 正文", 12000, 1)
            return package, files
    except (KeyError, TypeError, ValueError, UnicodeError, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError):
        raise ValueError("版本包格式、文件路径、内容哈希或运行器版本不合法") from None


def import_bundle(registry, data, profile):
    package, files = read_bundle(data)
    if not isinstance(profile, str) or not profile:
        raise ValueError("导入时必须显式选择本地模型连接")
    settings, profile, _ = registry.models.resolve(profile)
    if settings.model != package["model_name"]:
        raise ValueError("所选连接的模型 ID 与版本包不同，请先配置匹配的模型连接")
    manifest = validate_manifest({"schema": SCHEMA, "runner": package["runner"], "runtime_version": package["runtime_version"],
        "model": {"profile": profile, "model": settings.model, "base_url": settings.base_url}, "files": files})
    # Imported assets form a new local baseline; remote provenance cannot forge
    # a local parent, recommendation or evaluation decision.
    content = {"name": package["name"], "parent": None, "manifest": manifest, "manifest_sha256": digest(manifest)}
    ident = "av-" + digest(content)[:20]
    import_id = "import-" + digest({"bundle": package["sha256"], "version": ident})
    with registry.store.lock, registry.store.db:
        version = registry.store.get("evo_version", ident)
        if not version:
            version = {"id": ident, **content, "created": now()}
            registry.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_version", ident, canonical(version), now()))
        if not registry.store.get("evo_import", import_id):
            record = {"id": import_id, "version": ident, "package_sha256": package["sha256"], "source": package["source"], "created": now()}
            registry.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_import", import_id, canonical(record), now()))
    return version


def rollback_targets(store, project):
    adopted = {project["initial_version"]} | {d["candidate"] for d in store.list("evo_decision")
        if d.get("project") == project["id"] and d["status"] == "accepted"}
    targets, seen = [], {project["current_version"]}
    current = store.get("evo_version", project["current_version"])
    while current and current.get("parent") and current["parent"] not in seen:
        ident = current["parent"]; seen.add(ident)
        if ident in adopted:
            targets.append(ident)
        if ident == project["initial_version"]:
            break
        current = store.get("evo_version", ident)
    return targets


def rollback(store, ident, payload, key):
    bounded_text(key, "回滚幂等键", 128, 1)
    if (not isinstance(payload, dict) or set(payload) != {"target", "expected_version", "expected_revision", "reason"}
            or type(payload["expected_revision"]) is not int or payload["expected_revision"] < 0
            or any(not isinstance(payload[k], str) for k in ("target", "expected_version", "reason"))):
        raise ValueError("回滚需要目标、当前版本与修订号，以及原因")
    bounded_text(payload["reason"].strip(), "回滚原因", 300, 1)
    change_id = "rollback-" + digest({"project": ident, "key": key})
    with store.lock, store.db:
        previous = store.get("evo_project_change", change_id)
        if previous:
            if previous["payload_sha256"] != digest(payload):
                raise ValueError("回滚幂等键冲突")
            return previous
        project = store.get("evo_project", ident)
        if not project:
            raise ValueError("项目不存在")
        if (project["current_version"] != payload["expected_version"] or project["revision"] != payload["expected_revision"]):
            raise ValueError("推荐版本已变化，请刷新后重试")
        if payload["target"] not in rollback_targets(store, project):
            raise ValueError("只能回滚到本项目曾采用的祖先版本")
        old = canonical(project)
        change = {"id": change_id, "project": ident, "action": "rollback", "from": project["current_version"],
                  "to": payload["target"], "revision": project["revision"] + 1, "reason": payload["reason"].strip(),
                  "created": now(), "payload_sha256": digest(payload)}
        project.update(current_version=change["to"], revision=change["revision"], updated=now())
        changed = store.db.execute("UPDATE objects SET data=? WHERE kind='evo_project' AND id=? AND data=?", (canonical(project), ident, old))
        if changed.rowcount != 1:
            raise ValueError("推荐版本已变化，请刷新后重试")
        store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_project_change", change_id, canonical(change), now()))
        store.db.execute("INSERT INTO events(scope,kind,data,created) VALUES(?,?,?,?)", (ident, "evo.rollback", canonical(change), now()))
    return change


def experiment_report(store, ident):
    with store.lock:
        return _experiment_report(store, ident)


def _experiment_report(store, ident):
    ex = store.get("evo_experiment", ident)
    if not ex:
        raise ValueError("实验不存在")
    version_ids = {ex[k] for k in ("baseline", "candidate", "initial_version") if ex.get(k)}
    versions = []
    for key in sorted(version_ids):
        v = store.get("evo_version", key)
        if v:
            m = verify_version(v)
            versions.append({"id": v["id"], "parent": v["parent"], "manifest_sha256": v["manifest_sha256"],
                "runner": m["runner"], "runtime_version": m["runtime_version"], "model": m["model"]["model"], "files": file_hashes(m)})
    calls = [c for c in store.list("evo_call") if c["experiment"] == ident]
    pack = store.get("evo_task_pack", ex.get("task_pack", ""))
    execution = store.get("evo_control", ident)
    def loading_evidence(run):
        version = store.get("evo_version", run.get("version_id", ""))
        if not version:
            return {"verified": False}
        manifest = verify_version(version)
        expected = {"version_id": version["id"], "manifest_sha256": version["manifest_sha256"],
                    "runner": manifest["runner"], "runtime_version": manifest["runtime_version"], "file_hashes": file_hashes(manifest)}
        return {"verified": True, **expected} if run.get("version_load") == expected else {"verified": False}
    cost = {}
    for call in calls:
        phase = call["phase"]
        item = cost.setdefault(phase, {"calls": 0, "known_tokens": 0, "unknown_calls": 0, "pending_calls": 0})
        item["calls"] += 1
        item["known_tokens"] += call.get("tokens") or 0
        item["unknown_calls"] += call["status"] == "unknown"
        item["pending_calls"] += call["status"] == "pending"
    # Deliberate allowlist: no free-form traces, model outputs, connection URL,
    # credential fields, arbitrary exception strings or local paths in a report.
    report = {"schema": "evo-experiment-report-v1", "exported": now(), "experiment": {k: ex.get(k) for k in
        ("id", "mode", "feedback_mode", "status", "phase", "created", "finished", "baseline", "candidate", "initial_version", "task_pack", "target", "epoch")},
        "optimization": {"feedback_mode": ex.get("feedback_mode", "development"),
            "input_schema": ex.get("evidence_bundle", {}).get("schema"),
            "input_sha256": digest(ex["evidence_bundle"]) if ex.get("evidence_bundle") else None,
            "policy": ex.get("optimizer_policy"), "model": ex.get("optimizer_model", {}).get("model")}
            if ex.get("automatic_evolution") else None,
        "versions": versions, "plan": ex.get("plan"), "evaluation": ex.get("evaluation"),
        "runtime": {"harbor_version": ex.get("harbor_version"), "image_id": ex.get("image_id"),
                    "protocol_sha256": execution.get("protocol_sha256") if execution else None},
        "task_pack": {k: pack.get(k) for k in ("id", "sha256", "source_hashes")} if pack else None,
        "budget": store.get("evo_budget", ident), "cost_by_phase": cost,
        "decision": {k: ex["decision"].get(k) for k in ("id", "status", "reason", "created", "project", "plan_sha256")} if ex.get("decision") else None,
        "runs": [{**{k: r.get(k) for k in ("id", "key", "phase", "task", "role", "repeat", "version_id", "status", "seconds", "usage", "verifier_attested", "execution_frozen", "task_checksum")},
                  "checks": [{"key": c.get("id", c.get("name")), "passed": c.get("passed"), "critical": c.get("critical", False)} for c in r.get("checks", [])],
                  "version_load": loading_evidence(r), "input_registration_verified": r.get("input_evidence", {}).get("registration_verified", False)} for r in ex["runs"]],
        "scope": "Model-reported known usage only; unknown and pending calls are not zero cost. Raw traces, generated text, file contents and connection configuration are excluded. Exporting a running experiment is an interim snapshot."}
    report["sha256"] = digest(report)
    return report
