"""Coverage before quality. Missing and infrastructure-failed trials never lose a match."""
from collections import defaultdict


def evaluate(plan, runs):
    expected = {row["key"]: row for row in plan["trials"]}
    valid, invalid, duplicates = {}, [], []
    task_fingerprints = defaultdict(set)
    for run in runs:
        if run.get("task_checksum"):
            task_fingerprints[run.get("task")].add(run["task_checksum"])
    for run in runs:
        key = run.get("key")
        if key in valid:
            duplicates.append(key)
            continue
        row = expected.get(key)
        if (not row or any(run.get(k) != row[k] for k in ("task", "case_checksum", "role", "version_id", "repeat"))
                or run.get("status") not in {"passed", "failed"}
                or run.get("verifier_mode") != "separate"
                or not run.get("verifier_attested") or not run.get("execution_frozen")
                or not run.get("task_checksum") or len(task_fingerprints[run.get("task")]) != 1
                or not run.get("checks") or not run.get("input_evidence", {}).get("registration_verified")
                or run.get("usage", {}).get("unknown_usage_calls", 0)):
            invalid.append(key)
        else:
            valid[key] = run
    groups = defaultdict(lambda: {"baseline": 0, "candidate": 0, "baseline_completed": 0, "candidate_completed": 0})
    violations = 0
    for run in valid.values():
        role = run["role"]
        groups[run["task"]][role] += run["status"] == "passed"
        groups[run["task"]][role + "_completed"] += 1
        if role == "candidate":
            violations += sum(c.get("critical") and not c["passed"] for c in run["checks"])
    totals = {role: sum(g[role] for g in groups.values()) for role in ("baseline", "candidate")}
    complete = len(valid) == len(expected) and not invalid and not duplicates
    if not complete:
        outcome = "insufficient_evidence"
    elif violations or any(g["candidate"] < g["baseline"] for g in groups.values()):
        outcome = "regression"
    elif totals["candidate"] > totals["baseline"]:
        outcome = "improved"
    else:
        outcome = "unchanged"
    return {"outcome": outcome, "complete": complete, "valid_trials": len(valid), "planned_trials": len(expected),
        "missing": sorted(set(expected) - set(valid)), "invalid": invalid, "duplicates": duplicates,
        "per_case": dict(groups), "successes": totals, "critical_violations": violations,
        "gate_eligible": complete and plan["mode"] == "validation" and outcome == "improved",
        "automatic_promotion": False, "statistical_significance": "not_claimed"}
