import hashlib
import hmac
import json
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock
from pathlib import Path

from evo_arena.business import BusinessService, cases
from evo_arena.evaluation import evaluate
from evo_arena.experiment_budget import ExperimentBudget
from evo_arena.provider import BudgetExceeded
from evo_arena.store import Store
from evo_arena.taskpacks import FIXTURES, current_pack, make_plan, materialize_business
from evo_arena.config import Settings
from evo_arena.harness import TrialGateway


def solve(service):
    """Calibration policy uses public responses, never the fault name or hidden truth."""
    _, context = service.call("inspect")
    if context["family"] == "receipt":
        for _ in range(10):
            _, state = service.call("status")
            if not state["consistent"]:
                continue
            if state["orders"] == 1:
                return
            if state["operation"] != "pending":
                service.call("submit")
    elif context["family"] == "listing":
        rows, cursor, snapshot = {}, None, None
        for _ in range(15):
            code, page = service.call("list", {"cursor": cursor, "snapshot": snapshot} if cursor else {})
            if code == 409:
                rows, cursor, snapshot = {}, None, None
                continue
            if not page["complete"]:
                service.call("refresh")
                rows, cursor, snapshot = {}, None, None
                continue
            for row in page["orders"]:
                if row["id"] not in rows or rows[row["id"]]["revision"] < row["revision"]:
                    rows[row["id"]] = row
            cursor, snapshot = page["next_cursor"], page["snapshot"]
            if not cursor:
                selected = [r for r in rows.values() if r["customer"] == context["customer"] and r["status"] == "pending"]
                service.call("report", {"ids": [r["id"] for r in selected], "total": sum(r["amount"] for r in selected)})
                return
    else:
        _, state = service.call("status")
        if state["reserved"] == 0:
            code, response = service.call("reserve")
            if response.get("permanent"):
                service.call("cancel")
                service.call("status")
                return
            _, state = service.call("status")
        for _ in range(3):
            code, response = service.call("confirm")
            if response.get("permanent"):
                service.call("release")
                service.call("cancel")
                service.call("status")
                return
            if code == 200:
                service.call("status")
                return
    raise AssertionError("Calibration policy did not terminate")


def verify_many(items):
    script = "const {verify}=require(process.argv[1]);const fs=require('node:fs');console.log(JSON.stringify(JSON.parse(fs.readFileSync(0)).map(x=>{try{return verify(x.envelope,x.case,x.key)}catch(e){return {error:e.message}}})))"
    return json.loads(subprocess.check_output(["node", "-e", script, str(FIXTURES / "verify-business.cjs")], input=json.dumps(items).encode()))


def signed(service, key="test-secret"):
    payload = json.dumps(service.ledger())
    return {"envelope": {"payload": payload, "signature": hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()},
            "case": service.case, "key": key}


