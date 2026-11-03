from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .arena import SeasonRunner
from .provider import Budget, ResponsesClient
from .scenario import build_image, docker
from .store import Store, digest, now

STATIC = Path(__file__).with_name("static")


def recover(store: Store) -> None:
    """Preserve evidence; ambiguous executions are never automatically replayed."""
    for run in store.list("run"):
        if run["status"] in {"preparing", "running", "verifying"}:
            name = run.get("container_name", "")
            try:
                label = docker("inspect", name, "--format", '{{index .Config.Labels "evoarena.managed"}}')
                if label == "true":
                    docker("rm", "--force", name)
            except RuntimeError:
                pass
            run.update(status="interrupted", error={"category": "recovery", "reason": "owner_process_lost"}, finished_at=now())
            store.update("run", run["id"], run)
            store.event(run["season"], "run.interrupted", {"run_id": run["id"], "reason": "owner_process_lost"})
    for season in store.list("season"):
        if season["status"] in {"queued", "preparing", "running", "reporting"}:
            season.update(status="interrupted", reason="owner_process_lost", finished_at=now())
            store.update("season", season["id"], season)
            store.event(season["id"], "season.interrupted", {"reason": "owner_process_lost"})


class Manager:
    def __init__(self, store: Store, settings, base_image: str):
        self.store, self.settings, self.base_image = store, settings, base_image
        self.lock = threading.Lock()
        self.worker = None
        self.cancel = threading.Event()
        self.current = None

    def start(self, payload: dict, key: str) -> tuple[dict, int]:
        with self.lock:
            request_id = digest(key)
            old = self.store.get("request", request_id)
            if old:
                if old["payload_sha256"] != digest(payload):
                    return {"error": "idempotency_conflict"}, 409
                return self.store.get("season", old["season"]), 200
            if self.worker and self.worker.is_alive():
                return {"error": "season_already_running"}, 409
            if self.settings is None:
                return {"error": "model_configuration_missing"}, 400
            population = payload.get("population", 2)
            if set(payload) - {"population", "rounds", "max_calls", "max_tokens", "version_ids"}:
                return {"error": "unknown_fields"}, 400
            rounds = payload.get("rounds", 2)
            calls = payload.get("max_calls", 32)
            tokens = payload.get("max_tokens", 220000)
            if any(type(v) is not int for v in [population, rounds, calls, tokens]):
                return {"error": "integer_limits_required"}, 400
            if not (1 <= calls <= 128 and 1000 <= tokens <= 2000000):
                return {"error": "invalid_budget"}, 400
            self.cancel = threading.Event()
            client = ResponsesClient(self.settings, Budget(calls, tokens))
            runner = SeasonRunner(self.store, client, self.cancel,
                                  image_builder=lambda: build_image(self.base_image))
            season = runner.create(population, rounds, payload.get("version_ids"))
            self.current = season["id"]
            self.store.create("request", request_id,
                              {"payload_sha256": digest(payload), "season": season["id"]})
            self.worker = threading.Thread(target=runner.execute, args=(season,), daemon=True)
            self.worker.start()
            return season, 202


def serve(store: Store, settings, host="127.0.0.1", port=8787, base_image="python:3.13-slim"):
    recover(store)
    manager = Manager(store, settings, base_image)

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/overview":
                return self.send_json({"seasons": store.list("season"), "versions": store.list("version"),
                    "model": settings.public() if settings else None,
                    "running": manager.current if manager.worker and manager.worker.is_alive() else None})
            if path.startswith("/api/seasons/"):
                ident = path.rsplit("/", 1)[-1]
                season = store.get("season", ident)
                if not season:
                    return self.send_json({"error": "not_found"}, 404)
                return self.send_json({"season": season, "seats": store.seats(ident),
                    "runs": [r for r in store.list("run") if r["season"] == ident],
                    "mutations": [r for r in store.list("mutation") if r["season"] == ident],
                    "decisions": [r for r in store.list("decision") if r["season"] == ident]})
            if path.startswith("/api/runs/"):
                ident = path.rsplit("/", 1)[-1]
                run = store.get("run", ident)
                return self.send_json({"run": run, "events": store.events(ident)} if run else {"error": "not_found"}, 200 if run else 404)
            if path == "/api/events":
                args = parse_qs(parsed.query)
                try:
                    return self.send_json({"events": store.events(args.get("scope", [None])[0], int(args.get("after", [0])[0]))})
                except ValueError:
                    return self.send_json({"error": "invalid_cursor"}, 400)
            files = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            name = files.get(path)
            if name:
                body = (STATIC / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}[name.rsplit(".", 1)[-1]])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return self.wfile.write(body)
            self.send_json({"error": "not_found"}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
            host_header = self.headers.get("Host", "")
            if (self.headers.get("X-EvoArena-Request") != "1" or
                host_header not in {f"127.0.0.1:{port}", f"localhost:{port}"} or
                (origin is not None and origin not in allowed_origins)):
                return self.send_json({"error": "same_origin_request_required"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= (32768 if self.path == "/api/versions" else 4096):
                    raise ValueError()
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError()
                if self.path == "/api/seasons":
                    key = self.headers.get("Idempotency-Key", "")
                    if not 1 <= len(key) <= 128:
                        return self.send_json({"error": "idempotency_key_required"}, 400)
                    data, code = manager.start(payload, key)
                    return self.send_json(data, code)
                if self.path == "/api/versions":
                    if set(payload) != {"name", "policy"}:
                        raise ValueError()
                    return self.send_json(store.version(payload["name"], payload["policy"]), 201)
                if self.path == "/api/cancel":
                    manager.cancel.set()
                    return self.send_json({"status": "cancellation_requested"})
            except (ValueError, TypeError):
                return self.send_json({"error": "invalid_request"}, 400)
            self.send_json({"error": "not_found"}, 404)

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"EvoArena: http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.cancel.set()
        server.server_close()
        if manager.worker:
            manager.worker.join(timeout=65)
