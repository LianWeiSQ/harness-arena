import copy
import unittest

from evo_arena.evaluation import evaluate
from evo_arena.store import digest
from evo_arena.taskpacks import current_pack, make_plan
from manual_feedback_report import summarize


def signed(value):
    value["sha256"] = digest({k: v for k, v in value.items() if k != "sha256"})
    return value


class StudyReportTests(unittest.TestCase):
    """Synthetic local report fixtures only; never create model runs or user history."""
    def setUp(self):
        pack = current_pack()
        self.study = {"id": "unit-study", "baseline": "initial", "baseline_manifest_sha256": "baseline-hash",
                      "model": "unit-model", "runner": "pi", "runtime_version": "0.82.1", "protocol_sha256": "code-hash",
                      "image_id": "unit-image", "task_pack": pack["id"], "sealed_limits": {"calls": 288, "tokens": 2400000, "seconds": 9600},
                      "arms": [{"name": name, "payload": {"mode": "improve", "baseline": "initial", "target": "policy.md",
                                "optimizer_profile": "startup", "feedback_mode": mode,
                                "limits": {"calls": 363, "tokens": 3000000, "seconds": 14400}}}
                               for name, mode in (("feedback", "development"), ("control", "none"))]}
        baseline = {"id": "initial", "parent": None, "manifest_sha256": "baseline-hash", "runner": "pi", "runtime_version": "0.82.1",
                    "model": "unit-model", "files": {"policy.md": "original-file"}}
        def report(arm=None):
            name = arm["name"] if arm else "sealed"
            child = {**baseline, "id": name + "-candidate", "parent": "initial", "manifest_sha256": name + "-hash", "files": {"policy.md": "new-file"}}
            candidate = child["id"] if arm else "initial"
            versions = {v["id"]: v for v in ([baseline, child] if arm else [baseline])}
            limits = arm["payload"]["limits"] if arm else self.study["sealed_limits"]
            plan = make_plan(pack, "initial", candidate, "validation" if arm else "report", limits=limits)
            rows = plan["trials"]
            if arm:
                rows = [{"key": c["id"] + ":0:baseline", "task": c["id"], "case_checksum": c["checksum"], "role": "baseline",
                         "repeat": 0, "version_id": "initial", "phase": "development"} for c in pack["cases"] if c["split"] == "development"] + rows
            runs = []
            for i, row in enumerate(rows):
                v = versions[row["version_id"]]
                runs.append({**row, "id": name + "-trial-" + str(i), "status": "passed", "phase": row.get("phase", "validation" if arm else None),
                    "checks": [{"key": "goal", "passed": True, "critical": True}], "verifier_attested": True, "execution_frozen": True,
                    "input_registration_verified": True, "input_evidence": {"registration_verified": True}, "verifier_mode": "separate",
                    "task_checksum": row["case_checksum"], "version_load": {"verified": True, "version_id": v["id"],
                    "manifest_sha256": v["manifest_sha256"], "file_hashes": v["files"]},
                    "usage": {"calls": 1, "tokens": 10, "unknown_usage_calls": 0}})
            cost = {"execution": {"calls": len(runs), "known_tokens": len(runs) * 10, "unknown_calls": 0, "pending_calls": 0}}
            if arm:
                cost.update({p: {"calls": 1, "known_tokens": 20, "unknown_calls": 0, "pending_calls": 0} for p in ("diagnosis", "proposal")})
            return signed({"experiment": {"id": name, "status": "completed", "mode": "improve" if arm else "report", "baseline": "initial",
                "candidate": candidate, "initial_version": "initial", "task_pack": pack["id"], "target": "policy.md",
                "feedback_mode": arm["payload"]["feedback_mode"] if arm else None}, "plan": plan, "runs": runs,
                "optimization": {"model": "unit-model", "policy": {"max_candidates": 1}, "feedback_mode": arm["payload"]["feedback_mode"],
                    "input_schema": "public-prior-v1" if name == "control" else "development-evidence-v1"} if arm else None,
                "versions": list(versions.values()), "task_pack": {"id": pack["id"]}, "runtime": {"protocol_sha256": "code-hash", "image_id": "unit-image"},
                "evaluation": evaluate(plan, [r for r in runs if r["key"] in {r["key"] for r in plan["trials"]}]),
                "budget": {"limits": limits, "calls": sum(c["calls"] for c in cost.values()), "tokens": sum(c["known_tokens"] for c in cost.values()),
                           "unknown_usage_calls": 0, "pending": 0}, "cost_by_phase": cost,
                "decision": {"status": "rejected", "reason": "unchanged"} if arm else None})
        self.arms = {a["name"]: report(a) for a in self.study["arms"]}
        self.sealed = {"sealed": report()}
        self.completion = {"plan_sha256": digest(self.study), "selected_versions": {"feedback": "initial", "control": "initial"},
                           "sealed_reports": {"feedback": "sealed", "control": "sealed"}}

    def test_complete_equal_result_keeps_initial_and_counts_shared_sealed_once(self):
        summary = summarize(self.study, self.arms, self.sealed, self.completion)
        self.assertEqual((summary["total_calls"], summary["total_known_tokens"]), (88, 920))
        self.assertEqual(summary["sealed"]["sealed"]["outcome"], "same_version")
        self.assertEqual(summary["arms"]["feedback"]["selected"], "initial")

    def test_incomplete_unsettled_missing_and_tampered_reports_are_rejected(self):
        mutations = [lambda r: r["experiment"].update(status="incomplete"),
                     lambda r: r["budget"].update(unknown_usage_calls=1),
                     lambda r: r["runs"].pop(),
                     lambda r: r["runtime"].update(image_id="different-image"),
                     lambda r: r["evaluation"].update(outcome="improved"),
                     lambda r: r["cost_by_phase"]["execution"].update(known_tokens=1),
                     lambda r: r["optimization"].update(input_schema="public-prior-v1"),
                     lambda r: r["versions"][1]["files"].update({"unapproved.md": "file-hash"})]
        for mutate in mutations:
            arms = copy.deepcopy(self.arms); mutate(arms["feedback"]); signed(arms["feedback"])
            with self.assertRaises(ValueError): summarize(self.study, arms, self.sealed, self.completion)
        self.arms["feedback"]["sha256"] = "tampered"
        with self.assertRaises(ValueError): summarize(self.study, self.arms, self.sealed, self.completion)

    def test_sealed_cannot_be_substituted_for_another_selected_version(self):
        completion = copy.deepcopy(self.completion)
        completion["selected_versions"]["feedback"] = self.arms["feedback"]["experiment"]["candidate"]
        with self.assertRaises(ValueError): summarize(self.study, self.arms, self.sealed, completion)

    def test_accepted_candidate_requires_its_own_sealed_comparison(self):
        feedback = self.arms["feedback"]
        for run in feedback["runs"]:
            if run["task"] == "receipt-validation-1" and run["role"] == "baseline":
                run["status"] = "failed"; run["checks"][0]["passed"] = False
        feedback["evaluation"] = evaluate(feedback["plan"], [r for r in feedback["runs"] if r["phase"] == "validation"])
        feedback["decision"] = {"status": "accepted", "reason": "improved"}; signed(feedback)
        candidate = feedback["versions"][1]
        sealed = copy.deepcopy(self.sealed["sealed"])
        sealed["experiment"].update(id="sealed-feedback", candidate=candidate["id"])
        sealed["versions"].append(candidate)
        sealed["plan"] = make_plan(current_pack(), "initial", candidate["id"], "report", limits=self.study["sealed_limits"])
        for run, row in zip(sealed["runs"], sealed["plan"]["trials"]):
            run.update(row)
            if row["role"] == "candidate":
                run["version_load"].update(version_id=candidate["id"], manifest_sha256=candidate["manifest_sha256"], file_hashes=candidate["files"])
        sealed["evaluation"] = evaluate(sealed["plan"], sealed["runs"]); signed(sealed)
        self.sealed["sealed-feedback"] = sealed
        self.completion["selected_versions"]["feedback"] = candidate["id"]
        self.completion["sealed_reports"]["feedback"] = "sealed-feedback"
        summary = summarize(self.study, self.arms, self.sealed, self.completion)
        self.assertEqual(summary["total_calls"], 112)
        self.assertEqual(summary["arms"]["feedback"]["outcome"], "improved")
        self.assertEqual(summary["sealed"]["sealed-feedback"]["outcome"], "unchanged")


if __name__ == "__main__":
    unittest.main()
