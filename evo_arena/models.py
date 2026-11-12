"""Local model profiles. Credentials never appear in public records or match snapshots."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings


class ModelProfiles:
    def __init__(self, root: Path, inherited: Settings | None):
        self.path = root / "models.json"
        self.inherited = inherited
        self.lock = threading.RLock()

    def _read(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"profiles": [], "default": "startup" if self.inherited else None}

    def _write(self, data):
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def _items(self, data):
        items = list(data["profiles"])
        if self.inherited and not any(item["id"] == "startup" for item in items):
            items.insert(0, {"id": "startup", "name": "启动配置", "model": self.inherited.model,
                            "base_url": self.inherited.base_url, "api_key": self.inherited.api_key})
        return items

    def public(self):
        with self.lock:
            data = self._read()
            items = self._items(data)
            default = data["default"]
            if default not in {item["id"] for item in items}:
                default = items[0]["id"] if items else None
            return {"default": default, "profiles": [
                {k: item[k] for k in ("id", "name", "model", "base_url")} |
                {"credential_ready": bool(item["api_key"]), "editable": True,
                 "deletable": item["id"] != "startup",
                 "source": "startup" if item["id"] == "startup" and not any(
                     p["id"] == "startup" for p in data["profiles"]) else "local"}
                for item in items]}

    def resolve(self, ident=None):
        with self.lock:
            ident = ident or self.public()["default"]
            item = next((row for row in self._items(self._read()) if row["id"] == ident), None)
            if item is None:
                raise ValueError("请先在设置中保存模型连接")
            return Settings(item["base_url"], item["model"], item["api_key"], "local profile"), item["id"], item["name"]

    def save(self, payload):
        allowed = {"id", "name", "model", "base_url", "api_key", "default"}
        if set(payload) - allowed or any(not isinstance(payload.get(k, ""), str) for k in allowed - {"default"}):
            raise ValueError("模型配置格式错误")
        if "default" in payload and type(payload["default"]) is not bool:
            raise ValueError("默认模型配置格式错误")
        ident = payload.get("id") or "model-" + uuid.uuid4().hex[:12]
        with self.lock:
            data = self._read()
            old = next((row for row in self._items(data) if row["id"] == ident), None)
            if payload.get("id") and old is None:
                raise ValueError("模型配置不存在，请刷新页面")
            values = {key: payload.get(key, "").strip() for key in ("name", "model", "base_url", "api_key")}
            values["api_key"] = values["api_key"] or (old or {}).get("api_key", "")
            parsed = urlparse(values["base_url"])
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or
                    parsed.password or parsed.query or parsed.fragment):
                raise ValueError("请填写有效的 HTTP 或 HTTPS API 地址")
            try:
                parsed.port
            except ValueError:
                raise ValueError("API 地址的端口无效") from None
            if any(not value or len(value) > 2048 or any(ord(c) < 32 for c in value) for value in values.values()):
                raise ValueError("请完整填写连接名称、模型 ID、API 地址和 API Key")
            values["base_url"] = values["base_url"].rstrip("/")
            item = {"id": ident, **values}
            data["profiles"] = [row for row in data["profiles"] if row["id"] != ident] + [item]
            if payload.get("default") or not self.public()["default"]:
                data["default"] = ident
            self._write(data)
            return {"id": ident, **self.public()}

    def remove(self, ident):
        with self.lock:
            if ident == "startup":
                raise ValueError("启动连接可以编辑，不能删除")
            data = self._read()
            if ident not in {row["id"] for row in data["profiles"]}:
                raise ValueError("该连接不可删除或已不存在")
            data["profiles"] = [row for row in data["profiles"] if row["id"] != ident]
            if data["default"] == ident:
                items = self._items(data)
                data["default"] = items[0]["id"] if items else None
            self._write(data)
            return self.public()
