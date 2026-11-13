"""Bounded Responses decoding and content-free diagnostics.

Terminal status and usage are independent: an incomplete response may still
have billable usage. A [DONE] marker alone never proves successful completion.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import time


READ_ERRORS = {"TimeoutError", "URLError", "IncompleteRead", "RemoteDisconnected", "ConnectionResetError",
               "ConnectionAbortedError", "BrokenPipeError", "SSLError", "OSError", "HTTPException"}


TERMINALS = {"response.completed": "completed", "response.failed": "failed",
             "response.incomplete": "incomplete"}
PROVIDER_CODES = {"server_error", "rate_limit_exceeded", "invalid_api_key", "authentication_error",
                  "insufficient_quota", "billing_hard_limit_reached", "model_not_found",
                  "context_length_exceeded", "content_filter", "policy_violation"}
CATEGORIES = {"ok", "invalid_response", "missing_terminal", "conflicting_terminal",
              "response_failed", "response_incomplete", "missing_usage", "http_error",
              "transport_error", "timeout", "response_too_large", "cancelled", "task_ended",
              "budget_timeout", "job_timeout"}
MESSAGES = {
    "invalid_response": "模型响应格式无效", "missing_terminal": "模型流缺少终止事件",
    "conflicting_terminal": "模型流包含冲突的终止事件", "response_failed": "模型响应以失败状态结束",
    "response_incomplete": "模型响应未完成", "missing_usage": "模型响应未报告有效用量",
    "http_error": "模型供应商 HTTP 错误", "transport_error": "模型连接或读取失败",
    "timeout": "模型响应超时", "response_too_large": "模型响应超过大小上限",
    "cancelled": "用户已停止评测，模型连接已关闭", "task_ended": "任务执行已结束，模型连接已关闭",
    "budget_timeout": "实验总时限已到，模型连接已关闭", "job_timeout": "评测进程总时限已到，模型连接已关闭",
}


def public_summary(value):
    """Allowlist again at storage/export boundaries; never include provider text."""
    value = value if isinstance(value, dict) else {}
    category = value.get("category")
    result = {"category": category if isinstance(category, str) and category in CATEGORIES else "invalid_response"}
    for key, choices in (("transport", {"json", "sse"}), ("terminal", {*TERMINALS.values(), "missing", "conflict"}),
                         ("usage_state", {"known", "unknown"}), ("read_termination", {"terminal", "eof", "error", "limit"}),
                         ("error_type", READ_ERRORS)):
        if isinstance(value.get(key), str) and value[key] in choices:
            result[key] = value[key]
    if value.get("timeout_phase") in ("connect", "response"):
        result["timeout_phase"] = value["timeout_phase"]
    if isinstance(value.get("provider_code"), str) and value["provider_code"] in PROVIDER_CODES:
        result["provider_code"] = value["provider_code"]
    for key in ("bytes", "event_count", "http_status"):
        if type(value.get(key)) is int and 0 <= value[key] <= 6_000_001:
            result[key] = value[key]
    if type(value.get("elapsed_ms")) is int and 0 <= value["elapsed_ms"] <= 43_200_000:
        result["elapsed_ms"] = value["elapsed_ms"]
    sha = value.get("sha256")
    if isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha):
        result["sha256"] = sha
    return result


def read_response(response, content_type, *, deadline=None):
    """Stop HTTP SSE at its terminal event, not at an eventual socket EOF.

    There is no default request deadline. An explicit caller deadline is checked
    between reads; task cancellation closes the transport to wake blocked reads.
    Partial transport data is retained for diagnosis and accounting, not replay.
    """
    chunks, size, data = [], 0, []
    try:
        if "text/event-stream" not in content_type.lower():
            return response.read(6_000_001), {"read_termination": "eof"}
        while size <= 6_000_000:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError()
            line = response.readline(6_000_001 - size)
            if not line:
                return b"".join(chunks), {"read_termination": "eof"}
            chunks.append(line); size += len(line)
            line = line.rstrip(b"\r\n")
            if not line:
                try:
                    event = json.loads(b"\n".join(data)) if data else None
                except (ValueError, UnicodeError):
                    event = None
                data = []
                if isinstance(event, dict) and isinstance(event.get("type"), str) and event["type"] in TERMINALS:
                    return b"".join(chunks), {"read_termination": "terminal"}
            elif line.startswith(b"data:"):
                data.append(line[5:].removeprefix(b" "))
        return b"".join(chunks), {"read_termination": "limit"}
    except (OSError, http.client.HTTPException) as error:
        partial = getattr(error, "partial", b"")
        if isinstance(partial, bytes):
            chunks.append(partial[:max(0, 6_000_001 - size)])
        kind = type(error).__name__
        timeout = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
        return b"".join(chunks), {"read_termination": "error", "category": "timeout" if timeout else "transport_error",
                                  "error_type": kind if kind in READ_ERRORS else "OSError"}


def decode_response(body: bytes, content_type: str):
    summary = {"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
               "transport": "sse" if "text/event-stream" in content_type.lower() else "json",
               "category": "ok", "terminal": "missing", "usage_state": "unknown"}
    response = None
    try:
        if len(body) > 6_000_000:
            summary["category"] = "response_too_large"
            return None, summary
        text = body.decode("utf-8")
        if summary["transport"] == "sse":
            records, data = [], []
            # SSE data can span several lines; comments and other fields are not payloads.
            for line in text.splitlines() + [""]:
                if not line:
                    payload = "\n".join(data); data = []
                    if payload.strip() not in {"", "[DONE]"}:
                        record = json.loads(payload)
                        if not isinstance(record, dict):
                            raise ValueError()
                        records.append(record)
                elif line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
            summary["event_count"] = len(records)
            terminals = [r for r in records if r.get("type") in TERMINALS]
            if len(terminals) != 1:
                summary.update(category="conflicting_terminal" if terminals else "missing_terminal",
                               terminal="conflict" if terminals else "missing")
                return None, summary
            event = terminals[0]
            summary["terminal"] = TERMINALS[event["type"]]
            response = event.get("response")
            if not isinstance(response, dict) or response.get("status") != summary["terminal"]:
                raise ValueError()
        else:
            response = json.loads(text)
            if not isinstance(response, dict):
                raise ValueError()
            status = response.get("status", "completed")
            if status not in TERMINALS.values():
                raise ValueError()
            summary["terminal"] = status
        usage = response.get("usage")
        error = response.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str) and error["code"] in PROVIDER_CODES:
            summary["provider_code"] = error["code"]
        known = isinstance(usage, dict) and type(usage.get("total_tokens")) is int and usage["total_tokens"] >= 0
        summary["usage_state"] = "known" if known else "unknown"
        if summary["terminal"] != "completed":
            summary["category"] = "response_" + summary["terminal"]
        elif not known:
            summary["category"] = "missing_usage"
        output = response.get("output", [])
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            summary["category"] = "invalid_response"
        elif any(item.get("type") == "message" and (not isinstance(item.get("content", []), list)
                 or any(not isinstance(part, dict) for part in item.get("content", []))) for item in output):
            summary["category"] = "invalid_response"
    except (UnicodeError, ValueError, TypeError):
        summary["category"] = "invalid_response"
        response = None
    return response, summary
