"""Run the packaged desktop launcher against the completed pilot project."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "pilot" / "workspace"
EVIDENCE = ROOT / "evidence" / "pilot"
BUNDLE = ROOT / "dist" / "Seeknal Studio.app"
BASE = "http://127.0.0.1:18780"


def api(path: str) -> dict:
    with urlopen(BASE + path, timeout=5) as response:
        return json.load(response)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    executable = BUNDLE / "Contents" / "MacOS" / "Seeknal Studio"
    assert executable.is_file(), "rebuild the bundle before package acceptance"
    resource = BUNDLE / "Contents" / "Resources" / "studio"
    copied_sources = (
        "server.py", "engine_bridge.py", "project_service.py", "environment_service.py", "repl_service.py",
        "repl_worker.py", "agent_runtime.py", "agent_worker.py", "launch.py",
        "index.html", "styles.css", "app.js",
    )
    manifest = {}
    for name in copied_sources:
        source_hash = sha256(ROOT / name)
        bundled_hash = sha256(resource / name)
        assert bundled_hash == source_hash, f"stale bundled source: {name}"
        manifest[name] = source_hash
    environment = os.environ.copy()
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    process = subprocess.Popen(
        [str(executable), "--project", str(PROJECT), "--port", "18780"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    receipt = {"bundle": str(BUNDLE), "project": str(PROJECT), "source_manifest": manifest}
    try:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            assert process.poll() is None, "packaged launcher exited during readiness"
            try:
                status = api("/api/agent/status")
                if status.get("ready"):
                    break
            except (URLError, OSError):
                pass
            time.sleep(.25)
        else:
            raise AssertionError("packaged owned runtime did not become ready")
        state = api("/api/state")
        graph = api("/api/graph")
        preview = api("/api/preview?node=transform.customer_value_pilot")
        assert state["project"] == PROJECT.name and len(graph["nodes"]) == 3 and len(graph["edges"]) == 2
        assert preview["fresh"] is True and preview["row_count"] == 3
        processes = subprocess.check_output(
            ["ps", "-axo", "pid=,ppid=,comm="], text=True
        ).splitlines()
        children = [
            line.strip().split(None, 2)
            for line in processes
            if len(line.strip().split(None, 2)) == 3
            and line.strip().split(None, 2)[1] == str(process.pid)
        ]
        windows = [int(fields[0]) for fields in children if fields[2].endswith("/webframe")]
        assert len(windows) == 1, children
        receipt.update({
            "model": status.get("model"),
            "project_verified": status.get("project_verified"),
            "nodes": 3,
            "edges": 2,
            "preview_rows": 3,
            "window_pid": windows[0],
        })
        os.kill(windows[0], signal.SIGTERM)
        process.wait(timeout=15)
        assert process.returncode == 0
        try:
            api("/api/state")
            raise AssertionError("owned package backend remained after window close")
        except URLError:
            receipt["backend_stopped"] = True
        print("Packaged pilot: exact model/project ready; closing native window stopped owned backend.")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        if process.stdout:
            process.stdout.close()
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "e2e-package.json").write_text(json.dumps(receipt, indent=2))

if __name__ == "__main__":
    main()
