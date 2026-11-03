from __future__ import annotations

import difflib
import json
import threading
import time
import uuid

from .provider import Budget, BudgetExceeded, ProviderError, parse_object
from .scenario import DockerScenario, EnvironmentError, build_image
from .store import Store, digest, now

PROTOCOL = """你是受限工具环境中的订单服务修复 Agent。目标是恢复真实业务。
每次只输出一个 JSON 对象，不输出 Markdown。可用动作：
{"action":"inspect","summary":"简短操作说明"} 检查当前业务配置、服务监听和日志；
{"action":"patch_config","upstream":"http://127.0.0.1:端口","summary":"简短说明"} 修改网关上游；
{"action":"probe","summary":"简短说明"} 探测服务健康；
{"action":"submit","summary":"简短说明"} 提交独立验收。
只能使用上述动作。不能要求读取隐藏测试、修改验证器或扩大权限。
所有端口都以当前实例观测为准。说明应简短，不需要给出内部思维过程。
总共最多 3 次动作；最后一次动作完成后也会自动进行业务验收。"""

INITIAL = [
    ("行动派", "快速恢复业务。先根据实际观测确认故障和服务位置，然后做最小修改。避免重复探测已经明确的信息。"),
    ("证据派", "先检查配置、服务监听和日志，使用证据排除不合理假设。修改应尽量小，完成后验证业务恢复。"),
    ("规划派", "根据当前观测制定简短操作顺序，优先处理造成业务不可用的根因。严格控制有限的工具调用次数。"),
    ("验证派", "以可验证的恢复为目标，检查依赖和实际加载配置，修复后探测结果。不要把进程存活当作业务已恢复。"),
]


def admission(parent_results: list[dict], child_results: list[dict]) -> dict:
    if not parent_results or len(parent_results) != len(child_results):
        return {"status": "pending", "reason": "incomplete_pair_coverage"}
    for parent, child in zip(parent_results, child_results):
        if parent["seed"] != child["seed"] or parent["phase"] != child["phase"]:
            raise ValueError("Mismatched paired task instances")
        if parent["status"] != "completed" or child["status"] != "completed":
            return {"status": "pending", "reason": "paired_execution_interrupted"}
    p = sum(bool(r["passed"]) for r in parent_results)
    c = sum(bool(r["passed"]) for r in child_results)
    regressed = any(p["passed"] and not c["passed"] for p, c in zip(parent_results, child_results))
    if regressed:
        return {"status": "rejected", "reason": "task_success_regression", "parent_successes": p, "child_successes": c}
    return {"status": "accepted" if c > p else "rejected",
            "reason": "strict_success_gain" if c > p else "no_strict_success_gain",
            "parent_successes": p, "child_successes": c}


