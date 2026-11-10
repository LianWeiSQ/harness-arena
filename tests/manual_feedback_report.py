"""Verify and summarize a completed preregistered study without model calls.

Reads the public experiment reports produced by manual_feedback_study.py.
Checks artifact consistency, not provider billing or original verifier signatures.
Incomplete studies fail closed; this tool never relaunches or selects candidates.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from evo_arena.store import digest
from manual_feedback_study import selected_version


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_hash(value, label):
    require(value.get("sha256") == digest({k: v for k, v in value.items() if k != "sha256"}), label + " hash mismatch")


def verify_report(study, report, *, arm=None, selected=None):
    verify_hash(report, "report")
    ex, plan, budget = report["experiment"], report["plan"], report["budget"]
    verify_hash(plan, "paired plan")
    is_arm = arm is not None
    require(ex["status"] == "completed" and report["evaluation"]["complete"], "Incomplete experiment")
    require(ex["baseline"] == study["baseline"] and ex["task_pack"] == study["task_pack"], "Baseline or task pack differs")
    require(report["runtime"]["protocol_sha256"] == study["protocol_sha256"] and
            report["runtime"]["image_id"] == study["image_id"], "Protocol or image differs")
    require(report["task_pack"]["id"] == study["task_pack"] and plan["task_pack"] == study["task_pack"], "Pack binding differs")
    expected_limits = arm["payload"]["limits"] if is_arm else study["sealed_limits"]
    require(budget["limits"] == plan["experiment_limits"] == expected_limits, "Budget allocation differs")
    require(budget["unknown_usage_calls"] == budget["pending"] == 0, "Unsettled usage")
    versions = {v["id"]: v for v in report["versions"]}
    initial = versions[study["baseline"]]
    require(initial["manifest_sha256"] == study["baseline_manifest_sha256"], "Initial asset differs")
    for version in versions.values():
        require(all(version[k] == study[k] for k in ("model", "runner", "runtime_version")), "Execution configuration differs")
    if is_arm:
        require(ex["mode"] == "improve" and ex["target"] == arm["payload"]["target"] and
                ex["feedback_mode"] == arm["payload"]["feedback_mode"] and ex["initial_version"] == study["baseline"], "Arm binding differs")
        optimization = report["optimization"]
        expected_schema = "public-prior-v1" if ex["feedback_mode"] == "none" else "development-evidence-v1"
        require(optimization["feedback_mode"] == ex["feedback_mode"] and optimization["input_schema"] == expected_schema,
                "Optimization input mode differs")
        candidate = versions[ex["candidate"]]
        require(candidate["parent"] == study["baseline"], "Candidate parent differs")
        changed = {name for name in initial["files"].keys() | candidate["files"].keys()
                   if initial["files"].get(name) != candidate["files"].get(name)}
        require(changed == {ex["target"]}, "Candidate changes more than the authorized file")
    else:
        require(ex["mode"] == "report" and ex["candidate"] == selected, "Sealed selection differs")
    expected_mode = "validation" if is_arm else "report"
    require(plan["mode"] == expected_mode and plan["repeats"] == 2, "Pairing mode differs")
    expected = {r["key"]: r for r in plan["trials"]}
    require(len(expected) == len(plan["trials"]) == 24, "Expected 24 distinct paired Trials")
    groups = defaultdict(set)
    for row in expected.values():
        require(row["version_id"] == ex[row["role"]], "Planned version differs")
        require(row["split"] == ("validation" if is_arm else "sealed"), "Split differs")
        groups[row["task"]].add((row["repeat"], row["role"]))
    require(len(groups) == 6 and all(g == {(n, role) for n in (0, 1) for role in ("baseline", "candidate")}
                                   for g in groups.values()), "Pairing is incomplete")
    require(Counter(r["family"] for r in expected.values()) == {"receipt": 8, "listing": 8, "workflow": 8}, "Mechanism coverage differs")
    runs = report["runs"]
    if is_arm:
        development = [r for r in runs if r["phase"] == "development"]
        require(len(development) == len({r["task"] for r in development}) == 6 and
                all(r["version_id"] == study["baseline"] and r["role"] == "baseline" for r in development), "Development coverage differs")
        paired = [r for r in runs if r["phase"] == "validation"]
        require(len(runs) == 30, "Unexpected arm Trials")
    else:
        paired = runs
    require(len(paired) == len({r["key"] for r in paired}) == 24 and {r["key"] for r in paired} == set(expected), "Missing or duplicate paired results")
    for run in runs:
        require(run["status"] in {"passed", "failed"} and run["verifier_attested"] and run["execution_frozen"]
                and run["input_registration_verified"] and run["version_load"].get("verified") and run["checks"], "Invalid Trial evidence")
        v = versions[run["version_id"]]
        require(run["version_load"]["version_id"] == v["id"] and
                run["version_load"]["manifest_sha256"] == v["manifest_sha256"] and
                run["version_load"]["file_hashes"] == v["files"], "Loading receipt differs")
        require(run["usage"]["unknown_usage_calls"] == 0, "Unknown Trial usage")
    successes, per_case, fingerprints = Counter(), {}, defaultdict(set)
    violations = 0
    for run in paired:
        row = expected[run["key"]]
        require(all(run[k] == row[k] for k in ("task", "role", "repeat", "version_id")), "Paired result binding differs")
        require(run.get("task_checksum"), "Missing materialized task fingerprint")
        fingerprints[run["task"]].add(run["task_checksum"])
        counts = per_case.setdefault(run["task"], {"baseline": 0, "candidate": 0, "baseline_completed": 0, "candidate_completed": 0})
        counts[run["role"]] += run["status"] == "passed"
        counts[run["role"] + "_completed"] += 1
        successes[run["role"]] += run["status"] == "passed"
        if run["role"] == "candidate":
            violations += sum(c["critical"] and not c["passed"] for c in run["checks"])
    require(all(len(v) == 1 for v in fingerprints.values()), "Paired environments differ")
    outcome = ("same_version" if not is_arm and ex["baseline"] == ex["candidate"] else
               "regression" if violations or any(c["candidate"] < c["baseline"] for c in per_case.values()) else
               "improved" if successes["candidate"] > successes["baseline"] else "unchanged")
    evaluation = report["evaluation"]
    require(evaluation["outcome"] == outcome and evaluation["per_case"] == per_case and
            evaluation["successes"] == successes and evaluation["critical_violations"] == violations and
            evaluation["valid_trials"] == evaluation["planned_trials"] == 24 and
            not any(evaluation[k] for k in ("missing", "invalid", "duplicates")), "Quality summary differs from Trials")
    require(evaluation["gate_eligible"] == (is_arm and outcome == "improved") and
            evaluation["automatic_promotion"] is False, "Gate declaration differs")
    cost = report["cost_by_phase"]
    require(sum(c["calls"] for c in cost.values()) == budget["calls"] and
            sum(c["known_tokens"] for c in cost.values()) == budget["tokens"] and
            not any(c["unknown_calls"] or c["pending_calls"] for c in cost.values()), "Cost totals disagree")
    require(sum(r["usage"]["calls"] for r in runs) == cost["execution"]["calls"] and
            sum(r["usage"]["tokens"] for r in runs) == cost["execution"]["known_tokens"], "Execution cost disagrees with Trials")
    if is_arm:
        require(cost.get("proposal", {}).get("calls") == 1 and 1 <= cost.get("diagnosis", {}).get("calls", 0) <= 2,
                "Search opportunity differs")
        selected = selected_version({**ex, "evaluation": evaluation, "decision": report["decision"], "budget": budget}, study["baseline"])
    return {"experiment": ex["id"], "report_sha256": report["sha256"], "candidate": ex["candidate"],
            "selected": selected, "outcome": outcome, "successes": dict(successes), "per_case": per_case,
            "valid_trials": 24, "calls": budget["calls"], "known_tokens": budget["tokens"], "cost_by_phase": cost}


def summarize(study, arms, sealed, completion):
    require(completion["plan_sha256"] == digest(study), "Completion belongs to another study")
    require({a["name"] for a in study["arms"]} == set(arms) == {"feedback", "control"}, "Two arms required")
    require({a["name"]: a["payload"]["feedback_mode"] for a in study["arms"]} ==
            {"feedback": "development", "control": "none"}, "Feedback/control interventions differ")
    require(study["arms"][0]["payload"]["limits"] == study["arms"][1]["payload"]["limits"], "Search budget differs")
    require(all(study["arms"][0]["payload"].get(k) == study["arms"][1]["payload"].get(k)
                for k in ("mode", "baseline", "target", "optimizer_profile")), "Search configuration differs")
    checked = {a["name"]: verify_report(study, arms[a["name"]], arm=a) for a in study["arms"]}
    require(all(arms["feedback"]["optimization"][k] == arms["control"]["optimization"][k]
                for k in ("model", "policy")), "Optimization model or policy differs")
    require(len({v["experiment"] for v in checked.values()}) == 2, "Arms must be independent experiments")
    selections = {k: v["selected"] for k, v in checked.items()}
    require(selections == completion["selected_versions"] and set(completion["sealed_reports"]) == set(arms), "Selected versions differ")
    require(set(completion["sealed_reports"].values()) == set(sealed), "Sealed report coverage differs")
    require(len(sealed) == len(set(selections.values())), "Same selected version must share the preregistered sealed report")
    reports = {}
    for arm, ident in completion["sealed_reports"].items():
        require(sealed[ident]["experiment"]["id"] == ident, "Sealed report identity differs")
        reports[ident] = verify_report(study, sealed[ident], selected=selections[arm])
    total = list(checked.values()) + list(reports.values())
    result = {"schema": "evo-feedback-summary-v1", "study": study["id"], "plan_sha256": digest(study),
              "status": "complete", "arms": checked, "sealed": reports, "sealed_by_arm": completion["sealed_reports"],
              "total_calls": sum(v["calls"] for v in total), "total_known_tokens": sum(v["known_tokens"] for v in total),
              "unknown_usage_calls": 0, "pending_calls": 0,
              "scope": "Artifact consistency checked; not a provider billing or verifier-signature audit. One candidate per arm; no statistical significance claim. Failed earlier studies are reported separately, not pooled."}
    return {**result, "sha256": digest(result)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    read = lambda p: json.loads(p.read_text())
    study, completion = read(args.plan), read(args.source / "complete.json")
    arms = {a["name"]: read(args.source / (a["name"] + "-report.json")) for a in study["arms"]}
    sealed = {}
    for path in args.source.glob("sealed-*-report.json"):
        report = read(path); sealed[report["experiment"]["id"]] = report
    summary = summarize(study, arms, sealed, completion)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"study": study["id"], "status": summary["status"], "calls": summary["total_calls"],
                      "known_tokens": summary["total_known_tokens"]}))


if __name__ == "__main__":
    main()
