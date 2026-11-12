"""Local HTTP tests; no provider credentials, Docker or paid model requests."""
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

from evo_arena.config import Settings
from evo_arena.experiment_budget import ExperimentBudget
from evo_arena.harness import TrialGateway
from evo_arena.store import Store


PAYLOAD = b'{"model":"model","stream":true}'
TERMINAL = b'data: {"type":"response.completed","response":{"status":"completed","output":[],"usage":{"total_tokens":17}}}\n\n'


class RequestControlTests(unittest.TestCase):
    def test_stream_past_75_seconds_completes_without_a_request_deadline(self):
        gateway = TrialGateway(Settings("http://invalid/v1", "model", "test"), lambda *args: None)
        response = MagicMock(); response.__enter__.return_value = response
        response.headers.get.return_value = "text/event-stream"
        lines = io.BytesIO(b'data: {"type":"response.created"}\n\n' + TERMINAL)
        elapsed = [0]
        def read_line(*args):
            elapsed[0] += 35
            return lines.readline(*args)
        response.readline.side_effect = read_line
        gateway.opener = MagicMock(); gateway.opener.open.return_value = response
        with patch("evo_arena.harness.time.monotonic", side_effect=lambda: elapsed[0]):
            self.assertEqual(gateway.model(PAYLOAD)[0], 200)
        self.assertGreater(elapsed[0], 75)
        self.assertEqual(gateway.budget.tokens, 17)

    def test_cancel_wakes_waiting_headers_partial_sse_line_and_json_body(self):
        for mode in ("headers", "sse", "json"):
            with self.subTest(mode=mode):
                self._blocked_request(mode, "cancelled")

    def test_harbor_task_end_closes_an_inflight_request(self):
        self._blocked_request("sse", "task_ended")

    def test_explicit_experiment_deadline_remains_effective(self):
        self._blocked_request("headers", "budget_timeout")

    def _blocked_request(self, mode, reason):
        started, release, cancel = threading.Event(), threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                if mode != "headers":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json" if mode == "json" else "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b'{"status":' if mode == "json" else b'data: {"type":')
                    self.wfile.flush()
                started.set()
                release.wait(5)
                self.close_connection = True
            def log_message(self, *_): pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        with tempfile.TemporaryDirectory() as folder, ThreadPoolExecutor(max_workers=1) as pool:
            store = Store(Path(folder))
            gateway = TrialGateway(Settings(f"http://127.0.0.1:{server.server_port}", "model", "test"), lambda *args: None)
            if reason == "task_ended":
                gateway.end_marker = Path(folder) / "ended"
            elif reason == "budget_timeout":
                gateway.experiment_budget = ExperimentBudget(store, "ex")
                gateway.experiment_budget.start({"calls": 3, "tokens": 1000, "seconds": 1})
                gateway.trial_id = "trial"
            else:
                gateway.stop_check = lambda: "cancelled" if cancel.is_set() else None
            try:
                result = pool.submit(gateway.model, PAYLOAD)
                self.assertTrue(started.wait(2))
                if reason == "task_ended": gateway.end_marker.write_text("ended")
                else: cancel.set()
                self.assertEqual(result.result(timeout=3)[0], 502)
                self.assertEqual(gateway.failure_detail["category"], reason)
                self.assertEqual(gateway.budget.unknown_usage_calls, 1)
                self.assertEqual(gateway.model(PAYLOAD)[0], 502)
                self.assertEqual(gateway.budget.calls, 1, "Interrupted requests must never be replayed")
                if gateway.experiment_budget:
                    self.assertEqual(gateway.experiment_budget.public()["pending"], 0)
            finally:
                release.set(); server.shutdown(); server.server_close(); worker.join(); store.close()


if __name__ == "__main__": unittest.main()
