"""Seeknal Studio acceptance against the isolated managed Seeknal 2.14 runtime.

This test uses disposable copies of the seven-definition demo project. It makes
one real model turn through the backend-owned key-file contract and never reads
credential bytes itself.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_access import TOKEN_HEADER, link_secret, read_access_link  # noqa: E402
from runtime_resolver import resolve_runtime  # noqa: E402


EVIDENCE = ROOT / "evidence" / "seeknal-2.14"
PROJECT_A = EVIDENCE / "project-a"
PROJECT_B = EVIDENCE / "project-b"
PROJECTS_FILE = EVIDENCE / "projects.json"
SESSION = "seeknal-sdk214"
TERMINAL = {"succeeded", "success", "failed", "cancelled", "canceled"}
NAME = "sdk214_paid_orders"
DRAFT = f".seeknal/drafts/draft_transform_{NAME}.yml"
ACTIVE = f"seeknal/transforms/{NAME}.yml"
# The launcher's per-launch browser secret, sent as a header by api().
ACCESS: dict[str, str] = {}
TRANSFORM = f'''kind: transform
name: {NAME}
description: Managed Seeknal 2.14 browser acceptance
inputs:
  - ref: source.orders
transform: |
  SELECT customer_id, SUM(amount) AS total_amount, COUNT(*) AS paid_order_count
  FROM input_0
  WHERE status = 'paid'
  GROUP BY customer_id
output:
  columns:
    customer_id: Customer identifier
    total_amount: Paid order total
    paid_order_count: Paid order count
'''
AGENT_PROMPT = (
    "Call read_pipeline exactly once for transform.customer_value_pilot in this isolated synthetic project. "
    "Do not create or write a draft and do not run anything. Reply with the transform name and say "
    "that no changes were made."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def assert_that(condition: Any, message: str, evidence: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {evidence!r}")


def record(receipt: dict[str, Any], name: str, evidence: Any) -> None:
    receipt["checks"].append({"name": name, "passed": True, "evidence": evidence})


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def api(
    base: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 30,
) -> tuple[int, dict[str, Any]]:
    request = Request(
        base + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Accept": "application/json", "Content-Type": "application/json", "Origin": base, **ACCESS},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        detail = json.load(error)
        raise AssertionError(f"{path}: HTTP {error.code}: {detail}") from error


def launcher_link(log: Path) -> str:
    """Wait for launch.py's private one-time link and authorize api() with it."""
    link = wait_for(
        lambda: read_access_link(log.read_text(errors="replace")), bool, timeout=120, label="launcher access link",
    )
    ACCESS[TOKEN_HEADER] = link_secret(link)
    return link


def wait_for(
    getter: Callable[[], Any], predicate: Callable[[Any], bool], *, timeout: float, label: str,
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


def wait_job(base: str, job_id: str, timeout: float = 180) -> dict[str, Any]:
    return wait_for(
        lambda: api(base, f"/api/jobs/{quote(job_id)}", timeout=10)[1],
        lambda value: value.get("status") in TERMINAL,
        timeout=timeout,
        label=f"job {job_id}",
    )


def browser(arguments: list[str], *, timeout: float = 60) -> str:
    result = subprocess.run(
        ["agent-browser", "--session", SESSION, *arguments],
        capture_output=True, text=True, check=True, timeout=timeout,
    )
    return result.stdout.strip()


def browser_eval(source: str, *, timeout: float = 60) -> Any:
    result = subprocess.run(
        ["agent-browser", "--session", SESSION, "--json", "eval", source],
        capture_output=True, text=True, check=True, timeout=timeout,
    )
    payload = json.loads(result.stdout)
    assert_that(payload.get("success"), "browser evaluation failed", payload)
    return payload.get("data", {}).get("result")


def copy_seven_definition_project(destination: Path, name: str) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(ROOT / "pilot" / "template", destination)
    transform = destination / "seeknal" / "transforms" / "customer_value_pilot.yml"
    transform.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "pilot" / "workspace" / "seeknal" / "transforms" / transform.name, transform)
    definition = (destination / "seeknal_project.yml").read_text()
    (destination / "seeknal_project.yml").write_text(
        definition.replace("name: studio_agent_pilot", f"name: {name}", 1)
    )
    definitions = sorted((destination / "seeknal").rglob("*.yml"))
    assert_that(len(definitions) == 3, "isolated authority fixture plus known transform must contain three definitions", definitions)
    assert_that(transform.is_file(), "known model transform missing")
    assert_that(not (destination / "seeknal/sources/data.yml").exists(), "missing-data user definition leaked into isolated fixture")


