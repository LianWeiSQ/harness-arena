from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .arena import SeasonRunner
from .harness import ACTIVE, TASKS, HarnessMatch
from .comparison import compare_match
from .models import ModelProfiles
from .provider import Budget, ResponsesClient
from .scenario import build_image, docker
from .store import Store, digest, now
from .versions import CAPABILITIES, VersionRegistry, version_diff
from .evolution import VersionExperiment
from .improvement import ImprovementExperiment, IMPROVE_LIMITS
from .taskpacks import MODES, current_pack
from . import execution_control as control
from .trial_resources import reconcile_experiment

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

    for match in store.list("harness_match"):
        if match["status"] in ACTIVE:
            for run in match["runs"]:
                if run["status"] == "running":
                    run.update(status="error", error="服务重启，执行中断")
            match.update(status="incomplete", finished=now())
            store.update("harness_match", match["id"], match)

    control.recover(store)
    for experiment in store.list("evo_experiment"):
        if experiment["status"] in {"interrupted", "cancelled", "incomplete"}:
            reconcile_experiment(store, experiment["id"])


class Manager:
    def __init__(self, store: Store, settings, base_image: str):
        self.store, self.settings, self.base_image = store, settings, base_image
        self.lock = threading.Lock()
        self.worker = None
        self.cancel = threading.Event()
        self.current = None

    def start_evolution(self, engine, payload, key):
        with self.lock:
            previous = self.store.get("evo_request", digest(key))
            if previous:
                if previous["payload_hash"] != digest(payload):
                    return {"error": "同一请求不能更改版本"}, 409
                return self.store.get("evo_experiment", previous["experiment"]), 200
            if self.worker and self.worker.is_alive():
                return {"error": "已有实验正在运行"}, 409
            experiment, settings, _ = engine.create(payload, key)
            self.current = experiment["id"]
            self.worker = threading.Thread(target=self.run_evolution, args=(engine, experiment, settings), daemon=True)
            self.worker.start()
            return experiment, 202

    def run_evolution(self, engine, experiment, settings):
        try:
            engine.execute(experiment, settings)
        except control.ExecutionStopped:
            saved = self.store.get("evo_experiment", experiment["id"])
            if saved and saved["status"] in ACTIVE:
                control.recover(self.store)
                reconcile_experiment(self.store, experiment["id"])
        except Exception as error:
            # Unexpected controller failure must not strand an active intent.
            control.recover(self.store)
            reconcile_experiment(self.store, experiment["id"])
            self.store.event(experiment["id"], "evo.worker_interrupted", {"category": type(error).__name__})

    def resume_evolution(self, engine, ident, key):
        with self.lock:
            old = self.store.get("evo_control_request", "resume-" + digest(key))
            if old:
                if old["experiment"] != ident:
                    return {"error": "恢复请求幂等键冲突"}, 409
                return self.store.get("evo_experiment", ident), 200
            if self.worker and self.worker.is_alive():
                return {"error": "已有实验正在运行"}, 409
            experiment = self.store.get("evo_experiment", ident)
            if not experiment:
                return {"error": "实验不存在"}, 404
            settings = engine.resume_settings(experiment)
            reconcile_experiment(self.store, ident)
            experiment, _ = control.enqueue_resume(self.store, ident, key)
            self.current = ident
            self.worker = threading.Thread(target=self.run_evolution, args=(engine, experiment, settings), daemon=True)
            self.worker.start()
            return experiment, 202

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

    def start_harness(self, harness: HarnessMatch, key: str, payload=None, models=None) -> tuple[dict, int]:
        payload = {} if payload is None else payload
        with self.lock:
            request_id = digest(key)
            old = self.store.get("harness_request", request_id)
            if old:
                if old.get("payload_sha256", digest({})) != digest(payload):
                    return {"error": "同一请求不能更换模型或选手，请重新创建对局"}, 409
                return self.store.get("harness_match", old["match"]), 200
            if self.worker and self.worker.is_alive():
                return {"error": "已有一场对比正在运行"}, 409
            if set(payload) - {"runners", "model_profile"} or ("model_profile" in payload and not isinstance(payload["model_profile"], str)):
                return {"error": "对局配置格式错误"}, 400
            profile, label = None, None
            if models:
                selected, profile, label = models.resolve(payload.get("model_profile"))
                harness = HarnessMatch(harness.store, selected, harness.repo, harness.harbor, harness.image)
            if not harness.ready:
                return {"error": "请先配置模型、任务仓库和 Harbor CLI"}, 400
            match = harness.create(payload.get("runners"), profile, label)
            self.store.create("harness_request", request_id, {"match": match["id"], "payload_sha256": digest(payload)})
            self.current = None
            self.worker = threading.Thread(target=harness.execute, args=(match,), daemon=True)
            self.worker.start()
            return match, 202


