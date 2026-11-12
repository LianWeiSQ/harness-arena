"""Install pinned Harnesses in the benchmark's own environment, without changing tasks."""
from pathlib import Path

from .harbor_agents import ArenaDSH, ArenaPi

NODE_VERSION = "22.22.0"
PACKAGES = {"pi": "@earendil-works/pi-coding-agent@0.82.1", "dsh": "@deepseek-ai/dsh@0.1.2-rc.1"}


async def install_runtime(agent, environment, runner):
    await agent.ensure_system_dependencies(environment, ("curl", "bash", "git", "procps"))
    # Install into an isolated prefix; do not replace the task's node/npm binaries.
    script = f"""set -eu
mkdir -p /opt/evo-agent /usr/local/lib
case "$(uname -m)" in
  x86_64) evo_arch=x64 ;;
  aarch64|arm64) evo_arch=arm64 ;;
  *) echo 'Unsupported CPU architecture' >&2; exit 1 ;;
esac
curl --http1.1 --fail --silent --show-error --location --retry 2 --retry-all-errors --connect-timeout 20 --max-time 120 \
  "https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-$evo_arch.tar.gz" \
  -o /tmp/evo-node.tar.gz
tar -xzf /tmp/evo-node.tar.gz -C /opt/evo-agent --strip-components=1
rm /tmp/evo-node.tar.gz
export PATH="/opt/evo-agent/bin:$PATH"
npm install --global --prefix /opt/evo-agent --no-audit --no-fund {PACKAGES[runner]}
{runner} --version
"""
    await agent.exec_as_root(environment, script, timeout_sec=1800)
    await environment.upload_file(
        Path(__file__).with_name("fixtures") / "harness" / "relay.cjs",
        "/usr/local/lib/evoarena-relay.cjs",
    )


class RuntimePath:
    async def exec_as_agent(self, environment, command, **kwargs):
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
