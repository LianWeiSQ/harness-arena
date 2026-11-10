import http.client
import json
from pathlib import Path
import tempfile
import unittest
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
                if mode == "timeout": response.read.side_effect = TimeoutError("PRIVATE")
                if mode == "truncated": response.read.side_effect = http.client.IncompleteRead(b"PRIVATE")
                gateway.opener = MagicMock(); gateway.opener.open.return_value = response
                self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
                self.assertEqual(ledger.public()["unknown_usage_calls"], 1)
                self.assertEqual(ledger.public()["pending"], 0)
                category = "transport_error" if mode == "truncated" else mode
                self.assertEqual(gateway.failure_detail["category"], category)
                self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
                self.assertEqual(gateway.opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
