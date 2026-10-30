"""Prove the model-generated transform responds to changed source data."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "pilot" / "workspace"
EVIDENCE = ROOT / "evidence" / "pilot"
BASE = "http://127.0.0.1:18778"
SESSION = "seeknal-pilot-metamorphic"


def api(path: str) -> dict:
    with urlopen(Request(BASE + path, headers={"Accept": "application/json"}), timeout=10) as response:
        return json.load(response)


def browser_eval(source: str, timeout: float = 90):
    result = subprocess.run(
        ["agent-browser", "--session", SESSION, "--json", "eval", source],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    response = json.loads(result.stdout)
    if not response["success"]:
        raise AssertionError(response)
    return response.get("data", {}).get("result")


def wait_ready(timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if api("/api/state").get("project") == PROJECT.name:
                return
        except (URLError, OSError):
            pass
        time.sleep(0.2)
    raise AssertionError("pilot service did not become ready")


def run_pipeline() -> dict:
    browser_eval("(async()=>{await studio.startJob('run');return true})()")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = browser_eval("studio.snapshot().activeJob")
        if job and job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.25)
    raise AssertionError("pipeline job timed out")


def normalized_preview() -> list[dict]:
    preview = api("/api/preview?node=transform.customer_value_pilot")
    rows = [dict(zip(preview["columns"], row)) for row in preview["rows"]]
    for row in rows:
        row["lifetime_value"] = int(float(row["lifetime_value"]))
        row["orders"] = int(row["orders"])
    return sorted(rows, key=lambda row: row["customer_id"])


def main() -> None:
    orders = PROJECT / "data" / "orders.csv"
    original = orders.read_text()
    environment = os.environ.copy()
    environment.update({
        "SEEKNAL_STUDIO_PROJECT": str(PROJECT),
        "SEEKNAL_STUDIO_PORT": "18778",
        "SEEKNAL_STUDIO_AGENT_MODE": "external",
        "PYTHONUNBUFFERED": "1",
    })
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    server = subprocess.Popen(
        [os.sys.executable, str(ROOT / "server.py")],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    receipt: dict[str, object] = {"project": str(PROJECT), "synthetic_change": "add one C001 paid order for 60000"}
    try:
        wait_ready()
        subprocess.run(["agent-browser", "--session", SESSION, "open", BASE], check=True, capture_output=True, text=True)
        browser_eval(
            "new Promise((resolve,reject)=>{let n=0;const t=setInterval(()=>{"
            "if(window.studio?.snapshot().nodes===3){clearInterval(t);resolve(true)}"
            "else if(n++>100){clearInterval(t);reject(Error('Studio readiness timeout'))}},200)})"
        )
        receipt["baseline"] = normalized_preview()
        orders.write_text(original + "O1007,C001,60000,paid,2026-10-25\n")
        changed_job = run_pipeline()
        assert changed_job["status"] == "succeeded", changed_job
        changed = normalized_preview()
        c001 = next(row for row in changed if row["customer_id"] == "C001")
        assert c001["lifetime_value"] == 400000 and c001["orders"] == 3, c001
        receipt["changed"] = changed
        receipt["changed_run"] = {"id": changed_job["id"], "status": changed_job["status"], "exit_code": changed_job["exit_code"]}
    finally:
        orders.write_text(original)
        if server.poll() is None:
            try:
                restored_job = run_pipeline()
                receipt["restored_run"] = {"id": restored_job["id"], "status": restored_job["status"], "exit_code": restored_job["exit_code"]}
                receipt["restored"] = normalized_preview()
            except Exception as error:
                receipt["restore_error"] = str(error)
            subprocess.run(["agent-browser", "--session", SESSION, "close"], capture_output=True, text=True)
            server.terminate()
            server.wait(timeout=15)
        (EVIDENCE / "e2e-metamorphic.json").write_text(json.dumps(receipt, indent=2))
    assert receipt.get("restored") == receipt.get("baseline"), receipt
    print("Metamorphic pilot: input change altered output, then restore returned the original three rows.")


if __name__ == "__main__":
    main()
