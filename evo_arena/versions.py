"""Immutable, executable Agent versions. Credentials never belong to a manifest."""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

from .store import digest, now


CAPABILITIES = {
    "pi": {"name": "Pi", "runtime_version": "0.82.1",
           "policy": "append-system-prompt", "skills": "explicit-skill-paths",
           "unsupported": ["extensions", "memory", "model-routing", "runtime-code"]},
    "dsh": {"name": "DeepSeek Harness", "runtime_version": "0.1.2-rc.1",
            "policy": "task-instruction-prefix", "skills": None,
            "unsupported": ["native-skills", "extensions", "memory", "model-routing", "runtime-code"]},
}
SCHEMA = "evo-agent-v1"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def bounded_text(value, label, maximum, minimum=0):
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or "\0" in value:
        raise ValueError(f"{label} 长度应为 {minimum}—{maximum} 字符，且不能包含空字符")
    return value


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "runner", "runtime_version", "model", "files"}:
        raise ValueError("版本 manifest 格式错误")
    runner = manifest["runner"]
    if not isinstance(runner, str) or runner not in CAPABILITIES or manifest["schema"] != SCHEMA:
        raise ValueError("未知运行器或版本协议")
    if manifest["runtime_version"] != CAPABILITIES[runner]["runtime_version"]:
        raise ValueError("运行器版本未经验证")
    model = manifest["model"]
    if (not isinstance(model, dict) or set(model) != {"profile", "model", "base_url"}
            or any(not isinstance(v, str) or not v for v in model.values())):
        raise ValueError("模型快照格式错误")
    files = manifest["files"]
    if not isinstance(files, dict) or "policy.md" not in files or len(files) > 5:
        raise ValueError("版本应包含策略文件和至多四个 Skill")
    total = 0
    for path, content in files.items():
        if path != "policy.md":
            if not re.fullmatch(r"skills/[a-z][a-z0-9-]{0,47}/SKILL\.md", path):
                raise ValueError("只允许 policy.md 和 skills/<name>/SKILL.md")
            if not CAPABILITIES[runner]["skills"]:
                raise ValueError("该运行器尚未验证原生 Skill 加载，请使用策略配置")
        bounded_text(content, "版本文件", 16000)
        total += len(content.encode("utf-8"))
    if total > 64000:
        raise ValueError("版本内容超过 64 KB")
    return manifest


def verify_version(version):
    manifest = validate_manifest(version["manifest"])
    if digest(manifest) != version["manifest_sha256"]:
        raise ValueError("版本内容与哈希不一致")
    return manifest


def file_hashes(manifest):
    return {path: sha256(content) for path, content in manifest["files"].items()}


def materialize_version(version, root: Path):
    """Write only into a newly created trial-owned directory, then mount read-only."""
    manifest = verify_version(version)
    root.mkdir()  # Refuse an existing directory, including a symlink.
    for name, content in manifest["files"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o444)
    metadata = root / "version.json"
    metadata.write_text(json.dumps(version, ensure_ascii=False), encoding="utf-8")
    metadata.chmod(0o444)
    return metadata


