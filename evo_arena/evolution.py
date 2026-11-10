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


class VersionExperiment:
    def __init__(self, store, registry, harbor, image):
        self.store, self.registry, self.harbor, self.image = store, registry, harbor, image

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
        if not baseline or not candidate or candidate["parent"] != baseline["id"]:
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
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)",
                ("evo_request", request_id, canonical({"experiment": ident, "payload_hash": digest(payload)}), now()))
        return experiment, settings, True

    def save(self, experiment):
        if experiment.get("plan"):
            experiment["evaluation"] = evaluate(experiment["plan"], experiment["runs"])
        self.store.update("evo_experiment", experiment["id"], experiment)

    def execute(self, experiment, settings):
        budget = ExperimentBudget(self.store, experiment["id"])
        try:
            budget.start(experiment.get("limits", {"calls": 24, "tokens": 360000, "seconds": 900}))
            experiment["status"] = "preparing"
            self.save(experiment)
            experiment["harbor_version"] = command(str(self.harbor), "--version")
            experiment["image_id"] = command("docker", "image", "inspect", self.image, "--format", "{{.Id}}")
            image_ref = "evoarena-snapshot:" + experiment["image_id"].split(":")[-1][:24]
            command("docker", "tag", experiment["image_id"], image_ref)
            experiment["image_ref"] = image_ref
            root = self.store.root / "experiments" / experiment["id"]
            plan = experiment.get("plan")
            pack = self.store.get("evo_task_pack", experiment["task_pack"]) if plan else None
            if pack:
                materialize_business(root / "tasks", image_ref, pack, {r["task"] for r in plan["trials"]})
            else:
                materialize_tasks(root / "tasks", image_ref, {}, ["lost-receipt"])
            experiment["status"] = "running"
            rows = plan["trials"] if plan else [{"role": role, "task": "lost-receipt"} for role in ("baseline", "candidate")]
            for row in rows:
                budget.check()
                role = row["role"]
                version = self.store.get("evo_version", experiment[role])
                run = {**row, "id": "tr-" + uuid.uuid4().hex[:10], "role": role, "version_id": version["id"],
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
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    run.update(status="error", error="版本执行异常 · " + type(error).__name__)
                self.save(experiment)
                if run["status"] == "error":
                    break  # Do not spend on the second version after an infrastructure failure.
            experiment["status"] = "completed" if len(experiment["runs"]) == len(rows) and all(
                r["status"] in {"passed", "failed"} for r in experiment["runs"]) else "incomplete"
        except BudgetExceeded:
            experiment.update(status="incomplete", error="实验总预算耗尽或用量未结算；剩余题目不计为失败")
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            experiment.update(status="incomplete", error="实验准备失败 · " + type(error).__name__)
        finally:
            experiment["budget"] = budget.public()
            if experiment.get("plan"):
                experiment["evaluation"] = evaluate(experiment["plan"], experiment["runs"])
            experiment["finished"] = now()
            self.save(experiment)