def start_managed(project: Path, port: int, *, external_agent: bool = False) -> tuple[subprocess.Popen[str], Any]:
    log_handle = (EVIDENCE / "managed.log").open("w")
    environment = os.environ.copy()
    environment.update({
        "BROWSER": "/usr/bin/true",
        "PYTHONUNBUFFERED": "1",
        "SEEKNAL_STUDIO_PROJECTS_FILE": str(PROJECTS_FILE),
    })
    if external_agent:
        environment["SEEKNAL_STUDIO_AGENT_MODE"] = "external"
    for key in (
        "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__", "SEEKNAL_SIGNAL",
        "ATLAS_API_URL", "ATLAS_PORTAL_URL",
    ):
        environment.pop(key, None)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "launch.py"), "--browser", "--project", str(project), "--port", str(port)],
        cwd=ROOT, env=environment, stdout=log_handle, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    return process, log_handle


def browser_job(action: str, *, after: str | None = None, timeout: float = 180) -> dict[str, Any]:
    return wait_for(
        lambda: browser_eval("(()=>{const j=studio.state.activeJob;return j?{id:j.id,action:j.action,status:j.status,logs:j.logs||''}:null})()"),
        lambda value: bool(value and value.get("action") == action and value.get("status") in TERMINAL and value.get("id") != after),
        timeout=timeout, label=f"browser {action} job",
    )