class VersionRegistry:
    def __init__(self, store, models):
        self.store, self.models = store, models

    def register(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"name", "runner", "model_profile", "parent", "policy", "skills"}:
            raise ValueError("版本包含不支持的字段")
        name = bounded_text(payload.get("name"), "名称", 80, 1).strip()
        runner = payload.get("runner")
        if not name or not isinstance(runner, str) or runner not in CAPABILITIES:
            raise ValueError("请选择已支持的运行器，并填写名称")
        profile = payload.get("model_profile")
        if profile is not None and not isinstance(profile, str):
            raise ValueError("模型连接格式错误")
        settings, profile, _ = self.models.resolve(profile)
        files = {"policy.md": bounded_text(payload.get("policy", ""), "策略", 8000)}
        skills = payload.get("skills", [])
        if not isinstance(skills, list) or len(skills) > 4:
            raise ValueError("每个版本至多四个 Skill")
        for skill in skills:
            if not isinstance(skill, dict) or set(skill) != {"name", "description", "body"}:
                raise ValueError("Skill 需要 name、description、body")
            slug = skill["name"]
            if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", slug):
                raise ValueError("Skill 名称只接受小写字母、数字和连字符，且以字母开头")
            description = bounded_text(skill["description"], "Skill 描述", 600, 1)
            body = bounded_text(skill["body"], "Skill 正文", 12000, 1)
            path = f"skills/{slug}/SKILL.md"
            if path in files:
                raise ValueError("Skill 名称不能重复")
            files[path] = f"---\nname: {json.dumps(slug)}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n{body}\n"
        manifest = validate_manifest({"schema": SCHEMA, "runner": runner,
            "runtime_version": CAPABILITIES[runner]["runtime_version"],
            "model": {"profile": profile, "model": settings.model, "base_url": settings.base_url}, "files": files})
        parent_id = payload.get("parent") or None
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError("父版本格式错误")
        parent = self.store.get("evo_version", parent_id) if parent_id else None
        if parent_id and not parent:
            raise ValueError("父版本不存在")
        if parent and any(parent["manifest"][k] != manifest[k] for k in ("runner", "runtime_version", "model")):
            raise ValueError("本阶段候选必须沿用父版本的运行器和模型")
        if parent and parent["manifest_sha256"] == digest(manifest):
            raise ValueError("候选没有实际变更")
        content = {"name": name, "parent": parent_id, "manifest": manifest, "manifest_sha256": digest(manifest)}
        ident = "av-" + digest(content)[:20]
        with self.store.lock:
            existing = self.store.get("evo_version", ident)
            if existing:
                return existing
            return self.store.create("evo_version", ident, {"id": ident, **content, "created": now()})

    def settings_for(self, version):
        manifest = verify_version(version)
        model = manifest["model"]
        settings, _, _ = self.models.resolve(model["profile"])
        if settings.model != model["model"] or settings.base_url != model["base_url"]:
            raise ValueError("模型连接已变更，请恢复该连接或创建新版本；旧实验不会静默换模型")
        return settings

    def derive(self, parent, name, target, content):
        """A generated candidate may replace exactly one existing supported asset."""
        import yaml
        manifest = verify_version(parent)
        if target not in manifest["files"]:
            raise ValueError("生成器只能修改已授权的现有文件")
        name = bounded_text(name, "候选名称", 80, 1).strip()
        content = bounded_text(content, "候选文件", 8000 if target == "policy.md" else 16000, 1)
        if not name or content == manifest["files"][target]:
            raise ValueError("候选没有实际变更")
        if target != "policy.md":
            try:
                header, body = content.removeprefix("---\n").split("\n---\n", 1)
                metadata = yaml.safe_load(header)
                if (not content.startswith("---\n") or not isinstance(metadata, dict)
                        or set(metadata) != {"name", "description"} or metadata["name"] != target.split("/")[1]):
                    raise ValueError()
                bounded_text(metadata["description"], "Skill 描述", 600, 1)
                bounded_text(body.strip(), "Skill 正文", 12000, 1)
                content = ("---\nname: " + json.dumps(metadata["name"]) + "\ndescription: "
                           + json.dumps(metadata["description"], ensure_ascii=False) + "\n---\n" + body)
            except (ValueError, TypeError, yaml.YAMLError):
                raise ValueError("Skill 必须保留合法名称、描述与正文") from None
        if content == manifest["files"][target]:
            raise ValueError("候选没有实际变更")
        updated = validate_manifest({**manifest, "files": {**manifest["files"], target: content}})
        data = {"name": name, "parent": parent["id"], "manifest": updated, "manifest_sha256": digest(updated)}
        ident = "av-" + digest(data)[:20]
        with self.store.lock:
            existing = self.store.get("evo_version", ident)
            return existing or self.store.create("evo_version", ident, {"id": ident, **data, "created": now()})


def version_diff(parent, child):
    before, after = parent["manifest"]["files"], child["manifest"]["files"]
    return "\n".join("\n".join(difflib.unified_diff(before.get(path, "").splitlines(),
        after.get(path, "").splitlines(), "baseline/" + path, "candidate/" + path, lineterm=""))
        for path in sorted(set(before) | set(after)))


class InputEvidence:
    """Observe actual provider-bound inputs, without storing prompts or secrets."""
    def __init__(self, version):
        self.manifest = verify_version(version)
        self.receipt = {"manifest_sha256": version["manifest_sha256"], "requests_observed": 0,
                        "policy_in_input": not bool(self.manifest["files"]["policy.md"]),
                        "skill_catalog": [], "skill_contents_in_input": []}

    def observe(self, data):
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
        text = "\n".join(strings({"instructions": data.get("instructions"), "input": data.get("input")}))
        messages = data.get("input") if isinstance(data.get("input"), list) else []
        system = "\n".join(strings([data.get("instructions"), [m for m in messages
            if isinstance(m, dict) and m.get("role") in {"system", "developer"}]]))
        policy_input = system
        if self.manifest["runner"] == "dsh":
            policy_input += "\n" + "\n".join(strings([m for m in messages
                if isinstance(m, dict) and m.get("role") == "user"]))
        self.receipt["requests_observed"] += 1
        self.receipt["policy_in_input"] |= self.manifest["files"]["policy.md"] in policy_input
        for path, content in self.manifest["files"].items():
            if path == "policy.md":
                continue
            for field, observed in (("skill_catalog", "/evo-version/" + path in system),
                                    ("skill_contents_in_input", content.strip() in text)):
                if observed and path not in self.receipt[field]:
                    self.receipt[field].append(path)

    def public(self):
        paths = set(self.manifest["files"]) - {"policy.md"}
        return {**self.receipt, "registration_verified": bool(self.receipt["requests_observed"]
                and self.receipt["policy_in_input"] and paths <= set(self.receipt["skill_catalog"]))}
