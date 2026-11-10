"""Deterministic business faults. State stays on the host, outside the agent sandbox.

Progress is driven by public tool calls, never wall-clock sleeps. Tests and the
optimizer use the same public API; the independent verifier reads a signed audit.
"""
from __future__ import annotations

from copy import deepcopy

from .store import digest


MECHANISMS = {
    "receipt": {
        "development": ["commit_timeout", "async_commit"],
        "validation": ["reject_before_commit", "stale_status"],
        "sealed": ["existing_order", "async_reject"],
    },
    "listing": {
        "development": ["pagination", "index_incomplete"],
        "validation": ["opaque_cursor", "overlapping_pages"],
        "sealed": ["snapshot_changed", "cancellation_update"],
    },
    "workflow": {
        "development": ["already_reserved", "confirm_transient"],
        "validation": ["reserve_timeout", "confirm_rejected"],
        "sealed": ["cancel_timeout", "stock_conflict"],
    },
}
TITLES = {"receipt": "下单结果核对", "listing": "完整订单对账", "workflow": "订单履约与补偿"}


def cases():
    result = []
    for family, splits in MECHANISMS.items():
        for split, mechanisms in splits.items():
            for number, mechanism in enumerate(mechanisms, 1):
                case = {"id": f"{family}-{split}-{number}", "family": family,
                        "split": split, "mechanism": mechanism, "title": TITLES[family]}
                case["checksum"] = digest(case)
                result.append(case)
    return result


