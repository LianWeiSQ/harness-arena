from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    source: str = "environment"

    def public(self) -> dict:
        return {"model": self.model, "endpoint_host": urlparse(self.base_url).hostname,
                "credential_ready": bool(self.api_key), "source": self.source}


def load_settings(import_config: Path | None = None, credentials: Path | None = None) -> Settings:
    base_url = os.environ.get("ARENA_BASE_URL", "")
    model = os.environ.get("ARENA_MODEL", "")
    key_env = "ARENA_API_KEY"
    source = "environment"
    if import_config:
        import yaml
        config = yaml.safe_load(import_config.read_text()) or {}
        item = config.get("model") or {}
        base_url = base_url or str(item.get("base_url", ""))
        model = model or str(item.get("name", ""))
        key_env = str(item.get("api_key_env", "ARENA_API_KEY"))
        source = "imported configuration"
    key = os.environ.get("ARENA_API_KEY") or os.environ.get(key_env, "")
    if not key and credentials:
        import yaml
        data = yaml.safe_load(credentials.expanduser().read_text()) or {}
        key = str((data.get("refs") or {}).get(key_env, ""))
    parsed = urlparse(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Configure ARENA_BASE_URL or pass --import-config with a valid model endpoint")
    if not model or not key:
        raise ValueError("Model or credential missing; configure environment or explicit credential-file reference")
    return Settings(base_url.rstrip("/"), model, key, source)
