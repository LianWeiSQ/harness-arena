"""Retry only execution infrastructure faults, never an actual benchmark score."""
DEFAULT_RETRIES = 2
BACKOFF_SECONDS = (10, 30, 60, 120, 180)


def retry_reason(trial):
    if trial["status"] != "error" or trial.get("budget_exhausted"):
        return None
    failure = trial.get("provider_failure") or {}
    if failure.get("provider_code") in {"invalid_api_key", "authentication_error", "insufficient_quota",
            "billing_hard_limit_reached", "model_not_found", "context_length_exceeded", "content_filter", "policy_violation"}:
        return None
    category = failure.get("category")
    if category == "http_error":
        status = failure.get("http_status")
        if status in {408, 429, 500, 502, 503, 504}:
            return "模型服务暂时不可用" if status != 429 else "模型服务限流"
        return None
    if category in {"transport_error", "timeout", "missing_terminal", "response_failed"}:
        return "模型连接或响应中断"
    if failure:
        return None
    if (trial.get("failure_stage") in {"environment", "setup"}
            and not (trial.get("usage") or {}).get("calls")):
        return "任务环境或运行器安装失败，将重建环境"
    return None
