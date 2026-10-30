"""Real ``seeknal draft`` and isolated agent-feed browser acceptance.

The CLI scenario uses a disposable copy of the synthetic pilot template and the
real installed Seeknal executable through Studio's job API.  The visual agent
scenario uses a browser-local fetch double: it makes no provider request and
contains no user or credential data.
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

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_access import TOKEN_HEADER, link_secret, read_access_link  # noqa: E402

TEMPLATE = ROOT / "pilot" / "template"
CANONICAL_ROOTS = (ROOT / "pilot" / "template", ROOT / "pilot" / "workspace")
EVIDENCE = ROOT / "evidence" / "cli-draft"
PROJECT = EVIDENCE / "project"
SESSION = "seeknal-cli-draft"
TERMINAL = {"succeeded", "success", "failed", "cancelled", "canceled"}
# The packaged launcher's per-launch browser secret, sent as a header by api().
ACCESS: dict[str, str] = {}
NAME = "cli_paid_orders"
DRAFT_RELATIVE = Path(".seeknal/drafts") / f"draft_transform_{NAME}.yml"
ACTIVE_RELATIVE = Path("seeknal/transforms") / f"{NAME}.yml"
VALID_TRANSFORM = f'''kind: transform
name: {NAME}
description: "Paid order totals generated through the real seeknal draft CLI"
owner: "data-team@example.com"
tags: ["transform", "cli-draft-e2e"]
inputs:
  - ref: source.orders
transform: |
  SELECT
    customer_id,
    SUM(amount) AS total_amount,
    COUNT(*) AS paid_order_count
  FROM input_0
  WHERE status = 'paid'
  GROUP BY customer_id
output:
  columns:
    customer_id: "Customer identifier"
    total_amount: "Paid order total"
    paid_order_count: "Paid order count"
'''


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
    expected_error: bool = False,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        base + path,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": base,
            **ACCESS,
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


def prepare_project() -> dict[str, dict[str, str]]:
    before = {str(root): tree_hashes(root) for root in CANONICAL_ROOTS}
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    shutil.copytree(TEMPLATE, PROJECT)
    project_file = PROJECT / "seeknal_project.yml"
    project_file.write_text(
        project_file.read_text().replace("studio_agent_pilot", "studio_cli_draft_e2e", 1)
    )
    return before


def start_server(port: int) -> tuple[subprocess.Popen[str], Any]:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    log_handle = (EVIDENCE / "server.log").open("w")
    environment = os.environ.copy()
    environment.update(
        {
            "SEEKNAL_STUDIO_PROJECT": str(PROJECT),
            "SEEKNAL_STUDIO_PORT": str(port),
            "SEEKNAL_STUDIO_AGENT_MODE": "external",
            "PYTHONUNBUFFERED": "1",
        }
    )
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")],
        cwd=ROOT,
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return process, log_handle


def stop_server(process: subprocess.Popen[str] | None, log_handle: Any) -> None:
    try:
        browser(["close"], timeout=15)
    except (subprocess.SubprocessError, OSError):
        pass
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    if log_handle is not None:
        log_handle.close()


def select_draft_form(node_type: str, name: str) -> dict[str, Any]:
    """Use the rendered primary Draft affordance, not an internal JS shortcut."""
    return browser_eval(
        f'''(async()=>{{
          const button=document.querySelector('#draft-editor, #menu-new-node, #new-draft, #create-new-draft, [data-action="new-draft"]');
          if(!button) throw Error('primary new Draft button not found');
          button.click();
          const dialog=document.querySelector('#new-draft-dialog, #cli-draft-dialog, dialog[data-dialog="new-draft"]');
          if(!dialog?.open) throw Error('new Draft dialog did not open');
          for(let i=0;i<240&&!studio.state.draftOptions.length;i++)await new Promise(r=>setTimeout(r,50));
          const type=dialog.querySelector('[name="node_type"], #draft-node-type, #new-draft-type');
          const name=dialog.querySelector('[name="name"], #draft-node-name, #new-draft-name');
          if(!type||!name) throw Error('new Draft type/name controls missing');
          type.value={json.dumps(node_type)}; type.dispatchEvent(new Event('change',{{bubbles:true}}));
          name.value={json.dumps(name)}; name.dispatchEvent(new Event('input',{{bubbles:true}}));
          const submit=dialog.querySelector('#new-draft-submit, #create-cli-draft, [type="submit"]');
          if(!submit||submit.disabled) throw Error('new Draft submit unavailable');
          await studio.submitCliDraft();
          return {{type:type.value,name:name.value,dialogOpen:dialog.open,jobId:studio.state.activeJob?.id||null,status:document.querySelector('#status-message').textContent}};
        }})()'''
    )


def wait_browser_job(
    timeout: float = 180,
    *,
    after_id: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    return wait_for(
        lambda: browser_eval(
            "(()=>{const j=studio.state.activeJob;return j?{id:j.id,status:j.status,action:j.action,logs:j.logs||j.output||''}:null})()"
        ),
        lambda value: bool(
            value
            and value.get("status") in TERMINAL
            and (after_id is None or value.get("id") != after_id)
            and (action is None or value.get("action") == action)
        ),
        timeout=timeout,
        label="browser-visible draft job",
    )


def set_editor(value: str) -> None:
    browser_eval(
        f'''(()=>{{const e=document.querySelector('#code-editor');
        e.value={json.dumps(value)};e.dispatchEvent(new Event('input',{{bubbles:true}}));return true}})()'''
    )


def click(selector: str) -> None:
    browser_eval(
        f'''(()=>{{const e=document.querySelector({json.dumps(selector)});if(!e)throw Error('missing {selector}');
        if(e.disabled)throw Error('disabled {selector}');e.click();return true}})()'''
    )


def run_real_cli_flow(base: str, receipt: dict[str, Any]) -> None:
    options = api(base, "/api/drafts/options")[1]
    transforms = [item for item in options.get("node_types", []) if item.get("value") == "transform"]
    assert_that(transforms, "installed Seeknal did not advertise transform drafting", options)
    record(receipt, "actual installed Seeknal draft metadata populated the type picker", transforms[0])

    browser(["set", "viewport", "1584", "992"])
    browser(["open", base])
    wait_for(
        lambda: browser_eval("window.studio?.snapshot?.()"),
        lambda value: bool(value and value.get("nodes") == 2),
        timeout=45,
        label="Studio project readiness",
    )
    form = select_draft_form("transform", NAME)
    draft_job = wait_browser_job()
    draft_job = api(base, f"/api/jobs/{quote(draft_job['id'])}")[1]
    assert_that(draft_job.get("status") in {"succeeded", "success"}, "real seeknal draft job failed", draft_job)
    assert_that("[OK] Created: draft_transform_" in draft_job.get("logs", ""), "job logs omit real CLI creation output", draft_job)
    draft_path = PROJECT / DRAFT_RELATIVE
    assert_that(draft_path.is_file(), "managed CLI draft was not created", draft_path)
    generated_hash = sha256(draft_path)
    generated = yaml.safe_load(draft_path.read_text())
    assert_that(generated.get("kind") == "transform" and generated.get("name") == NAME, "generated template identity mismatch", generated)
    opened = wait_for(
        lambda: browser_eval(
            "({path:studio.state.selectedFile,buffer:document.querySelector('#code-editor').value,dirty:studio.snapshot().dirty})"
        ),
        lambda value: value.get("path") == DRAFT_RELATIVE.as_posix(),
        timeout=30,
        label="generated draft editor opening",
    )
    assert_that(opened.get("path") == DRAFT_RELATIVE.as_posix(), "new generated draft did not open", opened)
    assert_that(opened.get("buffer") == draft_path.read_text(), "opened editor does not contain generated bytes", opened)
    record(receipt, "primary Draft form ran real CLI and opened generated draft", {
        "form": form,
        "job": draft_job,
        "path": DRAFT_RELATIVE.as_posix(),
        "generated_sha256": generated_hash,
    })

    browser_eval("document.querySelector('#draft-dialog').close()")
    set_editor(VALID_TRANSFORM)
    dirty_before = browser_eval("({dirty:studio.snapshot().dirty,buffer:document.querySelector('#code-editor').value})")
    assert_that(dirty_before == {"dirty": True, "buffer": VALID_TRANSFORM}, "edited draft was not dirty", dirty_before)
    saved = browser_eval("(async()=>{await studio.save();return {dirty:studio.snapshot().dirty,diff:studio.state.currentDiff?.path||null}})()")
    assert_that(saved == {"dirty": False, "diff": DRAFT_RELATIVE.as_posix()}, "generated draft Save did not finish hash refresh", saved)
    assert_that(draft_path.read_text() == VALID_TRANSFORM, "saved generated draft differs from editor")
    browser_eval("studio.openDraftReview()")
    wait_for(
        lambda: browser_eval("Boolean(studio.state.currentDiff)"),
        bool,
        timeout=20,
        label="updated generated-draft diff",
    )
    # The browser protects a dirty buffer before submission. Exercise that
    # guard, then send the same request from this controlled page so the
    # backend's non-overwrite collision is also proven while the draft exists.
    set_editor(VALID_TRANSFORM + "# unsaved collision sentinel\n")
    managed_before = draft_path.read_bytes()
    previous_job_id = browser_eval("studio.state.activeJob?.id")
    select_draft_form("transform", NAME)
    time.sleep(0.2)
    guarded = browser_eval(
        "({jobId:studio.state.activeJob?.id,dirty:studio.snapshot().dirty,"
        "buffer:document.querySelector('#code-editor').value,status:document.querySelector('#status-message').textContent})"
    )
    assert_that(guarded.get("jobId") == previous_job_id, "dirty-buffer guard launched a CLI job", guarded)
    assert_that(guarded.get("dirty") is True and guarded.get("buffer").endswith("# unsaved collision sentinel\n"), "Draft form changed dirty buffer", guarded)
    browser_eval("document.querySelector('#new-draft-dialog').close()")
    collision_id = browser_eval(
        f'''(async()=>{{const response=await fetch('/api/jobs',{{method:'POST',headers:{{'Content-Type':'application/json'}},
        body:JSON.stringify({{action:'draft',node_type:'transform',name:{json.dumps(NAME)},description:'',python:false,deps:''}})}});
        const payload=await response.json();if(!response.ok)throw Error(payload.error||String(response.status));return (payload.job||payload).id}})()'''
    )
    collision = wait_job(base, collision_id)
    assert_that(collision.get("status") == "failed", "managed-draft collision unexpectedly succeeded", collision)
    assert_that(draft_path.read_bytes() == managed_before, "collision overwrote managed draft")
    preserved = browser_eval("({dirty:studio.snapshot().dirty,buffer:document.querySelector('#code-editor').value})")
    assert_that(preserved == {"dirty": True, "buffer": VALID_TRANSFORM + "# unsaved collision sentinel\n"}, "collision discarded dirty buffer", preserved)
    record(receipt, "collision failed without overwrite and preserved dirty buffer", {
        "job": collision,
        "dirty_guard": guarded,
        "managed_sha256": hashlib.sha256(managed_before).hexdigest(),
        "dirty": preserved["dirty"],
    })
    set_editor(VALID_TRANSFORM)
    assert_that(browser_eval("studio.snapshot().dirty") is False, "reviewed draft buffer did not return to saved content")
    browser_eval("(async()=>{await studio.openDraftReview();return Boolean(studio.state.currentDiff)})()")
    click("#dry-run-draft")
    validation_id = wait_for(
        lambda: browser_eval("studio.state.draftValidationJobId"),
        lambda value: isinstance(value, str) and value != previous_job_id,
        timeout=20,
        label="generated-draft validation identity",
    )
    dry_run = wait_browser_job(after_id=previous_job_id, action="validate-draft")
    assert_that(dry_run.get("id") == validation_id, "browser validated a different draft job", {"job": dry_run, "validation_id": validation_id})
    assert_that(dry_run.get("status") in {"succeeded", "success"}, "dry run failed", dry_run)
    assert_that("Could not execute transform" not in dry_run.get("logs", ""), "native dry-run returned only a warning instead of query output", dry_run)
    assert_that(all(value in dry_run.get("logs", "") for value in ("C001", "C002", "C003", "340000", "1215000", "2320000")), "native dry-run did not preview all expected synthetic rows", dry_run)
    assert_that(not (PROJECT / ACTIVE_RELATIVE).exists(), "dry run mutated active pipeline")
    assert_that(browser_eval("document.querySelector('#code-editor').value") == VALID_TRANSFORM, "dry run changed editor buffer")
    record(receipt, "edited generated draft dry-ran with three real preview rows without applying", dry_run)
    wait_for(
        lambda: browser_eval("({disabled:document.querySelector('#apply-reviewed-draft').disabled,reviewDisabled:document.querySelector('#apply-draft').disabled,validationId:studio.state.draftValidationJobId,activeId:studio.state.activeJob?.id,currentDiff:studio.state.currentDiff?.path,status:document.querySelector('#draft-review-status').textContent})"),
        lambda value: value.get("disabled") is False,
        timeout=30,
        label="Apply enabled for validated generated draft",
    )

    click("#apply-reviewed-draft")
    apply_job = wait_browser_job(after_id=dry_run["id"], action="apply-draft")
    assert_that(apply_job.get("status") in {"succeeded", "success"}, "explicit apply failed", apply_job)
    active_path = PROJECT / ACTIVE_RELATIVE
    assert_that(active_path.is_file() and active_path.read_text() == VALID_TRANSFORM, "apply wrote unexpected active transform", active_path)
    record(receipt, "explicit Apply wrote only the reviewed validated hash", {
        "job": apply_job,
        "active_path": ACTIVE_RELATIVE.as_posix(),
        "sha256": sha256(active_path),
    })

    click("#run-button")
    run_job = wait_browser_job(timeout=240, after_id=apply_job["id"], action="run")
    assert_that(run_job.get("status") in {"succeeded", "success"}, "pipeline run failed", run_job)
    record(receipt, "pipeline with CLI-created transform ran", run_job)

    browser_eval("studio.setMainView('repl')")
    click("#repl-start")
    wait_for(
        lambda: browser_eval("document.querySelector('#repl-status').textContent"),
        lambda value: value == "Ready",
        timeout=90,
        label="REPL readiness",
    )
    query = f"SELECT * FROM {NAME} ORDER BY customer_id"
    query_id = browser_eval(
        f'''(async()=>{{const e=document.querySelector('#repl-editor');e.value={json.dumps(query)};
        e.dispatchEvent(new Event('input',{{bubbles:true}}));await studio.runReplQuery();return studio.state.repl.queryId}})()'''
    )
    assert_that(isinstance(query_id, str), "REPL did not accept real output query", query_id)
    query_payload = wait_for(
        lambda: api(base, f"/api/repl/query?id={quote(query_id)}", timeout=10)[1],
        lambda value: value.get("status") in TERMINAL,
        timeout=120,
        label="real REPL query",
    )
    assert_that(query_payload.get("status") in {"succeeded", "success"}, "real output query failed", query_payload)
    query_result = wait_for(
        lambda: browser_eval("studio.state.repl.result"),
        lambda value: bool(value and value.get("rows")),
        timeout=120,
        label="real query output",
    )
    assert_that(query_result.get("columns") == ["customer_id", "total_amount", "paid_order_count"], "unexpected query columns", query_result)
    record(receipt, "real query output rendered for generated transform", {
        "query": query,
        "query_id": query_id,
        "columns": query_result.get("columns"),
        "rows": query_result.get("rows"),
    })

    browser(["screenshot", str(EVIDENCE / "real-cli-flow.png")])


def run_mock_agent_visual(base: str, receipt: dict[str, Any]) -> None:
    browser(["open", base])
    wait_for(
        lambda: browser_eval("Boolean(window.studio?.state?.project)"),
        bool,
        timeout=30,
        label="visual fixture readiness",
    )
    long_csv = "event_id,user_id,event,channel,amount,event_date\\n" + "\\n".join(
        f"e{i:08d}-9f2a-4c11-8a01-00000000{i:04d},u{i:04d},purchase,paid_search,{i * 125},2026-10-{(i % 28) + 1:02d}"
        for i in range(48)
    )
    answer = "The synthetic draft is ready. Validation, apply, and run remain explicit actions."
    fixture = {"csv": long_csv, "answer": answer}
    browser_eval(
        f'''(()=>{{
          const fixture={json.dumps(fixture)}; let polls=0; const original=window.fetch.bind(window);
          window.__agentMock={{polls:0,fixture}};
          window.fetch=async(input,init={{}})=>{{const url=String(input);
            if(url.endsWith('/api/agent/ask')) return new Response(JSON.stringify({{
              session_id:'visual-session',turn_id:JSON.parse(init.body).turn_id,status:'running',events:[
                {{seq:1,type:'tool_call',data:{{name:'inspect_source'}}}},
                {{seq:2,type:'tool_result',data:{{name:'inspect_source',message:fixture.csv}}}}
              ]
            }}),{{status:202,headers:{{'Content-Type':'application/json'}}}});
            if(url.includes('/api/agent/sessions/')){{polls++;window.__agentMock.polls=polls;
              const turnId=new URL(url,location.href).searchParams.get('turn_id');
              const terminal=polls>=3;
              return new Response(JSON.stringify({{session_id:'visual-session',turn_id:turnId,status:terminal?'succeeded':'running',
                events:[
                  {{seq:1,type:'tool_call',data:{{name:'inspect_source'}}}},
                  {{seq:2,type:'tool_result',data:{{name:'inspect_source',message:fixture.csv}}}},
                  ...(terminal?[{{seq:3,type:'answer',data:{{message:fixture.answer}}}}]:[])
                ],answer:terminal?fixture.answer:null}}),{{status:200,headers:{{'Content-Type':'application/json'}}}});
            }}
            return original(input,init);
          }};
          Object.assign(studio.state.agent,{{mode:'owned',configured:true,healthy:true,ready:true,
            project_verified:true,project_path:studio.state.project.project_path,capabilities:{{draft_only:true}},
            model:'mock-agent-no-provider'}});
          return true;
        }})()'''
    )
    browser_eval(f"studio.askAgent({json.dumps('Inspect the synthetic CSV and draft a transform.')})")
    wait_for(
        lambda: browser_eval("({busy:studio.state.agent.busy,polls:window.__agentMock.polls,feed:document.querySelector('#agent-feed').innerText,status:document.querySelector('#status-message').textContent,turn:document.querySelector('.turn-status-copy')?.textContent||''})"),
        lambda value: bool(value and not value.get("busy") and value.get("polls", 0) >= 3 and value.get("turn") == "Turn complete · no new draft reported"),
        timeout=30,
        label="repeated mocked agent polling",
    )

    def layout(width: int, height: int, filename: str) -> dict[str, Any]:
        browser(["set", "viewport", str(width), str(height)])
        result = browser_eval(
            f'''(()=>{{
              const feed=document.querySelector('#agent-feed'),panel=document.querySelector('#agent-panel');
              const composer=document.querySelector('#agent-form'),footer=document.querySelector('.statusbar, footer');
              const visible=(e)=>{{const s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'}};
              const rect=e=>{{const r=e.getBoundingClientRect();return {{left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height}}}};
              const intersects=(a,b)=>a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top;
              const answers=[...feed.querySelectorAll('.message.agent')].filter(e=>e.textContent.includes({json.dumps(answer)}));
              const turnStatuses=[...feed.querySelectorAll('.turn-status')];
              const visibleText=feed.innerText;
              const raw=[...feed.querySelectorAll('details, [aria-label*="raw" i], [data-raw-event]')].filter(e=>e.textContent.includes('event_id,user_id'));
              const pr=rect(panel),cr=rect(composer),fr=footer?rect(footer):null;
              return {{viewport:{{width:innerWidth,height:innerHeight}},answers:answers.length,turnStatuses:turnStatuses.length,
                turnStatusText:turnStatuses.map(e=>e.querySelector('.turn-status-copy')?.textContent.trim()||''),polls:window.__agentMock.polls,
                feedOverflow:feed.scrollWidth>feed.clientWidth+1,panelOverflow:panel.scrollWidth>panel.clientWidth+1,
                composerFooterIntersect:fr?intersects(cr,fr):false,composerInside:cr.left>=pr.left&&cr.right<=pr.right&&cr.bottom<=pr.bottom+1,
                visibleCsvOccurrences:(visibleText.match(/event_id,user_id/g)||[]).length,rawAccessible:raw.length>0,
                panel:pr,composer:cr,footer:fr}};
            }})()'''
        )
        assert_that(result["answers"] == 1, "answer duplicated across answer event/session.answer", result)
        assert_that(result["turnStatuses"] == 1 and result["turnStatusText"] == ["Turn complete · no new draft reported"], "terminal status card duplicated or stale", result)
        assert_that(not result["feedOverflow"] and not result["panelOverflow"], "agent panel has horizontal overflow", result)
        assert_that(not result["composerFooterIntersect"] and result["composerInside"], "agent composer overlaps footer/panel", result)
        assert_that(result["visibleCsvOccurrences"] == 0, "long raw CSV remained expanded/duplicated", result)
        assert_that(result["rawAccessible"], "collapsed raw tool result is not accessible", result)
        browser(["screenshot", str(EVIDENCE / filename)])
        return result

    wide = layout(1584, 992, "agent-dedupe-1584x992.png")
    narrow = layout(1280, 900, "agent-dedupe-1280x900.png")
    record(receipt, "mock-agent repeated polling deduplicated answer and compacted raw CSV", {
        "fixture": "synthetic UUID/CSV only; no provider request",
        "wide": wide,
        "narrow": narrow,
    })


def run_packaged_flow(receipt: dict[str, Any]) -> None:
    bundle = ROOT / "dist" / "Seeknal Studio.app"
    executable = bundle / "Contents" / "MacOS" / "Seeknal Studio"
    resources = bundle / "Contents" / "Resources" / "studio"
    copied_sources = (
        "server.py", "engine_bridge.py", "project_service.py", "environment_service.py", "repl_service.py",
        "repl_worker.py", "agent_runtime.py", "agent_worker.py", "launch.py", "local_access.py",
        "index.html", "styles.css", "app.js",
    )
    assert_that(executable.is_file(), "packaged app executable is missing", executable)
    manifest = {name: sha256(ROOT / name) for name in copied_sources}
    mismatches = [name for name, digest in manifest.items() if not (resources / name).is_file() or sha256(resources / name) != digest]
    assert_that(not mismatches, "packaged app is stale", mismatches)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update({"SEEKNAL_STUDIO_AGENT_MODE": "external", "PYTHONUNBUFFERED": "1"})
    environment.pop("ATLAS_API_URL", None)
    environment.pop("ATLAS_PORTAL_URL", None)
    log_handle = (EVIDENCE / "package.log").open("w")
    process = subprocess.Popen(
        [str(executable), "--project", str(PROJECT), "--port", str(port)],
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        # launch.py keeps its one-time browser link in a private file it names.
        link = wait_for(
            lambda: read_access_link((EVIDENCE / "package.log").read_text(errors="replace")), bool,
            timeout=60, label="packaged launcher access link",
        )
        ACCESS[TOKEN_HEADER] = link_secret(link)
        state = wait_for(
            lambda: api(base, "/api/state", timeout=5)[1],
            lambda value: Path(value.get("project_path", "")).resolve() == PROJECT.resolve(),
            timeout=60,
            label="packaged Studio project binding",
        )
        options = api(base, "/api/drafts/options", timeout=40)[1]
        assert_that(any(item.get("value") == "transform" for item in options.get("node_types", [])), "packaged backend omitted real draft metadata", options)
        app_bytes = urlopen(Request(base + "/app.js", headers=ACCESS), timeout=10).read()
        assert_that(hashlib.sha256(app_bytes).hexdigest() == manifest["app.js"], "packaged HTTP app.js differs from frozen source")
        descendants = subprocess.check_output(["ps", "-axo", "pid=,ppid=,command="], text=True).splitlines()
        webframes = [line.strip() for line in descendants if "Resources/studio/bin/webframe" in line and base in line]
        assert_that(len(webframes) == 1, "packaged app did not own exactly one native webframe", webframes)
        record(receipt, "rebuilt 11-module packaged app launched with isolated external-agent authority", {
            "bundle": str(bundle),
            "manifest": manifest,
            "state": {"project": state.get("project"), "project_path": state.get("project_path")},
            "draft_types": [item.get("value") for item in options.get("node_types", [])],
            "native_webframe": webframes[0],
            "provider_request": "none; SEEKNAL_STUDIO_AGENT_MODE=external",
        })
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        log_handle.close()
    try:
        api(base, "/api/state", timeout=2)
        raise AssertionError("packaged backend remained after launcher shutdown")
    except (URLError, OSError):
        record(receipt, "packaged launcher shutdown closed its isolated backend", {"port": port})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-only", action="store_true")
    parser.add_argument("--visual-only", action="store_true")
    parser.add_argument("--package-only", action="store_true")
    args = parser.parse_args()
    assert_that(sum((args.real_only, args.visual_only, args.package_only)) <= 1, "choose only one partial mode")
    mode = "real" if args.real_only else "visual" if args.visual_only else "package" if args.package_only else "combined"
    receipt_path = EVIDENCE / ({"real": "real-receipt.json", "visual": "visual-receipt.json", "package": "package-receipt.json", "combined": "receipt.json"}[mode])
    failed_path = EVIDENCE / ({"real": "real-receipt-failed.json", "visual": "visual-receipt-failed.json", "package": "package-receipt-failed.json", "combined": "receipt-failed.json"}[mode])
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    canonical_before = prepare_project()
    receipt: dict[str, Any] = {
        "scope": {
            "real": "real installed seeknal draft browser acceptance",
            "visual": "isolated mock-agent visual and event-deduplication regression",
            "package": "rebuilt 11-module packaged native-app smoke",
            "combined": "real installed seeknal draft plus isolated mock-agent visual regression",
        }[mode],
        "project": str(PROJECT),
        "credentials": "not read; visual scenario uses browser-local controlled fixture",
        "mode": mode,
        "related_receipts": {
            "package": "evidence/cli-draft/package-receipt.json",
            "combined": "evidence/cli-draft/receipt.json",
            "bundle_manifest": "evidence/cli-draft/bundle-manifest.json",
            "user_error_inspection": "evidence/cli-draft/user-error-inspection.json",
        },
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_hashes": {
            name: sha256(ROOT / name)
            for name in ("server.py", "engine_bridge.py", "index.html", "styles.css", "app.js", "tests/cli_draft_e2e.py")
        },
        "checks": [],
    }
    process: subprocess.Popen[str] | None = None
    log_handle: Any = None
    try:
        if args.package_only:
            run_packaged_flow(receipt)
        else:
            port = free_port()
            base = f"http://127.0.0.1:{port}"
            process, log_handle = start_server(port)
            wait_for(
                lambda: api(base, "/api/state", timeout=5)[1],
                lambda value: bool(value.get("project")),
                timeout=45,
                label="Studio backend",
            )
            if not args.visual_only:
                run_real_cli_flow(base, receipt)
            if not args.real_only:
                run_mock_agent_visual(base, receipt)
        canonical_after = {str(root): tree_hashes(root) for root in CANONICAL_ROOTS}
        assert_that(canonical_after == canonical_before, "canonical pilot fixtures changed")
        record(receipt, "canonical pilot fixtures remained byte-identical", canonical_after)
        receipt["passed"] = True
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        failed_path.unlink(missing_ok=True)
    except BaseException as error:
        receipt["passed"] = False
        receipt["error"] = f"{type(error).__name__}: {error}"
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        failed_path.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    finally:
        stop_server(process, log_handle)


if __name__ == "__main__":
    main()
