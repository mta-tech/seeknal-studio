"""One-shot real model → draft → review → apply → run acceptance pilot.

This test intentionally makes a real Sumopod request. It never opens the API
key file itself; the owned Studio worker is the only credential reader.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PILOT_ROOT = ROOT / "pilot"
TEMPLATE = PILOT_ROOT / "template"
PILOT = PILOT_ROOT / "workspace"
EVIDENCE = ROOT / "evidence" / "pilot"
BASE = "http://127.0.0.1:18776"
SESSION = "seeknal-agent-pilot"
TERMINAL = {"succeeded", "failed", "cancelled"}
PROMPT = """Inspect the source definitions for orders and customers in this isolated synthetic project.
Create exactly one YAML transform draft named customer_value_pilot. It must join source.orders to
source.customers by customer_id, include paid orders only, and group by customer_id. Its output columns
must be customer_id, customer_name, segment, lifetime_value as SUM(amount), and orders as COUNT(*).
Set both source nodes as inputs. Use the available draft-only tools to create and fully populate the draft.
Do not validate, apply, run, execute Python/SQL, or edit active pipeline files. End by giving the draft path
and saying Studio review, validation, apply, and run are still pending."""


def api(path: str, payload: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        detail = json.load(error)
        raise AssertionError(f"{path}: HTTP {error.code}: {detail.get('error', 'request failed')}") from error


def browser(arguments: list[str], *, timeout: float = 60) -> str:
    result = subprocess.run(
        ["agent-browser", "--session", SESSION, *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout.strip()


def browser_eval(source: str, *, timeout: float = 60):
    result = subprocess.run(
        ["agent-browser", "--session", SESSION, "--json", "eval", source],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    response = json.loads(result.stdout)
    if not response.get("success"):
        raise AssertionError(response)
    return response.get("data", {}).get("result")


def wait_api(path: str, predicate, timeout: float, label: str) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        try:
            last = api(path, timeout=10)
            if predicate(last):
                return last
        except (URLError, TimeoutError, ConnectionError, OSError):
            pass
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {label}: {last}")


def wait_job(job_id: str, timeout: float = 120) -> dict:
    return wait_api(
        f"/api/jobs/{quote(job_id)}",
        lambda job: job.get("status") in TERMINAL,
        timeout,
        f"job {job_id}",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_preflight() -> None:
    transforms = PILOT / "seeknal" / "transforms"
    drafts = PILOT / ".seeknal" / "drafts"
    if transforms.exists() and any(transforms.iterdir()):
        raise AssertionError("pilot already has a transform; preserve evidence and use a fresh pilot copy")
    if drafts.exists() and any(drafts.iterdir()):
        raise AssertionError("pilot already has a draft; preserve evidence and use a fresh pilot copy")


def prepare_workspace() -> None:
    if PILOT.exists():
        shutil.rmtree(PILOT)
    shutil.copytree(TEMPLATE, PILOT)


def safe_session_evidence(session: dict) -> dict:
    events = []
    for event in session.get("events", []):
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        events.append({
            "seq": event.get("seq"),
            "type": event.get("type"),
            **({"tool": data.get("name")} if data.get("name") else {}),
        })
    return {
        "session_id": session.get("session_id"),
        "turn_id": session.get("turn_id"),
        "status": session.get("status"),
        "events": events,
        "error": session.get("error"),
        "answer_sha256": hashlib.sha256(str(session.get("answer") or "").encode()).hexdigest(),
    }


def main() -> None:
    prepare_workspace()
    clean_preflight()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    server_log = EVIDENCE / "e2e-server.log"
    environment = os.environ.copy()
    environment.update({
        "SEEKNAL_STUDIO_PROJECT": str(PILOT),
        "SEEKNAL_STUDIO_PORT": "18776",
        "SEEKNAL_STUDIO_AGENT_MODE": "owned",
        "PYTHONUNBUFFERED": "1",
    })
    # This source-only fixture has no Atlas identity or governance contract.
    # Do not inherit an operator shell's unrelated endpoint into the pilot.
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    receipt: dict[str, object] = {
        "provider": "Sumopod OpenAI-compatible",
        "model": "deepseek-v4.1-flash:netra",
        "project": str(PILOT),
        "input": "synthetic local CSV (6 orders, 3 customers)",
        "credential_evidence": "backend-only key file; value not read or recorded by test",
        "checks": [],
    }
    server = None
    try:
        with server_log.open("w") as log:
            server = subprocess.Popen(
                [os.sys.executable, str(ROOT / "server.py")],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        status = wait_api(
            "/api/agent/status",
            lambda value: value.get("ready") is True,
            180,
            "owned agent readiness",
        )
        assert status.get("mode") == "owned", status
        assert status.get("provider") == "openai", status
        assert status.get("model") == "deepseek-v4.1-flash:netra", status
        assert status.get("project_verified") is True, status
        assert Path(status.get("project_path", "")).resolve() == PILOT.resolve(), status
        assert status.get("capabilities", {}).get("draft_only") is True, status
        allowed_tools = set(status.get("capabilities", {}).get("tools", []))
        forbidden_fragments = {"apply", "run", "execute", "shell", "python", "publish", "ingest", "write_project"}
        assert allowed_tools and not any(
            fragment in tool.lower() for tool in allowed_tools for fragment in forbidden_fragments
        ), allowed_tools
        receipt["identity"] = {
            "project_path": status.get("project_path"),
            "project_fingerprint": status.get("project_fingerprint"),
            "project_verified": status.get("project_verified"),
            "capabilities": status.get("capabilities"),
        }
        receipt["checks"].append("exact model, tool calling, and canonical project identity verified")

        browser(["set", "viewport", "1584", "992"])
        browser(["open", BASE])
        browser_eval(
            "new Promise((resolve,reject)=>{let n=0;const t=setInterval(()=>{"
            "const s=window.studio?.snapshot();if(s?.agentReady&&s.nodes===2){clearInterval(t);resolve(s)}"
            "else if(n++>200){clearInterval(t);reject(Error('Studio readiness timeout'))}},250)})",
            timeout=70,
        )
        session_id = browser_eval("studio.state.agent.sessionId")
        assert isinstance(session_id, str) and session_id
        browser_eval(
            "(()=>{const original=window.fetch.bind(window);window.__pilotAgentAskBody=null;"
            "window.fetch=(input,init={})=>{const url=String(input);"
            "if(url.endsWith('/api/agent/ask')&&typeof init.body==='string'){"
            "window.__pilotAgentAskBody=JSON.parse(init.body)}return original(input,init)};return true})()"
        )
        browser_eval(f"(async()=>{{await studio.askAgent({json.dumps(PROMPT)});return true}})()", timeout=700)
        ask_body = browser_eval("window.__pilotAgentAskBody")
        assert ask_body.get("session_id") == session_id, ask_body
        turn_id = ask_body.get("turn_id")
        assert isinstance(turn_id, str) and turn_id, ask_body
        session = wait_api(
            f"/api/agent/sessions/{quote(session_id)}?turn_id={quote(turn_id)}",
            lambda value: value.get("status") in TERMINAL,
            30,
            "agent turn completion",
        )
        assert session.get("turn_id") == turn_id, session
        receipt["agent_turn"] = safe_session_evidence(session)
        receipt["agent_turn"]["client_turn_id"] = turn_id
        receipt["agent_turn"]["turn_id_matched"] = session.get("turn_id") == turn_id
        if session.get("status") != "succeeded":
            (EVIDENCE / "e2e-real-failed.json").write_text(json.dumps(receipt, indent=2))
        assert session.get("status") == "succeeded", safe_session_evidence(session)
        tool_names = {
            event.get("data", {}).get("name")
            for event in session.get("events", [])
            if isinstance(event.get("data"), dict) and event.get("data", {}).get("name")
        }
        assert tool_names <= allowed_tools, (tool_names, allowed_tools)
        assert tool_names & {"draft_node", "write_draft"}, tool_names
        receipt["checks"].append("real browser-submitted model turn used only draft-safe tools")

        target = PILOT / "seeknal" / "transforms" / "customer_value_pilot.yml"
        output = PILOT / "target" / "intermediate" / "transform_customer_value_pilot.parquet"
        assert not target.exists(), "agent applied its own draft"
        assert not output.exists(), "agent ran the pipeline"
        all_drafts = sorted(path for path in (PILOT / ".seeknal" / "drafts").glob("*") if path.is_file())
        drafts = [path for path in all_drafts if "customer_value_pilot" in path.name and path.suffix == ".yml"]
        assert len(all_drafts) == 1 and len(drafts) == 1, all_drafts
        draft = drafts[0]
        receipt["draft"] = {"path": draft.relative_to(PILOT).as_posix(), "sha256": sha256(draft)}
        receipt["checks"].append("agent stopped at one draft; active transform and output absent")

        review = browser_eval(
            "(async()=>{await studio.openDraftReview();return {"
            "path:studio.state.currentDiff?.path,destination:studio.state.currentDiff?.destination,"
            "content_hash:studio.state.currentDiff?.content_hash,"
            "apply_disabled:document.querySelector('#apply-draft').disabled}})()",
            timeout=40,
        )
        assert review["path"] == draft.relative_to(PILOT).as_posix(), review
        assert review["destination"] == "seeknal/transforms/customer_value_pilot.yml", review
        assert review["apply_disabled"] is True, review
        assert review["content_hash"] == receipt["draft"]["sha256"], review
        receipt["review"] = review

        browser_eval("(async()=>{await studio.validateCurrentDraft();return true})()", timeout=30)
        validation_id = browser_eval("studio.state.draftValidationJobId")
        validation = wait_job(validation_id)
        assert validation["status"] == "succeeded" and validation["exit_code"] == 0, validation
        receipt["validation"] = {"job_id": validation_id, "status": validation["status"], "exit_code": 0}

        deadline = time.monotonic() + 15
        while browser_eval("document.querySelector('#apply-draft').disabled") and time.monotonic() < deadline:
            time.sleep(0.25)
        assert browser_eval("document.querySelector('#apply-draft').disabled") is False
        browser_eval("(async()=>{await studio.applyCurrentDraft();return true})()", timeout=30)
        apply_id = browser_eval("studio.state.activeJob.id")
        applied = wait_job(apply_id)
        assert applied["action"] == "apply-draft" and applied["status"] == "succeeded", applied
        assert target.is_file() and not draft.exists()
        assert sha256(target) == receipt["draft"]["sha256"]
        receipt["apply"] = {"job_id": apply_id, "status": applied["status"], "target_sha256": sha256(target)}
        receipt["checks"].append("reviewed hash validated and explicitly applied through browser")

        browser_eval("(async()=>{document.querySelector('#draft-dialog').close();await studio.startJob('run');return true})()", timeout=30)
        run_id = browser_eval("studio.state.activeJob.id")
        run = wait_job(run_id)
        receipt["run"] = {
            "job_id": run_id,
            "status": run["status"],
            "exit_code": run["exit_code"],
            "log_sha256": hashlib.sha256(str(run.get("logs") or "").encode()).hexdigest(),
        }
        if run["status"] != "succeeded" or run["exit_code"] != 0:
            (EVIDENCE / "e2e-real-run-failed.json").write_text(json.dumps(receipt, indent=2))
        assert run["status"] == "succeeded" and run["exit_code"] == 0, run

        preview = api("/api/preview?node=transform.customer_value_pilot")
        expected = json.loads((PILOT / "EXPECTED_OUTPUT.json").read_text())["rows"]
        actual = [dict(zip(preview["columns"], row)) for row in preview["rows"]]
        for row in actual:
            row["lifetime_value"] = int(float(row["lifetime_value"]))
            row["orders"] = int(row["orders"])
        actual.sort(key=lambda row: row["customer_id"])
        assert actual == expected, {"actual": actual, "expected": expected}
        assert preview.get("fresh") is True
        graph = api("/api/graph")
        node_ids = {node["id"] for node in graph["nodes"]}
        edges = {(edge["source"], edge["target"]) for edge in graph["edges"]}
        assert "transform.customer_value_pilot" in node_ids
        assert ("source.orders", "transform.customer_value_pilot") in edges
        assert ("source.customers", "transform.customer_value_pilot") in edges
        receipt["output"] = {"fresh": True, "rows": actual, "columns": preview["columns"]}
        receipt["lineage"] = {"nodes": sorted(node_ids), "edges": sorted([list(edge) for edge in edges])}
        receipt["checks"].append("explicit run materialized independently verified output and lineage")

        browser(["screenshot", str(EVIDENCE / "e2e-final.png")])
        visible = browser_eval(
            "({model:studio.snapshot().agentModel,ready:studio.snapshot().agentReady,"
            "draftCount:Number(document.querySelector('#draft-count').textContent),"
            "selected:studio.snapshot().selectedNode,previewRows:studio.state.preview?.rows?.length})"
        )
        receipt["browser"] = visible
        (EVIDENCE / "e2e-real.json").write_text(json.dumps(receipt, indent=2))
        print(json.dumps({"checks": len(receipt["checks"]), "model": receipt["model"], "rows": len(actual)}))
    finally:
        try:
            browser(["close"], timeout=10)
        except Exception:
            pass
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
