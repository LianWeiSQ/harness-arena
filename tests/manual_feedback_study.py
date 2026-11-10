"""Opt-in paid study runner against a local Evo server, using a preregistered plan.

Each arm has one candidate and the same execution and search limits. A failed or
incomplete arm stops the study. Re-running this client uses the same submission
keys; it does not create replacement experiments or replay unknown calls.
"""
import argparse
import json
from pathlib import Path
import time
import urllib.request

from evo_arena.execution_control import protocol_hash
from evo_arena.store import digest


def selected_version(ex, baseline):
    if (ex["status"] != "completed" or not ex.get("candidate") or not ex.get("evaluation", {}).get("complete")
            or ex["baseline"] != baseline or ex["initial_version"] != baseline
            or ex.get("budget", {}).get("unknown_usage_calls") or ex.get("budget", {}).get("pending")):
        raise ValueError("Study arm has incomplete evidence; preserve it and stop")
    decision = ex.get("decision") or {}
    if decision.get("status") == "accepted" and decision.get("reason") == "improved":
        return ex["candidate"]
    if decision.get("status") == "rejected" and decision.get("reason") in {"unchanged", "regression"}:
        return baseline
    raise ValueError("Study arm has no completed quality decision")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if plan["protocol_sha256"] != protocol_hash():
        raise ValueError("Code differs from the preregistered protocol")
    args.output.mkdir(parents=True, exist_ok=True)
    key = digest(plan)
    def api(path, payload=None, submission=None):
        request = urllib.request.Request("http://127.0.0.1:8787" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Content-Type": "application/json", "X-EvoArena-Request": "1", "Idempotency-Key": submission or ""})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    def save(name, value):
        target = args.output / name
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(target)
    overview = api("/api/evo")
    baseline = next(v for v in overview["versions"] if v["id"] == plan["baseline"])
    if baseline["manifest_sha256"] != plan["baseline_manifest_sha256"] or overview["task_pack"]["id"] != plan["task_pack"]:
        raise ValueError("Baseline or task pack differs from the plan")
    image = next(c for c in api("/api/evo/system")["checks"] if c["name"] == "任务镜像")
    if not image["ready"] or image["detail"] != plan["image_id"]:
        raise ValueError("Task image differs from the preregistered image")
    if any(e["status"] in {"queued", "preparing", "running"} for e in overview["experiments"]):
        print("An experiment is active; the server will only accept its original request key", flush=True)
    def run(label, payload):
        ex = api("/api/evo/experiments", payload, key + ":" + label)
        save(label + "-id.json", {"experiment": ex["id"], "request_key": key + ":" + label, "payload": payload})
        print(json.dumps({"stage": label, "experiment": ex["id"], "status": ex["status"]}), flush=True)
        previous = None
        while True:
            ex = api("/api/evo/experiments/" + ex["id"])
            if (ex.get("control", {}).get("protocol_sha256") != plan["protocol_sha256"]
                    or ex["task_pack"] != plan["task_pack"] or ex["model"] != plan["model"] or ex["runner"] != plan["runner"]
                    or ex.get("image_id") and ex["image_id"] != plan["image_id"]):
                if ex["status"] in {"queued", "preparing", "running"}:
                    api("/api/evo/experiments/" + ex["id"] + "/cancel", {})
                raise ValueError("Server execution differs from the preregistered conditions")
            progress = (ex["status"], ex["phase"], sum(r["status"] in {"passed", "failed"} for r in ex["runs"]))
            if progress != previous:
                print(json.dumps({"stage": label, "status": progress[0], "phase": progress[1], "valid_trials": progress[2]}), flush=True)
                previous = progress
            if ex["status"] not in {"queued", "preparing", "running"}:
                break
            time.sleep(5)
        report = api("/api/evo/experiments/" + ex["id"] + "/report")
        save(label + "-report.json", report)
        return ex
    selections = {}
    for arm in plan["arms"]:
        ex = run(arm["name"], arm["payload"])
        selections[arm["name"]] = selected_version(ex, plan["baseline"])
        save("selection.json", selections)
    reports = {}
    for arm, selected in selections.items():
        if selected not in reports:
            ex = run("sealed-" + arm, {"mode": "report", "baseline": plan["baseline"], "candidate": selected,
                                     "repeats": 2, "limits": plan["sealed_limits"]})
            if ex["status"] != "completed" or not ex.get("evaluation", {}).get("complete"):
                raise ValueError("Sealed report incomplete; preserve it and stop")
            reports[selected] = ex["id"]
    save("complete.json", {"plan_sha256": key, "selected_versions": selections,
                           "sealed_reports": {arm: reports[v] for arm, v in selections.items()},
                           "shared_sealed_report_when_selected_version_is_identical": True})
    print("Preregistered study complete; inspect results before drawing conclusions", flush=True)


if __name__ == "__main__":
    main()
