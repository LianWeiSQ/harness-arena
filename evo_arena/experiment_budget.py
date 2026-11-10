"""Durable, fail-closed model-call accounting shared by all experiment phases."""
from __future__ import annotations

import time
import uuid

from .provider import BudgetExceeded
from .store import canonical, now


def limits_checked(value, default):
    value = default if value is None else value
    maxima = {"calls": 1000, "tokens": 10000000, "seconds": 43200}
    if not isinstance(value, dict) or set(value) != set(maxima) or any(
            type(value[k]) is not int or not 1 <= value[k] <= maxima[k] for k in maxima):
        raise ValueError("预算须包含正整数 calls、tokens、seconds，且不超过平台上限")
    return dict(value)


class ExperimentBudget:
    def __init__(self, store, ident):
        self.store, self.ident = store, ident

    def start(self, limits):
        existing = self.store.get("evo_budget", self.ident)
        if existing:
            if existing["limits"] != limits:
                raise ValueError("不能变更已启动实验的预算")
            return existing
        return self.store.create("evo_budget", self.ident, {"limits": dict(limits), "started": now(),
            "deadline": time.time() + limits["seconds"], "calls": 0, "tokens": 0, "pending": 0,
            "unknown_usage_calls": 0})

    def public(self):
        return self.store.get("evo_budget", self.ident)

    def remaining_seconds(self):
        return max(0, self.public()["deadline"] - time.time())

    def check(self):
        budget = self.public()
        if (budget["pending"] or budget["unknown_usage_calls"] or budget["calls"] >= budget["limits"]["calls"]
                or budget["tokens"] >= budget["limits"]["tokens"] or time.time() >= budget["deadline"]):
            raise BudgetExceeded("实验预算耗尽或存在未结算用量")
        return budget

    def claim(self, trial, phase, request_hash):
        with self.store.lock, self.store.db:
            budget = self.check()
            budget["calls"] += 1
            budget["pending"] += 1
            ident = "call-" + uuid.uuid4().hex
            call = {"id": ident, "experiment": self.ident, "trial": trial, "phase": phase,
                    "request_hash": request_hash, "status": "pending", "created": now()}
            self.store.db.execute("INSERT INTO objects VALUES(?,?,?,?)", ("evo_call", ident, canonical(call), now()))
            self.store.db.execute("UPDATE objects SET data=? WHERE kind='evo_budget' AND id=?",
                                  (canonical(budget), self.ident))
        return ident

    def settle(self, ident, usage=None):
        value = usage.get("total_tokens") if isinstance(usage, dict) else None
        valid = type(value) is int and value >= 0
        with self.store.lock, self.store.db:
            call = self.store.get("evo_call", ident)
            if not call or call["experiment"] != self.ident:
                raise ValueError("未知模型调用")
            if call["status"] != "pending":
                if call.get("tokens") != (value if valid else None):
                    raise ValueError("调用结算冲突")
                return
            budget = self.public()
            budget["pending"] -= 1
            if valid:
                budget["tokens"] += value
            else:
                budget["unknown_usage_calls"] += 1
            call.update(status="settled" if valid else "unknown", tokens=value if valid else None, finished=now())
            for kind, key, data in (("evo_call", ident, call), ("evo_budget", self.ident, budget)):
                self.store.db.execute("UPDATE objects SET data=? WHERE kind=? AND id=?", (canonical(data), kind, key))
