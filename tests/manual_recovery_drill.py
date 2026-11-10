"""Opt-in paid Harbor/Pi crash drill. Never included in unittest discovery.

Start deliberately SIGKILLs this controller after its first confirmed Trial.
Resume uses the same DB, model binding, protocol and absolute budget deadline.
Run in a fresh data directory, never the serving application's data directory.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import threading

from evo_arena.cli import owner_lock
from evo_arena.config import load_settings
from evo_arena import execution_control as control
from evo_arena.evolution import VersionExperiment
from evo_arena.models import ModelProfiles
from evo_arena.store import Store
from evo_arena.trial_resources import reconcile_experiment
from evo_arena.versions import VersionRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["start", "resume", "cancel"])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-data", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--harbor-bin", type=Path, required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    if root == args.source_data.resolve():
        raise ValueError("Drill data must be separate from the serving data")
    profile = json.loads(args.profile.read_text())
    def reference(name):
        value = Path(profile[name]).expanduser()
        return value if value.is_absolute() else args.profile.resolve().parent / value
    settings = load_settings(reference("import_config"), reference("credentials"))
    with owner_lock(root):
        store = Store(root)
        registry = VersionRegistry(store, ModelProfiles(root, settings))
        engine = VersionExperiment(store, registry, args.harbor_bin.resolve(), "evoarena-harbor:arena")
        if args.phase in {"start", "cancel"}:
            if store.list("evo_experiment"):
                raise ValueError("Start requires a fresh drill directory")
            with sqlite3.connect(f"file:{args.source_data.resolve() / 'arena.sqlite3'}?mode=ro", uri=True) as source:
                for ident in (args.baseline, args.candidate):
                    row = source.execute("SELECT data FROM objects WHERE kind='evo_version' AND id=?", (ident,)).fetchone()
                    if not row:
                        raise ValueError("Version missing")
                    store.create("evo_version", ident, json.loads(row[0]))
            ex, binding, _ = engine.create({"baseline": args.baseline, "candidate": args.candidate, "mode": "smoke"}, "real-crash-drill")
            original_save = engine.save
            cancellation = []
            def crash(experiment, completed_run=None):
                original_save(experiment, completed_run)
                if args.phase == "start" and completed_run:
                    print(json.dumps({"experiment": ex["id"], "checkpoint": completed_run["id"], "injection": "SIGKILL after committed Trial"}), flush=True)
                    os.kill(os.getpid(), signal.SIGKILL)
                if args.phase == "cancel" and not cancellation and any(
                        event["label"] == "Harbor 启动" for r in experiment["runs"] for event in r.get("trace", [])):
                    timer = threading.Timer(1, control.request_cancel, args=(store, ex["id"]))
                    cancellation.append(timer); timer.start()
            engine.save = crash
        else:
            control.recover(store)
            ex = store.list("evo_experiment")[0]
            reconcile_experiment(store, ex["id"])
            before = {"runs": [r["id"] for r in ex["runs"]], "budget": store.get("evo_budget", ex["id"])}
            (root / "before-resume.json").write_text(json.dumps(before, indent=2))
            ex, _ = control.enqueue_resume(store, ex["id"], "real-resume-once")
            binding = engine.resume_settings(ex)
        print(json.dumps({"experiment": ex["id"], "phase": args.phase}), flush=True)
        engine.execute(ex, binding)
        result = store.get("evo_experiment", ex["id"])
        print(json.dumps({k: result.get(k) for k in ("id", "epoch", "status", "budget", "evaluation")}), flush=True)
        store.close()


if __name__ == "__main__": main()
