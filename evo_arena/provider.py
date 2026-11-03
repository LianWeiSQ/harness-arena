from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import Settings


class ProviderError(RuntimeError):
    def __init__(self, reason: str, status: int | None = None):
        super().__init__(reason)
        self.status = status


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_calls: int = 24
    max_tokens: int = 150000
    calls: int = 0
    tokens: int = 0
    unknown_usage_calls: int = 0

    def claim(self) -> None:
        if self.unknown_usage_calls or self.calls >= self.max_calls or self.tokens >= self.max_tokens:
            raise BudgetExceeded("Season model budget exhausted")
        self.calls += 1

    def record(self, usage: dict) -> None:
        value = usage.get("total_tokens")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            self.unknown_usage_calls += 1
            raise ProviderError("Provider did not report valid token usage; stopping budgeted execution")
        self.tokens += value

    def public(self) -> dict:
        return {"calls": self.calls, "tokens": self.tokens,
                "max_calls": self.max_calls, "max_tokens": self.max_tokens,
                "unknown_usage_calls": self.unknown_usage_calls}


class ResponsesClient:
    def __init__(self, settings: Settings, budget: Budget, timeout: float = 60):
        self.settings, self.budget, self.timeout = settings, budget, timeout
        # Model credentials are sent only to the explicitly configured endpoint.
        self.opener = urllib.request.build_opener(NoRedirect())

    def complete(self, instructions: str, payload: dict, max_output: int = 700) -> tuple[str, dict]:
        self.budget.claim()
        body = json.dumps({"model": self.settings.model, "instructions": instructions,
                           "input": json.dumps(payload, ensure_ascii=False),
                           "max_output_tokens": max_output}, ensure_ascii=False).encode()
        req = urllib.request.Request(self.settings.base_url + "/responses", data=body,
                headers={"Authorization": "Bearer " + self.settings.api_key,
                         "Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            self.budget.unknown_usage_calls += 1
            raise ProviderError(f"Model provider HTTP {exc.code}", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            self.budget.unknown_usage_calls += 1
            raise ProviderError("Model provider transport or response error") from None
        usage = data.get("usage") or {}
        self.budget.record(usage)
        if data.get("status") not in {None, "completed"}:
            raise ProviderError("Model response did not complete")
        text = data.get("output_text")
        if not isinstance(text, str):
            text = "\n".join(c["text"] for row in data.get("output") or []
                for c in row.get("content") or [] if isinstance(c.get("text"), str))
        if not text.strip():
            raise ProviderError("Model returned no text")
        return text, {k: usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_object(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1])
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected one JSON object")
    return value
