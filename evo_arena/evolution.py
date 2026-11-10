"""Version experiments. A real loading check is distinct from a promotion eval."""
from __future__ import annotations

import subprocess
import uuid

from .harness import HarborTrial, LIMITS, command, materialize_tasks
from .store import digest, now
from .versions import version_diff


class VersionExperiment:
    def __init__(self, store, registry, harbor, image):
        self.store, self.registry, self.harbor, self.image = store, registry, harbor, image

    def create(self, payload, key):
        if set(payload) != {"baseline", "candidate"} or any(not isinstance(v, str) for v in payload.values()):
            raise ValueError("请选择基线与候选版本")
        if not self.harbor or not self.harbor.is_file():
            raise ValueError("请配置 Harbor CLI")
        baseline, candidate = [self.store.get("evo_version", payload[k]) for k in ("baseline", "candidate")]
        if not baseline or not candidate or candidate["parent"] != baseline["id"]:
            raise ValueError("候选必须从所选基线创建")
        settings = self.registry.settings_for(baseline)
        self.registry.settings_for(candidate)
        request_id = digest(key)
        with self.store.lock, self.store.db:
            previous = self.store.get("evo_request", request_id)
            if previous:
                if previous["payload_hash"] != digest(payload):
                    raise ValueError("同一请求不能更改版本")
                return self.store.get("evo_experiment", previous["experiment"]), settings, False
            ident = "ex-" + uuid.uuid4().hex[:12]
            experiment = {"id": ident, "schema": "evo-experiment-v1", "kind": "version-check",
                "status": "queued", "phase": "loading-check", "created": now(), **payload,
                "task_pack": "receipt-loading-smoke-v1", "limits_per_trial": dict(LIMITS),
                "coverage": "SMOKE", "automatic_evolution": False, "decision": None,
                "diff": version_diff(baseline, candidate), "runs": [],
                "runner": baseline["manifest"]["runner"], "model": settings.model}
            # One transaction for request + intent. Avoid nested create() commits.
            from .store import canonical
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)",
                ("evo_experiment", ident, canonical(experiment), now()))
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)",
                ("evo_request", request_id, canonical({"experiment": ident, "payload_hash": digest(payload)}), now()))
        return experiment, settings, True

    def save(self, experiment):
        self.store.update("evo_experiment", experiment["id"], experiment)

    def execute(self, experiment, settings):
        try:
            experiment["status"] = "preparing"
            self.save(experiment)
            experiment["harbor_version"] = command(str(self.harbor), "--version")
            experiment["image_id"] = command("docker", "image", "inspect", self.image, "--format", "{{.Id}}")
            root = self.store.root / "experiments" / experiment["id"]
            materialize_tasks(root / "tasks", self.image, {}, ["lost-receipt"])
            experiment["status"] = "running"
            for role in ("baseline", "candidate"):
                version = self.store.get("evo_version", experiment[role])
                run = {"id": "tr-" + uuid.uuid4().hex[:10], "role": role, "version_id": version["id"],
                       "runner": experiment["runner"], "model": settings.model, "task": "lost-receipt",
                       "status": "running", "trace": [], "checks": [], "patch": ""}
                experiment["runs"].append(run)
                self.save(experiment)

                def record(label, message):
                    run["trace"].append({"label": label, "text": message, "time": now()})
                    self.save(experiment)

                try:
                    HarborTrial(settings, self.harbor).execute(run, root, {}, record, version)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    run.update(status="error", error="版本执行异常 · " + type(error).__name__)
                self.save(experiment)
                if run["status"] == "error":
                    break  # Do not spend on the second version after an infrastructure failure.
            experiment["status"] = "completed" if len(experiment["runs"]) == 2 and all(
                r["status"] in {"passed", "failed"} for r in experiment["runs"]) else "incomplete"
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            experiment.update(status="incomplete", error="实验准备失败 · " + type(error).__name__)
        finally:
            experiment["finished"] = now()
            self.save(experiment)
