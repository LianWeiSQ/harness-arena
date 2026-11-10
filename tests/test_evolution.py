import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo_arena.config import Settings
from evo_arena.evolution import VersionExperiment
from evo_arena.models import ModelProfiles
from evo_arena.store import Store
from evo_arena.versions import InputEvidence, VersionRegistry, file_hashes, materialize_version, verify_version
from evo_arena.web import Manager, recover
from test_web import DummyThread


class VersionExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.models = ModelProfiles(self.root, Settings("https://example.invalid/v1", "test-model", "private-test-value"))
        self.registry = VersionRegistry(self.store, self.models)
        self.payload = {"name": "Baseline", "runner": "pi", "policy": "Read the recovery skill before making a write.",
            "skills": [{"name": "recovery", "description": "Recover uncertain operations", "body": "Query status before retrying a write."}]}
        self.baseline = self.registry.register(self.payload)
        self.candidate = self.registry.register({**self.payload, "parent": self.baseline["id"],
            "name": "Candidate", "policy": "Read the recovery skill, then verify the final state."})
        self.harbor = self.root / "harbor"
        self.harbor.touch()
        self.engine = VersionExperiment(self.store, self.registry, self.harbor, "test-image")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_manifest_is_immutable_credential_free_and_survives_restart(self):
        self.assertNotIn("private-test-value", json.dumps(self.baseline))
        self.assertEqual(self.registry.register(self.payload), self.baseline)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update("evo_version", self.baseline["id"], {**self.baseline, "name": "mutated"})
        self.store.close()
        self.store = Store(self.root)
        self.assertEqual(self.store.get("evo_version", self.baseline["id"]), self.baseline)

    def test_capabilities_paths_and_parent_contract_are_enforced(self):
        for changes in ({"runner": "dsh"}, {"memory": {}}, {"parent": self.baseline["id"]},
                        {"skills": [{"name": "../escape", "description": "test", "body": "test"}]},
                        {"skills": self.payload["skills"] * 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.registry.register({**self.payload, **changes})
        dsh = self.registry.register({"name": "DSH", "runner": "dsh", "policy": "Inspect state before writing."})
        self.assertEqual(dsh["manifest"]["runner"], "dsh")
        with self.assertRaises(ValueError):
            self.registry.register({"name": "Wrong runner", "runner": "dsh", "parent": self.baseline["id"]})

    def test_materialization_checks_hash_and_refuses_existing_or_symlink_destination(self):
        target = self.root / "bundle"
        materialize_version(self.candidate, target)
        for path, expected in file_hashes(self.candidate["manifest"]).items():
            from evo_arena.versions import sha256
            self.assertEqual(sha256((target / path).read_text()), expected)
        with self.assertRaises(FileExistsError):
            materialize_version(self.candidate, target)
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(FileExistsError):
            materialize_version(self.candidate, link)
        damaged = json.loads(json.dumps(self.candidate))
        damaged["manifest"]["files"]["policy.md"] = "tampered"
        with self.assertRaises(ValueError):
            verify_version(damaged)

    def test_input_evidence_distinguishes_registration_from_skill_reading_and_echoes(self):
        observer = InputEvidence(self.baseline)
        files = self.baseline["manifest"]["files"]
        skill_path = "skills/recovery/SKILL.md"
        observer.observe({"input": [{"role": "assistant", "content": files["policy.md"] + "/evo-version/" + skill_path}]})
        self.assertFalse(observer.public()["registration_verified"])
        observer.observe({"instructions": files["policy.md"] + "\n<skill>/evo-version/" + skill_path + "</skill>"})
        self.assertTrue(observer.public()["registration_verified"])
        self.assertEqual(observer.public()["skill_contents_in_input"], [])
        observer.observe({"input": [{"type": "function_call_output", "output": files[skill_path]}]})
        self.assertEqual(observer.public()["skill_contents_in_input"], [skill_path])

    def test_model_edits_cannot_silently_change_an_existing_version(self):
        profile = self.models.save({"name": "Editable", "model": "model-a", "base_url": "https://a.invalid/v1", "api_key": "test-key"})
        version = self.registry.register({**self.payload, "model_profile": profile["id"]})
        self.models.save({"id": profile["id"], "name": "Editable", "model": "model-b", "base_url": "https://a.invalid/v1", "api_key": ""})
        with self.assertRaises(ValueError):
            self.registry.settings_for(version)

    def test_creation_is_atomic_and_duplicate_request_does_not_launch_again(self):
        manager = Manager(self.store, None, "unused")
        payload = {"baseline": self.baseline["id"], "candidate": self.candidate["id"]}
        with patch("evo_arena.web.threading.Thread", DummyThread):
            first, code = manager.start_evolution(self.engine, payload, "same-key")
            self.assertEqual(code, 202)
            again, code = manager.start_evolution(self.engine, payload, "same-key")
            self.assertEqual((code, again["id"]), (200, first["id"]))
            self.assertEqual(manager.start_evolution(self.engine, {**payload, "candidate": "other"}, "same-key")[1], 409)
            self.assertEqual(manager.start_evolution(self.engine, payload, "new-key")[1], 409)
        self.assertEqual(len(self.store.list("evo_experiment")), 1)
        self.assertIsNone(first["decision"])
        self.assertEqual(first["coverage"], "SMOKE")

    def test_request_write_failure_rolls_back_experiment_intent(self):
        self.store.db.executescript("""CREATE TRIGGER fail_request BEFORE INSERT ON objects
            WHEN NEW.kind='evo_request' BEGIN SELECT RAISE(ABORT,'test failure'); END;""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.engine.create({"baseline": self.baseline["id"], "candidate": self.candidate["id"]}, "failed-key")
        self.assertEqual(self.store.list("evo_experiment"), [])

    def test_recovery_retains_completed_evidence_and_does_not_replay(self):
        experiment, _, _ = self.engine.create({"baseline": self.baseline["id"], "candidate": self.candidate["id"]}, "recover")
        experiment.update(status="running", runs=[{"status": "passed", "version_load": {"proof": "retained"}}, {"status": "running"}])
        self.engine.save(experiment)
        recover(self.store)
        result = self.store.get("evo_experiment", experiment["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["runs"][0], experiment["runs"][0])
        self.assertEqual(result["runs"][1]["status"], "interrupted")

    def test_paired_protocol_freezes_limits_splits_and_task_pack(self):
        payload = {"baseline": self.baseline["id"], "candidate": self.candidate["id"], "mode": "validation",
                   "limits": {"calls": 12, "tokens": 100000, "seconds": 600}}
        experiment, _, _ = self.engine.create(payload, "validation")
        self.assertEqual(len(experiment["plan"]["trials"]), 24)
        self.assertEqual(experiment["plan"]["experiment_limits"], payload["limits"])
        self.assertEqual({r["split"] for r in experiment["plan"]["trials"]}, {"validation"})
        pack = self.store.get("evo_task_pack", experiment["task_pack"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update("evo_task_pack", pack["id"], {**pack, "cases": []})
        again, _, created = self.engine.create(payload, "validation")
        self.assertFalse(created)
        self.assertEqual(experiment["id"], again["id"])
        with self.assertRaises(ValueError):
            self.engine.create({**payload, "limits": {"calls": 0}}, "bad")

    def test_sealed_report_supports_unchanged_selection_and_multigeneration_descendant(self):
        grandchild = self.registry.derive(self.candidate, "Grandchild", "policy.md", "Inspect and verify the final result.")
        for selected in (self.baseline, grandchild):
            ex, _, _ = self.engine.create({"baseline": self.baseline["id"], "candidate": selected["id"], "mode": "report"}, selected["id"])
            self.assertEqual(len(ex["plan"]["trials"]), 24)
            self.assertFalse(ex["plan"]["automatic_promotion"])
            self.assertEqual({r["split"] for r in ex["plan"]["trials"]}, {"sealed"})
            self.assertEqual(len({r["key"] for r in ex["plan"]["trials"]}), 24)
        unrelated = self.registry.register({**self.payload, "name": "Unrelated"})
        for candidate, mode in ((unrelated, "report"), (self.baseline, "validation"), (grandchild, "validation")):
            with self.assertRaises(ValueError):
                self.engine.create({"baseline": self.baseline["id"], "candidate": candidate["id"], "mode": mode}, "bad-report")


if __name__ == "__main__":
    unittest.main()