def run_cli_browser_flow(base: str, receipt: dict[str, Any]) -> None:
    browser(["set", "viewport", "1584", "992"])
    browser(["open", base])
    wait_for(
        lambda: browser_eval("studio?.snapshot?.()"),
        lambda value: bool(value and value.get("nodes") == 3),
        timeout=60, label="authority-template Studio browser",
    )
    created = browser_eval(
        f'''(async()=>{{document.querySelector('#draft-editor').click();
        for(let i=0;i<240&&!studio.state.draftOptions.length;i++)await new Promise(r=>setTimeout(r,50));
        const type=document.querySelector('#new-draft-type'),name=document.querySelector('#new-draft-name');
        type.value='transform';type.dispatchEvent(new Event('change',{{bubbles:true}}));
        name.value={json.dumps(NAME)};name.dispatchEvent(new Event('input',{{bubbles:true}}));
        await studio.submitCliDraft();return studio.state.activeJob?.id}})()'''
    )
    draft_job = browser_job("draft")
    assert_that(draft_job["id"] == created and draft_job["status"] == "succeeded", "YAML draft failed", draft_job)
    wait_for(
        lambda: browser_eval("studio.state.selectedFile"), lambda value: value == DRAFT,
        timeout=30, label="generated YAML editor",
    )
    browser_eval("document.querySelector('#draft-dialog').close()")
    saved = browser_eval(
        f'''(async()=>{{const e=document.querySelector('#code-editor');e.value={json.dumps(TRANSFORM)};
        e.dispatchEvent(new Event('input',{{bubbles:true}}));await studio.save();
        return {{dirty:studio.snapshot().dirty,diff:studio.state.currentDiff?.path}}}})()'''
    )
    assert_that(saved == {"dirty": False, "diff": DRAFT}, "managed YAML Save/hash refresh failed", saved)
    previous = draft_job["id"]
    browser_eval("(async()=>{await studio.openDraftReview();document.querySelector('#dry-run-draft').click();return true})()")
    dry = browser_job("validate-draft", after=previous)
    assert_that(dry["status"] == "succeeded", "2.14 Dry run failed", dry)
    assert_that(all(value in dry["logs"] for value in ("C001", "C002", "C003")), "Dry run lacks actual preview rows", dry)
    wait_for(
        lambda: browser_eval("document.querySelector('#apply-reviewed-draft').disabled"),
        lambda value: value is False, timeout=30, label="Apply enabled",
    )
    browser_eval("document.querySelector('#apply-reviewed-draft').click()")
    applied = browser_job("apply-draft", after=dry["id"])
    assert_that(applied["status"] == "succeeded", "2.14 Apply failed", applied)
    browser_eval("document.querySelector('#run-button').click()")
    run = browser_job("run", after=applied["id"], timeout=240)
    assert_that(run["status"] == "succeeded" and "sdk214_paid_orders" in run["logs"], "2.14 Run failed", run)

    browser_eval("studio.setMainView('repl')")
    browser_eval("(async()=>{await studio.startRepl();return true})()")
    wait_for(
        lambda: api(base, "/api/repl/status")[1],
        lambda value: value.get("state") == "ready", timeout=90, label="2.14 REPL",
    )
    query_id = browser_eval(
        f'''(async()=>{{const e=document.querySelector('#repl-editor');e.value='SELECT * FROM {NAME} ORDER BY customer_id';
        e.dispatchEvent(new Event('input',{{bubbles:true}}));await studio.runReplQuery();return studio.state.repl.queryId}})()'''
    )
    query = wait_for(
        lambda: api(base, f"/api/repl/query?id={quote(query_id)}")[1],
        lambda value: value.get("status") in TERMINAL, timeout=120, label="2.14 REPL query",
    )
    assert_that(query.get("status") == "succeeded" and query.get("row_count") == 3, "2.14 REPL query failed", query)
    record(receipt, "managed 2.14 YAML Draft Save Dry run Apply Run and REPL passed", {
        "draft": draft_job, "dry_run": dry, "apply": applied, "run": run,
        "query": {"columns": query.get("columns"), "rows": query.get("rows")},
    })

    python_status, python_payload = api(base, "/api/jobs", {
        "action": "draft", "node_type": "transform", "name": "sdk214_python",
        "description": "SDK 2.14 Python scaffold", "python": True, "deps": "pandas",
    })
    assert_that(python_status == 202, "Python draft was not queued", python_payload)
    python_job = wait_job(base, (python_payload.get("job") or python_payload)["id"])
    python_path = PROJECT_A / python_job["draft_path"]
    assert_that(python_job["status"] == "succeeded" and "# /// script" in python_path.read_text(), "real Python draft failed", python_job)
    record(receipt, "managed 2.14 real Python CLI scaffold includes PEP 723 metadata", {
        "job": python_job, "sha256": sha256(python_path),
    })


