"""Real order service and configurable gateway, containing no agent or verifier."""
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("ORDER_PORT", "18100"))
ORDERS = {}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Orders(Handler):
    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"service": "orders", "ready": True})
        with LOCK:
            order = ORDERS.get(self.path.removeprefix("/orders/"))
        self.reply(200 if order else 404, order or {"error": "not_found"})

    def do_POST(self):
        if self.path != "/orders":
            return self.reply(404, {"error": "not_found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError()
            data = json.loads(self.rfile.read(size))
            if not isinstance(data.get("sku"), str) or type(data.get("quantity")) is not int or data["quantity"] < 1:
                raise ValueError()
        except (ValueError, TypeError):
            return self.reply(400, {"error": "invalid_order"})
        order = {"id": uuid.uuid4().hex, "sku": data["sku"], "quantity": data["quantity"], "status": "created"}
        with LOCK:
            ORDERS[order["id"]] = order
        self.reply(201, order)


class Gateway(Handler):
    def forward(self):
        try:
            config = json.loads(Path("/config/gateway.json").read_text())
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 4096:
                raise ValueError()
            body = self.rfile.read(size) if size else None
            request = urllib.request.Request(config["upstream"] + self.path, data=body,
                headers={"Content-Type": "application/json"}, method=self.command)
            with urllib.request.urlopen(request, timeout=1) as response:
                result = json.load(response)
                self.reply(response.status, result)
        except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
            print(json.dumps({"service": "gateway", "error_type": type(exc).__name__, "detail": str(exc)}), flush=True)
            self.reply(502, {"error": "upstream_unreachable", "service": "gateway"})

    do_GET = forward
    do_POST = forward


threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", PORT), Orders).serve_forever, daemon=True).start()
print(json.dumps({"service": "orders", "listen": f"127.0.0.1:{PORT}"}), flush=True)
ThreadingHTTPServer(("0.0.0.0", 8800), Gateway).serve_forever()
