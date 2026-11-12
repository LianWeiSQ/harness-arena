from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

from .arena import SeasonRunner
from .config import load_settings
from .provider import Budget, ResponsesClient
from .scenario import DockerScenario, build_image, docker
from .store import Store


@contextmanager
def owner_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "owner.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another EvoArena process owns this data directory") from None
        yield


def find_harbor(explicit=None):
    if explicit is not None:
        return explicit.expanduser().resolve()
    # Prefer the CLI installed with this Python environment over a global tool.
    local = Path(sys.executable).parent / "harbor"
    if local.is_file() and os.access(local, os.X_OK):
        return local.resolve()
    found = shutil.which("harbor")
    return Path(found).resolve() if found else None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="evo-arena")
    sub = parser.add_subparsers(dest="command", required=True)
    from . import workspace_cli
    workspace_cli.add_commands(sub)
    for name in ["doctor", "calibrate", "calibrate-business", "demo", "serve", "export", "register"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("--data-dir", type=Path, default=Path(".evoarena"))
        cmd.add_argument("--import-config", type=Path)
        cmd.add_argument("--credentials", type=Path)
        cmd.add_argument("--profile", type=Path, help="Local JSON containing configuration-file references, never keys")
        cmd.add_argument("--base-image")
        if name == "doctor":
            cmd.add_argument("--probe-model", action="store_true")
        if name == "demo":
            cmd.add_argument("--population", type=int, default=2, choices=[2, 4])
            cmd.add_argument("--rounds", type=int, default=2, choices=range(1, 5))
            cmd.add_argument("--max-calls", type=int, default=32)
            cmd.add_argument("--max-tokens", type=int, default=220000)
            cmd.add_argument("--version", action="append", dest="version_ids")
        if name == "register":
            cmd.add_argument("--name", required=True)
            cmd.add_argument("--policy-file", type=Path, required=True)
        if name == "serve":
            cmd.add_argument("--port", type=int, default=8787)
            cmd.add_argument("--harness-repo", type=Path, help="Optional source repository for the legacy /harness maintenance task")
            cmd.add_argument("--harbor-bin", type=Path, help="Harbor CLI (defaults to this Python environment, then PATH)")
            cmd.add_argument("--harness-image", default="evoarena-harbor:arena")
        if name == "export":
            cmd.add_argument("--season", required=True)
            cmd.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"project", "experiment", "campaign", "package"}:
            return workspace_cli.run(args)
        profile = {}
        if args.profile:
            profile = json.loads(args.profile.read_text())
            if not isinstance(profile, dict) or set(profile) - {"import_config", "credentials", "base_image"}:
                raise ValueError("Profile accepts only import_config, credentials and base_image references")
            if any(not isinstance(value, str) for value in profile.values()):
                raise ValueError("Profile references must be strings")
            for field in ["import_config", "credentials"]:
                if getattr(args, field) is None and profile.get(field):
                    referenced = Path(profile[field]).expanduser()
                    setattr(args, field, referenced if referenced.is_absolute() else args.profile.resolve().parent / referenced)
        args.base_image = args.base_image or os.environ.get("EVOARENA_BASE_IMAGE") or profile.get("base_image") or "python:3.13-slim"
        if args.command == "calibrate-business":
            from .calibration import calibrate
            report = calibrate()
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] else 1
        if args.command == "register":
            with StoreContext(args.data_dir) as store:
                version = store.version(args.name, args.policy_file.read_text())
                print(json.dumps(version, ensure_ascii=False, indent=2))
            return 0
        if args.command == "doctor":
            report = {}
            try:
                report["docker"] = {"ready": True, "version": docker("info", "--format", "{{.ServerVersion}}")}
            except RuntimeError:
                report["docker"] = {"ready": False}
            try:
                settings = load_settings(args.import_config, args.credentials)
                report["model"] = settings.public()
                if args.probe_model:
                    client = ResponsesClient(settings, Budget(1, 20000))
                    text, usage = client.complete("Reply exactly EVOARENA_OK.", {"check": "connectivity"}, 64)
                    report["probe"] = {"passed": "EVOARENA_OK" in text, "usage": usage}
            except (ValueError, OSError, RuntimeError) as exc:
                report["model"] = {"ready": False, "reason": str(exc)}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["docker"]["ready"] and report["model"].get("credential_ready") and report.get("probe", {"passed": True})["passed"] else 1
        if args.command == "calibrate":
            image = build_image(args.base_image)
            with StoreContext(args.data_dir) as store, owner_lock(args.data_dir):
                import uuid
                with DockerScenario(store.root, "cal-" + uuid.uuid4().hex[:10], 41, image["image_id"]) as env:
                    print(json.dumps({"environment": image, "calibration": env.calibrate()}, indent=2))
            return 0
        if args.command == "export":
            with StoreContext(args.data_dir) as store:
                season = store.get("season", args.season)
                if not season:
                    raise ValueError("Unknown season")
                season_runs = [v for v in store.list("run") if v["season"] == args.season]
                season_mutations = [v for v in store.list("mutation") if v["season"] == args.season]
                version_ids = set(season["initial_versions"] + [r["version"] for r in season_runs])
                version_ids.update(m["child"] for m in season_mutations if m.get("child"))
                report = {"schema": "evoarena-report-v1", "season": season, "seats": store.seats(args.season),
                    "versions": [v for v in store.list("version") if v["id"] in version_ids],
                    **{kind + "s": [v for v in store.list(kind) if v["season"] == args.season]
                       for kind in ["run", "mutation", "decision"]},
                    "events": store.events(args.season, limit=1000),
                    "traces": {r["id"]: store.events(r["id"], limit=1000) for r in season_runs}}
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(str(args.output.resolve()))
            return 0
        try:
            settings = load_settings(args.import_config, args.credentials)
        except (ValueError, OSError):
            if args.command != "serve":
                raise
            settings = None
        with owner_lock(args.data_dir), StoreContext(args.data_dir) as store:
            if args.command == "serve":
                from .web import serve
                serve(store, settings, port=args.port, base_image=args.base_image,
                      harness_repo=args.harness_repo, harbor_bin=find_harbor(args.harbor_bin),
                      harness_image=args.harness_image)
                return 0
            if args.max_calls < 1 or args.max_tokens < 1000:
                raise ValueError("Budget limits must be positive")
            from .web import recover
            recover(store)
            client = ResponsesClient(settings, Budget(args.max_calls, args.max_tokens))
            runner = SeasonRunner(store, client, image_builder=lambda: build_image(args.base_image))
            season = runner.create(args.population, args.rounds, args.version_ids)
            print(json.dumps({"season": season["id"], "status": "started", "model": settings.model}), flush=True)
            result = runner.execute(season)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "completed" else 1
    except (ValueError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


class StoreContext:
    def __init__(self, root):
        self.store = Store(root)

    def __enter__(self):
        return self.store

    def __exit__(self, *_):
        self.store.close()
