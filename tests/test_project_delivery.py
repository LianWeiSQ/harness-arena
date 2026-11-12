import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import test_improvement as helpers
from evo_arena.cli import main
from evo_arena.config import Settings
from evo_arena.models import ModelProfiles
from evo_arena.project_delivery import export_project, import_project, read_project
from evo_arena.projects import create_project
from evo_arena.store import Store, digest
from evo_arena.versions import VersionRegistry


class ProjectDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = helpers.ImprovementTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.project = create_project(self.f.store, self.f.registry, {"name": "Orders", "goal": "Avoid duplicate orders",
            "baseline": self.f.baseline["id"], "target": "policy.md"}, "project")
        self.f.payload["project"] = self.project["id"]
        ex, settings, _ = self.f.create()
        self.ex = self.f.execute(ex, settings)
        self.raw = export_project(self.f.store, self.project["id"])

    def test_round_trip_into_independent_store_preserves_files_not_authority(self):
        manifest, source, files = read_project(self.raw)
        self.assertEqual(source["cost"]["calls"], 32)
        self.assertEqual(len([p for p in files if p.startswith("reports/")]), 1)
        for forbidden in (b"private-key", b"private-endpoint", b"VALIDATION_CANARY", b"raw_output"):
            self.assertNotIn(forbidden, b"".join(v for k, v in files.items() if not k.startswith("assets/")))
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            try:
                registry = VersionRegistry(store, ModelProfiles(store.root, Settings("https://new-host.invalid/v1", "model", "other-key")))
                result = import_project(store, registry, self.raw, "startup", "import")
                self.assertEqual(result["revision"], 0)
                self.assertEqual(result["cost"]["calls"], 0)
                self.assertEqual(result["experiment_ids"], [])
                self.assertEqual(result["initial_version"], result["current_version"])
                self.assertEqual(store.list("evo_decision"), [])
                self.assertEqual(result["imported_from"]["source_current_version"], self.ex["candidate"])
                self.assertEqual(result, import_project(store, registry, self.raw, "startup", "import"))
                imported = store.get("evo_version", result["current_version"])
                original = self.f.store.get("evo_version", self.ex["candidate"])
                self.assertEqual(imported["manifest"]["files"], original["manifest"]["files"])
                self.assertEqual(len(store.list("evo_version")), 2)
            finally: store.close()

    def test_rejects_tampering_paths_truncation_and_protocol_mismatch(self):
        for raw in (b"invalid", self.raw[:200], b"x" * 8_000_001):
            with self.assertRaises(ValueError): read_project(raw)
        with zipfile.ZipFile(io.BytesIO(self.raw)) as archive:
            files = {i.filename: archive.read(i) for i in archive.infolist()}
        files["../escape"] = b"bad"
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            for p, content in files.items(): archive.writestr(p, content)
        with self.assertRaises(ValueError): read_project(out.getvalue())
        before = self.f.store.list("evo_import")
        with patch("evo_arena.project_delivery.current_pack", return_value={"id": "changed"}):
            with self.assertRaises(ValueError): import_project(self.f.store, self.f.registry, self.raw, "startup", "bad")
        self.assertEqual(before, self.f.store.list("evo_import"))

    def test_cli_inspects_without_server_and_uses_same_api_for_stages(self):
        archive = self.f.store.root / "project.zip"; archive.write_bytes(self.raw)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream): self.assertEqual(main(["package", "inspect", str(archive)]), 0)
        self.assertEqual(json.loads(stream.getvalue())["project"]["id"], self.project["id"])
        config = self.f.store.root / "stage.json"; config.write_text(json.dumps({"mode": "improve", "stage": "diagnose"}))
        with patch("evo_arena.workspace_cli.request", return_value={"id": "test"}) as request, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["experiment", "start", "--file", str(config), "--key", "fixed"]), 0)
            request.assert_called_once_with("http://127.0.0.1:8787", "/api/evo/experiments", {"mode": "improve", "stage": "diagnose"}, "fixed")


if __name__ == "__main__": unittest.main()
