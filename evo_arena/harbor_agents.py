"""Harbor adapters. Imported by the configured Harbor worker, not the web process."""
from __future__ import annotations

import json
import shlex

from agents.deepseek_harness import DeepSeekHarness
from harbor.agents.installed.base import BaseInstalledAgent


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

    async def run(self, instruction, environment, context):
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

    async def run(self, instruction, environment, context):
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
                "--no-themes", "--no-context-files", instruction]
        await self.exec_as_agent(environment, shlex.join(args) + " > /logs/agent/pi.jsonl",
                                 env={**local_env(self), "PI_CODING_AGENT_DIR": home})
