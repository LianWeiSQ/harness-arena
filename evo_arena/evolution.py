"""Version experiments. A real loading check is distinct from a promotion eval."""
from __future__ import annotations

import subprocess
import uuid

from .harness import HarborTrial, LIMITS, command, materialize_tasks
from .store import digest, now
from .versions import version_diff
from .taskpacks import MODES, current_pack, make_plan, materialize_business
from .experiment_budget import ExperimentBudget, limits_checked
from .evaluation import evaluate
from .provider import BudgetExceeded
from . import execution_control as control


class VersionExperiment:
    def __init__(self, store, registry, harbor, image):
        self.store, self.registry, self.harbor, self.image = store, registry, harbor, image

    def resume_settings(self, experiment):
        settings = self.registry.settings_for(self.store.get("evo_version", experiment["baseline"]))
        if experiment.get("candidate"):
            self.registry.settings_for(self.store.get("evo_version", experiment["candidate"]))
        if experiment.get("automatic_evolution"):
            model = experiment["optimizer_model"]
            optimizer, _, _ = self.registry.models.resolve(model["profile"])
            if optimizer.model != model["model"] or optimizer.base_url != model["base_url"]:
                raise ValueError("诊断模型连接已变化，请创建新实验")
            return {"execution": settings, "optimizer": optimizer}
        return settings

    def create(self, payload, key):
        if (not {"baseline", "candidate"} <= set(payload) or set(payload) - {"baseline", "candidate", "mode", "repeats", "limits"}
                or any(not isinstance(payload[k], str) for k in ("baseline", "candidate"))):
            raise ValueError("请选择基线与候选版本")
        mode = payload.get("mode", "loading")
        if mode not in {"loading", *MODES}:
            raise ValueError("未知评测模式")
        if mode == "loading" and "repeats" in payload:
            raise ValueError("加载检查不支持重复参数")
        limits = limits_checked(payload.get("limits"), MODES[mode]["limits"] if mode != "loading" else
                                {"calls": 24, "tokens": 360000, "seconds": 900})
        if not self.harbor or not self.harbor.is_file():
            raise ValueError("请配置 Harbor CLI")
        baseline, candidate = [self.store.get("evo_version", payload[k]) for k in ("baseline", "candidate")]
        if not baseline or not candidate:
            raise ValueError("版本不存在")
        if mode == "report":
            # Final selection can retain the initial version or follow several
            # accepted generations. Reporting never grants promotion rights.
            cursor, seen = candidate, set()
            while cursor and cursor["id"] not in seen and cursor["id"] != baseline["id"]:
                seen.add(cursor["id"])
                cursor = self.store.get("evo_version", cursor.get("parent") or "")
            if not cursor or cursor["id"] != baseline["id"]:
                raise ValueError("封存报告只能比较同一版本或初始基线的后代")
            if any(baseline["manifest"][k] != candidate["manifest"][k] for k in ("runner", "runtime_version", "model")):
                raise ValueError("封存报告必须固定模型与运行器")
        elif candidate["parent"] != baseline["id"]:
            raise ValueError("候选必须从所选基线创建")
        settings = self.registry.settings_for(baseline)
        self.registry.settings_for(candidate)
        pack = current_pack() if mode != "loading" else None
        plan = make_plan(pack, baseline["id"], candidate["id"], mode, payload.get("repeats"), limits) if pack else None
        request_id = digest(key)
        with self.store.lock, self.store.db:
            previous = self.store.get("evo_request", request_id)
            if previous:
                if previous["payload_hash"] != digest(payload):
                    raise ValueError("同一请求不能更改版本")
                return self.store.get("evo_experiment", previous["experiment"]), settings, False
            ident = "ex-" + uuid.uuid4().hex[:12]
            experiment = {"id": ident, "schema": "evo-experiment-v1", "kind": "version-check",
                "status": "queued", "phase": "evaluation" if pack else "loading-check", "created": now(), **payload,
                "mode": mode, "limits": limits,
                "task_pack": pack["id"] if pack else "receipt-loading-smoke-v2", "limits_per_trial": dict(LIMITS),
                "coverage": "SMOKE", "automatic_evolution": False, "decision": None,
                "diff": version_diff(baseline, candidate), "runs": [],
                "runner": baseline["manifest"]["runner"], "model": settings.model}
            if plan:
                experiment.update(kind="paired-evaluation", plan=plan, coverage=MODES[mode]["split"], evaluation=None)
            # One transaction for request + intent. Avoid nested create() commits.
            from .store import canonical
            if pack and not self.store.get("evo_task_pack", pack["id"]):
                self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_task_pack", pack["id"], canonical(pack), now()))
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)",
                ("evo_experiment", ident, canonical(experiment), now()))
            control.insert_intent(self.store, experiment)
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)",
                ("evo_request", request_id, canonical({"experiment": ident, "payload_hash": digest(payload)}), now()))
        return experiment, settings, True

    def save(self, experiment, completed_run=None):
        if experiment.get("plan"):
            runs = [r for r in experiment["runs"] if r.get("phase") == "validation"] if experiment.get("automatic_evolution") else experiment["runs"]
            experiment["evaluation"] = evaluate(experiment["plan"], runs)
        if "epoch" in experiment:
            control.save(self.store, experiment, completed_run)
        else:
            self.store.update("evo_experiment", experiment["id"], experiment)

    def prepare(self, experiment, selected):
        experiment["status"] = "preparing"
        self.save(experiment)
        harbor_version = command(str(self.harbor), "--version")
        if experiment.get("harbor_version") and experiment["harbor_version"] != harbor_version:
            raise ValueError("Harbor 版本已变化，请创建新实验")
        experiment["harbor_version"] = harbor_version
        # Resume the pinned content, never a tag that may have moved meanwhile.
        experiment["image_id"] = command("docker", "image", "inspect", experiment.get("image_id", self.image), "--format", "{{.Id}}")
        image_ref = "evoarena-snapshot:" + experiment["image_id"].split(":")[-1][:24]
        command("docker", "tag", experiment["image_id"], image_ref)
        experiment["image_ref"] = image_ref
        root = self.store.root / "experiments" / experiment["id"]
        pack = self.store.get("evo_task_pack", experiment["task_pack"]) if experiment.get("plan") else None
        if pack:
            materialize_business(root / "tasks", image_ref, pack, selected)
        else:
            materialize_tasks(root / "tasks", image_ref, {}, ["lost-receipt"])
        experiment["status"] = "running"
        self.save(experiment)
        return root, pack

    def run_row(self, experiment, settings, row, root, pack, budget):
        version = self.store.get("evo_version", row.get("version_id") or experiment[row["role"]])
        row = {**row, "version_id": version["id"]}
        if "epoch" in experiment:
            previous = control.completed_row(self.store, experiment, row)
            if previous:
                return previous
        budget.check()
        run = {**row, "id": "tr-" + uuid.uuid4().hex[:10], "version_id": version["id"],
            "runner": experiment["runner"], "model": settings.model,
            "status": "running", "trace": [], "checks": [], "patch": ""}
        experiment["runs"].append(run)
        self.save(experiment)

        def record(label, message):
            run["trace"].append({"label": label, "text": message, "time": now()})
            self.save(experiment)

        try:
            case = next(c for c in pack["cases"] if c["id"] == row["task"]) if pack else None
            HarborTrial(settings, self.harbor).execute(run, root, {}, record, version, case, budget)
            resource = self.store.get("evo_resource", run["id"])
            if resource and resource["state"] != "cleaned":
                run.update(status="error", error="本题执行资源尚未完成对账，停止后续派发")
        except control.ExecutionStopped:
            state = self.store.get("evo_control", experiment["id"])
            run.update(status="cancelled" if state and state["state"] == "cancelling" else "interrupted",
                       error="执行中止；该题结果不计入质量比较")
            raise
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            run.update(status="error", error="版本执行异常 · " + type(error).__name__)
        self.save(experiment, completed_run=run if run["status"] in {"passed", "failed"} else None)
        return run

    def execute(self, experiment, settings):
        control.acquire(self.store, experiment)
        budget = ExperimentBudget(self.store, experiment["id"], experiment)
        stopped = False
        try:
            budget.start(experiment.get("limits", {"calls": 24, "tokens": 360000, "seconds": 900}))
            plan = experiment.get("plan")
            rows = plan["trials"] if plan else [{"role": role, "task": "lost-receipt"} for role in ("baseline", "candidate")]
            root, pack = self.prepare(experiment, {r["task"] for r in rows})
            for row in rows:
                run = self.run_row(experiment, settings, row, root, pack, budget)
                if run["status"] == "error":
                    break  # Do not spend on the second version after an infrastructure failure.
            experiment["status"] = "completed" if len(experiment["runs"]) == len(rows) and all(
                r["status"] in {"passed", "failed"} for r in experiment["runs"]) else "incomplete"
        except control.ExecutionStopped:
            stopped = True
        except BudgetExceeded:
            experiment.update(status="incomplete", error="实验总预算耗尽或用量未结算；剩余题目不计为失败")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            experiment.update(status="incomplete", error="实验准备失败 · " + type(error).__name__)
        finally:
            try:
                state = control.assert_epoch(self.store, experiment)
            except control.ExecutionStopped:
                return  # An old epoch may not overwrite recovery state.
            if state["state"] == "cancelling":
                experiment.update(status="cancelled", error="实验已取消；已完成的证据与用量保留")
            elif stopped:
                experiment.update(status="interrupted", error="执行中止，需核对检查点")
            experiment["budget"] = budget.public()
            if experiment.get("plan"):
                experiment["evaluation"] = evaluate(experiment["plan"], experiment["runs"])
            experiment["finished"] = now()
            with self.store.lock, self.store.db:
                # save() has its own transaction; inline to keep terminal fencing atomic.
                from .store import canonical
                if control.assert_epoch(self.store, experiment)["state"] == "cancelling":
                    experiment.update(status="cancelled", error="实验已取消；已完成的证据与用量保留")
                control.terminal(self.store, experiment, experiment["status"])
                self.store.db.execute("UPDATE objects SET data=? WHERE kind='evo_experiment' AND id=?",
                                      (canonical(experiment), experiment["id"]))
