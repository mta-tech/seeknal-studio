"""Real IDE workflow acceptance against disposable, managed project copies.

This test makes one real, billable Sumopod request.  It never reads the API
key: only Studio's backend-owned worker may open the configured key file.
Canonical pilot/template/workspace files are hashed before and after the run
and are never used as mutable project roots.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[1]
PILOT_TEMPLATE = ROOT / "pilot" / "template"
PILOT_WORKSPACE = ROOT / "pilot" / "workspace"
EVIDENCE = ROOT / "evidence" / "ide-workflow"
PROJECT_A = EVIDENCE / "project-a"
PROJECT_B = EVIDENCE / "project-b"
PROJECTS_CONFIG = EVIDENCE / "private-projects.json"
AI_PROPOSAL = EVIDENCE / "ai-proposal.yml"
BASE = "http://127.0.0.1:18782"
PORT = 18782
SESSION = "seeknal-ide-workflow"
TRANSFORM = Path("seeknal/transforms/customer_value_pilot.yml")
TERMINAL = {"succeeded", "failed", "cancelled", "canceled", "success"}
AI_REQUEST = (
    "Add average_order_value to the selected transform SQL as AVG(o.amount) while preserving "
    "the existing kind, name, description, owner, tags, inputs, and every existing output "
    "column description. Add only the matching average_order_value output description. "
    "Create one reviewable draft and do not validate, apply, save, run, or execute SQL."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def record(receipt: dict[str, Any], name: str, evidence: Any) -> None:
    receipt["checks"].append({"name": name, "passed": True, "evidence": evidence})


def assert_that(condition: Any, message: str, evidence: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {evidence!r}")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def api(
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    base: str = BASE,
    timeout: float = 30,
    expected_error: bool = False,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        base + path,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": base,
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        detail = json.load(error)
        if expected_error:
            return error.code, detail
        raise AssertionError(f"{path}: HTTP {error.code}: {detail}") from error


def wait_for(
    getter: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    timeout: float,
    label: str,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = getter()
            if predicate(last):
                return last
        except (URLError, TimeoutError, ConnectionError, OSError):
            pass
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {label}: {last!r}")


def wait_api(path: str, predicate: Callable[[dict[str, Any]], bool], timeout: float, label: str) -> dict[str, Any]:
    return wait_for(lambda: api(path, timeout=10)[1], predicate, timeout=timeout, label=label)


def wait_job(job_id: str, timeout: float = 180) -> dict[str, Any]:
    return wait_api(
        f"/api/jobs/{quote(job_id)}",
        lambda value: value.get("status") in TERMINAL,
        timeout,
        f"job {job_id}",
    )


def browser(arguments: list[str], *, timeout: float = 60) -> str:
    result = subprocess.run(
        ["agent-browser", "--session", SESSION, *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def browser_eval(source: str, *, timeout: float = 60) -> Any:
    result = subprocess.run(
        ["agent-browser", "--session", SESSION, "--json", "eval", source],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    response = json.loads(result.stdout)
    assert_that(response.get("success"), "browser evaluation failed", response)
    return response.get("data", {}).get("result")


def prepare_project(destination: Path, name: str) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(PILOT_TEMPLATE, destination)
    target_transform = destination / TRANSFORM
    target_transform.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PILOT_WORKSPACE / TRANSFORM, target_transform)
    shutil.copytree(PILOT_WORKSPACE / "target", destination / "target")
    project_definition = destination / "seeknal_project.yml"
    project_definition.write_text(project_definition.read_text().replace("studio_agent_pilot", name, 1))
    canonical = str(PILOT_WORKSPACE.resolve())
    fixture = str(destination.resolve())
    for path in (destination / "target").rglob("*.json"):
        path.write_text(path.read_text().replace(canonical, fixture))
    drafts = destination / ".seeknal" / "drafts"
    if drafts.exists():
        shutil.rmtree(drafts)


def process_descendants(parent: int) -> set[int]:
    rows = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True).splitlines()
    children: dict[int, set[int]] = {}
    for row in rows:
        fields = row.split()
        if len(fields) == 2:
            children.setdefault(int(fields[1]), set()).add(int(fields[0]))
    result: set[int] = set()
    pending = list(children.get(parent, set()))
    while pending:
        pid = pending.pop()
        if pid in result:
            continue
        result.add(pid)
        pending.extend(children.get(pid, set()))
    return result


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def verify_standalone_unmanaged(project: Path, receipt: dict[str, Any]) -> None:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update({
        "SEEKNAL_STUDIO_PROJECT": str(project),
        "SEEKNAL_STUDIO_PORT": str(port),
        "SEEKNAL_STUDIO_AGENT_MODE": "external",
        "SEEKNAL_STUDIO_PROJECTS_FILE": str(PROJECTS_CONFIG),
    })
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        projects = wait_for(
            lambda: api("/api/projects", base=base, timeout=5)[1],
            lambda value: value.get("managed") is False,
            timeout=30,
            label="standalone project service",
        )
        assert_that(projects.get("can_switch") is False, "standalone server unexpectedly permits switching", projects)
        record(receipt, "standalone server reports project switching unmanaged", projects)
    finally:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)


def run_baseline_governance_unit_facts(receipt: dict[str, Any]) -> None:
    command = [
        sys.executable,
        "-m",
        "unittest",
        "tests.test_project_switch.ProjectSwitchTests.test_private_baseline_restores_parent_governance_after_pilot_switch",
        "tests.test_project_switch.ProjectSwitchTests.test_ordinary_project_environment_preserves_governance_values",
    ]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert_that(result.returncode == 0, "governance baseline facts failed", result.stdout + result.stderr)
    record(receipt, "pilot isolation clears only authorized fixtures and ordinary projects restore governance", {
        "command": command,
        "returncode": result.returncode,
        "summary": (result.stdout + result.stderr).strip().splitlines()[-1],
    })


def wait_repl_query(query_id: str, timeout: float = 120) -> dict[str, Any]:
    payload = wait_api(
        f"/api/repl/query?id={quote(query_id)}",
        lambda value: value.get("status") in TERMINAL,
        timeout,
        f"REPL query {query_id}",
    )
    ui = wait_for(
        lambda: browser_eval("({id:studio.state.repl.queryId,result:studio.state.repl.result,status:document.querySelector('#repl-result-status').textContent,error:document.querySelector('#repl-results').textContent})"),
        lambda value: value.get("id") is None,
        timeout=20,
        label="REPL result rendering",
    )
    return {"payload": payload, **ui}


def run_repl_sql(sql: str, timeout: float = 120) -> dict[str, Any]:
    query_id = browser_eval(
        f"(async()=>{{const e=document.querySelector('#repl-editor');e.value={json.dumps(sql)};"
        "e.dispatchEvent(new Event('input',{bubbles:true}));await studio.runReplQuery();return studio.state.repl.queryId})()"
    )
    assert_that(isinstance(query_id, str), "REPL did not accept query", {"sql": sql, "query_id": query_id})
    return wait_repl_query(query_id, timeout)


def safe_agent_session(session: dict[str, Any]) -> dict[str, Any]:
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
        "answer_sha256": hashlib.sha256(str(session.get("answer") or "").encode()).hexdigest(),
        "error": session.get("error"),
    }


def manual_snapshot_regression() -> None:
    """Provider-free proof that draft review can retain a dirty editor snapshot."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    prepare_project(PROJECT_B, "ide_manual_snapshot")
    transform = PROJECT_B / TRANSFORM
    original = transform.read_text()
    dirty = original.replace(
        "SUM(o.amount) AS lifetime_value,",
        "SUM(o.amount) AS lifetime_value, -- reviewed dirty snapshot",
        1,
    )
    assert_that(dirty != original, "manual regression could not construct a distinct buffer")
    environment = os.environ.copy()
    environment.update({
        "BROWSER": "/usr/bin/true",
        "PYTHONUNBUFFERED": "1",
        "SEEKNAL_STUDIO_AGENT_MODE": "external",
        "SEEKNAL_STUDIO_PROJECTS_FILE": str(PROJECTS_CONFIG),
    })
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    log_path = EVIDENCE / "manual-dirty-host.log"
    launcher: subprocess.Popen[str] | None = None
    result: dict[str, Any] = {
        "project": str(PROJECT_B.resolve()),
        "provider_request": "none",
        "checks": [],
    }
    try:
        with log_path.open("w") as log:
            launcher = subprocess.Popen(
                [sys.executable, str(ROOT / "launch.py"), "--browser", "--project", str(PROJECT_B), "--port", str(PORT)],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        wait_api("/api/state", lambda value: Path(value.get("project_path", "")).resolve() == PROJECT_B.resolve(), 45, "manual regression host")
        browser(["set", "viewport", "1584", "992"])
        browser(["open", BASE])
        browser_eval(
            "new Promise((resolve,reject)=>{let n=0;const t=setInterval(()=>{const s=studio?.snapshot();"
            "if(s?.selectedFile?.endsWith('customer_value_pilot.yml')){clearInterval(t);resolve(s)}"
            "else if(n++>240){clearInterval(t);reject(Error('manual regression UI timeout'))}},250)})",
            timeout=70,
        )
        browser_eval(
            f"(()=>{{const e=document.querySelector('#code-editor');e.value={json.dumps(dirty)};"
            "e.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#draft-menu-trigger').click();"
            "document.querySelector('#draft-from-editor').click();return true})()"
        )
        review = wait_for(
            lambda: browser_eval("({dialog:document.querySelector('#draft-dialog').open,diff:studio.state.currentDiff,dirty:studio.snapshot().dirty,buffer:document.querySelector('#code-editor').value})"),
            lambda value: value.get("dialog") is True and value.get("diff") is not None,
            timeout=30,
            label="manual editor draft review",
        )
        assert_that(review["dirty"] is True and review["buffer"] == dirty, "manual draft creation replaced the dirty buffer", review)
        assert_that(transform.read_text() == original, "manual draft creation wrote the source")
        browser_eval("document.querySelector('#dry-run-draft').click()")
        validation_id = wait_for(
            lambda: browser_eval("studio.state.draftValidationJobId"),
            lambda value: isinstance(value, str),
            timeout=10,
            label="manual dirty validation job",
        )
        validation = wait_job(validation_id)
        assert_that(validation.get("status") == "succeeded", "manual dirty draft dry run failed", validation)
        dry_state = browser_eval("({dirty:studio.snapshot().dirty,buffer:document.querySelector('#code-editor').value})")
        assert_that(dry_state == {"dirty": True, "buffer": dirty}, "Dry run discarded or changed the dirty snapshot", dry_state)
        assert_that(transform.read_text() == original, "Dry run wrote the source")
        wait_for(lambda: browser_eval("document.querySelector('#apply-reviewed-draft').disabled"), lambda value: value is False, timeout=20, label="manual Apply enabled")
        browser_eval("document.querySelector('#apply-reviewed-draft').click()")
        apply_id = wait_for(
            lambda: browser_eval("studio.state.activeJob?.action==='apply-draft'?studio.state.activeJob.id:null"),
            lambda value: isinstance(value, str),
            timeout=10,
            label="manual dirty apply job",
        )
        applied = wait_job(apply_id)
        assert_that(applied.get("status") == "succeeded", "manual dirty draft Apply failed", applied)
        applied_state = wait_for(
            lambda: browser_eval("({dirty:studio.snapshot().dirty,buffer:document.querySelector('#code-editor').value,graphState:document.querySelector('#graph-state').textContent})"),
            lambda value: value.get("dirty") is False and "buffer modified" not in value.get("graphState", ""),
            timeout=20,
            label="post-Apply editor synchronization",
        )
        assert_that(transform.read_text() == dirty and applied_state["buffer"] == dirty, "only Apply did not write and synchronize the exact snapshot", applied_state)
        result["checks"] = [
            "dirty buffer preserved when draft is created",
            "Dry run validates the reviewed snapshot without a source write",
            "explicit Apply alone writes the exact snapshot and clears dirty state",
        ]
        result.update({
            "result": "passed",
            "validation_job": validation_id,
            "apply_job": apply_id,
            "applied_sha256": sha256(transform),
        })
        browser(["screenshot", str(EVIDENCE / "manual-dirty-apply.png")])
    except Exception as error:
        result.update({"result": "failed", "failure": f"{type(error).__name__}: {error}"})
        raise
    finally:
        try:
            browser(["close"], timeout=10)
        except Exception:
            pass
        if launcher is not None and launcher.poll() is None:
            os.killpg(launcher.pid, signal.SIGTERM)
            launcher.wait(timeout=20)
        (EVIDENCE / "manual-dirty-regression.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


def main(*, replay_ai_proposal: bool = False) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    previous_receipt = json.loads((EVIDENCE / "receipt.json").read_text()) if replay_ai_proposal else None
    prior_ai_check = next(
        (
            item for item in (previous_receipt or {}).get("checks", [])
            if item.get("name") in {
                "toolbar AI revised SQL from the dirty buffer without saving or executing",
                "captured real AI proposal replayed on source-frozen dirty workflow without a second provider request",
            }
        ),
        None,
    )
    if replay_ai_proposal:
        assert_that(prior_ai_check is not None, "prior real AI turn receipt is unavailable")
        assert_that(AI_PROPOSAL.is_file(), "captured real AI proposal is unavailable", AI_PROPOSAL)
        prior_ai_evidence = prior_ai_check["evidence"]
        assert_that(
            sha256(AI_PROPOSAL) == prior_ai_evidence.get("draft_sha256", prior_ai_evidence.get("proposal_sha256")),
            "captured AI proposal does not match the real-turn receipt",
        )
    PROJECTS_CONFIG.unlink(missing_ok=True)
    canonical_before = {
        "template": tree_hashes(PILOT_TEMPLATE),
        "workspace": tree_hashes(PILOT_WORKSPACE),
    }
    prepare_project(PROJECT_A, "ide_workflow_project_a")
    prepare_project(PROJECT_B, "ide_workflow_project_b")
    (PROJECT_B / TRANSFORM).write_text(
        (PROJECT_B / TRANSFORM).read_text().replace("Per-customer lifetime value", "Project B per-customer lifetime value", 1)
    )
    receipt: dict[str, Any] = {
        "scope": "disposable managed IDE projects only",
        "provider": "Sumopod OpenAI-compatible",
        "model": "deepseek-v4.1-flash:netra",
        "credential_evidence": "backend-only key file; value was not read or recorded by this test",
        "ai_execution": "captured real turn replay; no new provider request" if replay_ai_proposal else "one real provider request",
        "projects": {"a": str(PROJECT_A.resolve()), "b": str(PROJECT_B.resolve())},
        "preferences": {
            "path": str(PROJECTS_CONFIG.resolve()),
            "operator_preferences_touched": False,
        },
        "checks": [],
    }
    verify_standalone_unmanaged(PROJECT_A, receipt)
    run_baseline_governance_unit_facts(receipt)

    environment = os.environ.copy()
    environment.update({
        "BROWSER": "/usr/bin/true",
        "PYTHONUNBUFFERED": "1",
        "SEEKNAL_STUDIO_PROJECTS_FILE": str(PROJECTS_CONFIG),
    })
    # The disposable synthetic projects have no Atlas contract.  The launcher
    # under test receives a scoped environment; the parent shell is untouched.
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    server_log = EVIDENCE / "managed-host.log"
    launcher: subprocess.Popen[str] | None = None
    try:
        with server_log.open("w") as log:
            launcher = subprocess.Popen(
                [sys.executable, str(ROOT / "launch.py"), "--browser", "--project", str(PROJECT_A), "--port", str(PORT)],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        initial_state = wait_api("/api/state", lambda value: Path(value.get("project_path", "")).resolve() == PROJECT_A.resolve(), 45, "managed project A")
        projects = api("/api/projects")[1]
        assert_that(projects.get("managed") is True and projects.get("can_switch") is True, "managed launcher did not enable switching", projects)
        record(receipt, "launch.py --browser owns a same-port managed project host", {"state": initial_state, "projects": projects})

        agent = wait_api("/api/agent/status", lambda value: value.get("ready") is True, 240, "project A agent readiness")
        assert_that(agent.get("provider") == "openai", "unexpected provider", agent)
        assert_that(agent.get("model") == receipt["model"], "unexpected model", agent)
        assert_that(agent.get("project_verified") is True, "agent project identity unverified", agent)
        assert_that(Path(agent.get("project_path", "")).resolve() == PROJECT_A.resolve(), "agent bound to wrong project", agent)
        tools = set(agent.get("capabilities", {}).get("tools", []))
        assert_that(tools and not any(part in tool.lower() for tool in tools for part in ("run", "apply", "execute", "shell", "python")), "agent received execution tools", tools)
        record(receipt, "backend-only Sumopod agent is exact-model, canonical-project, and draft-only", {
            "provider": agent.get("provider"),
            "model": agent.get("model"),
            "project_path": agent.get("project_path"),
            "project_fingerprint": agent.get("project_fingerprint"),
            "tools": sorted(tools),
        })

        browser(["set", "viewport", "1584", "992"])
        browser(["open", BASE])
        browser_eval(
            "new Promise((resolve,reject)=>{let n=0;const t=setInterval(()=>{const s=studio?.snapshot();"
            "if(s?.agentReady&&s.nodes===3&&s.selectedFile?.endsWith('customer_value_pilot.yml')){clearInterval(t);resolve(s)}"
            "else if(n++>480){clearInterval(t);reject(Error('Studio readiness timeout'))}},250)})",
            timeout=130,
        )
        toolbar = browser_eval("({draft:document.querySelector('#draft-editor').textContent.trim(),dry:document.querySelector('#dry-run-draft').textContent.trim(),apply:document.querySelector('#apply-reviewed-draft').textContent.trim(),menu:document.querySelector('#menu-open-project').textContent.trim(),repl:document.querySelector('#repl-tab').textContent.trim()})")
        assert_that(toolbar == {"draft": "Draft", "dry": "Dry run", "apply": "Apply", "menu": "Open Project… ⌘O", "repl": "SQL REPL"}, "primary IDE affordances are not obvious", toolbar)
        record(receipt, "Draft, Dry run, Apply, SQL REPL, and File Open Project are first-class controls", toolbar)

        original_path = PROJECT_A / TRANSFORM
        original_text = original_path.read_text()
        original_document = yaml.safe_load(original_text)
        dirty_text = AI_PROPOSAL.read_text() if replay_ai_proposal else original_text + "\n# unsaved editor context retained through AI draft\n"
        browser_eval(
            f"(()=>{{const e=document.querySelector('#code-editor');e.value={json.dumps(dirty_text)};"
            "e.dispatchEvent(new Event('input',{bubbles:true}));return studio.snapshot()})()"
        )
        assert_that(browser_eval("studio.snapshot().dirty") is True, "editor did not become dirty")
        if replay_ai_proposal:
            browser_eval("(async()=>{await studio.createDraftFromEditor();return true})()")
        else:
            browser_eval("(()=>{document.querySelector('#draft-menu-trigger').click();document.querySelector('#draft-sql-ai').click();return true})()")
            browser_eval(
                f"(()=>{{document.querySelector('#sql-ai-request').value={json.dumps(AI_REQUEST)};"
                "document.querySelector('#sql-ai-send').click();return true})()"
            )
        ai_ui = wait_for(
            lambda: browser_eval("({busy:studio.state.agent.busy,drafts:studio.state.drafts.length,dialog:document.querySelector('#draft-dialog').open,dirty:studio.snapshot().dirty,buffer:document.querySelector('#code-editor').value})"),
            lambda value: value.get("busy") is False and value.get("drafts") == 1 and value.get("dialog") is True,
            timeout=720,
            label="captured or real AI SQL draft",
        )
        assert_that(ai_ui["dirty"] is True and ai_ui["buffer"] == dirty_text, "AI draft replaced the dirty editor buffer", ai_ui)
        assert_that(original_path.read_text() == original_text, "AI draft implicitly wrote the active transform")
        draft_path = PROJECT_A / browser_eval("studio.state.currentDiff.path")
        assert_that(draft_path.is_file(), "AI did not create a reviewable draft", draft_path)
        draft_document = yaml.safe_load(draft_path.read_text())
        for key in ("kind", "name", "description", "owner", "tags", "inputs"):
            assert_that(draft_document.get(key) == original_document.get(key), f"AI changed preserved YAML field {key}", draft_document.get(key))
        original_columns = original_document["output"]["columns"]
        draft_columns = draft_document["output"]["columns"]
        assert_that(all(draft_columns.get(key) == value for key, value in original_columns.items()), "AI changed existing output metadata", draft_columns)
        assert_that("average_order_value" in draft_columns and "average_order_value" in draft_document["transform"], "AI omitted requested average column", draft_document)
        if replay_ai_proposal:
            record(receipt, "captured real AI proposal replayed on source-frozen dirty workflow without a second provider request", {
                "request": AI_REQUEST,
                "proposal_sha256": sha256(draft_path),
                "active_sha256": sha256(original_path),
                "real_turn": prior_ai_evidence.get("agent_turn", prior_ai_evidence.get("real_turn")),
                "real_turn_tools": prior_ai_evidence.get("tools", prior_ai_evidence.get("real_turn_tools")),
                "source_receipt": "evidence/ide-workflow/receipt.json (previous completed run)",
            })
        else:
            session_id = browser_eval("studio.state.agent.sessionId")
            session = api(f"/api/agent/sessions/{quote(session_id)}")[1]
            assert_that(session.get("status") == "succeeded", "real AI SQL turn failed", safe_agent_session(session))
            used_tools = {
                event.get("data", {}).get("name")
                for event in session.get("events", [])
                if isinstance(event.get("data"), dict) and event.get("data", {}).get("name")
            }
            assert_that(used_tools <= tools and used_tools, "AI used tools outside the advertised draft-only set", used_tools)
            record(receipt, "toolbar AI revised SQL from the dirty buffer without saving or executing", {
                "request": AI_REQUEST,
                "draft": draft_path.relative_to(PROJECT_A).as_posix(),
                "draft_sha256": sha256(draft_path),
                "active_sha256": sha256(original_path),
                "agent_turn": safe_agent_session(session),
                "tools": sorted(used_tools),
            })

        review = browser_eval("studio.state.currentDiff")
        status, stale = api("/api/jobs", {
            "action": "apply-draft",
            "path": review["path"],
            "content_hash": "0" * 64,
            "destination_hash": review.get("destination_hash"),
            "confirmed": True,
        }, expected_error=True)
        assert_that(status == 400 and "draft changed" in stale.get("error", ""), "stale draft hash was accepted", stale)
        status, premature = api("/api/jobs", {
            "action": "apply-draft",
            "path": review["path"],
            "content_hash": review["content_hash"],
            "destination_hash": review.get("destination_hash"),
            "confirmed": True,
        }, expected_error=True)
        assert_that(status == 400 and "validate this exact draft" in premature.get("error", ""), "unvalidated draft was accepted", premature)
        browser_eval("document.querySelector('#dry-run-draft').click()")
        validation_id = wait_for(
            lambda: browser_eval("studio.state.draftValidationJobId"),
            lambda value: isinstance(value, str),
            timeout=10,
            label="draft validation job",
        )
        validation = wait_job(validation_id)
        assert_that(validation.get("status") == "succeeded" and validation.get("exit_code") == 0, "draft dry run failed", validation)
        after_dry_run = browser_eval("({dirty:studio.snapshot().dirty,buffer:document.querySelector('#code-editor').value})")
        assert_that(after_dry_run["dirty"] is True and after_dry_run["buffer"] == dirty_text, "Dry run did not preserve the reviewed dirty editor snapshot", after_dry_run)
        assert_that(original_path.read_text() == original_text, "Dry run implicitly wrote the active transform")
        wait_for(lambda: browser_eval("document.querySelector('#apply-reviewed-draft').disabled"), lambda value: value is False, timeout=15, label="enabled Apply button")
        browser_eval("document.querySelector('#apply-reviewed-draft').click()")
        apply_id = wait_for(
            lambda: browser_eval("studio.state.activeJob?.action==='apply-draft'?studio.state.activeJob.id:null"),
            lambda value: isinstance(value, str),
            timeout=10,
            label="apply job",
        )
        applied = wait_job(apply_id)
        assert_that(applied.get("status") == "succeeded", "explicit Apply failed", applied)
        assert_that(yaml.safe_load(original_path.read_text()) == draft_document, "applied transform differs from reviewed draft")
        applied_editor = wait_for(
            lambda: browser_eval("({dirty:studio.snapshot().dirty,buffer:document.querySelector('#code-editor').value})"),
            lambda value: value.get("dirty") is False and value.get("buffer") == original_path.read_text(),
            timeout=20,
            label="applied editor synchronization",
        )
        assert_that(applied_editor["dirty"] is False and applied_editor["buffer"] == original_path.read_text(), "Apply did not synchronize the editor to the exact applied draft", applied_editor)
        record(receipt, "content hashes gate Apply and the visible Dry run then Apply sequence succeeds", {
            "stale_hash_status": status,
            "validation": {"id": validation_id, "status": validation["status"]},
            "apply": {"id": apply_id, "status": applied["status"], "sha256": sha256(original_path)},
            "dirty_preserved_through_dry_run": True,
            "editor_synchronized_only_after_apply": True,
        })

        browser_eval("(async()=>{await studio.startJob('run');return true})()")
        run_id = browser_eval("studio.state.activeJob.id")
        run = wait_job(run_id, 240)
        assert_that(run.get("status") == "succeeded" and run.get("exit_code") == 0, "explicit pipeline Run failed", run)
        preview = api("/api/preview?node=transform.customer_value_pilot")[1]
        assert_that(preview.get("fresh") is True and preview.get("row_count") == 3, "fresh three-row output missing", preview)
        assert_that("average_order_value" in preview.get("columns", []), "AI SQL result omitted average_order_value", preview)
        record(receipt, "explicit Run materialized the real three-row AI-revised output", {"job_id": run_id, "columns": preview.get("columns"), "rows": preview.get("rows")})

        browser_eval("studio.setMainView('repl')")
        browser_eval("(async()=>{await studio.startRepl();return true})()")
        repl_ready = wait_api("/api/repl/status", lambda value: value.get("state") == "ready", 90, "project-bound REPL")
        assert_that(Path(repl_ready.get("project_path", "")).resolve() == PROJECT_A.resolve(), "REPL bound to wrong project", repl_ready)
        generation = repl_ready.get("generation")
        tables = run_repl_sql(".tables")
        table_rows = tables["payload"].get("rows", [])
        assert_that(any(row[0] == "source_orders" for row in table_rows), "source_orders absent from .tables", table_rows)
        schema = run_repl_sql(".schema source_orders")
        assert_that("order_id" in {row[0] for row in schema["payload"].get("rows", [])}, "source_orders schema missing order_id", schema)
        query = run_repl_sql("SELECT customer_id, average_order_value FROM customer_value_pilot ORDER BY customer_id")
        assert_that(query["payload"].get("row_count") == 3 and query["payload"].get("generation") == generation, "persistent REPL did not return three current rows", query)
        invalid = run_repl_sql("SELECT definitely_missing FROM source_orders")
        assert_that(invalid["payload"].get("status") == "failed" and invalid.get("status") == "Error" and "definitely_missing" in invalid.get("error", ""), "invalid SQL error not visible", invalid)
        record(receipt, "persistent REPL supports tables, schema, current three-row SELECT, and visible SQL errors", {
            "session_id": repl_ready.get("session_id"),
            "generation": generation,
            "run_id": repl_ready.get("current_run_id"),
            "tables": table_rows,
            "schema_columns": [row[0] for row in schema["payload"].get("rows", [])],
            "query_rows": query["payload"].get("rows"),
            "history_length": browser_eval("studio.state.repl.history.length"),
        })

        browser_eval("studio.setMainView('pipeline')")
        browser_eval("(async()=>{await studio.sendSqlToRepl();return true})()")
        extracted = browser_eval("({sql:document.querySelector('#repl-editor').value,meta:document.querySelector('#repl-query-meta').textContent,result:studio.state.repl.result})")
        assert_that("source_orders" in extracted["sql"] and "source.orders" not in extracted["sql"], "Send SQL to REPL did not AST-rewrite source.orders", extracted)
        assert_that("reference rewrite" in extracted["meta"], "REPL rewrite provenance is absent", extracted)
        prior_result = extracted["result"]
        assert_that(prior_result is not None, "Send SQL to REPL unexpectedly cleared prior result")
        extracted_query_id = browser_eval("(async()=>{await studio.runReplQuery();return studio.state.repl.queryId})()")
        extracted_result = wait_repl_query(extracted_query_id, 120)
        assert_that(extracted_result["payload"].get("row_count") == 3, "explicit Run of extracted transform SQL did not return three rows", extracted_result)
        record(receipt, "Send SQL to REPL fills only, shows AST rewrite provenance, then explicit Run executes", {
            "sql_sha256": hashlib.sha256(extracted["sql"].encode()).hexdigest(),
            "meta": extracted["meta"],
            "rows": extracted_result["payload"].get("row_count"),
        })

        # Cancel a genuinely running query, then prove the worker recovered.
        cancel_query_id = browser_eval(
            "(async()=>{const e=document.querySelector('#repl-editor');e.value='SELECT sum(i) FROM range(1000000000000) AS values(i)';"
            "e.dispatchEvent(new Event('input',{bubbles:true}));await studio.runReplQuery();return studio.state.repl.queryId})()"
        )
        assert_that(isinstance(cancel_query_id, str), "long REPL query was not accepted", cancel_query_id)
        wait_api(f"/api/repl/query?id={quote(cancel_query_id)}", lambda value: value.get("status") == "running", 15, "running cancellable REPL query")
        browser_eval("document.querySelector('#repl-cancel').click()")
        cancelled = wait_api(f"/api/repl/query?id={quote(cancel_query_id)}", lambda value: value.get("status") == "cancelled", 30, "REPL cancellation")
        wait_for(lambda: browser_eval("studio.state.repl.queryId"), lambda value: value is None, timeout=30, label="REPL cancellation rendering")
        recovered = run_repl_sql("SELECT 1 AS recovered")
        assert_that(cancelled.get("status") == "cancelled" and recovered["payload"].get("rows") == [[1]], "REPL did not recover after cancellation", recovered)
        recovered_generation = recovered["payload"].get("generation")
        assert_that(recovered_generation > generation, "cancellation did not hard-restart the REPL worker", recovered)
        record(receipt, "running query cancellation hard-restarts a clean persistent REPL worker", {"before_generation": generation, "after_generation": recovered_generation})

        # A new pipeline run makes the bound output snapshot stale until the
        # user explicitly reloads it.
        browser_eval("studio.setMainView('pipeline')")
        browser_eval("(async()=>{await studio.startJob('run');return true})()")
        refresh_run_id = browser_eval("studio.state.activeJob.id")
        refresh_run = wait_job(refresh_run_id, 240)
        assert_that(refresh_run.get("status") == "succeeded", "refresh run failed", refresh_run)
        stale_status = wait_api("/api/repl/status", lambda value: value.get("needs_reload") is True, 30, "REPL reload requirement")
        stale_query_status, stale_query = api("/api/repl/query", {"sql": "SELECT 1"})
        stale_id = stale_query.get("query_id")
        stale_result = wait_api(f"/api/repl/query?id={quote(stale_id)}", lambda value: value.get("status") in TERMINAL, 30, "stale REPL rejection")
        assert_that(stale_result.get("error_code") == "reload_required", "stale context query was not rejected", stale_result)
        browser_eval("studio.setMainView('repl')")
        browser_eval("document.querySelector('#repl-reload').click()")
        reloaded = wait_api("/api/repl/status", lambda value: value.get("state") == "ready" and value.get("needs_reload") is False, 90, "REPL reload")
        assert_that(reloaded.get("generation") > recovered_generation, "REPL reload did not create a fresh generation", reloaded)
        wait_for(
            lambda: browser_eval("document.querySelector('#repl-status').textContent"),
            lambda value: value == "Ready",
            timeout=30,
            label="REPL Ready UI",
        )
        record(receipt, "run changes require explicit REPL reload and preserve run/session provenance", {
            "stale_bound_run_id": stale_status.get("bound_run_id"),
            "stale_current_run_id": stale_status.get("current_run_id"),
            "stale_error_code": stale_result.get("error_code"),
            "reloaded_generation": reloaded.get("generation"),
            "reloaded_run_id": reloaded.get("current_run_id"),
        })

        browser(["screenshot", str(EVIDENCE / "project-a-repl.png")])
        old_runtime = api("/api/state")[1]["runtime_id"]
        old_descendants = process_descendants(launcher.pid)

        # File -> Open Project: keyboard shortcut, invalid path, and cancel all
        # retain A; a valid B selection restarts the same managed port.
        browser_eval("document.dispatchEvent(new KeyboardEvent('keydown',{key:'o',metaKey:true,bubbles:true}))")
        assert_that(browser_eval("document.querySelector('#project-dialog').open") is True, "Cmd+O did not open project dialog")
        browser_eval("(()=>{const i=document.querySelector('#project-path-input');i.value='/definitely/not/a/seeknal/project';document.querySelector('#project-open').click();return true})()")
        time.sleep(0.5)
        assert_that(Path(api("/api/state")[1]["project_path"]).resolve() == PROJECT_A.resolve(), "invalid project changed current binding")
        browser_eval("document.querySelector('#project-dialog').close()")
        assert_that(Path(api("/api/state")[1]["project_path"]).resolve() == PROJECT_A.resolve(), "cancel changed current project")
        browser_eval("studio.openProjectDialog()")
        browser_eval(f"(()=>{{document.querySelector('#project-path-input').value={json.dumps(str(PROJECT_B.resolve()))};document.querySelector('#project-open').click();return true}})()")
        switched_state = wait_api("/api/state", lambda value: Path(value.get("project_path", "")).resolve() == PROJECT_B.resolve(), 90, "managed switch to project B")
        assert_that(switched_state.get("runtime_id") != old_runtime, "project switch reused old backend runtime", switched_state)
        wait_for(lambda: old_descendants, lambda pids: all(not process_exists(pid) for pid in pids), timeout=30, label="old project workers reaped")
        browser(["open", BASE])
        browser_eval(
            "new Promise((resolve,reject)=>{let n=0;const t=setInterval(()=>{const s=studio?.snapshot();if(s?.selectedFile&&s.nodes===3){clearInterval(t);resolve(s)}else if(n++>360){clearInterval(t);reject(Error('project B browser timeout'))}},250)})",
            timeout=100,
        )
        agent_b = wait_api("/api/agent/status", lambda value: value.get("ready") is True, 240, "project B agent readiness")
        assert_that(Path(agent_b.get("project_path", "")).resolve() == PROJECT_B.resolve() and agent_b.get("project_verified") is True, "project B agent identity invalid", agent_b)
        wait_for(
            lambda: browser_eval("studio.snapshot().agentReady"),
            lambda value: value is True,
            timeout=60,
            label="project B verified agent UI",
        )
        reset = browser_eval("({dirty:studio.snapshot().dirty,jobs:studio.state.jobs.length,replHistory:studio.state.repl.history.length,replResult:studio.state.repl.result,replEditor:document.querySelector('#repl-editor').value,selected:studio.snapshot().selectedFile})")
        assert_that(reset["dirty"] is False and reset["jobs"] == 0 and reset["replHistory"] == 0 and reset["replResult"] is None and reset["replEditor"] == "", "project A browser/runtime state bled into B", reset)
        repl_b = api("/api/repl/status")[1]
        assert_that(repl_b.get("state") == "stopped" and Path(repl_b.get("project_path", "")).resolve() == PROJECT_B.resolve(), "project B REPL was not reset and rebound", repl_b)
        record(receipt, "File Open Project handles shortcut/invalid/cancel and A to B rebind without state bleed", {
            "old_runtime_id": old_runtime,
            "new_runtime_id": switched_state.get("runtime_id"),
            "old_processes_reaped": sorted(old_descendants),
            "agent_b_project": agent_b.get("project_path"),
            "reset": reset,
            "repl_b": repl_b,
        })
        browser(["screenshot", str(EVIDENCE / "project-b-fresh.png")])

        canonical_after = {
            "template": tree_hashes(PILOT_TEMPLATE),
            "workspace": tree_hashes(PILOT_WORKSPACE),
        }
        assert_that(canonical_after == canonical_before, "canonical pilot/template files were mutated")
        receipt["canonical_hashes"] = canonical_after
        record(receipt, "canonical pilot template and prior workspace evidence are byte-for-byte unchanged", {
            "template_files": len(canonical_after["template"]),
            "workspace_files": len(canonical_after["workspace"]),
        })
        receipt["result"] = "passed"
    except Exception as error:
        receipt["result"] = "failed"
        receipt["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        try:
            browser(["close"], timeout=10)
        except Exception:
            pass
        if launcher is not None and launcher.poll() is None:
            os.killpg(launcher.pid, signal.SIGTERM)
            try:
                launcher.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(launcher.pid, signal.SIGKILL)
                launcher.wait(timeout=10)
        (EVIDENCE / "receipt.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps({"result": receipt["result"], "checks": len(receipt["checks"]), "receipt": str(EVIDENCE / "receipt.json")}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manual-snapshot-regression",
        action="store_true",
        help="exercise dirty Draft -> Dry run -> Apply without a provider request",
    )
    parser.add_argument(
        "--replay-ai-proposal",
        action="store_true",
        help="replay the hash-bound captured real AI proposal without another provider request",
    )
    arguments = parser.parse_args()
    if arguments.manual_snapshot_regression:
        manual_snapshot_regression()
    else:
        main(replay_ai_proposal=arguments.replay_ai_proposal)