def serve(store: Store, settings, host="127.0.0.1", port=8787, base_image="python:3.13-slim",
          harness_repo=None, harbor_bin=None, harness_image="evoarena-harbor:arena"):
    recover(store)
    manager = Manager(store, settings, base_image)
    harness = HarnessMatch(store, settings, harness_repo, harbor_bin, harness_image)
    models = ModelProfiles(store.root, settings)
    registry = VersionRegistry(store, models)
    evolution = VersionExperiment(store, registry, harbor_bin, harness_image)
    improvement = ImprovementExperiment(store, registry, harbor_bin, harness_image)

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
            if path == "/api/evo/diff":
                args = parse_qs(parsed.query)
                baseline = store.get("evo_version", args.get("baseline", [""])[0])
                candidate = store.get("evo_version", args.get("candidate", [""])[0])
                if not baseline or not candidate or candidate["parent"] != baseline["id"]:
                    return self.send_json({"error": "请选择有父子关系的两个版本"}, 400)
                return self.send_json({"diff": version_diff(baseline, candidate)})
            if path == "/api/evo":
                return self.send_json({"capabilities": CAPABILITIES, "models": models.public(),
                    "protocols": {**MODES, "improve": {"label": "自动改进 · 开发反馈 → 配对验证", "limits": IMPROVE_LIMITS}},
                    "projects": store.list("evo_project"),
                    "task_pack": {k: v for k, v in current_pack().items() if k in {"id", "schema", "sha256"}},
                    "ready": bool(harbor_bin and harbor_bin.is_file() and models.public()["profiles"]),
                    "versions": store.list("evo_version"),
                    "experiments": [{k: v for k, v in row.items() if k not in {"runs", "diff", "plan", "development_plan", "validation_template", "evidence_bundle", "diagnosis", "proposal"}}
                                    for row in store.list("evo_experiment")]})
            if path.startswith("/api/evo/experiments/"):
                result = store.get("evo_experiment", path.rsplit("/", 1)[-1])
                if result:
                    result["budget"] = store.get("evo_budget", result["id"])
                    result["control"] = store.get("evo_control", result["id"])
                    reasons = control.resume_reasons(store, result)
                    result["recovery"] = {"eligible": not reasons, "reasons": reasons}
                    result["resources"] = [{k: r[k] for k in ("id", "state", "error", "reconciled") if k in r}
                                           for r in store.list("evo_resource") if r["experiment"] == result["id"]]
                return self.send_json(result or {"error": "not_found"}, 200 if result else 404)
            if path.startswith("/api/evo/optimizer-outputs/"):
                result = store.get("evo_optimizer_output", path.rsplit("/", 1)[-1])
                return self.send_json(result or {"error": "not_found"}, 200 if result else 404)
            if path == "/api/evo/taskpacks":
                pack = current_pack()
                return self.send_json({"id": pack["id"], "schema": pack["schema"], "sha256": pack["sha256"],
                    "cases": [{k: c[k] for k in ("id", "family", "split", "title", "checksum")} for c in pack["cases"]],
                    "protocols": MODES})
            if path == "/api/harness":
                overview = harness.overview()
                profiles = models.public()
                overview["models"] = profiles
                overview["ready"] = bool(profiles["profiles"] and harness.repo and harness.repo.is_dir()
                                         and harness.harbor and harness.harbor.is_file())
                return self.send_json(overview)
            if path == "/api/models":
                return self.send_json(models.public())
            if path.startswith("/api/harness/matches/"):
                match = store.get("harness_match", path.rsplit("/", 1)[-1])
                if match:
                    match["comparison"] = compare_match(match, TASKS)
                return self.send_json(match or {"error": "not_found"}, 200 if match else 404)
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
            files = {"/": "evo.html", "/evo": "evo.html", "/evo.js": "evo.js", "/evo.css": "evo.css",
                     "/fixops": "index.html",
                     "/harness": "harness.html", "/harness.js": "harness.js", "/harness.css": "harness.css",
                     "/settings": "settings.html", "/settings.js": "settings.js",
                     "/app.js": "app.js", "/style.css": "style.css"}
            name = files.get(path)
            if name:
                body = (STATIC / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}[name.rsplit(".", 1)[-1]])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
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
                if not 0 < size <= (128000 if self.path == "/api/evo/versions" else 32768 if self.path == "/api/versions" else 4096):
                    raise ValueError()
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError()
                if self.path == "/api/evo/versions":
                    return self.send_json(registry.register(payload), 201)
                if self.path == "/api/evo/experiments":
                    key = self.headers.get("Idempotency-Key", "")
                    if not 1 <= len(key) <= 128:
                        raise ValueError("需要请求幂等键")
                    data, code = manager.start_evolution(improvement if payload.get("mode") == "improve" else evolution, payload, key)
                    return self.send_json(data, code)
                if self.path.startswith("/api/evo/experiments/"):
                    parts = self.path.split("/")
                    if len(parts) != 6 or payload:
                        raise ValueError("实验控制请求格式错误")
                    ident, action = parts[-2:]
                    if action == "cancel":
                        return self.send_json(control.request_cancel(store, ident))
                    if action == "resume":
                        key = self.headers.get("Idempotency-Key", "")
                        if not 1 <= len(key) <= 128:
                            raise ValueError("需要请求幂等键")
                        ex = store.get("evo_experiment", ident)
                        data, code = manager.resume_evolution(improvement if ex and ex.get("mode") == "improve" else evolution, ident, key)
                        return self.send_json(data, code)
                if self.path == "/api/models":
                    return self.send_json(models.save(payload))
                if self.path == "/api/models/delete":
                    if set(payload) != {"id"} or not isinstance(payload["id"], str):
                        raise ValueError()
                    return self.send_json(models.remove(payload["id"]))
                if self.path == "/api/harness/matches":
                    key = self.headers.get("Idempotency-Key", "")
                    if not 1 <= len(key) <= 128:
                        return self.send_json({"error": "invalid_harness_request"}, 400)
                    data, code = manager.start_harness(harness, key, payload, models)
                    return self.send_json(data, code)
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
            except ValueError as error:
                if self.path.startswith("/api/evo/") or self.path in {"/api/models", "/api/models/delete", "/api/harness/matches"}:
                    return self.send_json({"error": str(error) or "请求格式错误"}, 400)
                return self.send_json({"error": "invalid_request"}, 400)
            except TypeError:
                return self.send_json({"error": "invalid_request"}, 400)
            except OSError:
                return self.send_json({"error": "本地文件写入失败，请检查数据目录权限"}, 500)
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
