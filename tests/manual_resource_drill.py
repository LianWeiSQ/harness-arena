"""Opt-in local Docker cleanup drill; no model requests and no shared containers."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import uuid

from evo_arena.store import Store
from evo_arena.trial_resources import reconcile, shell


def main():
    with tempfile.TemporaryDirectory(prefix="evo-resource-drill-") as folder:
        root = Path(folder).resolve()
        store = Store(root)
        spool = root / "workspace" / "spool"; spool.mkdir(parents=True)
        sentinel = root / "unrelated"; sentinel.mkdir()
        harbor = root / "harbor"
        harbor.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(240)\n")
        harbor.chmod(0o700)
        suffix = uuid.uuid4().hex[:10]
        names = ["evo-drill-owned-" + suffix, "evo-drill-sentinel-" + suffix]
        job = "drill-" + suffix
        resource = {"id": job, "experiment": "resource-drill", "epoch": 1, "workspace": str(spool.parent),
                    "harbor": str(harbor), "job": job, "jobs_dir": str(root / "jobs"), "state": "declared"}
        store.create("evo_resource", job, resource)
        process = subprocess.Popen([str(harbor), "--job-name", job, "--jobs-dir", resource["jobs_dir"]],
                                   start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for name, source in zip(names, (spool, sentinel)):
                shell("docker", "run", "-d", "--name", name, "--network", "none", "--mount",
                      f"type=bind,source={source},target=/arena-relay", "--entrypoint", "/bin/sh",
                      "evoarena-harbor:arena", "-c", "sleep 240")
            result = reconcile(store, resource)
            process.wait(timeout=5)
            survivor = json.loads(shell("docker", "inspect", names[1]))[0]["State"]["Running"]
            assert result["state"] == "cleaned" and process.returncode is not None and survivor
            assert len(result["removed_containers"]) == 1
            print(json.dumps({"owned_process_stopped": True, "owned_container_removed": True,
                              "unrelated_container_preserved": survivor, "resource_state": result["state"]}))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
            for name in names:
                subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            store.close()


if __name__ == "__main__": main()
