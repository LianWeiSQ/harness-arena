"""One real, bounded development-feedback -> candidate -> validation cycle."""
from __future__ import annotations

import uuid

from .evaluation import evaluate
from .evolution import VersionExperiment
from .experiment_budget import ExperimentBudget, limits_checked
from .harness import LIMITS
from .optimizer import (BudgetedOptimizer, DIAGNOSIS_INSTRUCTIONS, PROPOSAL_INSTRUCTIONS, PRIOR_INSTRUCTIONS,
                        evidence_bundle, prior_bundle, parse_json, validate_diagnosis, validate_proposal)
from .provider import BudgetExceeded, ProviderError
from .store import canonical, digest, now
from .taskpacks import current_pack, make_plan
from .versions import version_diff
from . import execution_control as control
from .stages import STAGES, source_snapshot, finish_stage


IMPROVE_LIMITS = {"calls": 363, "tokens": 3000000, "seconds": 14400}


def development_plan(pack, baseline):
    return [{"key": c["id"] + ":0:baseline", "task": c["id"], "case_checksum": c["checksum"],
             "family": c["family"], "split": "development", "repeat": 0, "role": "baseline",
             "version_id": baseline, "phase": "development"}
            for c in pack["cases"] if c["split"] == "development"]


def finalize(store, experiment, failure=None):
    """Decision, recommendation CAS, experiment and event commit together."""
    ident = "decision-" + experiment["id"]
    with store.lock, store.db:
        previous = store.get("evo_decision", ident)
        if previous:
            return store.get("evo_experiment", experiment["id"])
        saved = store.get("evo_experiment", experiment["id"])
        execution = control.assert_epoch(store, experiment)
        parent = store.get("evo_campaign", saved.get("campaign", "")) if saved.get("campaign") else None
        if execution["state"] == "cancelling" or parent and parent["status"] != "running":
            failure = {"category": "cancelled", "message": "实验已取消；已完成的证据与用量保留"}
        if saved["status"] not in {"preparing", "running"}:
            raise ValueError("实验已停止，不能提交新判定")
        validation = [r for r in saved["runs"] if r.get("phase") == "validation"]
        result = evaluate(saved["plan"], validation)
        pack = store.get("evo_task_pack", saved["task_pack"])
        expected = make_plan(pack, saved["baseline"], saved.get("candidate") or "$candidate", "validation", 2, saved["limits"])
        template = make_plan(pack, saved["baseline"], "$candidate", "validation", 2, saved["limits"])
        if saved["plan"] != expected or saved["validation_template"] != template:
            failure = {"category": "protocol_changed", "message": "验证计划与预先声明的协议不一致"}
        decision = {"id": ident, "experiment": saved["id"], "created": now(), "project": saved["project"],
            "baseline": saved["baseline"], "candidate": saved.get("candidate"), "initial_version": saved["initial_version"],
            "target": saved["target"], "plan_sha256": saved["plan"]["sha256"], "evaluation": result,
            "status": "insufficient_evidence", "reason": "incomplete_coverage"}
        if failure:
            decision.update(status="rejected" if failure["category"] in {"invalid_candidate", "invalid_diagnosis"} else "insufficient_evidence",
                            reason=failure["category"], detail=failure["message"])
        elif result["complete"]:
            decision.update(status="accepted" if result["gate_eligible"] else "rejected", reason=result["outcome"])
        budget = store.get("evo_budget", saved["id"])
        if decision["status"] == "accepted":
            project = store.get("evo_project", saved["project"])
            candidate = store.get("evo_version", saved["candidate"])
            if (not candidate or candidate["parent"] != saved["baseline"] or not budget
                    or budget["pending"] or budget["unknown_usage_calls"]):
                decision.update(status="insufficient_evidence", reason="unsettled_evidence")
            elif project["current_version"] != saved["baseline"] or project["revision"] != saved["expected_revision"]:
                decision.update(status="not_applied", reason="project_changed")
            else:
                before = canonical(project)
                project.update(current_version=saved["candidate"], revision=project["revision"] + 1, updated=now())
                changed = store.db.execute("UPDATE objects SET data=? WHERE kind='evo_project' AND id=? AND data=?",
                    (canonical(project), project["id"], before))
                if changed.rowcount != 1:
                    decision.update(status="not_applied", reason="project_changed")
        saved.update(decision=decision, evaluation=result, budget=budget, phase="decided", finished=now(),
                     status="incomplete" if decision["status"] == "insufficient_evidence" else "completed")
        if failure and failure["category"] == "cancelled":
            saved["status"] = "cancelled"
        if failure:
            saved["error"] = failure["message"]
        store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_decision", ident, canonical(decision), now()))
        store.db.execute("UPDATE objects SET data=? WHERE kind='evo_experiment' AND id=?", (canonical(saved), saved["id"]))
        store.db.execute("INSERT INTO events(scope,kind,data,created) VALUES(?,?,?,?)",
                         (saved["project"], "evo.decision", canonical({"decision": ident, "status": decision["status"]}), now()))
        control.terminal(store, experiment, saved["status"])
    experiment.clear(); experiment.update(saved)
    return saved


