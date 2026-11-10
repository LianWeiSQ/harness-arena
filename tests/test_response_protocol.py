import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

from evo_arena.config import Settings
from evo_arena.experiment_budget import ExperimentBudget
from evo_arena.harness import TrialGateway
from evo_arena.response_protocol import decode_response, public_summary
from evo_arena.store import Store


def stream(status="completed", usage=17):
    response = {"status": status, "output": [], "usage": {"total_tokens": usage},
                "error": {"message": "PRIVATE-PROVIDER-TEXT"}}
    return (": heartbeat\r\nevent: response." + status + "\r\ndata: " +
            json.dumps({"type": "response." + status, "response": response}, indent=2).replace("\n", "\ndata: ") +
            "\r\n\r\ndata: [DONE]\r\n\r\n").encode()


class ResponseProtocolTests(unittest.TestCase):
    def test_sse_multiline_crlf_and_comments_keep_terminal_usage(self):
        response, summary = decode_response(stream(), "text/event-stream; charset=utf-8")
        self.assertEqual(summary["category"], "ok")
        self.assertEqual(response["usage"]["total_tokens"], 17)
        self.assertEqual(summary["event_count"], 1)
        self.assertNotIn("PRIVATE", json.dumps(public_summary(summary)))

    def test_missing_conflicting_or_malformed_terminal_is_not_completed(self):
        samples = [(b'data: [DONE]\n\n', "missing_terminal"),
                   (stream() + stream("failed"), "conflicting_terminal"),
                   (b'data: {broken}\n\n', "invalid_response"),
                   (b'data: []\n\n', "invalid_response"),
                   (stream().replace(b'"status": "completed"', b'"status": "incomplete"'), "invalid_response")]
        for body, category in samples:
            with self.subTest(category=category):
                response, summary = decode_response(body, "text/event-stream")
                self.assertIsNone(response)
                self.assertEqual(summary["category"], category)

    def test_receipts_are_strictly_content_free(self):
        malicious = {"category": [], "transport": {}, "terminal": "PRIVATE", "usage_state": "known",
                     "http_status": True, "error": "PRIVATE", "sha256": "PRIVATE", "bytes": 12}
        self.assertEqual(public_summary(malicious), {"category": "invalid_response", "usage_state": "known", "bytes": 12})

    def test_known_usage_survives_failed_and_incomplete_response_and_no_retry(self):
        for status in ("failed", "incomplete"):
            for streamed in (True, False):
                with self.subTest(status=status, streamed=streamed), tempfile.TemporaryDirectory() as tmp:
                    store = Store(Path(tmp)); self.addCleanup(store.close)
                    ledger = ExperimentBudget(store, "ex")
                    ledger.start({"calls": 3, "tokens": 1000, "seconds": 60})
                    gateway = TrialGateway(Settings("https://invalid/v1", "model", "secret"), lambda *x: None,
                                           experiment_budget=ledger, trial_id="trial")
                    response = MagicMock(); response.__enter__.return_value = response
                    response.headers.get.return_value = "text/event-stream" if streamed else "application/json"
                    response.read.return_value = stream(status) if streamed else json.dumps(
                        {"status": status, "usage": {"total_tokens": 17}, "output": []}).encode()
                    response.readline.side_effect = io.BytesIO(response.read.return_value).readline
                    gateway.opener = MagicMock(); gateway.opener.open.return_value = response
                    self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
                    self.assertEqual((ledger.public()["tokens"], ledger.public()["unknown_usage_calls"], ledger.public()["pending"]), (17, 0, 0))
                    self.assertEqual(gateway.budget.unknown_usage_calls, 0)
                    self.assertEqual(gateway.failure_detail["category"], "response_" + status)
                    self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
                    self.assertEqual(gateway.opener.open.call_count, 1)
                    call = store.list("evo_call")[0]
                    self.assertEqual(call["response_summary"]["category"], "response_" + status)
                    self.assertNotIn("PRIVATE", json.dumps(call))

    def test_transport_and_missing_usage_stop_and_settle_unknown(self):
        for mode in ("timeout", "truncated", "missing_usage"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp)); self.addCleanup(store.close)
                ledger = ExperimentBudget(store, "ex"); ledger.start({"calls": 3, "tokens": 1000, "seconds": 60})
                gateway = TrialGateway(Settings("https://invalid/v1", "model", "secret"), lambda *x: None,
                                       experiment_budget=ledger, trial_id="trial")
                response = MagicMock(); response.__enter__.return_value = response
                response.headers.get.return_value = "text/event-stream"
                response.read.return_value = stream(usage=None)
                response.readline.side_effect = io.BytesIO(response.read.return_value).readline
                if mode == "timeout": response.read.side_effect = TimeoutError("PRIVATE")
                if mode == "truncated": response.read.side_effect = http.client.IncompleteRead(b"PRIVATE")
                if mode in {"timeout", "truncated"}: response.readline.side_effect = response.read.side_effect
                gateway.opener = MagicMock(); gateway.opener.open.return_value = response
                self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
                self.assertEqual(ledger.public()["unknown_usage_calls"], 1)
                self.assertEqual(ledger.public()["pending"], 0)
                category = "transport_error" if mode == "truncated" else mode
                self.assertEqual(gateway.failure_detail["category"], category)
                self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
                self.assertEqual(gateway.opener.open.call_count, 1)

    def test_terminal_event_finishes_without_waiting_for_socket_eof(self):
        release, ended = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200); self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close"); self.end_headers()
                self.wfile.write(stream()); self.wfile.flush()
                release.wait(3); ended.set()
            def log_message(self, *_): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            gateway = TrialGateway(Settings(f"http://127.0.0.1:{server.server_port}", "model", "test"), lambda *x: None)
            status, _, body = gateway.model(b'{"model":"model","stream":true}')
            self.assertEqual(status, 200)
            self.assertFalse(ended.is_set(), "Gateway waited for EOF despite a complete response")
            self.assertEqual(gateway.budget.tokens, 17)
            self.assertIn('response.completed', body)
        finally:
            release.set(); server.shutdown(); server.server_close(); worker.join()

    def test_partial_transport_reply_retains_usage_without_replaying(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp)); self.addCleanup(store.close)
            ledger = ExperimentBudget(store, "ex"); ledger.start({"calls": 3, "tokens": 1000, "seconds": 60})
            gateway = TrialGateway(Settings("https://invalid/v1", "model", "secret"), lambda *x: None,
                                   experiment_budget=ledger, trial_id="trial")
            response = MagicMock(); response.__enter__.return_value = response
            response.headers.get.return_value = "text/event-stream"
            response.readline.side_effect = http.client.IncompleteRead(stream())
            gateway.opener = MagicMock(); gateway.opener.open.return_value = response
            self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
            self.assertEqual(ledger.public()["tokens"], 17)
            self.assertEqual(ledger.public()["unknown_usage_calls"], 0)
            detail = store.list("evo_call")[0]["response_summary"]
            self.assertEqual(detail["error_type"], "IncompleteRead")
            self.assertEqual(detail["read_termination"], "error")
            self.assertEqual(detail["usage_state"], "known")
            self.assertNotIn("PRIVATE", json.dumps(detail))
            self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
            self.assertEqual(gateway.opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
