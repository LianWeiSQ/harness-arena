"""Bounded workspace snapshots; imported assets never import evaluation authority."""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
import zlib

from .delivery import export_bundle, import_bundle, read_bundle, experiment_report
from .projects import create_project, project_view
from .store import canonical, digest, now
from .taskpacks import current_pack
from .versions import bounded_text

SCHEMA = "evo-project-bundle-v1"
MAX_BYTES = 8_000_000
PROJECT_FIELDS = ("id", "name", "goal", "target", "feedback_mode", "initial_version", "current_version",
                  "runner", "model", "task_pack", "revision", "created", "cost", "version_ids", "experiment_ids")


def export_project(store, ident):
    with store.lock:
        project = store.get("evo_project", ident)
        if not project: raise ValueError("项目不存在")
        view = project_view(store, project)
        files = {"project.json": canonical({k: view.get(k) for k in PROJECT_FIELDS}).encode()}
        for v in view["version_ids"]:
            files[f"assets/{v}.zip"] = export_bundle(store.get("evo_version", v))
        for e in view["experiment_ids"]:
            files[f"reports/{e}.json"] = canonical(experiment_report(store, e)).encode()
        plans = [{k: c.get(k) for k in ("id", "project", "starting_version", "target", "task_pack", "feedback_mode", "optimizer_model",
                    "max_rounds", "no_gain_limit", "limits", "round_limits", "protocol_sha256", "status", "reason", "experiments", "created", "finished")}
                 for c in store.list("evo_campaign") if c["project"] == ident]
        files["campaigns.json"] = canonical(plans).encode()
        pack = store.get("evo_task_pack", view["task_pack"])
        files["task-pack.json"] = canonical({k: pack.get(k) for k in ("id", "sha256", "source_hashes")} if pack else {}).encode()
        files["README.txt"] = ("EvoArena project snapshot\n"
            "Inspect: evo-arena package inspect project.zip\n"
            "Import: evo-arena package import project.zip --model-profile YOUR_LOCAL_PROFILE\n"
            "Run a separate server with --data-dir to import into a clean workspace.\n"
            "Imports the exported recommendation as a NEW untested local baseline.\n"
            "Historical reports remain in this archive; they do not authorize local adoption.\n"
            "Requires matching model ID, runner version and current task-pack fingerprint.\n"
            "No credentials, endpoints, raw model responses or execution traces are included.\n"
            "Policy/Skill file text and user-authored project name/goal ARE included.\n"
            "Hashes detect corruption, not a trusted publisher's signature.\n").encode()
        manifest = {"schema": SCHEMA, "exported": now(), "project": ident,
                    "files": {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}}
        manifest["sha256"] = digest(manifest)
        files["manifest.json"] = canonical(manifest).encode()
    if len(files) > 250 or sum(map(len, files.values())) > MAX_BYTES:
        raise ValueError("项目超过交付包上限，请分别导出版本与报告")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items(): archive.writestr(path, data)
    data = out.getvalue()
    if len(data) > MAX_BYTES: raise ValueError("项目包超过 8 MB 上限")
    return data


