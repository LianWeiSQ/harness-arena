"""Reusable, explicitly referenced outputs. No copied trials or copied expenses."""
from __future__ import annotations

from . import execution_control as control
from .optimizer import evidence_bundle, prior_bundle, validate_diagnosis, validate_proposal
from .store import canonical, digest, now
from .versions import verify_version

STAGES = {"full": "完整改进", "diagnose": "分析执行反馈", "propose": "生成候选", "validate": "评测候选"}


def source_snapshot(store, ident, *, stage, baseline, target, pack, project, feedback_mode):
    source = store.get("evo_experiment", ident)
    if not source or source["status"] in {"queued", "preparing", "running", "interrupted"}:
        raise ValueError("请选择已停止并保存产物的来源实验")
    if (source.get("project") != project or source["baseline"] != baseline["id"] or source.get("target") != target
            or source["task_pack"] != pack["id"] or source.get("feedback_mode", "development") != feedback_mode):
        raise ValueError("来源的项目、基线、文件、任务包和反馈方式必须一致")
    execution = store.get("evo_control", ident)
    if not execution or execution["protocol_sha256"] != control.protocol_hash():
        raise ValueError("来源执行代码与当前协议不同，旧记录仅供查看")
    if store.get("evo_intent", ident) != control.intent(source):
        raise ValueError("来源实验意图已变化")
    if any(r["state"] != "cleaned" for r in store.list("evo_resource") if r["experiment"] == ident):
        raise ValueError("来源资源尚未清理完成")
    inherited = source.get("source_snapshot")
    if inherited:
        if digest(inherited) != source.get("source_sha256"):
            raise ValueError("来源引用已变化")
        bundle = inherited["evidence_bundle"]
    else:
        runs = [r for r in source["runs"] if r.get("phase") == "development"]
        expected = {c["id"]: c for c in pack["cases"] if c["split"] == "development"}
        if len(runs) != len(expected) or {r["task"] for r in runs} != set(expected):
            raise ValueError("来源开发覆盖不完整")
        for r in runs:
            proof = store.get("evo_checkpoint", r["id"])
            if (not proof or proof["sha256"] != digest(r) or r.get("status") not in {"passed", "failed"} or r.get("version_id") != baseline["id"]
                    or r.get("task_checksum") != expected[r["task"]]["checksum"] or not r.get("verifier_attested")
                    or not r.get("execution_frozen") or not r.get("input_evidence", {}).get("registration_verified")):
                raise ValueError("来源开发执行缺少完整验收或检查点")
        bundle = prior_bundle(baseline, pack, target) if feedback_mode == "none" else evidence_bundle(baseline, pack, runs, target)
    snapshot = {"experiment": ident, "evidence_bundle": bundle, "image_id": source.get("image_id"), "harbor_version": source.get("harbor_version"),
                "source_chain": [ident] + (source.get("source_snapshot") or {}).get("source_chain", [])}
    if stage in {"propose", "validate"}:
        record = store.get("evo_diagnosis", ident)
        if not record or record["evidence_sha256"] != digest(bundle) or record["value"] != source.get("diagnosis"):
            raise ValueError("来源诊断尚未完整保存或与证据不匹配")
        snapshot["diagnosis"] = validate_diagnosis(record["value"], bundle)
    if stage == "validate":
        record = store.get("evo_proposal", ident)
        if not record or record["value"] != source.get("proposal"):
            raise ValueError("来源候选尚未完整保存")
        proposal = validate_proposal(record["value"])
        candidate = store.get("evo_version", source.get("candidate", ""))
        if not candidate or candidate["parent"] != baseline["id"]:
            raise ValueError("来源候选与基线不匹配")
        manifest = verify_version(candidate)
        expected = {**baseline["manifest"]["files"], target: proposal["content"]}
        if manifest["files"] != expected or any(manifest[k] != baseline["manifest"][k] for k in ("runner", "runtime_version", "model")):
            raise ValueError("来源候选超出授权修改范围")
        snapshot.update(candidate=candidate["id"], proposal=proposal)
    return snapshot


def finish_stage(store, experiment):
    with store.lock, store.db:
        control.assert_epoch(store, experiment, dispatch=True)
        budget = store.get("evo_budget", experiment["id"])
        if budget["pending"] or budget["unknown_usage_calls"]:
            raise ValueError("阶段产物存在未结算用量")
        experiment.update(status="completed", phase="diagnosed" if experiment["stage"] == "diagnose" else "candidate_ready",
                          finished=now(), budget=budget, evaluation=None, decision=None)
        store.db.execute("UPDATE objects SET data=? WHERE kind='evo_experiment' AND id=?", (canonical(experiment), experiment["id"]))
        control.terminal(store, experiment, "completed")
