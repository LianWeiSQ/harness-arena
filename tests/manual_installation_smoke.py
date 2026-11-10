"""Opt-in paid DSH/Pi loading check from a clean installation.

Uses ARENA_BASE_URL, ARENA_MODEL and ARENA_API_KEY from the environment.
Creates two manual versions per runner in a fresh data directory, not an
automatic evolution experiment. Upper bounds: 48 requests / 720k known tokens
across both runner experiments; unknown usage stops further dispatch.
"""
import argparse
import json
from pathlib import Path

from evo_arena.cli import find_harbor, owner_lock, StoreContext
from evo_arena.config import load_settings
from evo_arena.delivery import experiment_report
from evo_arena.evolution import VersionExperiment
from evo_arena.models import ModelProfiles
from evo_arena.versions import VersionRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings()
    harbor = find_harbor()
    if not harbor:
        raise ValueError("Install the pinned harbor extra first")
    with owner_lock(args.data_dir), StoreContext(args.data_dir) as store:
        if store.list("evo_experiment") or store.list("evo_version"):
            raise ValueError("Installation smoke requires a fresh data directory")
        registry = VersionRegistry(store, ModelProfiles(store.root, settings))
        engine = VersionExperiment(store, registry, harbor, "evoarena-harbor:arena")
        for runner in ("dsh", "pi"):
            policy = "Use the available tools to complete the task. Inspect the final business state before finishing."
            payload = {"name": runner + " installation baseline", "runner": runner, "policy": policy}
            if runner == "pi":
                payload["skills"] = [{"name": "order-check", "description": "Check order creation and recover lost receipts using the order CLI.",
                    "body": "Use the CLI help and inspect current state. After a failed order response, query status before deciding whether another write is needed."}]
            baseline = registry.register(payload)
            candidate = registry.register({**payload, "name": runner + " installation candidate", "parent": baseline["id"],
                "policy": policy + " Keep retries bounded and report what was verified."})
            ex, bound, _ = engine.create({"baseline": baseline["id"], "candidate": candidate["id"], "mode": "loading"},
                                        "installation-smoke-" + runner)
            print(json.dumps({"runner": runner, "experiment": ex["id"], "status": "started"}), flush=True)
            engine.execute(ex, bound)
            result = store.get("evo_experiment", ex["id"])
            report = experiment_report(store, ex["id"])
            (store.root / (runner + "-report.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({"runner": runner, "experiment": ex["id"], "status": result["status"],
                              "runs": [{k: r.get(k) for k in ("id", "status", "usage", "input_evidence", "version_load")} for r in result["runs"]]}), flush=True)
            if result["status"] != "completed" or len(result["runs"]) != 2 or any(r["status"] != "passed" for r in result["runs"]):
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
