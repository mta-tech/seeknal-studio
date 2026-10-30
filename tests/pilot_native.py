"""Verify the completed pilot through the actual native Webframe runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "pilot" / "workspace"
EVIDENCE = ROOT / "evidence" / "pilot"
BASE = "http://127.0.0.1:18779"


def wait_ready(timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(BASE + "/api/agent/status", timeout=5) as response:
                status = json.load(response)
            if status.get("ready"):
                return status
        except (URLError, OSError):
            pass
        time.sleep(.25)
    raise AssertionError("owned pilot runtime did not become ready")


def main() -> None:
    environment = os.environ.copy()
    environment.update({
        "SEEKNAL_STUDIO_PROJECT": str(PROJECT),
        "SEEKNAL_STUDIO_PORT": "18779",
        "SEEKNAL_STUDIO_AGENT_MODE": "owned",
        "PYTHONUNBUFFERED": "1",
    })
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    binary = ROOT.parent / "webframe-evaluation" / "bin" / "webframe"
    receipt = {"binary": str(binary), "project": str(PROJECT)}
    try:
        commands = [
            "new Promise((resolve,reject)=>{let n=0;const t=setInterval(()=>{const s=window.studio?.snapshot();if(s?.agentReady&&s.nodes===3){clearInterval(t);resolve(s)}else if(n++>150){clearInterval(t);reject(Error('Studio readiness timeout'))}},200)})",
            "studio.selectNode('transform.customer_value_pilot').then(()=>({selected:studio.snapshot().selectedNode,previewRows:studio.state.preview?.rows.length,previewFresh:studio.state.preview?.fresh,model:studio.snapshot().agentModel,projectVerified:studio.snapshot().agentProjectVerified}))",
            "studio.startJob('validate').then(()=>({job:studio.state.activeJob.id,status:studio.state.activeJob.status}))",
            "new Promise((resolve,reject)=>{let n=0;const t=setInterval(()=>{const j=studio.state.activeJob;if(j&&['succeeded','failed','cancelled'].includes(j.status)){clearInterval(t);resolve({status:j.status,exit:j.exit_code})}else if(n++>200){clearInterval(t);reject(Error('job timeout'))}},200)})",
        ]
        result = subprocess.run(
            [str(binary), "-c", BASE],
            input="\n".join(commands) + "\n",
            capture_output=True,
            text=True,
            timeout=90,
        )
        lines = result.stdout.splitlines()
        assert result.returncode == 0 and len(lines) == 4, {"exit": result.returncode, "stdout": lines, "stderr": result.stderr}
        parsed = [json.loads(line) for line in lines]
        status = wait_ready()
        assert parsed[0]["nodes"] == 3 and parsed[0]["edges"] == 2, parsed
        assert parsed[1] == {"selected": "transform.customer_value_pilot", "previewRows": 3, "previewFresh": True, "model": "deepseek-v4.1-flash:netra", "projectVerified": True}, parsed[1]
        assert parsed[3] == {"status": "succeeded", "exit": 0}, parsed[3]
        receipt.update({
            "provider": status.get("provider"),
            "model": status.get("model"),
            "project_verified": status.get("project_verified"),
            "capabilities": status.get("capabilities"),
            "checks": parsed,
        })
        print("Native pilot: exact model ready, project verified, three-node graph and three-row output validated.")
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(timeout=15)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        (EVIDENCE / "e2e-native.json").write_text(json.dumps(receipt, indent=2))

if __name__ == "__main__":
    main()
