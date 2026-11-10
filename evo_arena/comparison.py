"""Head-to-head results derived from the recorded independent verdicts."""
from __future__ import annotations


def compare_match(match, task_ids):
    runners = match.get("runners") or ["dsh"]
    lookup = {(run["task"], run["runner"]): run for run in match.get("runs", [])}
    rounds = []
    points = dict.fromkeys(runners, 0)
    shared_success = []
    paired = len(runners) == 2
    for task in task_ids:
        runs = [lookup.get((task, runner)) for runner in runners]
        verdict = "pending"
        if any(run and run["status"] == "error" for run in runs):
            verdict = "error"
        elif all(run and run["status"] in {"passed", "failed"} for run in runs):
            if paired and (runs[0].get("task_checksum") != runs[1].get("task_checksum") or
                           runs[0].get("model", match["model"]) != runs[1].get("model", match["model"])):
                verdict = "mismatch"
            else:
                passed = [run["runner"] for run in runs if run["status"] == "passed"]
                verdict = "draw" if paired and len(passed) != 1 else "decided"
                for runner in passed:
                    points[runner] += 1
                if paired and len(passed) == 2:
                    shared_success.append(runs)
        winner = None
        if paired and verdict == "decided":
            winner = next(run["runner"] for run in runs if run["status"] == "passed")
        rounds.append({"task": task, "verdict": verdict, "winner": winner})
    valid = sum(row["verdict"] in {"draw", "decided"} for row in rounds)
    complete = match["status"] == "completed" and valid == len(rounds)
    outcome = "single" if not paired else "pending"
    winner = None
    if paired and complete:
        outcome = "draw" if points[runners[0]] == points[runners[1]] else "win"
        if outcome == "win":
            winner = max(points, key=points.get)
    if paired and not complete and match["status"] not in {"queued", "preparing", "running"}:
        outcome = "incomplete"

    metrics = {}
    for metric in ("seconds", "tokens"):
        totals = dict.fromkeys(runners, 0)
        count = 0
        for runs in shared_success:
            values = [run.get("seconds") if metric == "seconds" else (run.get("usage") or {}).get("tokens")
                      for run in runs]
            if any(type(value) not in (int, float) or value < 0 for value in values):
                continue
            if metric == "tokens" and any((run.get("usage") or {}).get("unknown_usage_calls", 0) for run in runs):
                continue
            count += 1
            for runner, value in zip(runners, values):
                totals[runner] += value
        lower = None
        percent = 0
        if count and len(set(totals.values())) > 1:
            lower = min(totals, key=totals.get)
            percent = round((1 - min(totals.values()) / max(totals.values())) * 100, 1)
        metrics[metric] = {"values": totals, "tasks": count, "lower": lower, "reduction_pct": percent}
    return {"runners": runners, "points": points, "rounds": rounds, "valid_tasks": valid,
            "total_tasks": len(rounds), "outcome": outcome, "winner": winner, "metrics": metrics}
