"""Install pinned Harnesses in the benchmark's own environment, without changing tasks."""
from pathlib import Path
from .harness import atomic_json
from .store import now

from .harbor_agents import ArenaDSH, ArenaPi

NODE_VERSION = "22.22.0"
PACKAGES = {"pi": "@earendil-works/pi-coding-agent@0.82.1", "dsh": "@deepseek-ai/dsh@0.1.2-rc.1"}


async def install_runtime(agent, environment, runner):
    state = {}

    def stage(name):
        state.update(stage=name, started_at=now(), runner=runner)
        atomic_json(agent.logs_dir / "setup-progress.json", state)

    stage("dependencies")
    architecture = await agent.exec_as_root(environment, "uname -m")
    state["guest_arch"] = architecture.stdout.strip()
    stage("dependencies")
    await agent.exec_as_root(environment, "mkdir -p /logs/agent/setup")
    await agent.ensure_system_dependencies(environment, ("curl", "bash", "git", "procps"))
    stage("node")
    # Install into an isolated prefix; do not replace the task's node/npm binaries.
    script = f"""set -eu
mkdir -p /opt/evo-agent /usr/local/lib
if [ "$(/opt/evo-agent/bin/node --version 2>/dev/null || true)" != "v{NODE_VERSION}" ]; then
case "$(uname -m)" in
  x86_64) evo_arch=x64 ;;
  aarch64|arm64) evo_arch=arm64 ;;
  *) echo 'Unsupported CPU architecture' >&2; exit 1 ;;
esac
curl --http1.1 --fail --progress-bar --show-error --location --retry 2 --retry-all-errors --connect-timeout 20 --max-time 120 \
  "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$evo_arch.tar.gz" \
  -o /tmp/evo-node.tar.gz
tar -xzf /tmp/evo-node.tar.gz -C /opt/evo-agent --strip-components=1
rm /tmp/evo-node.tar.gz
else
  echo "Node.js v{NODE_VERSION} already installed"
fi
test "$(/opt/evo-agent/bin/node --version)" = "v{NODE_VERSION}"
"""
    await agent.exec_as_root(environment, "(\n" + script + "\n) 2>&1 | tee -a /logs/agent/setup/install.log", timeout_sec=600)
    stage("harness")
    version = PACKAGES[runner].rsplit("@", 1)[1]
    await agent.exec_as_root(environment,
        '(export PATH="/opt/evo-agent/bin:$PATH"; set -eu; '
        f'if [ "$(/opt/evo-agent/bin/{runner} --version 2>/dev/null || true)" != "{version}" ]; then '
        f'npm install --global --prefix /opt/evo-agent --no-audit --no-fund --loglevel=http --fetch-retries=2 {PACKAGES[runner]}; fi; '
        f'test "$(/opt/evo-agent/bin/{runner} --version)" = "{version}"; '
        f'echo "{runner} {version} ready") 2>&1 | tee -a /logs/agent/setup/install.log', timeout_sec=1800)
    await environment.upload_file(
        Path(__file__).with_name("fixtures") / "harness" / "relay.cjs",
        "/usr/local/lib/evoarena-relay.cjs",
    )
    stage("ready")


class RuntimePath:
    async def exec_as_root(self, environment, command, **kwargs):
        if command.startswith(("apt-get ", "dnf ", "yum ", "apk ")):
            command = "(" + command + ") 2>&1 | tee -a /logs/agent/setup/install.log"
        return await super().exec_as_root(environment, command, **kwargs)

    async def exec_as_agent(self, environment, command, **kwargs):
        if command.startswith("dsh --profile headless "):
            command += " 2>&1 | tee /logs/agent/dsh-output.log"
        return await super().exec_as_agent(
            environment, 'export PATH="/opt/evo-agent/bin:$PATH"; ' + command, **kwargs)

    def get_version_command(self):
        return '/opt/evo-agent/bin/node /opt/evo-agent/bin/' + self.runner + ' --version'

    async def install(self, environment):
        await install_runtime(self, environment, self.runner)


class BenchmarkPi(RuntimePath, ArenaPi):
    runner = "pi"


class BenchmarkDSH(RuntimePath, ArenaDSH):
    runner = "dsh"