def run_one_real_agent_turn(base: str, receipt: dict[str, Any]) -> None:
    status = wait_for(
        lambda: api(base, "/api/agent/status", timeout=20)[1],
        lambda value: value.get("ready") is True, timeout=300, label="managed 2.14 real agent",
    )
    assert_that(status.get("sdk_version") == "2.14.0", "agent SDK identity mismatch", status)
    assert_that("/.runtime/venv/" in status.get("sdk_module_file", ""), "agent imported non-managed SDK", status)
    browser_eval(
        "(()=>{const original=window.fetch.bind(window);window.__sdk214Ask=null;window.fetch=(input,init={})=>{"
        "if(String(input).endsWith('/api/agent/ask'))window.__sdk214Ask=JSON.parse(init.body);return original(input,init)};return true})()"
    )
    browser_eval(f"studio.askAgent({json.dumps(AGENT_PROMPT)})", timeout=700)
    ask = browser_eval("window.__sdk214Ask")
    session = wait_for(
        lambda: api(base, f"/api/agent/sessions/{quote(ask['session_id'])}?turn_id={quote(ask['turn_id'])}", timeout=20)[1],
        lambda value: value.get("status") in TERMINAL, timeout=60, label="real 2.14 agent turn",
    )
    assert_that(session.get("status") == "succeeded", "real DeepSeek turn failed", session)
    tools = [event.get("data", {}).get("name") for event in session.get("events", []) if event.get("type") == "tool_call"]
    allowed = set(status.get("capabilities", {}).get("tools", []))
    assert_that("read_pipeline" in tools and set(tools) <= allowed, "real turn used unexpected safe tools", {"tools": tools, "allowed": sorted(allowed)})
    ui = browser_eval(
        "(()=>{const feed=document.querySelector('#agent-feed');return {answers:feed.querySelectorAll('.message.agent[data-turn-id]').length,"
        "statuses:feed.querySelectorAll('.turn-status[data-turn-id]').length,overflow:feed.scrollWidth>feed.clientWidth+1,"
        "text:feed.innerText,bodyHasKey:/sk-[A-Za-z0-9]/.test(document.body.innerText)}})()"
    )
    assert_that(ui["answers"] == 1 and ui["statuses"] == 1 and not ui["overflow"], "real agent UI duplicated or overflowed", ui)
    assert_that(not ui["bodyHasKey"], "credential-like value reached DOM")
    browser(["screenshot", str(EVIDENCE / "sdk214-real-agent-1584x992.png")])
    record(receipt, "one real DeepSeek turn used verified 2.14 safe tools and deduplicated UI", {
        "model": status.get("model"), "sdk_version": status.get("sdk_version"),
        "sdk_module_file": status.get("sdk_module_file"), "tools": tools,
        "event_types": [event.get("type") for event in session.get("events", [])],
        "answer_sha256": hashlib.sha256(str(session.get("answer") or "").encode()).hexdigest(),
        "ui": {key: value for key, value in ui.items() if key != "text"},
        "credential_contract": "backend-owned key file; QA never read key bytes",
    })


def replay_observed_agent_ui(base: str, receipt: dict[str, Any]) -> None:
    answer = "transform.customer_value_pilot was inspected. No changes were made."
    replay_script = '''(()=>{let polls=0;const original=window.fetch.bind(window);window.__sdk214Replay={polls:0};
      window.fetch=async(input,init={})=>{const url=String(input);
        if(url.endsWith('/api/agent/ask')){const body=JSON.parse(init.body);return new Response(JSON.stringify({
          session_id:'sdk214-replay',turn_id:body.turn_id,status:'running',events:[
            {seq:1,type:'status',data:{message:'Inspecting known synthetic transform'}},
            {seq:2,type:'tool_call',data:{name:'read_pipeline'}}
          ]}),{status:202,headers:{'Content-Type':'application/json'}})}
        if(url.includes('/api/agent/sessions/')){polls++;window.__sdk214Replay.polls=polls;
          const turnId=new URL(url,location.href).searchParams.get('turn_id'),terminal=polls>=3;
          return new Response(JSON.stringify({session_id:'sdk214-replay',turn_id:turnId,status:terminal?'succeeded':'running',events:[
            {seq:1,type:'status',data:{message:'Inspecting known synthetic transform'}},
            {seq:2,type:'tool_call',data:{name:'read_pipeline'}},
            {seq:3,type:'tool_result',data:{name:'read_pipeline',message:'Synthetic transform metadata inspected; no user data included.'}},
            ...(terminal?[{seq:4,type:'answer',data:{message:__ANSWER__}}]:[])
          ],answer:terminal?__ANSWER__:null}),{status:200,headers:{'Content-Type':'application/json'}})}
        return original(input,init)};
      Object.assign(studio.state.agent,{mode:'owned',configured:true,healthy:true,ready:true,project_verified:true,
        project_path:studio.state.project.project_path,capabilities:{draft_only:true},model:'deepseek-v4.1-flash:netra'});
      return true})()'''.replace("__ANSWER__", json.dumps(answer))
    browser_eval(replay_script)
    browser_eval(f"studio.askAgent({json.dumps(AGENT_PROMPT)})")
    ui = wait_for(
        lambda: browser_eval(
            f'''(()=>{{const feed=document.querySelector('#agent-feed');return {{polls:window.__sdk214Replay.polls,
            busy:studio.state.agent.busy,answers:[...feed.querySelectorAll('.message.agent[data-turn-id]')].filter(e=>e.textContent.includes({json.dumps(answer)})).length,
            statuses:feed.querySelectorAll('.turn-status[data-turn-id]').length,overflow:feed.scrollWidth>feed.clientWidth+1,
            turn:feed.querySelector('.turn-status-copy')?.textContent||''}}}})()'''
        ),
        lambda value: value.get("polls") == 3 and not value.get("busy") and value.get("turn") == "Turn complete · no new draft reported",
        timeout=30, label="observed agent event replay",
    )
    assert_that(ui["answers"] == 1 and ui["statuses"] == 1 and not ui["overflow"], "replayed agent UI duplicated or overflowed", ui)
    browser(["screenshot", str(EVIDENCE / "sdk214-agent-replay-1584x992.png")])
    record(receipt, "observed real-turn event shape replayed without a second provider call", {
        "source_observation": "evidence/seeknal-2.14/real-agent-turn-observation.json",
        "provider_request": "none in replay run",
        "ui": ui,
    })