def read_project(data):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES: raise ValueError("项目包不能超过 8 MB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (not 5 <= len(entries) <= 250 or len({e.filename for e in entries}) != len(entries)
                    or sum(e.file_size for e in entries) > MAX_BYTES
                    or any(e.flag_bits & 1 or stat.S_ISLNK(e.external_attr >> 16)
                           or e.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} for e in entries)):
                raise ValueError()
            files = {e.filename: archive.read(e) for e in entries}
        manifest = json.loads(files.pop("manifest.json"))
        if (set(manifest) != {"schema", "exported", "project", "files", "sha256"} or manifest["schema"] != SCHEMA
                or manifest["sha256"] != digest({k: v for k, v in manifest.items() if k != "sha256"})
                or manifest["files"] != {p: hashlib.sha256(b).hexdigest() for p, b in files.items()}): raise ValueError()
        project = json.loads(files["project.json"])
        if set(project) != set(PROJECT_FIELDS) or project["id"] != manifest["project"]: raise ValueError()
        for k, size in (("name", 80), ("goal", 1200), ("target", 200), ("model", 256)):
            bounded_text(project[k], k, size, 1)
        for field, pattern in (("version_ids", r"av-[a-f0-9]{20}"), ("experiment_ids", r"ex-[a-f0-9]{12}")):
            values = project[field]
            if (not isinstance(values, list) or len(set(values)) != len(values)
                    or any(not isinstance(v, str) or not re.fullmatch(pattern, v) for v in values)): raise ValueError()
        expected = {"project.json", "task-pack.json", "README.txt"} | {f"assets/{v}.zip" for v in project["version_ids"]} | {f"reports/{e}.json" for e in project["experiment_ids"]}
        if "campaigns.json" in files:
            expected.add("campaigns.json")
            plans = json.loads(files["campaigns.json"])
            if not isinstance(plans, list) or any(c["project"] != project["id"] for c in plans): raise ValueError()
        if set(files) != expected or any(project[k] not in project["version_ids"] for k in ("initial_version", "current_version")): raise ValueError()
        for v in project["version_ids"]:
            package, _ = read_bundle(files[f"assets/{v}.zip"])
            if package["source"]["version_id"] != v: raise ValueError()
        for e in project["experiment_ids"]:
            report = json.loads(files[f"reports/{e}.json"])
            if (report["schema"] != "evo-experiment-report-v1" or report["experiment"]["id"] != e
                    or report["experiment"]["project"] != project["id"]
                    or report["sha256"] != digest({k: v for k, v in report.items() if k != "sha256"})): raise ValueError()
        pack = json.loads(files["task-pack.json"])
        if pack and pack["id"] != project["task_pack"]: raise ValueError()
        return manifest, project, files
    except (KeyError, TypeError, ValueError, UnicodeError, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError):
        raise ValueError("项目包格式、路径、报告或内容校验失败") from None


def import_project(store, registry, data, profile, key):
    bounded_text(key, "导入幂等键", 128, 1)
    manifest, source, files = read_project(data)
    if not isinstance(profile, str) or not profile: raise ValueError("请选择本地模型连接")
    settings, _, _ = registry.models.resolve(profile)
    if source["task_pack"] != current_pack()["id"]: raise ValueError("任务协议不同；仍可查看报告或单独导入版本资产")
    for v in source["version_ids"]:
        package, _ = read_bundle(files[f"assets/{v}.zip"])
        if package["model_name"] != settings.model: raise ValueError("本地模型 ID 必须与交付包一致")
    with store.lock:
        request_id = "project-import-" + digest(key)
        identity = digest({"package": manifest["sha256"], "profile": profile})
        old = store.get("evo_project_import", request_id)
        if old:
            if old["request_sha256"] != identity: raise ValueError("导入幂等键冲突")
            return project_view(store, store.get("evo_project", old["project"]))
        # Each asset is independently idempotent. A crash may leave valid assets;
        # retry fills the remainder. No historical experiments/decisions are replayed.
        mapping = {v: import_bundle(registry, files[f"assets/{v}.zip"], profile)["id"] for v in source["version_ids"]}
        project = create_project(store, registry, {"name": source["name"], "goal": source["goal"],
            "baseline": mapping[source["current_version"]], "target": source["target"],
            "feedback_mode": source.get("feedback_mode") or "development"}, request_id + identity[:16])
        store.create("evo_project_import", request_id, {"project": project["id"], "request_sha256": identity,
            "package_sha256": manifest["sha256"], "source_project": source["id"],
            "source_initial_version": source["initial_version"], "source_current_version": source["current_version"],
            "versions": mapping, "created": now()})
        return project_view(store, project)
