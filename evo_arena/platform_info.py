"""Local dependency inspection. Does not probe a paid model endpoint."""
import shutil
import subprocess

from .versions import CAPABILITIES
from .taskpacks import MODES
from .improvement import IMPROVE_LIMITS


def inspect_platform(store, harbor, image):
    checks = []
    commands = [("Docker", ["docker", "info", "--format", "{{.ServerVersion}}"]),
                ("Harbor", [str(harbor), "--version"] if harbor and harbor.is_file() else None),
                ("任务镜像", ["docker", "image", "inspect", image, "--format", "{{.Id}}"]),
                ("Node.js（本地校准）", ["node", "--version"])]
    for name, args in commands:
        try:
            if not args or not shutil.which(args[0]):
                raise OSError()
            result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=5)
            checks.append({"name": name, "ready": True, "detail": result.stdout.strip()[:200]})
        except (OSError, subprocess.SubprocessError):
            checks.append({"name": name, "ready": False, "detail": "未检测到可用依赖"})
    return {"checks": checks, "data_directory": str(store.root), "capabilities": CAPABILITIES,
            "defaults": {k: v["limits"] for k, v in MODES.items()} | {"improve": IMPROVE_LIMITS},
            "model_calls": 0}