class SeasonRunner:
    def __init__(self, store: Store, client, cancel: threading.Event | None = None,
                 scenario_factory=DockerScenario, image_builder=build_image):
        self.store, self.client = store, client
        self.cancel = cancel or threading.Event()
        self.scenario_factory, self.image_builder = scenario_factory, image_builder

    def check_cancel(self):
        if self.cancel.is_set():
            raise InterruptedError("Season canceled")

    def persist_season(self, season: dict, event: str, payload: dict | None = None):
        season["budget"] = self.client.budget.public()
        self.store.update("season", season["id"], season)
        self.store.event(season["id"], event, payload or {"status": season["status"]})

    def create(self, population: int = 2, rounds: int = 2, version_ids: list[str] | None = None) -> dict:
        if population not in {2, 4} or not 1 <= rounds <= 4:
            raise ValueError("Population must be 2 or 4; rounds must be 1 to 4")
        if version_ids is not None:
            if (not isinstance(version_ids, list) or len(version_ids) not in {2, 4}
                or any(not isinstance(v, str) for v in version_ids) or len(set(version_ids)) != len(version_ids)):
                raise ValueError("Choose 2 or 4 distinct version IDs")
            versions = [self.store.get("version", ident) for ident in version_ids]
            if any(v is None for v in versions):
                raise ValueError("Unknown entrant version")
            population = len(versions)
        else:
            versions = [self.store.version(name, policy) for name, policy in INITIAL[:population]]
        ident = "s-" + uuid.uuid4().hex[:12]
        settings = self.client.settings.public() if hasattr(self.client, "settings") else {"model": "test-client"}
        season = {"id": ident, "name": "Restore the Shop", "status": "queued", "created_at": now(),
                  "mode": "smoke", "model": settings, "population": population, "round_limit": rounds,
                  "round": 0, "initial_versions": [v["id"] for v in versions],
                  "rules": {"admission": "strict_success_gain_without_paired_regression",
                            "protocol_sha256": digest(PROTOCOL),
                            "max_actions": 3, "exploration_seed": 41, "admission_seed": 73, "report_seed": 97,
                            "context_reset_between_tasks": True, "max_candidates": max(0, rounds - 1)},
                  "budget": self.client.budget.public(), "leaderboard": [], "final_report": []}
        season["rules_sha256"] = digest(season["rules"])
        self.store.create("season", ident, season)
        self.store.initialize_seats(ident, [v["id"] for v in versions])
        self.store.event(ident, "season.created", {"population": population, "mode": "smoke"})
        return season

    def run_task(self, season: dict, version: dict, seed: int, phase: str) -> dict:
        self.check_cancel()
        ident = "r-" + uuid.uuid4().hex[:12]
        run = {"id": ident, "season": season["id"], "round": season["round"], "version": version["id"],
               "phase": phase, "seed": seed, "status": "preparing", "passed": None,
               "created_at": now(), "effective_policy_sha256": digest(version["policy"]),
               "effective_prompt_sha256": digest(PROTOCOL + "\n当前策略：\n" + version["policy"]),
               "container_name": "evoarena-" + ident, "usage": {"calls": 0, "tokens": 0}}
        self.store.create("run", ident, run)
        self.store.event(season["id"], "run.preparing", {"run_id": ident, "version": version["id"], "phase": phase})
        history = []
        protocol_failure = None
        started = time.monotonic()
        try:
            with self.scenario_factory(self.store.root, ident, seed, image=season["environment"]["image_id"]) as env:
                run["calibration"] = env.calibrate()
                run["status"] = "running"
                self.store.update("run", ident, run)
                agent_started = time.monotonic()
                for step in range(3):
                    self.check_cancel()
                    call_before, tokens_before = self.client.budget.calls, self.client.budget.tokens
                    try:
                        text, usage = self.client.complete(PROTOCOL + "\n当前策略：\n" + version["policy"],
                            {"task": "网关请求返回 502。检查当前系统，修复订单业务。", "remaining_actions": 3-step,
                             "history": history}, max_output=500)
                    finally:
                        run["usage"]["calls"] += self.client.budget.calls - call_before
                        run["usage"]["tokens"] += self.client.budget.tokens - tokens_before
                    self.check_cancel()
                    try:
                        action = parse_object(text)
                        result = env.tool(action)
                    except (ValueError, TypeError, KeyError) as exc:
                        protocol_failure = type(exc).__name__ + ": invalid agent action"
                        self.store.event(ident, "action.invalid", {"step": step + 1, "reason": protocol_failure})
                        break
                    item = {"step": step + 1, "action": action, "result": result}
                    history.append(item)
                    self.store.event(ident, "action.completed", {**item, "usage": usage})
                    self.store.event(season["id"], "agent.action", {"run_id": ident, "version": version["id"],
                        "step": step + 1, "action": action.get("action"), "summary": str(action.get("summary", ""))[:300]})
                    self.persist_season(season, "budget.updated")
                    if action.get("action") == "submit":
                        break
                run["execution_seconds"] = round(time.monotonic() - agent_started, 3)
                self.check_cancel()
                run["status"] = "verifying"
                self.store.update("run", ident, run)
                verification = env.verify()
                run.update(status="completed", passed=verification["passed"] and not protocol_failure,
                           verification=verification, protocol_failure=protocol_failure)
                self.store.event(ident, "verification.completed", verification)
        except ProviderError as exc:
            run.update(status="interrupted", error={"category": "provider", "reason": str(exc), "http_status": exc.status})
            if self.client.budget.unknown_usage_calls:
                run["usage"]["tokens"] = None
        except BudgetExceeded:
            run.update(status="interrupted", error={"category": "budget", "reason": "budget_exhausted"})
        except InterruptedError:
            run.update(status="canceled", error={"category": "cancel", "reason": "user_canceled"})
        except EnvironmentError:
            run.update(status="interrupted", error={"category": "environment", "reason": "environment_operation_failed"})
        except Exception as exc:
            run.update(status="interrupted", error={"category": "runtime", "reason": type(exc).__name__})
        run["finished_at"] = now()
        run["duration_seconds"] = round(time.monotonic() - started, 3)
        self.store.update("run", ident, run)
        self.store.event(season["id"], "run.finished", {"run_id": ident, "status": run["status"], "passed": run["passed"]})
        self.persist_season(season, "budget.updated")
        return run

    def mutate(self, season: dict, parent: dict, parent_run: dict) -> dict:
        self.check_cancel()
        ident = "m-" + uuid.uuid4().hex[:12]
        mutation = {"id": ident, "season": season["id"], "round": season["round"], "parent": parent["id"],
                    "status": "generating", "created_at": now(), "child": None}
        self.store.create("mutation", ident, mutation)
        self.store.event(season["id"], "mutation.started", {"mutation_id": ident, "parent": parent["id"]})
        try:
            text, usage = self.client.complete(
                "你是 Agent 策略进化器。根据父本策略和探索轨迹提出一个可检验改进。"
                "只输出 JSON：name、policy、hypothesis，均为字符串。policy 是下一代 Agent 实际执行的新策略。"
                "保持受限动作协议、3次动作预算、模型和权限不变。不要写当前任务的具体端口或答案。"
                "即使本次成功，也可以提出减少无效操作、提升验证质量的改进；准入可能拒绝。",
                {"parent": parent, "exploration": parent_run,
                 "trace": self.store.events(parent_run["id"])}, max_output=1000)
            payload = parse_object(text)
            if set(payload) != {"name", "policy", "hypothesis"} or not all(isinstance(v, str) for v in payload.values()):
                raise ValueError("Invalid mutation shape")
            if payload["policy"].strip() == parent["policy"]:
                raise ValueError("No policy change")
            child = self.store.version(payload["name"], payload["policy"], parent["id"], payload["hypothesis"])
            mutation.update(status="evaluating", child=child["id"], usage=usage,
                diff="\n".join(difflib.unified_diff(parent["policy"].splitlines(), child["policy"].splitlines(),
                    fromfile=parent["id"], tofile=child["id"], lineterm="")))
        except (ValueError, TypeError):
            mutation.update(status="rejected", reason="invalid_or_unchanged_policy")
        except (ProviderError, BudgetExceeded) as exc:
            mutation.update(status="pending", reason=type(exc).__name__)
        self.store.update("mutation", ident, mutation)
        self.persist_season(season, "mutation.generated", {"mutation_id": ident, "status": mutation["status"], "child": mutation["child"]})
        return mutation

    def execute(self, season: dict) -> dict:
        try:
            self.check_cancel()
            season["status"] = "preparing"
            self.persist_season(season, "season.preparing")
            season["environment"] = self.image_builder()
            for round_number in range(1, season["round_limit"] + 1):
                self.check_cancel()
                season.update(status="running", round=round_number)
                self.persist_season(season, "round.started", {"round": round_number})
                results = []
                for seat in self.store.seats(season["id"]):
                    version = self.store.get("version", seat["version"])
                    results.append(self.run_task(season, version, 41, "exploration"))
                    if results[-1]["status"] != "completed":
                        raise InterruptedError("Incomplete competition coverage")
                if any(r["status"] != "completed" for r in results):
                    raise InterruptedError("Incomplete competition coverage")
                results.sort(key=lambda r: (not r["passed"], r["execution_seconds"], r["version"]))
                season["leaderboard"] = [{"rank": i+1, "version": r["version"], "passed": r["passed"],
                    "execution_seconds": r["execution_seconds"], "tokens": r["usage"]["tokens"], "run_id": r["id"]}
                    for i, r in enumerate(results)]
                self.persist_season(season, "round.ranked", {"round": round_number, "leaderboard": season["leaderboard"]})
                if round_number == season["round_limit"]:
                    break
                parent_run = results[(round_number - 1) % min(2, len(results))]
                parent = self.store.get("version", parent_run["version"])
                mutation = self.mutate(season, parent, parent_run)
                if mutation["status"] == "pending":
                    raise InterruptedError("Candidate generation incomplete")
                if mutation["status"] == "rejected":
                    continue
                child = self.store.get("version", mutation["child"])
                paired_parent = self.run_task(season, parent, 73, "admission")
                paired_child = self.run_task(season, child, 73, "admission")
                decision = {"id": "d-" + mutation["id"][2:], "season": season["id"], "mutation": mutation["id"],
                    "parent": parent["id"], "child": child["id"], "parent_run": paired_parent["id"],
                    "child_run": paired_child["id"], **admission([paired_parent], [paired_child])}
                if decision["status"] == "accepted":
                    target = next(s for s in self.store.seats(season["id"]) if s["version"] == results[-1]["version"])
                    decision["replaced_version"] = target["version"]
                    self.store.promote(season["id"], target["slot"], target["version"], target["revision"], child["id"], decision)
                else:
                    self.store.create("decision", decision["id"], decision)
                    self.store.event(season["id"], "child." + decision["status"], decision)
                mutation.update(status=decision["status"], reason=decision["reason"], decision=decision["id"])
                self.store.update("mutation", mutation["id"], mutation)
                if decision["status"] == "pending":
                    raise InterruptedError("Admission coverage incomplete")
            season["status"] = "reporting"
            self.persist_season(season, "report.started")
            finalists = sorted(set(season["initial_versions"] + [s["version"] for s in self.store.seats(season["id"])]))
            for ident in finalists:
                result = self.run_task(season, self.store.get("version", ident), 97, "final_report")
                season["final_report"].append({"version": ident, "run_id": result["id"],
                    "passed": result["passed"], "status": result["status"],
                    "initial": ident in season["initial_versions"]})
                if result["status"] != "completed":
                    raise InterruptedError("Final report incomplete")
            season["status"] = "completed"
        except InterruptedError as exc:
            season.update(status="canceled" if self.cancel.is_set() else "interrupted", reason=str(exc))
        except Exception as exc:
            season.update(status="interrupted", reason=type(exc).__name__)
        if season["status"] in {"interrupted", "canceled"}:
            for mutation in self.store.list("mutation"):
                if mutation["season"] == season["id"] and mutation["status"] in {"generating", "evaluating"}:
                    mutation.update(status="pending", reason="season_" + season["status"])
                    self.store.update("mutation", mutation["id"], mutation)
        season["finished_at"] = now()
        self.persist_season(season, "season.finished")
        return season