def run_packaged_sdk214(receipt: dict[str, Any]) -> None:
    bundle = ROOT / "dist" / "Seeknal Studio.app"
    executable = bundle / "Contents" / "MacOS" / "Seeknal Studio"
    resources = bundle / "Contents" / "Resources" / "studio"
    names = (
        "server.py", "engine_bridge.py", "agent_runtime.py", "agent_worker.py",
        "project_service.py", "environment_service.py", "repl_service.py", "repl_worker.py", "runtime_resolver.py",
        "launch.py", "local_access.py", "index.html", "styles.css", "app.js",
    )
    assert_that(executable.is_file(), "final packaged app is missing")
    manifest = {name: sha256(ROOT / name) for name in names}
    assert_that(
        all((resources / name).is_file() and sha256(resources / name) == digest for name, digest in manifest.items()),
        "final package source manifest mismatch", manifest,
    )
    bundled_config = resources / ".runtime" / "runtime-config.json"
    assert_that(bundled_config.is_file() and json.loads(bundled_config.read_text())["expected_seeknal_version"] == "2.14.0", "bundle runtime config is not pinned to 2.14")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment["SEEKNAL_STUDIO_AGENT_MODE"] = "external"
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "SEEKNAL_SIGNAL", "ATLAS_API_URL", "ATLAS_PORTAL_URL"):
        environment.pop(key, None)
    log_handle = (EVIDENCE / "package.log").open("w")
    process = subprocess.Popen(
        [str(executable), "--project", str(PROJECT_A), "--port", str(port)],
        env=environment, stdout=log_handle, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    try:
        launcher_link(EVIDENCE / "package.log")
        state = wait_for(
            lambda: api(base, "/api/state", timeout=5)[1],
            lambda value: value.get("seeknal_version") == "2.14.0",
            timeout=60, label="packaged 2.14 backend",
        )
        options = api(base, "/api/drafts/options", timeout=40)[1]
        webframes = [
            line.strip() for line in subprocess.check_output(["ps", "-axo", "pid=,ppid=,command="], text=True).splitlines()
            if "Resources/studio/bin/webframe" in line and base in line
        ]
        assert_that(len(webframes) == 1, "package did not own one native webframe", webframes)
        record(receipt, "final packaged app matched 12 sources and launched managed 2.14", {
            "manifest": manifest, "state": state,
            "draft_types": [item.get("value") for item in options.get("node_types", [])],
            "webframe": webframes[0], "provider_request": "none; external agent mode",
        })
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=20)
        log_handle.close()
    try:
        api(base, "/api/state", timeout=2)
        raise AssertionError("packaged backend remained after shutdown")
    except (URLError, OSError):
        record(receipt, "final packaged 2.14 backend closed with its launcher", {"port": port})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay-agent-observation", action="store_true")
    parser.add_argument("--package-only", action="store_true")
    args = parser.parse_args()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    runtime = resolve_runtime(environ={"PYTHONPATH": str(ROOT.parent.parent / "signal" / "src")})
    assert_that(runtime.dist_version == "2.14.0" and runtime.provenance == "managed-pypi", "managed runtime identity failed", runtime)
    canonical_before = {str(path): tree_hashes(path) for path in (ROOT / "demo", ROOT / "pilot")}
    copy_seven_definition_project(PROJECT_A, "sdk214_project_a")
    copy_seven_definition_project(PROJECT_B, "sdk214_project_b")
    PROJECTS_FILE.write_text(json.dumps({"recent": []}))
    nonce = uuid.uuid4().hex
    receipt: dict[str, Any] = {
        "nonce": nonce,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "version": runtime.dist_version, "python": str(runtime.python),
            "seeknal": str(runtime.seeknal), "module_file": str(runtime.module_file),
            "provenance": runtime.provenance,
        },
        "release_provenance": json.loads((EVIDENCE / "runtime-receipt.json").read_text())["release"],
        "source_hashes": {
            path.name: sha256(path) for path in (
                ROOT / "server.py", ROOT / "engine_bridge.py", ROOT / "agent_worker.py",
                ROOT / "agent_runtime.py", ROOT / "repl_worker.py", ROOT / "app.js",
                ROOT / "tests/sdk214_e2e.py",
            )
        },
        "checks": [],
    }
    dependency_script = (
        "import importlib.metadata as m,json;"
        "print(json.dumps({n:m.version(n) for n in ('seeknal','pydantic-ai-slim','pydantic-deep')}))"
    )
    dependency_result = subprocess.run(
        [str(runtime.python), "-I", "-c", dependency_script],
        check=True, capture_output=True, text=True, timeout=20,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    receipt["managed_distributions"] = json.loads(dependency_result.stdout)
    if args.package_only:
        try:
            run_packaged_sdk214(receipt)
            receipt.update(passed=True, finished_at=datetime.now(timezone.utc).isoformat())
            (EVIDENCE / "package-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            (EVIDENCE / "package-receipt-failed.json").unlink(missing_ok=True)
        except BaseException as error:
            receipt.update(passed=False, error=f"{type(error).__name__}: {error}", finished_at=datetime.now(timezone.utc).isoformat())
            (EVIDENCE / "package-receipt-failed.json").write_text(json.dumps(receipt, indent=2) + "\n")
            raise
        return
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    launcher: subprocess.Popen[str] | None = None
    log_handle: Any = None
    try:
        launcher, log_handle = start_managed(PROJECT_A, port)
        link = launcher_link(EVIDENCE / "managed.log")
        state_a = wait_for(
            lambda: api(base, "/api/state", timeout=5)[1],
            lambda value: Path(value.get("project_path", "")).resolve() == PROJECT_A.resolve(),
            timeout=60, label="managed project A",
        )
        assert_that(state_a.get("seeknal_version") == "2.14.0", "server did not report 2.14", state_a)
        record(receipt, "server resolved managed PyPI Seeknal 2.14 identity", {
            "state": state_a, "runtime": receipt["runtime"],
        })
        browser(["set", "viewport", "1584", "992"])
        browser(["open", link])
        wait_for(
            lambda: browser_eval("studio?.snapshot?.()"),
            lambda value: bool(value and value.get("nodes") == 3),
            timeout=60, label="pre-mutation agent browser",
        )
        initial_agent: dict[str, Any] | None = None
        if args.replay_agent_observation:
            initial_result: dict[str, Any] = wait_for(
                lambda: api(base, "/api/agent/status", timeout=20)[1],
                lambda value: value.get("ready") is True,
                timeout=300, label="initial owned 2.14 worker identity",
            )
            initial_agent = initial_result
            assert_that(
                initial_result.get("sdk_version") == "2.14.0"
                and "/.runtime/venv/" in initial_result.get("sdk_module_file", ""),
                "initial worker did not use managed 2.14", initial_result,
            )
            failed_path = EVIDENCE / "e2e-receipt-failed.json"
            observation_path = EVIDENCE / "real-agent-turn-observation.json"
            if not observation_path.is_file():
                assert_that(failed_path.is_file(), "real agent observation receipt is missing")
                failed = json.loads(failed_path.read_text())
                observation = {
                    "observed_at": failed.get("finished_at"),
                    "runtime": failed.get("runtime"),
                    "source_hashes": failed.get("source_hashes"),
                    "prompt": AGENT_PROMPT,
                    "control_flow_evidence": [
                        "API session status succeeded before UI assertion",
                        "tool calls included read_pipeline and were a subset of the verified allowlist",
                        "browser observed one status card, read_pipeline tool event, no overflow, and no credential-like DOM value",
                    ],
                    "terminal_ui_race": failed.get("error"),
                    "provider_turns_replayed": 0,
                }
                observation_path.write_text(json.dumps(observation, indent=2) + "\n")
            replay_observed_agent_ui(base, receipt)
        else:
            run_one_real_agent_turn(base, receipt)
        run_cli_browser_flow(base, receipt)
        if args.replay_agent_observation:
            assert initial_agent is not None
            initial = initial_agent
            rebound = wait_for(
                lambda: api(base, "/api/agent/status", timeout=30)[1],
                lambda value: (
                    value.get("ready") is True
                    and value.get("project_verified") is True
                    and value.get("identity_nonce") != initial.get("identity_nonce")
                ),
                timeout=300, label="post-Apply owned worker rebind",
            )
            assert_that(
                rebound.get("sdk_version") == "2.14.0"
                and rebound.get("sdk_module_file") == str(runtime.module_file),
                "rebound worker lost managed 2.14 identity", rebound,
            )
            record(receipt, "post-Apply fingerprint change auto-rebound a fresh verified 2.14 worker", {
                "initial_nonce": initial.get("identity_nonce"),
                "rebound_nonce": rebound.get("identity_nonce"),
                "initial_fingerprint": initial.get("project_fingerprint"),
                "rebound_fingerprint": rebound.get("project_fingerprint"),
                "sdk_version": rebound.get("sdk_version"),
                "sdk_module_file": rebound.get("sdk_module_file"),
                "model_verified": rebound.get("model_verified"),
                "tool_calling_verified": rebound.get("tool_calling_verified"),
                "provider_turn": "readiness/tool-schema probe only; no second user turn",
            })

        api(base, "/api/projects/open", {"path": str(PROJECT_B)})
        state_b = wait_for(
            lambda: api(base, "/api/state", timeout=5)[1],
            lambda value: Path(value.get("project_path", "")).resolve() == PROJECT_B.resolve(),
            timeout=90, label="managed project B switch",
        )
        assert_that(state_b.get("runtime_id") != state_a.get("runtime_id") and state_b.get("seeknal_version") == "2.14.0", "project switch did not create fresh 2.14 runtime", {"a": state_a, "b": state_b})
        assert_that(not (PROJECT_B / ACTIVE).exists(), "project switch leaked applied transform")
        record(receipt, "managed project switch retained 2.14 and isolated project state", {
            "from_runtime_id": state_a.get("runtime_id"), "to_runtime_id": state_b.get("runtime_id"),
            "project_b": state_b.get("project_path"),
        })
        canonical_after = {str(path): tree_hashes(path) for path in (ROOT / "demo", ROOT / "pilot")}
        assert_that(canonical_after == canonical_before, "canonical user fixtures changed")
        record(receipt, "canonical demo and four-definition user pilot remained byte-identical", canonical_after)
        receipt["passed"] = True
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        (EVIDENCE / "e2e-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        (EVIDENCE / "e2e-receipt-failed.json").unlink(missing_ok=True)
    except BaseException as error:
        receipt.update(passed=False, error=f"{type(error).__name__}: {error}", finished_at=datetime.now(timezone.utc).isoformat())
        (EVIDENCE / "e2e-receipt-failed.json").write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    finally:
        try:
            browser(["close"], timeout=15)
        except (subprocess.SubprocessError, OSError):
            pass
        if launcher is not None and launcher.poll() is None:
            os.killpg(launcher.pid, signal.SIGTERM)
            try:
                launcher.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(launcher.pid, signal.SIGKILL)
                launcher.wait(timeout=5)
        if log_handle is not None:
            log_handle.close()


if __name__ == "__main__":
    main()
