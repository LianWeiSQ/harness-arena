"""CLI client for the same single-owner local workspace API used by the UI."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
import uuid
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .project_delivery import MAX_BYTES, read_project


def write_key(args):
    key = args.key or str(uuid.uuid4())
    print("Idempotency-Key: " + key, file=sys.stderr)
    return key


def add_commands(sub):
    for group, actions in {"project": ("list", "show", "create", "export"),
                           "experiment": ("show", "start", "report", "cancel"),
                           "campaign": ("show", "start", "cancel"), "package": ("inspect", "import")}.items():
        parser = sub.add_parser(group, help="项目工作台本地 API")
        nested = parser.add_subparsers(dest="action", required=True)
        for action in actions:
            cmd = nested.add_parser(action)
            cmd.add_argument("--server", default="http://127.0.0.1:8787")
            if action in {"show", "export", "report", "cancel"}: cmd.add_argument("id")
            if action in {"create", "start"}: cmd.add_argument("--file", required=True, type=Path, help="与页面 API 一致的 JSON 配置")
            if action in {"export", "report"}: cmd.add_argument("--output", "-o", required=True, type=Path)
            if action in {"inspect", "import"}: cmd.add_argument("archive", type=Path)
            if action == "import": cmd.add_argument("--model-profile", required=True)
            if action in {"create", "start", "import", "cancel"}: cmd.add_argument("--key", help="重试时复用同一幂等键")


def request(server, path, payload=None, key=None, *, binary=False):
    parsed = urlsplit(server)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("--server 必须是本机 HTTP 服务，例如 http://127.0.0.1:8787")
    req = Request(server.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "X-EvoArena-Request": "1", **({"Idempotency-Key": key} if key else {})})
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read(MAX_BYTES + 1)
    except HTTPError as error:
        try: message = json.loads(error.read(4096)).get("error", "请求失败")
        except (ValueError, AttributeError): message = f"HTTP {error.code}"
        raise ValueError(message) from None
    if len(raw) > MAX_BYTES: raise ValueError("响应超过大小上限")
    return raw if binary else json.loads(raw)


def run(args):
    group, action = args.command, args.action
    if group == "package":
        if args.archive.stat().st_size > MAX_BYTES: raise ValueError("项目包不能超过 8 MB")
        raw = args.archive.read_bytes()
        manifest, project, _ = read_project(raw)
        if action == "inspect": result = {"manifest": manifest, "project": project}
        else:
            key = write_key(args)
            result = request(args.server, "/api/evo/projects/import", {"archive": base64.b64encode(raw).decode(), "model_profile": args.model_profile}, key)
    elif action == "list":
        result = request(args.server, "/api/evo")["projects"]
    else:
        base = "/api/evo/" + {"project": "projects", "experiment": "experiments", "campaign": "campaigns"}[group]
        if action in {"create", "start"}:
            if args.file.stat().st_size > 16384: raise ValueError("配置文件过大")
            key = write_key(args)
            result = request(args.server, base, json.loads(args.file.read_text()), key)
        else:
            path = base + "/" + quote(args.id, safe="")
            if action == "cancel": result = request(args.server, path + "/cancel", {}, args.key or str(uuid.uuid4()))
            elif action in {"export", "report"}:
                raw = request(args.server, path + ("/bundle" if action == "export" else "/report"), binary=True)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_bytes(raw)
                result = {"output": str(args.output.resolve())}
            else: result = request(args.server, path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