class BusinessService:
    def __init__(self, case):
        self.case = deepcopy(case)
        self.family, self.mechanism = case["family"], case["mechanism"]
        self.calls, self.audit = {}, []
        self.state = {"orders": 1 if self.mechanism == "existing_order" else 0,
            "operation": None, "status_reads": 0, "verified": False,
            "reserved": 2 if self.mechanism == "already_reserved" else 0,
            "order_status": "pending", "violations": [], "report": None}
        self.rows = [
            {"id": "O-101", "customer": "C-42", "status": "pending", "amount": 1200, "revision": 1},
            {"id": "O-102", "customer": "C-42", "status": "pending", "amount": 3500, "revision": 1},
            {"id": "O-103", "customer": "C-99", "status": "pending", "amount": 900, "revision": 1},
            {"id": "O-104", "customer": "C-42", "status": "cancelled", "amount": 800, "revision": 1},
            {"id": "O-105", "customer": "C-42", "status": "pending", "amount": 600, "revision": 1},
        ]
        # Different business answers across splits; fault mechanisms remain the
        # reason cases differ, rather than merely renaming an identical task.
        base = 200 + int(case["checksum"][:6], 16) % 7000
        for index, row in enumerate(self.rows):
            row.update(id=f"O-{base + index}", amount=base * (index + 1) + 37)
        if self.mechanism == "cancellation_update":
            self.rows.append({**self.rows[1], "status": "cancelled", "revision": 2})
        self.snapshot = "s1"
        self.refreshed = False
        self.complete_scan = False
        self.seen_pages = set()

    def call(self, action, args=None):
        args = args if args is not None else {}
        allowed = {"receipt": {"submit", "status"},
                   "listing": {"list", "refresh", "report"},
                   "workflow": {"status", "reserve", "confirm", "release", "cancel"}}[self.family]
        if action == "inspect" and args == {}:
            return 200, {"family": self.family, "customer": "C-42", "order": "B-700", "units": 2,
                "amount_unit": "cents", "actions": sorted(allowed),
                "contract": "A timeout does not prove failure. status.consistent=false is not authoritative. "
                    "202 is pending, not complete. list.complete only describes the index; follow next_cursor "
                    "until null with one snapshot. On snapshot mismatch restart. Keep the highest revision "
                    "per order. HTTP 409/422 permanent errors cannot be fixed by blind retries. "
                    "reserve and submit are non-idempotent; release frees the whole allocation."}
        keys = {"list": {"cursor", "snapshot"}, "report": {"ids", "total"}}.get(action, set())
        if action not in allowed or not isinstance(args, dict) or set(args) - keys:
            return 400, {"error": "Unsupported action or arguments; no effect"}
        if action == "list" and any(not isinstance(v, str) for v in args.values()):
            return 400, {"error": "cursor and snapshot must be strings"}
        if action == "report" and (set(args) != {"ids", "total"} or
                not isinstance(args["ids"], list) or not all(isinstance(x, str) for x in args["ids"]) or
                type(args["total"]) is not int):
            return 400, {"error": "report needs ids[] and integer total in cents"}
        self.calls[action] = self.calls.get(action, 0) + 1
        code, result = getattr(self, "_" + self.family)(action, args)
        self.audit.append({"action": action, "args": deepcopy(args), "http": code, "result": deepcopy(result)})
        return code, result

    def _receipt(self, action, args):
        s, m = self.state, self.mechanism
        if action == "status":
            s["status_reads"] += 1
            if s["operation"] == "pending":
                if m == "async_reject" and self.calls.get("submit") == 1:
                    s["operation"] = "failed"
                else:
                    s["orders"] += 1
                    s["operation"] = "completed"
            if m == "stale_status" and self.calls.get("submit") and s["status_reads"] <= 2:
                return 200, {"orders": 0, "consistent": False, "retry": "query status again"}
            s["verified"] = s["orders"] == 1 and s["operation"] != "pending"
            return 200, {"orders": s["orders"], "consistent": True, "operation": s["operation"]}
        s["verified"] = False
        if m == "reject_before_commit" and self.calls["submit"] == 1:
            return 503, {"error": "Service unavailable; outcome unknown"}
        if m in {"async_commit", "async_reject"}:
            if s["operation"] == "pending":
                s["violations"].append("duplicate_pending_submission")
            s["operation"] = "pending"
            return 202, {"operation": "pending", "query": "status"}
        s["orders"] += 1
        if m in {"commit_timeout", "stale_status"} and self.calls["submit"] == 1:
            return 504, {"error": "Receipt timed out; outcome unknown"}
        return 201, {"created": True}

    def _listing(self, action, args):
        m = self.mechanism
        if action == "report":
            self.state["report"] = deepcopy(args)
            return 200, {"saved": True}  # Never leak hidden correctness feedback.
        if action == "refresh":
            self.refreshed = True
            self.snapshot = "s2"
            self.seen_pages.clear()
            self.complete_scan = False
            return 200, {"snapshot": self.snapshot, "query": "list from first page"}
        cursor = args.get("cursor", "")
        cursors = ["", "page-B7", "page-Q9"] if m == "opaque_cursor" else ["", "next-A", "next-B"]
        if cursor not in cursors:
            return 400, {"error": "Invalid cursor; use next_cursor from response"}
        if m == "snapshot_changed" and cursor and self.snapshot == "s1":
            self.snapshot = "s2"
            self.seen_pages.clear()
            self.complete_scan = False
            return 409, {"error": "Snapshot changed; restart from first page", "snapshot": self.snapshot}
        if cursor and args.get("snapshot") != self.snapshot:
            return 409, {"error": "Pass snapshot returned by first page; restart if changed"}
        index = cursors.index(cursor)
        rows = self.rows
        if m == "snapshot_changed" and self.snapshot == "s2":
            rows = rows + [{"id": "O-106", "customer": "C-42", "status": "pending", "amount": 400, "revision": 1}]
        pages = [rows[:2], rows[2:4], rows[4:]]
        if m == "overlapping_pages":
            pages = [rows[:2], rows[1:4], rows[3:]]
        complete = m != "index_incomplete" or self.refreshed
        if not complete:
            return 200, {"orders": rows[:1], "complete": False, "snapshot": self.snapshot,
                         "next_cursor": None, "retry": "refresh the index and list again"}
        self.seen_pages.add(index)
        self.complete_scan = self.seen_pages == {0, 1, 2}
        return 200, {"orders": deepcopy(pages[index]), "complete": True, "snapshot": self.snapshot,
                     "next_cursor": cursors[index + 1] if index < 2 else None}

    def _workflow(self, action, args):
        s, m = self.state, self.mechanism
        if action == "status":
            s["verified"] = True
            return 200, {"order_status": s["order_status"], "reserved": s["reserved"], "consistent": True}
        s["verified"] = False
        if action == "reserve":
            if m == "stock_conflict":
                return 409, {"error": "No stock available", "permanent": True}
            s["reserved"] += 2
            if s["reserved"] > 2:
                s["violations"].append("duplicate_reservation")
            if m == "reserve_timeout" and self.calls["reserve"] == 1:
                return 504, {"error": "Reservation receipt lost; outcome unknown"}
            return 200, {"reserved": s["reserved"]}
        if action == "confirm":
            if m == "confirm_transient" and self.calls["confirm"] == 1:
                return 503, {"error": "Temporary confirmation failure", "retryable": True}
            if m in {"confirm_rejected", "cancel_timeout"}:
                return 422, {"error": "Order rejected permanently", "permanent": True}
            if s["reserved"] != 2 or s["order_status"] == "cancelled":
                return 409, {"error": "Need exactly two reserved units and a pending order"}
            s["order_status"] = "confirmed"
        elif action == "release":
            if s["order_status"] == "confirmed":
                s["violations"].append("released_confirmed_allocation")
            s["reserved"] = 0
        elif action == "cancel":
            if s["order_status"] == "confirmed":
                s["violations"].append("cancelled_confirmed_order")
            s["order_status"] = "cancelled"
            if m == "cancel_timeout" and self.calls["cancel"] == 1:
                return 504, {"error": "Cancellation receipt lost; outcome unknown"}
        return 200, {"order_status": s["order_status"], "reserved": s["reserved"]}

    def ledger(self):
        rows = deepcopy(self.rows)
        if self.mechanism == "snapshot_changed":
            rows.append({"id": "O-106", "customer": "C-42", "status": "pending", "amount": 400, "revision": 1})
        return {"schema": "business-audit-v1", "case": self.case, "state": deepcopy(self.state),
                "truth": rows, "complete_scan": self.complete_scan, "audit": deepcopy(self.audit)}
