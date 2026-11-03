from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .store import digest

IMAGE = "evoarena-fixops:m0"
FIXTURES = Path(__file__).with_name("fixtures")


class EnvironmentError(RuntimeError):
    pass


def docker(*args: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise EnvironmentError("Docker command unavailable or timed out") from None
    if result.returncode:
        raise EnvironmentError("Docker operation failed: " + result.stderr.strip()[-500:])
    return (result.stdout + (result.stderr if args and args[0] == "logs" else "")).strip()


def build_image(base_image: str = "python:3.13-slim") -> dict:
    docker("build", "--build-arg", "BASE_IMAGE=" + base_image, "-t", IMAGE, str(FIXTURES), timeout=240)
    image_id = docker("image", "inspect", IMAGE, "--format", "{{.Id}}")
    return {"image": IMAGE, "image_id": image_id, "base_image": base_image,
            "architecture": docker("image", "inspect", IMAGE, "--format", "{{.Architecture}}"),
            "fixture_sha256": digest({p.name: p.read_text() for p in sorted(FIXTURES.iterdir()) if p.is_file()})}


class DockerScenario:
    def __init__(self, root: Path, run_id: str, seed: int, image: str = IMAGE):
        self.root, self.run_id, self.seed, self.image = root, run_id, seed, image
        self.name = "evoarena-" + run_id
        self.config_dir = root / "environments" / run_id
        self.config_file = self.config_dir / "gateway.json"
        self.order_port = 18100 + seed % 100
        self.healthy_upstream = f"http://127.0.0.1:{self.order_port}"
        self.endpoint = ""
        self.active = False

    def __enter__(self):
        self.config_dir.mkdir(parents=True, exist_ok=False)
        self.config_dir.chmod(0o755)
        self.patch("http://127.0.0.1:65531")
        try:
            docker("run", "--rm", "--detach", "--name", self.name,
                "--label", "evoarena.managed=true", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=128m", "--cpus=1",
                "--mount", f"type=bind,source={self.config_dir},target=/config,readonly",
                "--publish", "127.0.0.1::8800", "--env", f"ORDER_PORT={self.order_port}", self.image)
            self.active = True
            port = docker("port", self.name, "8800/tcp")
            if not re.fullmatch(r"127\.0\.0\.1:\d+", port):
                raise EnvironmentError("Unexpected container port binding")
            self.endpoint = "http://" + port
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if self.request("/health")[0] == 502:
                        return self
                except EnvironmentError:
                    pass
                time.sleep(0.2)
            raise EnvironmentError("Gateway did not become ready")
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        # Only this exact, generated container is addressed; unrelated containers stay untouched.
        if self.active:
            try:
                docker("rm", "--force", self.name)
            finally:
                self.active = False

    def patch(self, upstream: str) -> dict:
        if not isinstance(upstream, str) or not re.fullmatch(r"http://127\.0\.0\.1:[1-9]\d{0,4}", upstream):
            raise ValueError("upstream must be an HTTP loopback origin with a port")
        if int(upstream.rsplit(":", 1)[-1]) > 65535:
            raise ValueError("Invalid port")
        data = {"upstream": upstream}
        temporary = self.config_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(data))
        temporary.chmod(0o644)
        temporary.replace(self.config_file)
        if self.active:
            # Desktop bind mounts can expose an atomic rename after the host write completes.
            # Confirm the observed configuration, rather than declaring the repair applied early.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    observed = docker("exec", self.name, "python3", "-c",
                        "from pathlib import Path; print(Path('/config/gateway.json').read_text())")
                    if json.loads(observed) == data:
                        break
                except (EnvironmentError, ValueError):
                    pass
                time.sleep(0.05)
            else:
                raise EnvironmentError("Configuration write could not be confirmed inside the environment")
        return {"configuration": data, "sha256": digest(data)}

    def request(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        raw = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.endpoint + path, data=raw,
                                        headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)
        except (OSError, ValueError):
            raise EnvironmentError("Service probe unavailable") from None

    def tool(self, action: dict) -> dict:
        kind = action.get("action")
        if kind == "inspect":
            return {"configuration": json.loads(self.config_file.read_text()),
                    "services": [{"name": "gateway", "listen": "0.0.0.0:8800"},
                                 {"name": "orders", "listen": f"127.0.0.1:{self.order_port}"}],
                    "recent_logs": docker("logs", "--tail", "8", self.name)[-2000:]}
        if kind == "patch_config":
            if set(action) - {"action", "upstream", "summary"}:
                raise ValueError("Unknown patch fields")
            return self.patch(action.get("upstream"))
        if kind == "probe":
            status, body = self.request("/health")
            return {"status": status, "body": body}
        if kind == "submit":
            return {"submitted": True}
        raise ValueError("Unknown action")

    def verify(self) -> dict:
        """Host-side oracle. Its nonce, implementation and results are not agent tools."""
        tests = []
        status, health = self.request("/health")
        tests.append({"name": "upstream_health", "passed": status == 200 and health.get("service") == "orders"})
        sku = "verifier-" + uuid.uuid4().hex
        status, created = self.request("/orders", {"sku": sku, "quantity": 3})
        tests.append({"name": "create_order", "passed": status == 201 and created.get("sku") == sku and created.get("quantity") == 3})
        if created.get("id"):
            status, fetched = self.request("/orders/" + created["id"])
            valid = status == 200 and fetched == created
        else:
            valid = False
        tests.append({"name": "read_order", "passed": valid})
        tests.append({"name": "dependency_constraint", "passed":
                      json.loads(self.config_file.read_text()).get("upstream") == self.healthy_upstream})
        return {"passed": all(t["passed"] for t in tests), "tests": tests}

    def calibrate(self) -> dict:
        broken = self.verify()
        self.patch(self.healthy_upstream)
        try:
            healthy = self.verify()
        finally:
            self.patch("http://127.0.0.1:65531")
        if broken["passed"] or not healthy["passed"]:
            raise EnvironmentError("Scenario calibration failed")
        return {"broken_passed": False, "healthy_passed": True}
