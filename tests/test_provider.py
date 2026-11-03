import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from evo_arena.config import Settings
from evo_arena.provider import Budget, BudgetExceeded, ProviderError, ResponsesClient


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.mode = "ok"
        self.requests = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                test.requests.append(self.path)
                self.rfile.read(int(self.headers["Content-Length"]))
                if test.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "/must-not-receive-credential")
                    self.end_headers()
                    return
                if test.mode == "error":
                    self.send_response(402)
                    self.end_headers()
                    self.wfile.write(b"SECRET_SHOULD_NOT_APPEAR_IN_ERROR")
                    return
                payload = {"status": "completed", "output_text": '{"action":"submit"}'}
                if test.mode != "missing_usage":
                    payload["usage"] = {"total_tokens": 12}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        settings = Settings(f"http://127.0.0.1:{self.server.server_port}/v1", "test-only", "fake-test-secret")
        self.client = ResponsesClient(settings, Budget(5, 100))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_success_reports_actual_usage(self):
        text, usage = self.client.complete("test", {})
        self.assertIn("submit", text)
        self.assertEqual(usage["total_tokens"], 12)
        self.assertEqual(self.client.budget.tokens, 12)

    def test_provider_error_is_sanitized_and_unknown_usage_stops_dispatch(self):
        self.mode = "error"
        with self.assertRaises(ProviderError) as caught:
            self.client.complete("test", {})
        self.assertEqual(caught.exception.status, 402)
        self.assertNotIn("SECRET", str(caught.exception))
        with self.assertRaises(BudgetExceeded):
            self.client.complete("test", {})
        self.assertEqual(len(self.requests), 1)

    def test_redirect_does_not_forward_model_credentials(self):
        self.mode = "redirect"
        with self.assertRaises(ProviderError):
            self.client.complete("test", {})
        self.assertEqual(self.requests, ["/v1/responses"])

    def test_unknown_usage_is_not_counted_as_zero_and_retried(self):
        self.mode = "missing_usage"
        with self.assertRaises(ProviderError):
            self.client.complete("test", {})
        self.assertEqual(self.client.budget.unknown_usage_calls, 1)
        with self.assertRaises(BudgetExceeded):
            self.client.complete("test", {})


if __name__ == "__main__":
    unittest.main()
