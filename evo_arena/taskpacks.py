"""Content-addressed business task pack and predeclared paired evaluation plans."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .business import cases
from .store import digest


ROOT = Path(__file__).parent
FIXTURES = ROOT / "fixtures" / "harness"
SOURCES = ["business.py", "taskpacks.py", "harness.py", "harbor_agents.py", "fixtures/harness/business.cjs",
           "fixtures/harness/relay.cjs", "fixtures/harness/verify-business.cjs"]
PROMPTS = {
    "receipt": "确保客户 C-42 的本次业务请求最终恰好创建一笔订单，并查询确认实际结果。已有一笔时不要再创建。工具回执可能不可靠，返回成功也可能只是受理。",
    "listing": "核对客户 C-42 的全部待处理订单，使用 report 保存订单 ID 清单及以分为单位的总金额。不得遗漏、重复或包含其他客户及已取消订单。以一致快照中每笔订单最新 revision 的状态为准。",
    "workflow": "完成订单 B-700 的履约：需要恰好占用 2 个库存单位并确认订单。若遇到永久业务拒绝或库存不足，取消订单并释放其所有库存占用。已有占用应复用。最后查询确认订单及库存的实际状态。",
}
MODES = {
    "smoke": {"label": "开发冒烟 · 3 题 × 双版本", "split": "development", "repeats": 1,
              "limits": {"calls": 72, "tokens": 600000, "seconds": 2400}},
    "validation": {"label": "验证对比 · 6 题 × 2 次 × 双版本", "split": "validation", "repeats": 2,
                   "limits": {"calls": 288, "tokens": 2400000, "seconds": 9600}},
    "report": {"label": "封存报告 · 6 题 × 2 次 × 双版本", "split": "sealed", "repeats": 2,
               "limits": {"calls": 288, "tokens": 2400000, "seconds": 9600}},
}


def current_pack():
    content = {"schema": "business-tools-v1", "cases": cases(), "prompts": PROMPTS,
        "source_hashes": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in SOURCES}}
    return {"id": "tp-" + digest(content)[:20], "sha256": digest(content), **content}


def verify_pack(pack):
    current = current_pack()
    if pack != current:
        raise ValueError("任务包源码已变化；请以当前任务包创建新实验")


def make_plan(pack, baseline, candidate, mode, repeats=None, limits=None):
    if mode not in MODES:
        raise ValueError("未知评测模式")
    config = MODES[mode]
    repeats = config["repeats"] if repeats is None else repeats
    if type(repeats) is not int or not config["repeats"] <= repeats <= 3:
        raise ValueError("冒烟重复次数为 1–3；验证和封存为 2–3")
    selected = [c for c in pack["cases"] if c["split"] == config["split"]]
    if mode == "smoke":
        selected = selected[::2]
    rows = []
    for index, case in enumerate(selected):
        for repeat in range(repeats):
            roles = ("baseline", "candidate") if (index + repeat) % 2 == 0 else ("candidate", "baseline")
            for role in roles:
                rows.append({"key": f"{case['id']}:{repeat}:{role}", "task": case["id"],
                    "case_checksum": case["checksum"], "family": case["family"], "split": case["split"],
                    "repeat": repeat, "role": role, "version_id": baseline if role == "baseline" else candidate})
    plan = {"task_pack": pack["id"], "mode": mode, "repeats": repeats, "trials": rows,
            "experiment_limits": limits or dict(config["limits"]),
            "limits_per_trial": {"calls": 12, "tokens": 180000, "seconds": 240},
            "seed": None, "seed_support": "provider_not_declared",
            "quality_gate": "complete coverage; no per-case success-count regression; total strictly higher; zero critical violations",
            "automatic_promotion": False}
    return {**plan, "sha256": digest(plan)}


def materialize_business(root, image, pack, selected):
    verify_pack(pack)
    for case in pack["cases"]:
        if case["id"] not in selected:
            continue
        path = root / case["id"]
        files = {
            "instruction.md": PROMPTS[case["family"]] + "\n\n使用终端命令 evo-business 操作隔离的业务账户。先查看 evo-business help 和 inspect 获取工具契约。用中文简要报告实际结果。\n",
            "task.toml": f'''version = "1.0"
artifacts = []
[task]
name = "evo/{case['id']}"
description = {json.dumps(case['title'], ensure_ascii=False)}
[agent]
timeout_sec = 240
user = "node"
[environment]
cpus = 2
memory_mb = 1024
[verifier]
timeout_sec = 45
environment_mode = "separate"
include_logs = ["test-stdout.txt"]
[verifier.environment]
cpus = 1
memory_mb = 512
''',
            "environment/Dockerfile": f"FROM {image}\nCOPY business.cjs /usr/local/bin/evo-business\nCOPY relay.cjs /usr/local/lib/evoarena-relay.cjs\nRUN chmod 755 /usr/local/bin/evo-business\nWORKDIR /work\nRUN chown node:node /work\n",
            "environment/business.cjs": (FIXTURES / "business.cjs").read_text(),
            "environment/relay.cjs": (FIXTURES / "relay.cjs").read_text(),
            "environment/docker-compose.yaml": "services:\n  main:\n    network_mode: none\n",
            "tests/Dockerfile": f"FROM {image}\nCOPY . /tests\nRUN chmod +x /tests/test.sh\n",
            "tests/docker-compose.yaml": "services:\n  main:\n    network_mode: none\n    volumes:\n      - type: bind\n        source: ${EVO_LEDGER_DIR:?verifier ledger required}\n        target: /evo-ledger\n        read_only: true\n",
            "tests/case.json": json.dumps(case),
            "tests/verify.cjs": (FIXTURES / "verify-business.cjs").read_text(),
            "tests/test.sh": "#!/bin/sh\nset -eu\nnode /tests/verify.cjs\n",
        }
        for name, content in files.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
