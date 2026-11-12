"""Single-owner execution epochs, immutable intent and confirmed Trial boundaries.

This is fencing, not a distributed lease. The CLI's directory lock is still the
exclusive owner. Restart recovery invalidates every old epoch before cleanup.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .store import canonical, digest, now


class ExecutionStopped(RuntimeError):
    pass


INTENT_FIELDS = ("schema", "kind", "mode", "baseline", "target", "runner", "model", "optimizer_model",
    "task_pack", "limits", "limits_per_trial", "development_plan", "validation_template", "optimizer_policy",
    "project", "initial_version", "expected_revision", "feedback_mode", "stage", "source", "source_sha256", "campaign", "round")


def protocol_hash():
    root = Path(__file__).parent
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts})


def intent(ex):
    value = {k: ex[k] for k in INTENT_FIELDS if k in ex}
    if not ex.get("automatic_evolution"):
        value.update(candidate=ex.get("candidate"), plan=ex.get("plan"))
    return value


def insert_intent(store, ex):
    """Caller owns the creation transaction."""
    store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_intent", ex["id"], canonical(intent(ex)), now()))
    store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_control", ex["id"], canonical({
        "id": ex["id"], "epoch": 0, "state": "ready", "protocol_sha256": protocol_hash()}), now()))


def assert_epoch(store, ex, *, dispatch=False):
    control = store.get("evo_control", ex["id"])
    if not control or ex.get("epoch") != control["epoch"] or control["state"] not in {"active", "cancelling"}:
        raise ExecutionStopped("执行者已失效，不能写入或继续派发")
    if dispatch and ex.get("campaign"):
        parent = store.get("evo_campaign", ex["campaign"])
        if not parent or parent["status"] != "running":
            raise ExecutionStopped("多轮计划已停止，不能继续派发")
    if dispatch and control["state"] == "cancelling":
        raise ExecutionStopped("实验取消中，停止新调用")
    if store.get("evo_intent", ex["id"]) != intent(ex):
        raise ExecutionStopped("实验意图已变化，停止执行")
    return control


def acquire(store, ex):
    with store.lock, store.db:
        saved = store.get("evo_experiment", ex["id"])
        if saved["status"] != "queued":
            raise ExecutionStopped("只能领取已入队实验")
        if store.get("evo_intent", ex["id"]) != intent(saved):
            raise ExecutionStopped("实验意图不完整或已变化")
        old = store.get("evo_control", ex["id"])
        if old and old["state"] != "ready":
            raise ExecutionStopped("实验已被领取")
        control = {"id": ex["id"], "epoch": old["epoch"] + 1 if old else 1, "state": "active",
                   "protocol_sha256": protocol_hash(), "started": now()}
        if old and old["protocol_sha256"] != control["protocol_sha256"]:
            raise ExecutionStopped("执行代码已变化，请创建新实验")
        saved.update(epoch=control["epoch"], status="preparing")
        saved.pop("finished", None)
        store.db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?,?)", ("evo_control", ex["id"], canonical(control), now()))
        store.db.execute("UPDATE objects SET data=? WHERE kind='evo_experiment' AND id=?", (canonical(saved), ex["id"]))
    ex.clear(); ex.update(saved)
    return control


def save(store, ex, completed_run=None):
    with store.lock, store.db:
        assert_epoch(store, ex)
        if completed_run:
            if completed_run["status"] not in {"passed", "failed"}:
                raise ValueError("只允许完整 Trial 检查点")
            checkpoint = {"experiment": ex["id"], "run": completed_run["id"],
                          "row": row_key(completed_run), "sha256": digest(completed_run)}
            store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_checkpoint", completed_run["id"], canonical(checkpoint), now()))
        store.db.execute("UPDATE objects SET data=? WHERE kind='evo_experiment' AND id=?", (canonical(ex), ex["id"]))


def row_key(row):
    return digest({k: row.get(k) for k in ("phase", "key", "task", "repeat", "role", "version_id")})


def completed_row(store, ex, row):
    found = [r for r in ex["runs"] if row_key(r) == row_key(row)]
    if not found:
        return None
    if len(found) != 1:
        raise ExecutionStopped("重复 Trial 意图，不能继续")
    run = found[0]
    proof = store.get("evo_checkpoint", run["id"])
    if not proof or proof["sha256"] != digest(run) or run["status"] not in {"passed", "failed"}:
        raise ExecutionStopped("已有未确认 Trial，禁止自动重放")
    return run


def request_cancel(store, ident):
    with store.lock, store.db:
        ex = store.get("evo_experiment", ident)
        if not ex:
            raise ValueError("实验不存在")
        control = store.get("evo_control", ident)
        if control and control["state"] == "cancelling":
            return ex
        if ex["status"] not in {"queued", "preparing", "running", "interrupted"}:
            return ex
        if not control or control["state"] in {"ready", "interrupted"}:
            ex.update(status="cancelled", finished=now())
            store.db.execute("UPDATE objects SET data=? WHERE kind='evo_experiment' AND id=?", (canonical(ex), ident))
            if control:
                control.update(state="cancelled", epoch=control["epoch"] + 1)
        else:
            control.update(state="cancelling", cancel_requested=now())
        if control:
            store.db.execute("UPDATE objects SET data=? WHERE kind='evo_control' AND id=?", (canonical(control), ident))
        return ex


def terminal(store, ex, state):
    """Caller owns the decision/terminal transaction."""
    control = assert_epoch(store, ex)
    control.update(state=state, finished=now())
    store.db.execute("UPDATE objects SET data=? WHERE kind='evo_control' AND id=?", (canonical(control), ex["id"]))


def resume_reasons(store, ex):
    reasons = []
    if ex.get("campaign"):
        return ["多轮计划中断后保留全部记录，不单独续跑子实验；请从项目当前版本创建新计划"]
    control = store.get("evo_control", ex["id"])
    if ex["status"] != "interrupted" or not control or control["state"] != "interrupted":
        return ["仅支持新协议的中断实验"]
    if control["protocol_sha256"] != protocol_hash():
        reasons.append("执行代码已变化，需要新实验")
    if store.get("evo_intent", ex["id"]) != intent(ex):
        reasons.append("冻结的实验意图已变化")
    budget = store.get("evo_budget", ex["id"])
    if budget:
        from .experiment_budget import ExperimentBudget
        from .provider import BudgetExceeded
        try:
            ExperimentBudget(store, ex["id"]).check()
        except BudgetExceeded:
            reasons.append("预算耗尽、已过期或存在未知用量")
    for run in ex["runs"]:
        proof = store.get("evo_checkpoint", run.get("id", ""))
        if run["status"] not in {"passed", "failed"} or not proof or proof["sha256"] != digest(run):
            reasons.append("存在未确认 Trial，禁止重放其业务动作")
            break
    calls = [c for c in store.list("evo_call") if c["experiment"] == ex["id"] and c["phase"] in {"diagnosis", "proposal"}]
    if any(c["status"] != "settled" or not store.get("evo_optimizer_output", c["id"]) for c in calls):
        reasons.append("诊断或生成结果尚未落盘，禁止重复调用")
    if any(r["experiment"] == ex["id"] and r["state"] != "cleaned" for r in store.list("evo_resource")):
        reasons.append("旧执行资源尚未清理完成")
    return reasons


def enqueue_resume(store, ident, key):
    with store.lock, store.db:
        command = "resume-" + digest(key)
        previous = store.get("evo_control_request", command)
        if previous:
            if previous["experiment"] != ident:
                raise ValueError("恢复请求幂等键冲突")
            return store.get("evo_experiment", ident), False
        ex = store.get("evo_experiment", ident)
        if not ex:
            raise ValueError("实验不存在")
        reasons = resume_reasons(store, ex)
        if reasons:
            raise ValueError("；".join(reasons))
        control = store.get("evo_control", ident)
        control.update(state="ready")
        ex.update(status="queued", resumed=now())
        for kind, value in (("evo_control", control), ("evo_experiment", ex)):
            store.db.execute("UPDATE objects SET data=? WHERE kind=? AND id=?", (canonical(value), kind, ident))
        store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_control_request", command, canonical({"experiment": ident}), now()))
    return ex, True


def recover(store):
    """Called only after the CLI acquired the directory owner lock."""
    from .experiment_budget import ExperimentBudget
    for ex in store.list("evo_experiment"):
        if ex["status"] not in {"queued", "preparing", "running"}:
            continue
        with store.lock, store.db:
            control = store.get("evo_control", ex["id"])
            cancelled = control and control["state"] == "cancelling"
            if control:
                control.update(epoch=control["epoch"] + 1, state="cancelled" if cancelled else "interrupted", recovered=now())
                store.db.execute("UPDATE objects SET data=? WHERE kind='evo_control' AND id=?", (canonical(control), ex["id"]))
            for run in ex["runs"]:
                if run["status"] == "running":
                    run.update(status="interrupted", error="执行者丢失，业务结果未确认")
            ex.update(status="cancelled" if cancelled else "interrupted", interrupted=now())
            store.db.execute("UPDATE objects SET data=? WHERE kind='evo_experiment' AND id=?", (canonical(ex), ex["id"]))
        budget = ExperimentBudget(store, ex["id"])
        for call in store.list("evo_call"):
            if call["experiment"] == ex["id"] and call["status"] == "pending":
                budget.settle(call["id"])
