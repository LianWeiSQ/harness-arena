'''
Host-only calibration of all business verifier cases. No model calls.
'''
import hashlib
import hmac
import json
import secrets
import subprocess
from .business import BusinessService, cases
from .taskpacks import FIXTURES, current_pack
from .store import now

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
    return json.loads(subprocess.check_output(["node", "-e", script, str(FIXTURES / "verify-business.cjs")], input=json.dumps(items).encode(), timeout=10))


def signed(service, key="test-secret"):
    payload = json.dumps(service.ledger())
    return {"envelope": {"payload": payload, "signature": hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()},
            "case": service.case, "key": key}


def calibrate():
    inputs = []
    key = secrets.token_hex(32)
    for case in cases():
        good = BusinessService(case); solve(good)
        blank = BusinessService(case)
        bad = BusinessService(case)
        if case['family'] == 'receipt':
            for _ in range(4):
                bad.call('submit'); bad.call('status')
        elif case['family'] == 'listing':
            bad.call('list'); bad.call('report', {'ids': ['O-103'], 'total': 900})
        else:
            bad.call('reserve'); bad.call('reserve'); bad.call('confirm'); bad.call('status')
        inputs.extend(signed(s, key) for s in (good, blank, bad))
    results = verify_many(inputs)
    totals = {'correct_accepted': 0, 'empty_rejected': 0, 'bad_rejected': 0}
    for i in range(len(cases())):
        totals['correct_accepted'] += results[i * 3].get('reward') == 1
        totals['empty_rejected'] += results[i * 3 + 1].get('reward') == 0
        totals['bad_rejected'] += results[i * 3 + 2].get('reward') == 0
    return {'task_pack': current_pack()['id'], 'created': now(), 'cases': len(cases()), **totals,
            'passed': all(v == len(cases()) for v in totals.values()), 'model_calls': 0,
            'scope': 'Host reference policies and independent Node verifier; not Agent performance.'}
