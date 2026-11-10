import io
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from pathlib import Path

import test_improvement as helpers
from evo_arena.config import Settings
from evo_arena.delivery import export_bundle, read_bundle, import_bundle, rollback, rollback_targets, experiment_report
from evo_arena.models import ModelProfiles
from evo_arena.store import Store, digest
from evo_arena.versions import VersionRegistry
from evo_arena.calibration import calibrate
from evo_arena.improvement import finalize


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = helpers.ImprovementTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)

    def accepted(self):
        ex, bindings, _ = self.f.create()
        return self.f.execute(ex, bindings)

    def payload(self, ex):
        p = self.f.store.get("evo_project", ex["project"])
        return {"target": p["initial_version"], "expected_version": p["current_version"],
                "expected_revision": p["revision"], "reason": "Restore a known baseline"}

    def test_export_import_preserves_asset_bytes_and_rebinds_locally(self):
        ex = self.accepted(); version = self.f.store.get("evo_version", ex["candidate"])
        raw = export_bundle(version)
        self.assertEqual(raw, export_bundle(version))
        package, files = read_bundle(raw)
        self.assertEqual(files, version["manifest"]["files"])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            all_text = "\n".join(archive.read(n).decode() for n in archive.namelist())
        self.assertNotIn("private-endpoint", all_text); self.assertNotIn("private-key", all_text)
        self.assertNotIn("base_url", all_text); self.assertNotIn("profile", all_text)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            try:
                registry = VersionRegistry(store, ModelProfiles(store.root, Settings("https://new-host.invalid/v1", "model", "new-private-key")))
                imported = import_bundle(registry, raw, "startup")
                self.assertEqual(imported["manifest"]["files"], files)
                self.assertEqual(imported["manifest"]["model"]["base_url"], "https://new-host.invalid/v1")
                self.assertIsNone(imported["parent"])
                self.assertEqual(imported, import_bundle(registry, raw, "startup"))
                self.assertEqual(len(store.list("evo_import")), 1)
                self.assertEqual(store.list("evo_decision"), [])
            finally: store.close()

    def test_import_requires_matching_explicit_model_and_rejects_bad_archives(self):
        raw = export_bundle(self.f.baseline)
        for value in (b"invalid", raw[:20], b"x" * 100001):
            with self.assertRaises(ValueError): read_bundle(value)
        for profile in (None, "", "missing"):
            with self.assertRaises(ValueError): import_bundle(self.f.registry, raw, profile)
        package, files = read_bundle(raw)
        def archive(package, files):
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as z:
                z.writestr("package.json", json.dumps(package))
                for p, c in files.items(): z.writestr(p, c)
            return out.getvalue()
        with self.assertRaises(ValueError): read_bundle(archive(package, {**files, "policy.md": "tampered"}))
        for bad_path in ("../escape", "/tmp/policy.md", "skills/../../escape"):
            bad_files = {**files, bad_path: "outside"}
            from evo_arena.versions import sha256
            bad_package = {**package, "files": {p: sha256(c) for p, c in bad_files.items()}}
            bad_package["sha256"] = digest({k: v for k, v in bad_package.items() if k != "sha256"})
            with self.assertRaises(ValueError): read_bundle(archive(bad_package, bad_files))
        package["model_name"] = "another-model"
        package["sha256"] = digest({k: v for k, v in package.items() if k != "sha256"})
        with self.assertRaises(ValueError): import_bundle(self.f.registry, archive(package, files), "startup")

    def test_rollback_is_atomic_idempotent_and_does_not_rewrite_decision(self):
        ex = self.accepted(); payload = self.payload(ex)
        self.assertEqual(rollback_targets(self.f.store, self.f.store.get("evo_project", ex["project"])), [ex["baseline"]])
        change = rollback(self.f.store, ex["project"], payload, "one")
        self.assertEqual(change, rollback(self.f.store, ex["project"], payload, "one"))
        project = self.f.store.get("evo_project", ex["project"])
        self.assertEqual(project["current_version"], ex["baseline"])
        self.assertEqual((project["initial_version"], project["revision"]), (ex["baseline"], 2))
        self.assertEqual(self.f.store.get("evo_decision", ex["decision"]["id"]), ex["decision"])
        self.assertEqual(len(self.f.store.list("evo_project_change")), 1)
        self.assertEqual(len(self.f.store.events(ex["project"])), 2)
        with self.assertRaises(sqlite3.IntegrityError): self.f.store.update("evo_project_change", change["id"], {})
        with self.assertRaises(ValueError): rollback(self.f.store, ex["project"], {**payload, "reason": "different"}, "one")
        with self.assertRaises(ValueError): rollback(self.f.store, ex["project"], payload, "stale")

    def test_corrupt_compression_is_rejected_and_import_failure_rolls_back(self):
        raw = export_bundle(self.f.baseline)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            info = archive.infolist()[0]
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        damaged = bytearray(raw); damaged[offset] ^= 0xff
        with self.assertRaises(ValueError): read_bundle(bytes(damaged))
        ex = self.accepted(); child = self.f.store.get("evo_version", ex["candidate"])
        before = self.f.store.list("evo_version")
        self.f.store.db.executescript("CREATE TRIGGER fail_import BEFORE INSERT ON objects WHEN NEW.kind='evo_import' BEGIN SELECT RAISE(ABORT,'failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError): import_bundle(self.f.registry, export_bundle(child), "startup")
        self.assertEqual(self.f.store.list("evo_version"), before)
        self.assertEqual(self.f.store.list("evo_import"), [])

    def test_rollback_cannot_adopt_unverified_or_unrelated_version(self):
        ex = self.accepted(); payload = self.payload(ex)
        other = self.f.registry.derive(self.f.baseline, "Unverified", "policy.md", "Unverified alternate instructions")
        with self.assertRaises(ValueError): rollback(self.f.store, ex["project"], {**payload, "target": other["id"]}, "bad-target")
        with self.assertRaises(ValueError): rollback(self.f.store, ex["project"], {**payload, "expected_revision": 0}, "stale")
        self.assertEqual(self.f.store.get("evo_project", ex["project"])["current_version"], ex["candidate"])

    def test_failed_rollback_transaction_keeps_pointer_revision_and_events(self):
        ex = self.accepted(); payload = self.payload(ex)
        self.f.store.db.executescript("CREATE TRIGGER fail_rollback BEFORE INSERT ON objects WHEN NEW.kind='evo_project_change' BEGIN SELECT RAISE(ABORT,'failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError): rollback(self.f.store, ex["project"], payload, "fail")
        project = self.f.store.get("evo_project", ex["project"])
        self.assertEqual((project["current_version"], project["revision"]), (ex["candidate"], 1))
        self.assertEqual(len(self.f.store.events(ex["project"])), 1)

    def test_active_experiment_cannot_overwrite_a_newer_rollback(self):
        first = self.accepted(); payload = self.payload(first)
        self.f.payload.update(baseline=first["candidate"], project=first["project"])
        second, settings, _ = self.f.create("next-generation")
        def rollback_before_commit(store, experiment, failure=None):
            rollback(store, first["project"], payload, "rollback-during-evaluation")
            return finalize(store, experiment, failure)
        with patch("evo_arena.improvement.finalize", rollback_before_commit):
            result = self.f.execute(second, settings)
        self.assertEqual(result["decision"]["status"], "not_applied")
        self.assertEqual(result["decision"]["reason"], "project_changed")
        self.assertEqual(self.f.store.get("evo_project", first["project"])["current_version"], first["baseline"])

    def test_report_preserves_cost_and_coverage_without_raw_sensitive_fields(self):
        ex = self.accepted()
        ex["error"] = "private-key /local/secret/path https://private-endpoint.invalid"
        ex["runs"][0]["trace"].append({"label": "private", "text": "private-key"})
        ex["runs"][0]["version_load"] = {"base_url": "private-endpoint", "raw": "private-key"}
        ex["diagnosis"]["hypothesis"]["summary"] = "private-key"
        self.f.store.update("evo_experiment", ex["id"], ex)
        report = experiment_report(self.f.store, ex["id"])
        text = json.dumps(report)
        for forbidden in ("private-key", "private-endpoint", "/local/secret", "raw_output", "base_url"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(report["budget"]["calls"], 32)
        self.assertEqual(sum(x["calls"] for x in report["cost_by_phase"].values()), 32)
        self.assertEqual(report["evaluation"]["valid_trials"], 24)
        self.assertEqual(len(report["runs"]), 30)
        self.assertEqual(report["sha256"], digest({k: v for k, v in report.items() if k != "sha256"}))

    def test_taskpack_calibration_is_real_and_does_not_call_a_model(self):
        result = calibrate()
        self.assertTrue(result["passed"])
        self.assertEqual([result[k] for k in ("correct_accepted", "empty_rejected", "bad_rejected")], [18, 18, 18])
        self.assertEqual(result["model_calls"], 0)


if __name__ == "__main__": unittest.main()
