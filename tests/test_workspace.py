import unittest

import test_improvement as helpers
from evo_arena import execution_control as control
from evo_arena.projects import create_project, project_view
from evo_arena.taskpacks import current_pack


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.f = helpers.ImprovementTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.payload = {"name": "Order agent", "goal": "Avoid duplicate orders", "baseline": self.f.baseline["id"], "target": "policy.md"}
        self.project = create_project(self.f.store, self.f.registry, self.payload, "project")

    def run_stage(self, stage, source=None, key=None):
        payload = {**self.f.payload, "project": self.project["id"], "stage": stage}
        if source: payload["source"] = source["id"]
        ex, bindings, _ = self.f.engine.create(payload, key or stage)
        return self.f.execute(ex, bindings)

    def test_project_creation_is_idempotent_and_does_not_execute(self):
        again = create_project(self.f.store, self.f.registry, self.payload, "project")
        self.assertEqual(again, self.project)
        self.assertEqual(self.f.store.list("evo_experiment"), [])
        self.assertEqual(self.f.store.list("evo_call"), [])
        with self.assertRaises(ValueError): create_project(self.f.store, self.f.registry, {**self.payload, "name": "Other"}, "project")
        view = project_view(self.f.store, self.project)
        self.assertEqual(view["cost"]["calls"], 0)
        self.assertEqual(view["task_pack"], current_pack()["id"])

    def test_three_stages_reuse_outputs_without_repeating_trials_or_charging_twice(self):
        diagnosis = self.run_stage("diagnose")
        self.assertEqual(diagnosis["phase"], "diagnosed")
        self.assertIsNone(diagnosis["decision"])
        self.assertEqual(len(diagnosis["runs"]), 6)
        self.assertEqual(diagnosis["budget"]["calls"], 7)
        proposal = self.run_stage("propose", diagnosis)
        self.assertEqual(proposal["phase"], "candidate_ready")
        self.assertEqual(proposal["runs"], [])
        self.assertEqual(proposal["budget"]["calls"], 1)
        self.assertIsNone(proposal["decision"])
        self.assertEqual(self.f.store.get("evo_project", self.project["id"])["revision"], 0)
        validation = self.run_stage("validate", proposal)
        self.assertEqual(validation["decision"]["status"], "accepted")
        self.assertEqual(validation["candidate"], proposal["candidate"])
        self.assertEqual(len(validation["runs"]), 24)
        self.assertEqual(validation["budget"]["calls"], 24)
        project = self.f.store.get("evo_project", self.project["id"])
        self.assertEqual(project["initial_version"], self.f.baseline["id"])
        self.assertEqual(project["current_version"], proposal["candidate"])
        view = project_view(self.f.store, project)
        self.assertEqual(view["cost"]["calls"], 32)
        self.assertEqual(view["cost"]["tokens"], 340)
        self.assertEqual(len(view["experiment_ids"]), 3)
        self.assertNotIn("VALIDATION_CANARY", str(proposal["evidence_bundle"]))

    def test_project_identity_survives_a_second_generation(self):
        first = self.run_stage("full")
        self.f.payload["baseline"] = first["candidate"]
        second = self.run_stage("full", key="second")
        self.assertEqual(second["project"], first["project"])
        self.assertEqual(second["initial_version"], self.f.baseline["id"])
        self.assertEqual(self.f.store.get("evo_project", self.project["id"])["revision"], 2)
        self.assertEqual(len(self.f.store.list("evo_project")), 1)

    def test_source_cannot_cross_projects_protocols_or_changed_snapshot(self):
        diagnosis = self.run_stage("diagnose")
        other = create_project(self.f.store, self.f.registry, self.payload, "second-project")
        payload = {**self.f.payload, "stage": "propose", "source": diagnosis["id"], "project": other["id"]}
        with self.assertRaises(ValueError): self.f.engine.create(payload, "cross-project")
        payload["project"] = self.project["id"]
        ex, bindings, _ = self.f.engine.create(payload, "proposal")
        ex["source_snapshot"]["diagnosis"]["hypothesis"]["summary"] = "tampered"
        self.f.store.update("evo_experiment", ex["id"], ex)
        result = self.f.execute(ex, bindings)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["budget"]["calls"], 0)
        old = self.f.store.get("evo_control", diagnosis["id"]); old["protocol_sha256"] = "different"
        self.f.store.update("evo_control", diagnosis["id"], old)
        with self.assertRaises(ValueError): self.f.engine.create(payload, "changed-protocol")

    def test_incomplete_source_and_wrong_target_are_rejected(self):
        ex, _, _ = self.f.engine.create({**self.f.payload, "project": self.project["id"]}, "pending")
        with self.assertRaises(ValueError):
            self.f.engine.create({**self.f.payload, "project": self.project["id"], "stage": "propose", "source": ex["id"]}, "bad-source")
        with self.assertRaises(ValueError):
            self.f.engine.create({**self.f.payload, "project": self.project["id"], "target": "skills/orders/SKILL.md"}, "wrong-target")

    def test_cancellation_at_optimizer_return_finishes_each_stage_without_adoption(self):
        original = self.f.optimizer
        def cancelling(settings, ledger):
            optimizer = original(settings, ledger)
            complete = optimizer.complete
            def cancel_after_response(*args):
                result = complete(*args)
                control.request_cancel(self.f.store, ledger.ident)
                return result
            optimizer.complete = cancel_after_response
            return optimizer
        self.f.optimizer = cancelling
        cancelled = self.run_stage("diagnose", key="cancel-diagnose")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["budget"]["calls"], 7)
        self.f.optimizer = original
        source = self.run_stage("diagnose", key="ready-diagnosis")
        self.f.optimizer = cancelling
        proposal = self.run_stage("propose", source, key="cancel-propose")
        self.assertEqual(proposal["status"], "cancelled")
        self.assertEqual(proposal["budget"]["calls"], 1)
        self.assertIsNone(proposal["candidate"])
        self.assertEqual(self.f.store.get("evo_project", self.project["id"])["revision"], 0)
        self.assertEqual(self.f.store.get("evo_control", proposal["id"])["state"], "cancelled")


if __name__ == '__main__': unittest.main()
