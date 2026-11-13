"""Read-only, same-evaluation scores. Never combine incomparable runs."""
from collections import Counter
import math


ACTIVE = {"queued", "preparing", "running", "cancelling"}


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def entry_view(run, entry):
    trials = [t for t in run.get("trials", []) if t.get("entry") == entry["id"]]
    counts = Counter(t["status"] for t in trials)
    expected = {(task, repeat) for task in run["selected_tasks"] for repeat in range(1, run["repeats"] + 1)}
    actual = [(t["task"], t.get("repeat", 1)) for t in trials]
    grid_complete = bool(expected) and len(actual) == len(expected) and set(actual) == expected
    binary_complete = grid_complete and all(t["status"] in {"passed", "failed"} for t in trials)
    usages = [t.get("usage") or {} for t in trials]
    tokens = sum(u["tokens"] for u in usages if number(u.get("tokens")))
    usage_known = bool(trials) and all(number(u.get("tokens")) and not u.get("unknown_usage_calls")
                                    and not t.get("usage_unconfirmed") for t, u in zip(trials, usages))
    seconds = [t["seconds"] for t in trials if number(t.get("seconds"))]
    versions = sorted({t["agent_info"]["version"] for t in trials
                       if isinstance(t.get("agent_info"), dict) and t["agent_info"].get("version")})
    return {"id": entry["id"], "runner": entry["runner"], "model": entry["model"],
            "label": entry.get("label", ""), "versions": versions, "rank": None,
            "score": 100 * counts["passed"] / len(expected) if binary_complete else None,
            "passed": counts["passed"], "failed": counts["failed"], "errors": counts["error"],
            "graded": counts["passed"] + counts["failed"], "scored": counts["scored"],
            "pending": sum(counts[s] for s in ACTIVE), "not_run": counts["not_run"] + counts["cancelled"],
            "total": len(expected), "tokens": tokens, "usage_known": usage_known,
            "calls": sum(u["calls"] for u in usages if number(u.get("calls"))),
            "mean_seconds": sum(seconds) / len(seconds) if grid_complete and len(seconds) == len(expected) else None}


def run_brief(run):
    return {"id": run["id"], "status": run["status"], "created": run["created"],
            "dataset": run["dataset"]["name"], "task_count": len(run["selected_tasks"]),
            "entry_count": len(run["entries"]), "repeats": run["repeats"],
            "combinations": [{"runner": e["runner"], "model": e["model"]} for e in run["entries"]]}


def dashboard_view(store, run_id=None):
    runs = sorted(store.list("benchmark_run"), key=lambda r: (r["created"], r["id"]), reverse=True)
    selected = next((r for r in runs if r["id"] == run_id), None) if run_id else next(
        (r for r in runs if r["status"] == "completed"), runs[0] if runs else None)
    if run_id and selected is None:
        raise ValueError("这场评测不存在")
    view = {"runs": [run_brief(r) for r in runs], "active_runs": [run_brief(r) for r in runs if r["status"] in ACTIVE],
            "run": None, "entries": [], "tasks": [], "evaluation_count": len(runs)}
    if selected is None:
        return view
    entries = [entry_view(selected, entry) for entry in selected["entries"]]
    entries.sort(key=lambda e: (e["score"] is None, -(e["score"] or 0), e["runner"], e["model"], e["id"]))
    previous, rank = None, None
    for index, entry in enumerate(entries, 1):
        if entry["score"] is not None:
            if entry["score"] != previous:
                rank = index
            entry["rank"], previous = rank, entry["score"]
    dataset = selected["dataset"]
    catalog = {t["id"]: t for t in dataset.get("tasks", [])}
    task_count = len(selected["selected_tasks"])
    dataset_size = len(catalog) or dataset.get("expected_tasks") or task_count
    view.update(entries=entries, run={**run_brief(selected), "dataset_size": dataset_size,
                "revision": dataset.get("revision"), "is_subset": task_count < dataset_size,
                "finished": selected.get("finished"), "limits": selected.get("limits", {}),
                "error": selected.get("error"),
                "graded": sum(e["graded"] + e["scored"] for e in entries),
                "total": sum(e["total"] for e in entries)})
    for task in selected["selected_tasks"]:
        cells = []
        for entry in entries:
            trials = [t for t in selected.get("trials", []) if t["entry"] == entry["id"] and t["task"] == task]
            counts = Counter(t["status"] for t in trials)
            complete = (len(trials) == selected["repeats"] and
                        {t.get("repeat", 1) for t in trials} == set(range(1, selected["repeats"] + 1)))
            if complete and counts["passed"] + counts["failed"] == len(trials):
                status = "passed" if counts["passed"] == len(trials) else "failed" if not counts["passed"] else "mixed"
            else:
                status = next((s for s in ("running", "preparing", "cancelling", "error", "queued", "cancelled", "not_run", "scored") if counts[s]), "not_run")
            cells.append({"entry": entry["id"], "status": status, "passed": counts["passed"],
                          "total": selected["repeats"], "trial_ids": [t["id"] for t in trials],
                          "rewards": [t.get("rewards") for t in trials if t["status"] == "scored"]})
        view["tasks"].append({"id": task, "category": catalog.get(task, {}).get("category", "未分类"), "cells": cells})
    return view
