"""Harbor adapters. Imported by the configured Harbor worker, not the web process."""
from __future__ import annotations

import json
import os
import shlex
from functools import wraps
from pathlib import Path

from agents.deepseek_harness import DeepSeekHarness
from harbor.agents.installed.base import BaseInstalledAgent

from .versions import file_hashes, verify_version


def freeze_after_run(method):
    @wraps(method)
    async def wrapped(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        finally:
            marker = os.environ.get("EVO_TRIAL_END_FILE")
            if marker:
                # Host-only path, outside every sandbox mount. Runs on timeout too.
                Path(marker).write_text("ended")
    return wrapped


async def load_version(agent, environment, runner):
    source = os.environ.get("EVO_VERSION_FILE")
    if not source:
        return None
    version = json.loads(Path(source).read_text())
    manifest = verify_version(version)
    if manifest["runner"] != runner:
        raise ValueError("Version runner mismatch")
    actual_version = (await agent.exec_as_agent(environment, f"{runner} --version")).stdout.strip()
    if actual_version != manifest["runtime_version"]:
        raise ValueError("Installed runtime does not match immutable version")
    script = ("const fs=require('node:fs'),c=require('node:crypto');const paths="
              + json.dumps(list(manifest["files"]))
              + ";console.log(JSON.stringify(Object.fromEntries(paths.map(p=>[p,c.createHash('sha256').update(fs.readFileSync('/evo-version/'+p)).digest('hex')]))))")
    result = await agent.exec_as_agent(environment, "node -e " + shlex.quote(script))
    actual = json.loads(result.stdout)
    if actual != file_hashes(manifest):
        raise ValueError("Mounted version does not match expected hashes")
    # Written by the host adapter, before the Harness starts executing.
    (agent.logs_dir / "version-load.json").write_text(json.dumps({
        "version_id": version["id"], "manifest_sha256": version["manifest_sha256"],
        "runner": runner, "runtime_version": actual_version, "file_hashes": actual,
    }))
    return manifest


async def start_relay(agent, environment):
    await agent.exec_as_agent(environment,
        "nohup node /usr/local/lib/evoarena-relay.cjs </dev/null >/tmp/arena-relay.log 2>&1 & sleep 0.2")


def local_env(agent):
    return {
        "ARENA_TRIAL_URL": agent._get_env("ARENA_TRIAL_URL"),
        "ARENA_TRIAL_TOKEN": agent._get_env("ARENA_TRIAL_TOKEN"),
        "DO_NOT_TRACK": "1",
    }


class ArenaDSH(DeepSeekHarness):
    def __init__(self, **kwargs):
        super().__init__(provider="arena", api_key_env="ARENA_TRIAL_TOKEN", **kwargs)
        self.base_url = self._get_env("ARENA_TRIAL_URL") + "/v1"

    async def install(self, environment):
        await self.exec_as_agent(environment, "dsh --version")

    @freeze_after_run
    async def run(self, instruction, environment, context):
        manifest = await load_version(self, environment, "dsh")
        if manifest and manifest["files"]["policy.md"]:
            instruction = "Execution policy:\n" + manifest["files"]["policy.md"] + "\n\nTask:\n" + instruction
        await start_relay(self, environment)
        home = "/logs/agent/dsh-home"
        await self.exec_as_agent(environment, f"mkdir -p {home}")
        for name, content in (("settings.yaml", self._settings()), ("cordis.patch.yml", self._home_patch())):
            await self._upload_config_text(environment, content=content,
                                           remote_path=f"{home}/{name}", filename=name)
        await self.exec_as_agent(environment,
            f"dsh --profile headless {shlex.quote(instruction)}",
            env={**local_env(self), "DSH_HOME": home, "DSH_PERMISSION_MODE": "danger-full-access",
                 "DSH_TOOLS_MODE": "ptc", "DSH_TELEMETRY_DISABLED": "1"})


class ArenaPi(BaseInstalledAgent):
    @staticmethod
    def name() -> str:
        return "pi"

    def get_version_command(self) -> str:
        return "pi --version"

    async def install(self, environment):
        await self.exec_as_agent(environment, self.get_version_command())

    @freeze_after_run
    async def run(self, instruction, environment, context):
        manifest = await load_version(self, environment, "pi")
        await start_relay(self, environment)
        home = "/logs/agent/pi-home"
        model = self.model_name.split("/", 1)[-1]
        config = {"providers": {"arena": {
            "baseUrl": self._get_env("ARENA_TRIAL_URL") + "/v1",
            "api": "openai-responses",
            "apiKey": "$ARENA_TRIAL_TOKEN",
            "models": [{"id": model, "maxTokens": 8192}],
        }}}
        await self.exec_as_agent(environment, f"mkdir -p {home}")
        await self._upload_config_text(environment, content=json.dumps(config),
                                       remote_path=f"{home}/models.json", filename="models.json")
        args = ["pi", "-p", "--offline", "--mode", "json", "--provider", "arena", "--model", model,
                "--no-session", "--no-extensions", "--no-skills", "--no-prompt-templates",
                "--no-themes", "--no-context-files"]
        if manifest:
            if manifest["files"]["policy.md"]:
                args += ["--append-system-prompt", "/evo-version/policy.md"]
            for path in manifest["files"]:
                if path != "policy.md":
                    args += ["--skill", "/evo-version/" + path]
        args.append(instruction)
        await self.exec_as_agent(environment, shlex.join(args) + " > /logs/agent/pi.jsonl",
                                 env={**local_env(self), "PI_CODING_AGENT_DIR": home})