class ImprovementExperiment(VersionExperiment):
    optimizer_factory = BudgetedOptimizer

    def create(self, payload, key):
        allowed = {"baseline", "mode", "target", "limits", "optimizer_profile", "project", "feedback_mode", "stage", "source", "campaign", "round"}
        if (not isinstance(payload, dict) or set(payload) - allowed or payload.get("mode") != "improve"
                or not isinstance(payload.get("baseline"), str) or not isinstance(payload.get("target"), str)):
            raise ValueError("请选择基线、授权文件和改进预算")
        campaign_id = payload.get("campaign")
        if campaign_id:
            campaign = self.store.get("evo_campaign", campaign_id) if isinstance(campaign_id, str) else None
            if (not campaign or campaign["status"] != "running" or payload.get("stage", "full") != "full" or payload.get("source")
                    or payload.get("project") != campaign["project"] or payload.get("target") != campaign["target"]
                    or type(payload.get("round")) is not int or payload["round"] != len(campaign["experiments"]) + 1
                    or payload["round"] > campaign["max_rounds"]
                    or payload.get("limits") != campaign["round_limits"] or payload.get("optimizer_profile") != campaign["optimizer_profile"]
                    or payload.get("feedback_mode", "development") != campaign["feedback_mode"]):
                raise ValueError("多轮计划与子实验配置不一致")
        elif "round" in payload:
            raise ValueError("轮次必须属于多轮计划")
        stage = payload.get("stage", "full")
        if not isinstance(stage, str) or stage not in STAGES:
            raise ValueError("未知改进阶段")
        if stage in {"propose", "validate"} and not isinstance(payload.get("source"), str):
            raise ValueError("此阶段需要来源实验")
        if "source" in payload and (not isinstance(payload["source"], str) or stage == "full"):
            raise ValueError("来源实验配置错误")
        feedback_mode = payload.get("feedback_mode", "development")
        if not isinstance(feedback_mode, str) or feedback_mode not in {"development", "none"}:
            raise ValueError("反馈方式只能是 development 或 none")
        if not self.harbor or not self.harbor.is_file():
            raise ValueError("请配置 Harbor CLI")
        baseline = self.store.get("evo_version", payload["baseline"])
        if not baseline or payload["target"] not in baseline["manifest"]["files"]:
            raise ValueError("只能选择基线中已存在的策略或 Skill 文件")
        settings = self.registry.settings_for(baseline)
        optimizer_profile = payload.get("optimizer_profile", baseline["manifest"]["model"]["profile"])
        if not isinstance(optimizer_profile, str):
            raise ValueError("诊断模型连接格式错误")
        optimizer_settings, optimizer_profile, _ = self.registry.models.resolve(optimizer_profile)
        limits = limits_checked(payload.get("limits"), IMPROVE_LIMITS)
        pack = current_pack()
        template = make_plan(pack, baseline["id"], "$candidate", "validation", 2, limits)
        bindings = {"execution": settings, "optimizer": optimizer_settings}
        with self.store.lock, self.store.db:
            prior = self.store.get("evo_request", digest(key))
            if prior:
                if prior["payload_hash"] != digest(payload):
                    raise ValueError("同一请求不能更改实验")
                return self.store.get("evo_experiment", prior["experiment"]), bindings, False
            prefix = "control-project-" if feedback_mode == "none" else "project-"
            project_id = payload.get("project") or prefix + digest(baseline["id"])[:16]
            if not isinstance(project_id, str):
                raise ValueError("项目格式错误")
            project = self.store.get("evo_project", project_id)
            if payload.get("project") and not project:
                raise ValueError("项目不存在")
            if project and project.get("feedback_mode", "development") != feedback_mode:
                raise ValueError("反馈实验和无反馈对照必须使用独立项目")
            if project and project["current_version"] != baseline["id"]:
                raise ValueError("项目推荐版本已变化，请刷新后选择当前版本")
            if project and project.get("task_pack") and project["task_pack"] != pack["id"]:
                raise ValueError("项目任务包与当前执行协议不同，请保留历史并创建新项目")
            if project and project.get("target") and project["target"] != payload["target"]:
                raise ValueError("改进文件必须与项目约定一致")
            if not project:
                project = {"id": project_id, "name": ("无反馈对照 · " if feedback_mode == "none" else "") + baseline["name"],
                    "feedback_mode": feedback_mode, "initial_version": baseline["id"],
                    "current_version": baseline["id"], "revision": 0, "created": now()}
                self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_project", project_id, canonical(project), now()))
            snapshot = source_snapshot(self.store, payload["source"], stage=stage, baseline=baseline,
                target=payload["target"], pack=pack, project=project_id, feedback_mode=feedback_mode) if payload.get("source") else None
            ident = "ex-" + uuid.uuid4().hex[:12]
            experiment = {"id": ident, "schema": "evo-improvement-v1", "kind": "improvement", "mode": "improve",
                "created": now(), "status": "queued", "phase": "baseline", "automatic_evolution": True,
                "feedback_mode": feedback_mode, "stage": stage, "source": payload.get("source"),
                "campaign": campaign_id, "round": payload.get("round"),
                "source_snapshot": snapshot, "source_sha256": digest(snapshot) if snapshot else None,
                "baseline": baseline["id"], "candidate": None, "target": payload["target"], "diff": "", "runs": [],
                "runner": baseline["manifest"]["runner"], "model": settings.model,
                "optimizer_model": {"profile": optimizer_profile, "model": optimizer_settings.model, "base_url": optimizer_settings.base_url},
                "task_pack": pack["id"], "coverage": "development+validation", "limits": limits, "limits_per_trial": dict(LIMITS),
                "development_plan": development_plan(pack, baseline["id"]), "validation_template": template, "plan": template,
                "project": project_id, "initial_version": project["initial_version"], "expected_revision": project["revision"],
                "diagnosis": None, "proposal": None, "decision": None, "evaluation": None, "optimizer_calls": [],
                "optimizer_policy": {"max_diagnosis_attempts": 2, "max_proposal_attempts": 1}, "optimizer_attempts": []}
            if not self.store.get("evo_task_pack", pack["id"]):
                self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_task_pack", pack["id"], canonical(pack), now()))
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_experiment", ident, canonical(experiment), now()))
            control.insert_intent(self.store, experiment)
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)",
                ("evo_request", digest(key), canonical({"experiment": ident, "payload_hash": digest(payload)}), now()))
        return experiment, bindings, True

    def execute(self, experiment, settings):
        control.acquire(self.store, experiment)
        budget = ExperimentBudget(self.store, experiment["id"], experiment)
        failure = None
        experiment["status"] = "preparing"
        try:
            budget.start(experiment["limits"])
            stage = experiment.get("stage", "full")
            snapshot = experiment.get("source_snapshot")
            if snapshot and digest(snapshot) != experiment["source_sha256"]:
                raise ValueError("阶段来源指纹不匹配")
            if snapshot and snapshot.get("image_id"):
                experiment["image_id"] = snapshot["image_id"]
            if snapshot and snapshot.get("harbor_version"):
                experiment["harbor_version"] = snapshot["harbor_version"]
            if experiment.get("campaign"):
                parent = self.store.get("evo_campaign", experiment["campaign"])
                for field in ("image_id", "harbor_version"):
                    if parent.get(field): experiment[field] = parent[field]
            pack = self.store.get("evo_task_pack", experiment["task_pack"])
            root = self.store.root / "experiments" / experiment["id"]
            if not snapshot or stage == "validate":
                selected = {r["task"] for r in experiment["development_plan"] + experiment["plan"]["trials"]}
                root, pack = self.prepare(experiment, selected)
                if experiment.get("campaign"):
                    with self.store.lock:
                        control.assert_epoch(self.store, experiment, dispatch=True)
                        parent = self.store.get("evo_campaign", experiment["campaign"])
                        for field in ("image_id", "harbor_version"):
                            if parent.get(field) and parent[field] != experiment.get(field):
                                raise ValueError("多轮计划执行环境已变化")
                            if experiment.get(field): parent[field] = experiment[field]
                        self.store.update("evo_campaign", parent["id"], parent)
            else:
                experiment["status"] = "running"
                self.save(experiment)
            baseline = self.store.get("evo_version", experiment["baseline"])
            if not snapshot:
                for row in experiment["development_plan"]:
                    run = self.run_row(experiment, settings["execution"], row, root, pack, budget)
                    if run["status"] not in {"passed", "failed"}:
                        raise ProviderError("开发基线执行异常；不生成候选")
            experiment["phase"] = "diagnosing"
            bundle = snapshot["evidence_bundle"] if snapshot else (
                prior_bundle(baseline, pack, experiment["target"]) if experiment.get("feedback_mode") == "none" else
                evidence_bundle(baseline, pack, [r for r in experiment["runs"] if r.get("phase") == "development"], experiment["target"]))
            experiment["evidence_bundle"] = bundle
            self.save(experiment)
            optimizer = self.optimizer_factory(settings["optimizer"], budget)
            diagnosis = snapshot["diagnosis"] if snapshot and stage in {"propose", "validate"} else self.diagnose(experiment, optimizer, bundle)
            experiment["diagnosis"] = diagnosis
            with self.store.lock:
                control.assert_epoch(self.store, experiment, dispatch=True)
                self.store.create("evo_diagnosis", experiment["id"], {"experiment": experiment["id"], "evidence_sha256": digest(bundle), "value": diagnosis})
            if stage == "diagnose":
                return
            experiment["phase"] = "proposing"
            self.save(experiment)
            proposal_input = {"diagnosis": diagnosis, "target": experiment["target"],
                "parent_file": baseline["manifest"]["files"][experiment["target"]],
                "other_files": {p: c for p, c in baseline["manifest"]["files"].items() if p != experiment["target"]},
                "evidence": [e for e in bundle["evidence"] if e["id"] in {ref for o in diagnosis["observations"] for ref in o["evidence"]}]}
            if stage == "validate":
                proposal = snapshot["proposal"]
            else:
                raw, usage, _ = self.output(experiment, optimizer, "proposal", PROPOSAL_INSTRUCTIONS, proposal_input)
                proposal = validate_proposal(parse_json(raw))
            experiment["proposal"] = proposal
            with self.store.lock:
                control.assert_epoch(self.store, experiment, dispatch=True)
                self.store.create("evo_proposal", experiment["id"], {"experiment": experiment["id"], "input_sha256": digest(proposal_input), "value": proposal})
                candidate = (self.store.get("evo_version", snapshot["candidate"]) if stage == "validate" else
                             self.registry.derive(baseline, proposal["name"], experiment["target"], proposal["content"]))
            experiment.update(candidate=candidate["id"], diff=version_diff(baseline, candidate), phase="validating",
                plan=make_plan(pack, baseline["id"], candidate["id"], "validation", 2, experiment["limits"]))
            self.save(experiment)
            if stage == "propose":
                return
            for row in experiment["plan"]["trials"]:
                run = self.run_row(experiment, settings["execution"], {**row, "phase": "validation"}, root, pack, budget)
                if run["status"] not in {"passed", "failed"}:
                    failure = {"category": "execution_error", "message": run.get("error", "验证执行异常；剩余题目不计为失败")}
                    break
        except control.ExecutionStopped:
            failure = {"category": "cancelled", "message": "执行中止"}
        except BudgetExceeded:
            failure = {"category": "budget_exhausted", "message": "实验总预算耗尽或用量待结算，停止后续派发"}
        except ProviderError as error:
            failure = {"category": "execution_error" if experiment["phase"] == "baseline" else "provider_error", "message": str(error)}
        except ValueError as error:
            category = {"diagnosing": "invalid_diagnosis", "proposing": "invalid_candidate"}.get(experiment["phase"], "configuration_error")
            failure = {"category": category, "message": str(error)}
        except Exception as error:
            failure = {"category": "execution_error", "message": "实验执行异常 · " + type(error).__name__}
        finally:
            # Persist the latest phase outputs before the single decision transaction.
            try:
                # Linearize cancellation with stage completion: a cancel between
                # the state read and finish_stage must not strand a live intent.
                with self.store.lock:
                    self.save(experiment)
                    state = self.store.get("evo_control", experiment["id"])
                    if state and state["state"] == "cancelling":
                        failure = {"category": "cancelled", "message": "阶段结束前收到取消请求"}
                    if failure is None and experiment.get("stage") in {"diagnose", "propose"}:
                        finish_stage(self.store, experiment)
                    else:
                        finalize(self.store, experiment, failure)
            except control.ExecutionStopped:
                pass  # Recovery already fenced this worker; it cannot finalize.

    def output(self, experiment, optimizer, phase, instructions, payload):
        """Re-use a durably captured response after a crash, never call twice."""
        input_hash = digest(payload)
        prior = [r for r in self.store.list("evo_optimizer_output")
                 if r["experiment"] == experiment["id"] and r["phase"] == phase and r["input_sha256"] == input_hash]
        if prior:
            raw, usage = prior[0]["raw_output"], prior[0]["usage"]
        else:
            raw, usage = optimizer.complete(phase, instructions, payload)
        if not any(c["call_id"] == usage["call_id"] for c in experiment["optimizer_calls"]):
            experiment["optimizer_calls"].append({"phase": phase, **usage})
        attempt = self.record_attempt(experiment, phase, raw, usage, input_hash)
        return raw, usage, attempt

    def record_attempt(self, experiment, phase, raw, usage, input_hash):
        attempt = {"id": usage["call_id"], "experiment": experiment["id"], "phase": phase,
                   "input_sha256": input_hash, "raw_output": raw, "usage": usage, "created": now()}
        with self.store.lock:
            control.assert_epoch(self.store, experiment)
            if not self.store.get("evo_optimizer_output", attempt["id"]):
                self.store.create("evo_optimizer_output", attempt["id"], attempt)
        prior = next((a for a in experiment["optimizer_attempts"] if a["id"] == attempt["id"]), None)
        if prior:
            return prior
        experiment["optimizer_attempts"].append({"id": attempt["id"], "phase": phase, "input_sha256": input_hash})
        self.save(experiment)
        return experiment["optimizer_attempts"][-1]

    def diagnose(self, experiment, optimizer, bundle):
        payload = bundle
        for _ in range(experiment["optimizer_policy"]["max_diagnosis_attempts"]):
            instructions = PRIOR_INSTRUCTIONS if experiment.get("feedback_mode") == "none" else DIAGNOSIS_INSTRUCTIONS
            raw, usage, attempt = self.output(experiment, optimizer, "diagnosis", instructions, payload)
            try:
                diagnosis = validate_diagnosis(parse_json(raw), bundle)
                attempt["status"] = "valid"
                self.save(experiment)
                return diagnosis
            except ValueError as error:
                attempt.update(status="invalid", error=str(error))
                self.save(experiment)
                payload = {**bundle, "correction": {"previous_output": raw, "error": str(error)}}
        raise ValueError("诊断在预算内修正后仍未通过校验；原始响应已保留")
