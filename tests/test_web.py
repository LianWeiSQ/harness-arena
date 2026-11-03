import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evo_arena.config import Settings
from evo_arena.store import Store
from evo_arena.web import Manager, recover


class DummyThread:
    def __init__(self, **kwargs):
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_duplicate_start_cannot_create_another_paid_job(self):
        manager = Manager(self.store, Settings("http://example.invalid/v1", "test", "fake-test-key"), "test-only")
        payload = {"population": 2, "rounds": 2, "max_calls": 32}
        with patch("evo_arena.web.threading.Thread", DummyThread):
            first, code = manager.start(payload, "same-key")
            self.assertEqual(code, 202)
            again, code = manager.start(payload, "same-key")
            self.assertEqual(code, 200)
            self.assertEqual(again["id"], first["id"])
            _, code = manager.start({**payload, "rounds": 3}, "same-key")
            self.assertEqual(code, 409)
            _, code = manager.start(payload, "new-key")
            self.assertEqual(code, 409)
        self.assertEqual(len(self.store.list("season")), 1)

    def test_recovery_marks_unknown_execution_without_replay(self):
        self.store.create("season", "s-test", {"id": "s-test", "status": "running"})
        self.store.create("run", "r-test", {"id": "r-test", "season": "s-test", "status": "running", "container_name": "evoarena-r-test"})
        with patch("evo_arena.web.docker", return_value="true") as mocked:
            recover(self.store)
            self.assertEqual(self.store.get("run", "r-test")["status"], "interrupted")
            self.assertEqual(self.store.get("season", "s-test")["status"], "interrupted")
            mocked.assert_any_call("rm", "--force", "evoarena-r-test")
        original_events = self.store.events("s-test")
        with patch("evo_arena.web.docker") as mocked:
            recover(self.store)
            mocked.assert_not_called()
        self.assertEqual(original_events, self.store.events("s-test"))

    def test_custom_roster_reaches_the_execution_snapshot(self):
        a = self.store.version("Player A", "Inspect the running service before repair")
        b = self.store.version("Player B", "Verify configuration and submit the minimal fix")
        manager = Manager(self.store, Settings("http://example.invalid/v1", "test", "fake-test-key"), "test-only")
        with patch("evo_arena.web.threading.Thread", DummyThread):
            season, code = manager.start({"version_ids": [a["id"], b["id"]]}, "custom-roster")
        self.assertEqual(code, 202)
        self.assertEqual(season["initial_versions"], [a["id"], b["id"]])
        self.assertEqual([s["version"] for s in self.store.seats(season["id"])], [a["id"], b["id"]])


if __name__ == "__main__":
    unittest.main()
