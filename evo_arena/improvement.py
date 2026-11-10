"""One real, bounded development-feedback -> candidate -> validation cycle."""
from __future__ import annotations

import uuid

from .evaluation import evaluate
from .evolution import VersionExperiment
from .experiment_budget import ExperimentBudget, limits_checked
from .harness import LIMITS
from .optimizer import (BudgetedOptimizer, DIAGNOSIS_INSTRUCTIONS, PROPOSAL_INSTRUCTIONS,
                        evidence_bundle, parse_json, validate_diagnosis, validate_proposal)
from .provider import BudgetExceeded, ProviderError
from .store import canonical, digest, now
from .taskpacks import current_pack, make_plan
from .versions import version_diff


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
        if failure:
            saved["error"] = failure["message"]
        store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_decision", ident, canonical(decision), now()))
        store.db.execute("UPDATE objects SET data=? WHERE kind='evo_experiment' AND id=?", (canonical(saved), saved["id"]))
        store.db.execute("INSERT INTO events(scope,kind,data,created) VALUES(?,?,?,?)",
                         (saved["project"], "evo.decision", canonical({"decision": ident, "status": decision["status"]}), now()))
    experiment.clear(); experiment.update(saved)
    return saved


class ImprovementExperiment(VersionExperiment):
    optimizer_factory = BudgetedOptimizer

    def create(self, payload, key):
        allowed = {"baseline", "mode", "target", "limits", "optimizer_profile", "project"}
        if (not isinstance(payload, dict) or set(payload) - allowed or payload.get("mode") != "improve"
                or not isinstance(payload.get("baseline"), str) or not isinstance(payload.get("target"), str)):
            raise ValueError("请选择基线、授权文件和改进预算")
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
            project_id = payload.get("project") or "project-" + digest(baseline["id"])[:16]
            if not isinstance(project_id, str):
                raise ValueError("项目格式错误")
            project = self.store.get("evo_project", project_id)
            if payload.get("project") and not project:
                raise ValueError("项目不存在")
            if project and project["current_version"] != baseline["id"]:
                raise ValueError("项目推荐版本已变化，请刷新后选择当前版本")
            if not project:
                project = {"id": project_id, "name": baseline["name"], "initial_version": baseline["id"],
                    "current_version": baseline["id"], "revision": 0, "created": now()}
                self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_project", project_id, canonical(project), now()))
            ident = "ex-" + uuid.uuid4().hex[:12]
            experiment = {"id": ident, "schema": "evo-improvement-v1", "kind": "improvement", "mode": "improve",
                "created": now(), "status": "queued", "phase": "baseline", "automatic_evolution": True,
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
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)",
                ("evo_request", digest(key), canonical({"experiment": ident, "payload_hash": digest(payload)}), now()))
        return experiment, bindings, True

    def execute(self, experiment, settings):
        budget = ExperimentBudget(self.store, experiment["id"])
        failure = None
        experiment["status"] = "preparing"
        try:
            budget.start(experiment["limits"])
            selected = {r["task"] for r in experiment["development_plan"] + experiment["plan"]["trials"]}
            root, pack = self.prepare(experiment, selected)
            baseline = self.store.get("evo_version", experiment["baseline"])
            for row in experiment["development_plan"]:
                run = self.run_row(experiment, settings["execution"], row, root, pack, budget)
                if run["status"] not in {"passed", "failed"}:
                    raise ProviderError("开发基线执行异常；不生成候选")
            experiment["phase"] = "diagnosing"
            bundle = evidence_bundle(baseline, pack, experiment["runs"], experiment["target"])
            experiment["evidence_bundle"] = bundle
            self.save(experiment)
            optimizer = self.optimizer_factory(settings["optimizer"], budget)
            diagnosis = self.diagnose(experiment, optimizer, bundle)
            experiment["diagnosis"] = diagnosis
            self.store.create("evo_diagnosis", experiment["id"], {"experiment": experiment["id"], "evidence_sha256": digest(bundle), "value": diagnosis})
            experiment["phase"] = "proposing"
            self.save(experiment)
            proposal_input = {"diagnosis": diagnosis, "target": experiment["target"],
                "parent_file": baseline["manifest"]["files"][experiment["target"]],
                "other_files": {p: c for p, c in baseline["manifest"]["files"].items() if p != experiment["target"]},
                "evidence": [e for e in bundle["evidence"] if e["id"] in {ref for o in diagnosis["observations"] for ref in o["evidence"]}]}
            raw, usage = optimizer.complete("proposal", PROPOSAL_INSTRUCTIONS, proposal_input)
            experiment["optimizer_calls"].append({"phase": "proposal", **usage})
            self.record_attempt(experiment, "proposal", raw, usage, digest(proposal_input))
            proposal = validate_proposal(parse_json(raw))
            experiment["proposal"] = proposal
            self.store.create("evo_proposal", experiment["id"], {"experiment": experiment["id"], "input_sha256": digest(proposal_input), "value": proposal})
            candidate = self.registry.derive(baseline, proposal["name"], experiment["target"], proposal["content"])
            experiment.update(candidate=candidate["id"], diff=version_diff(baseline, candidate), phase="validating",
                plan=make_plan(pack, baseline["id"], candidate["id"], "validation", 2, experiment["limits"]))
            self.save(experiment)
            for row in experiment["plan"]["trials"]:
                run = self.run_row(experiment, settings["execution"], {**row, "phase": "validation"}, root, pack, budget)
                if run["status"] not in {"passed", "failed"}:
                    break
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
            self.save(experiment)
            finalize(self.store, experiment, failure)

    def record_attempt(self, experiment, phase, raw, usage, input_hash):
        attempt = {"id": usage["call_id"], "experiment": experiment["id"], "phase": phase,
                   "input_sha256": input_hash, "raw_output": raw, "usage": usage, "created": now()}
        self.store.create("evo_optimizer_output", attempt["id"], attempt)
        experiment["optimizer_attempts"].append({"id": attempt["id"], "phase": phase, "input_sha256": input_hash})
        self.save(experiment)
        return experiment["optimizer_attempts"][-1]

    def diagnose(self, experiment, optimizer, bundle):
        payload = bundle
        for _ in range(experiment["optimizer_policy"]["max_diagnosis_attempts"]):
            raw, usage = optimizer.complete("diagnosis", DIAGNOSIS_INSTRUCTIONS, payload)
            experiment["optimizer_calls"].append({"phase": "diagnosis", **usage})
            attempt = self.record_attempt(experiment, "diagnosis", raw, usage, digest(payload))
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