class BusinessTests(unittest.TestCase):
    def test_all_splits_calibrated_by_independent_node_verifier(self):
        inputs = []
        for case in cases():
            good = BusinessService(case)
            solve(good)
            inputs.append(signed(good))
            blank = BusinessService(case)
            inputs.append(signed(blank))
            bad = BusinessService(case)
            if case["family"] == "receipt":
                for _ in range(4):
                    bad.call("submit")
                    bad.call("status")
            elif case["family"] == "listing":
                bad.call("list")
                bad.call("report", {"ids": ["O-103"], "total": 900})
            else:
                bad.call("reserve")
                bad.call("reserve")
                bad.call("confirm")
                bad.call("status")
            inputs.append(signed(bad))
        results = verify_many(inputs)
        for index, case in enumerate(cases()):
            with self.subTest(case=case["id"]):
                self.assertEqual([r.get("reward") for r in results[index * 3:index * 3 + 3]], [1, 0, 0])

    def test_ledger_binding_and_tamper_detection(self):
        service = BusinessService(cases()[0]); solve(service)
        a, b, c = [signed(service) for _ in range(3)]
        a["envelope"]["payload"] += " "
        b["key"] = "other-trial"
        c["case"] = cases()[1]
        self.assertTrue(all("error" in r for r in verify_many([a, b, c])))

    def test_cancellation_without_observing_a_permanent_failure_is_not_success(self):
        inputs = []
        for case in cases():
            if case["mechanism"] in {"confirm_rejected", "cancel_timeout", "stock_conflict"}:
                service = BusinessService(case)
                service.call("cancel"); service.call("status")
                inputs.append(signed(service))
        self.assertEqual([r["reward"] for r in verify_many(inputs)], [0, 0, 0])

    def test_cli_help_and_bad_arguments_are_local_and_no_effect(self):
        for name, commands in (("business.cjs", [["submit", "--help"], ["help"], ["--help"]]),
                               ("order.cjs", [["submit", "--help"], ["--help"]])):
            for args in commands:
                result = subprocess.run(["node", str(FIXTURES / name), *args], capture_output=True)
                self.assertEqual(result.returncode, 0)
            result = subprocess.run(["node", str(FIXTURES / name), "submit", "--unknown"], capture_output=True)
            self.assertEqual(result.returncode, 2)
        service = BusinessService(cases()[0])
        self.assertEqual(service.call("submit", {"unexpected": 1})[0], 400)
        self.assertEqual(service.ledger()["state"]["orders"], 0)

    def test_pack_coverage_plan_and_verifier_isolation(self):
        pack = current_pack()
        self.assertEqual(len(pack["cases"]), 18)
        self.assertEqual(len({c["mechanism"] for c in pack["cases"]}), 18)
        plan = make_plan(pack, "b", "c", "validation")
        self.assertEqual(len(plan["trials"]), 24)
        self.assertEqual([r["role"] for r in plan["trials"][:4]], ["baseline", "candidate", "candidate", "baseline"])
        self.assertTrue(all(r["split"] == "validation" for r in plan["trials"]))
        with self.assertRaises(ValueError):
            make_plan(pack, "b", "c", "validation", 1)
        with tempfile.TemporaryDirectory() as temp:
            materialize_business(Path(temp), "test-image", pack, {c["id"] for c in pack["cases"]})
            for case in pack["cases"]:
                path = Path(temp) / case["id"]
                self.assertNotIn("EVO_LEDGER_DIR", (path / "environment/docker-compose.yaml").read_text())
                self.assertIn("EVO_LEDGER_DIR", (path / "tests/docker-compose.yaml").read_text())
                self.assertFalse((path / "environment/case.json").exists())


class BudgetAndEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_budget_intent_unknown_and_idempotent_settlement_survive_reopen(self):
        budget = ExperimentBudget(self.store, "ex")
        budget.start({"calls": 2, "tokens": 100, "seconds": 100})
        call = budget.claim("tr", "execution", "hash")
        with self.assertRaises(BudgetExceeded): budget.claim("tr2", "execution", "hash")
        self.store.close(); self.store = Store(Path(self.temp.name))
        budget = ExperimentBudget(self.store, "ex")
        with self.assertRaises(BudgetExceeded): budget.check()
        budget.settle(call, {"total_tokens": 99}); budget.settle(call, {"total_tokens": 99})
        self.assertEqual(budget.public()["tokens"], 99)
        next_call = budget.claim("tr", "diagnosis", "hash2")
        budget.settle(next_call)
        self.assertEqual(budget.public()["unknown_usage_calls"], 1)
        self.assertEqual(budget.public()["calls"], 2)
        with self.assertRaises(BudgetExceeded): budget.check()

    def test_last_response_can_cross_cap_but_no_next_call(self):
        budget = ExperimentBudget(self.store, "ex")
        budget.start({"calls": 10, "tokens": 5, "seconds": 100})
        call = budget.claim("tr", "execution", "hash")
        budget.settle(call, {"total_tokens": 9})
        self.assertEqual(budget.public()["tokens"], 9)
        with self.assertRaises(BudgetExceeded): budget.claim("tr", "execution", "hash2")

    def test_gateway_accounts_every_dispatch_and_freezes_business_effects(self):
        budget = ExperimentBudget(self.store, "ex")
        budget.start({"calls": 2, "tokens": 100, "seconds": 100})
        gateway = TrialGateway(Settings("https://example.invalid/v1", "model", "test-only"), lambda *args: None,
                               case=cases()[0], experiment_budget=budget, trial_id="trial")
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers.get.return_value = "application/json"
        response.read.return_value = json.dumps({"usage": {"total_tokens": 20}, "output": []}).encode()
        gateway.opener = MagicMock()
        gateway.opener.open.return_value = response
        request = json.dumps({"model": "model", "input": [{"type": "function_call_output", "call_id": "tool-123", "output": "result"}]}).encode()
        self.assertEqual(gateway.model(request)[0], 200)
        self.assertEqual(budget.public()["tokens"], 20)
        response.read.return_value = b'{"output": []}'
        self.assertEqual(gateway.model(request)[0], 502)
        self.assertEqual(budget.public()["unknown_usage_calls"], 1)
        self.assertEqual(budget.public()["pending"], 0)
        self.assertEqual(gateway.model(request)[0], 429)
        self.assertEqual(gateway.opener.open.call_count, 2)
        self.assertEqual(len(self.store.list("evo_call")), 2)
        ledger = Path(self.temp.name) / "ledger"; ledger.mkdir()
        gateway.end_marker = Path(self.temp.name) / "ended"
        gateway.end_marker.touch()
        self.assertEqual(gateway.dispatch({"route": "business", "body": '{"action":"submit","args":{}}'}, ledger)[0], 409)
        self.assertEqual(gateway.business.state["orders"], 0)

    def test_malformed_provider_shapes_settle_unknown_instead_of_crashing_worker(self):
        for index, payload in enumerate(([], {"usage": [7]})):
            budget = ExperimentBudget(self.store, f"bad-response-{index}")
            budget.start({"calls": 2, "tokens": 100, "seconds": 100})
            gateway = TrialGateway(Settings("https://example.invalid/v1", "model", "test-only"), lambda *args: None,
                                   experiment_budget=budget, trial_id="trial")
            response = MagicMock()
            response.__enter__.return_value = response
            response.headers.get.return_value = "application/json"
            response.read.return_value = json.dumps(payload).encode()
            gateway.opener = MagicMock()
            gateway.opener.open.return_value = response
            self.assertEqual(gateway.model(b'{"model":"model"}')[0], 502)
            self.assertEqual(budget.public()["unknown_usage_calls"], 1)
            self.assertEqual(budget.public()["pending"], 0)
            self.assertEqual(gateway.model(b'{"model":"model"}')[0], 429)

    def test_missing_infra_duplicate_and_per_case_regression_do_not_promote(self):
        plan = make_plan(current_pack(), "b", "c", "validation")
        runs = [{**r, "status": "passed", "task_checksum": r["task"], "checks": [{"passed": True, "critical": True}],
                 "input_evidence": {"registration_verified": True}, "usage": {"unknown_usage_calls": 0},
                 "verifier_mode": "separate", "verifier_attested": True, "execution_frozen": True} for r in plan["trials"]]
        self.assertEqual(evaluate(plan, runs)["outcome"], "unchanged")
        runs[0]["task_checksum"] = "changed-task"
        self.assertEqual(evaluate(plan, runs)["outcome"], "insufficient_evidence")
        runs[0]["task_checksum"] = runs[0]["task"]
        runs[0]["status"] = "failed"
        self.assertTrue(evaluate(plan, runs)["gate_eligible"])
        self.assertEqual(evaluate(plan, runs[:-1])["outcome"], "insufficient_evidence")
        self.assertEqual(evaluate(plan, runs + [runs[0]])["outcome"], "insufficient_evidence")
        runs[4]["status"] = "error"
        self.assertEqual(evaluate(plan, runs)["outcome"], "insufficient_evidence")
        runs[4]["status"] = "failed"  # Counterbalanced second case starts with candidate.
        self.assertEqual(evaluate(plan, runs)["outcome"], "regression")


if __name__ == "__main__":
    unittest.main()
