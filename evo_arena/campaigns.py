"""Sequential, bounded evolution plans sharing one durable model-call budget."""
from __future__ import annotations

import uuid

from . import execution_control as control
from .experiment_budget import ExperimentBudget, limits_checked
from .improvement import ImprovementExperiment, IMPROVE_LIMITS
from .provider import BudgetExceeded
from .store import canonical, digest, now
from .taskpacks import current_pack
from .versions import bounded_text
from .trial_resources import reconcile_experiment

ACTIVE = {"queued", "running", "cancelling"}


def cancel_campaign(store, ident):
    with store.lock, store.db:
        plan = store.get("evo_campaign", ident)
        if not plan: raise ValueError("多轮计划不存在")
        if plan["status"] not in ACTIVE: return plan
        for ex_id in plan["experiments"]:
            control.request_cancel(store, ex_id)
        plan.update(status="cancelled" if plan["status"] == "queued" else "cancelling", reason="user_cancelled")
        store.db.execute("UPDATE objects SET data=? WHERE kind='evo_campaign' AND id=?", (canonical(plan), ident))
        return plan


class Campaign:
    def __init__(self, store, registry, harbor, image):
        self.store, self.registry, self.harbor, self.image = store, registry, harbor, image
        self.engine = ImprovementExperiment(store, registry, harbor, image)

    def create(self, payload, key):
        bounded_text(key, "幂等键", 128, 1)
        if not isinstance(payload, dict) or set(payload) - {"project", "max_rounds", "no_gain_limit", "limits", "optimizer_profile"}:
            raise ValueError("多轮计划格式错误")
        for k in ("max_rounds", "no_gain_limit"):
            if type(payload.get(k)) is not int or not 1 <= payload[k] <= 5: raise ValueError("轮次与连续无收益上限必须为 1–5")
        limits = limits_checked(payload.get("limits"), IMPROVE_LIMITS)
        with self.store.lock, self.store.db:
            old = self.store.get("evo_campaign_request", digest(key))
            if old:
                if old["payload_sha256"] != digest(payload): raise ValueError("同一请求不能更改多轮计划")
                return self.store.get("evo_campaign", old["campaign"]), False
            project = self.store.get("evo_project", payload.get("project", ""))
            if not project or not project.get("target") or project.get("task_pack") != current_pack()["id"]:
                raise ValueError("请选择固定当前任务协议及改进文件的项目")
            baseline = self.store.get("evo_version", project["current_version"])
            self.registry.settings_for(baseline)
            settings, profile, _ = self.registry.models.resolve(payload.get("optimizer_profile", baseline["manifest"]["model"]["profile"]))
            plan = {"id": "campaign-" + uuid.uuid4().hex[:16], "project": project["id"], "initial_version": project["initial_version"],
                    "starting_version": project["current_version"], "expected_revision": project["revision"],
                    "target": project["target"], "task_pack": project["task_pack"], "feedback_mode": project.get("feedback_mode", "development"),
                    "optimizer_profile": profile, "optimizer_model": settings.model, "optimizer_endpoint": settings.base_url,
                    "max_rounds": payload["max_rounds"], "no_gain_limit": payload["no_gain_limit"], "limits": limits,
                    "round_limits": dict(IMPROVE_LIMITS), "protocol_sha256": control.protocol_hash(),
                    "status": "queued", "experiments": [], "no_gain_count": 0, "created": now(), "reason": None}
            for kind, ident, value in (("evo_campaign", plan["id"], plan), ("evo_campaign_request", digest(key), {"campaign": plan["id"], "payload_sha256": digest(payload)})):
                self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", (kind, ident, canonical(value), now()))
        return plan, True

    def execute(self, plan):
        ident = plan["id"]
        with self.store.lock:
            plan = self.store.get("evo_campaign", ident)
            if plan["status"] != "queued": return
            if plan["protocol_sha256"] != control.protocol_hash():
                plan.update(status="interrupted", reason="protocol_changed", finished=now()); self.store.update("evo_campaign", ident, plan); return
            plan.update(status="running", started=now()); self.store.update("evo_campaign", ident, plan)
        ledger = ExperimentBudget(self.store, ident)
        reason, status = "round_limit", "completed"
        try:
            ledger.start(plan["limits"])
            for index in range(plan["max_rounds"]):
                plan = self.store.get("evo_campaign", ident)
                if plan["status"] != "running": reason, status = "user_cancelled", "cancelled"; break
                ledger.check()
                project = self.store.get("evo_project", plan["project"])
                if project["revision"] != plan["expected_revision"]:
                    reason, status = "project_changed", "stopped"; break
                settings, _, _ = self.registry.models.resolve(plan["optimizer_profile"])
                if settings.model != plan["optimizer_model"] or settings.base_url != plan["optimizer_endpoint"]:
                    reason, status = "model_changed", "stopped"; break
                payload = {"mode": "improve", "stage": "full", "project": project["id"], "baseline": project["current_version"],
                           "target": plan["target"], "feedback_mode": plan["feedback_mode"], "optimizer_profile": plan["optimizer_profile"],
                           "limits": plan["round_limits"], "campaign": ident, "round": index + 1}
                with self.store.lock:
                    plan = self.store.get("evo_campaign", ident)
                    if plan["status"] != "running": reason, status = "user_cancelled", "cancelled"; break
                    ex, bindings, _ = self.engine.create(payload, ident + ":" + str(index + 1))
                    plan["experiments"].append(ex["id"])
                    self.store.update("evo_campaign", ident, plan)
                self.engine.execute(ex, bindings)
                ex = self.store.get("evo_experiment", ex["id"])
                with self.store.lock:
                    plan = self.store.get("evo_campaign", ident)
                    if plan["status"] != "running" or ex["status"] == "cancelled": reason, status = "user_cancelled", "cancelled"; break
                    decision = ex.get("decision") or {}
                    if ex["status"] != "completed" or decision.get("status") not in {"accepted", "rejected"}:
                        reason, status = decision.get("reason", "execution_incomplete"), "stopped"; break
                    if decision["status"] == "accepted":
                        plan["no_gain_count"] = 0; plan["expected_revision"] = ex["expected_revision"] + 1
                    elif decision["reason"] in {"unchanged", "regression"}:
                        plan["no_gain_count"] += 1
                    else:
                        reason, status = decision["reason"], "stopped"; break
                    self.store.update("evo_campaign", ident, plan)
                if plan["no_gain_count"] >= plan["no_gain_limit"]: reason = "no_gain_limit"; break
        except BudgetExceeded:
            reason, status = "budget_exhausted", "stopped"
        except control.ExecutionStopped:
            reason, status = "execution_interrupted", "interrupted"
        except Exception as error:
            reason, status = "controller_error", "interrupted"
            control.recover(self.store)
            for ex_id in self.store.get("evo_campaign", ident)["experiments"]:
                reconcile_experiment(self.store, ex_id)
            self.store.event(ident, "campaign.interrupted", {"category": type(error).__name__})
        finally:
            with self.store.lock:
                plan = self.store.get("evo_campaign", ident)
                if plan["status"] == "cancelling": reason, status = "user_cancelled", "cancelled"
                plan.update(status=status, reason=reason, finished=now(), budget=ledger.public())
                self.store.update("evo_campaign", ident, plan)


def campaign_view(store, plan):
    return {**{k: v for k, v in plan.items() if k != "optimizer_endpoint"}, "budget": store.get("evo_budget", plan["id"])}
