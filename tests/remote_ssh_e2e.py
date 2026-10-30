#!/usr/bin/env python3
"""Live SSH remote-workspace acceptance: register, open, run, query, return.

Requires key-based SSH to SEEKNAL_REMOTE_E2E_HOST with a Seeknal demo project at
SEEKNAL_REMOTE_E2E_PATH.  Uses a private registry and a copied local fixture so
the user's saved workspaces and running Studio instances are untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_access import TOKEN_HEADER, link_secret, read_access_link  # noqa: E402

EVIDENCE = ROOT / "evidence" / "remote-ssh"
HOST = os.environ.get("SEEKNAL_REMOTE_E2E_HOST", "fitra@100.73.79.64")
REMOTE_PATH = os.environ.get("SEEKNAL_REMOTE_E2E_PATH", "~/project/seeknal-studio-remote-demo")
PORT = int(os.environ.get("SEEKNAL_REMOTE_E2E_PORT", "18871"))
BASE = f"http://127.0.0.1:{PORT}"
PYTHON = ROOT / ".runtime" / "venv" / "bin" / "python"
receipt: dict[str, Any] = {"host": HOST, "remote_path": REMOTE_PATH, "port": PORT, "checks": []}
# The launcher's per-launch browser secret; the local gateway requires it too.
ACCESS: dict[str, str] = {}


def record(name: str, detail: object) -> None:
    receipt["checks"].append({"check": name, "detail": detail})
    print(f"PASS {name}: {json.dumps(detail)[:300]}", flush=True)


def check(condition: bool, message: str, detail: object = None) -> None:
    if not condition:
        raise AssertionError(f"{message}: {json.dumps(detail)[:1500]}")


def api(path: str, body: dict | None = None, timeout: float = 120) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method="POST" if data is not None else "GET",
        headers={**({"Content-Type": "application/json"} if data is not None else {}), **ACCESS},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def wait(predicate: Callable[[], Any], timeout: float, label: str, interval: float = 0.5) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, urllib.error.URLError, ValueError) as error:
            last = str(error)
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {label}: {str(last)[:800]}")


def ssh(command: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "--", HOST, command],
        capture_output=True, text=True, timeout=60, check=False,
    ).stdout.strip()


def current_workspace() -> dict:
    return api("/api/workspaces")[1].get("current") or {}


def run_job(action: str) -> dict:
    status, job = api("/api/jobs", {"action": action})
    check(status == 202, f"{action} job accepted", job)
    job_id = job["id"]
    return wait(
        lambda: (lambda value: value if value.get("status") in {"succeeded", "failed", "cancelled", "success", "error"} else None)(
            api(f"/api/jobs/{job_id}")[1]
        ),
        300, f"{action} job",
    )


def repl_count(table: str) -> int:
    status, started = api("/api/repl/start", {})
    check(status == 202, "REPL start accepted", started)
    wait(lambda: api("/api/repl/status")[1].get("ready") is True, 180, "REPL ready")
    status, query = api("/api/repl/query", {"sql": f"SELECT COUNT(*) AS n FROM {table};"})
    check(status == 202, "REPL query accepted", query)
    result = wait(
        lambda: (lambda value: value if value.get("status") in {"succeeded", "failed", "cancelled"} else None)(
            api(f"/api/repl/query?id={query['query_id']}")[1]
        ),
        120, "REPL query",
    )
    check(result.get("status") == "succeeded", "REPL query succeeded", result)
    rows = result.get("rows") or []
    first = rows[0]
    return int(first[0] if isinstance(first, list) else next(iter(first.values())))


def main() -> int:
    if EVIDENCE.exists():
        shutil.rmtree(EVIDENCE)
    EVIDENCE.mkdir(parents=True)
    os.chmod(EVIDENCE, 0o700)
    local = EVIDENCE / "local-commerce"
    shutil.copytree(
        ROOT / "evidence" / "workspaces" / "commerce", local,
        ignore=shutil.ignore_patterns("target", ".seeknal", "__pycache__"),
    )
    registry = EVIDENCE / "projects.json"
    log_path = EVIDENCE / "launcher.log"
    env = {key: value for key, value in os.environ.items() if key not in {
        "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__",
        # Synthetic fixtures only: keep governance services out of this test.
        "ATLAS_API_URL", "ATLAS_PORTAL_URL",
    }}
    env.update(SEEKNAL_STUDIO_PROJECTS_FILE=str(registry), BROWSER="true")
    with log_path.open("w") as log:
        launcher = subprocess.Popen(
            [str(PYTHON), str(ROOT / "launch.py"), "--browser", "--port", str(PORT), "--project", str(local)],
            cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True,
        )
    try:
        link = wait(lambda: read_access_link(log_path.read_text(errors="replace")), 60, "launcher access link")
        ACCESS[TOKEN_HEADER] = link_secret(link)
        wait(lambda: api("/api/state")[0] == 200, 60, "local Studio")
        local_state = api("/api/state")[1]
        record("local project opened", {"project_path": local_state["project_path"], "seeknal": local_state["seeknal_version"]})

        status, bad_host = api("/api/workspaces", {"name": "Injected", "host": "-oProxyCommand=touch /tmp/x", "path": "/tmp"})
        check(status == 400 and "SSH host" in bad_host.get("error", ""), "option-shaped host rejected", bad_host)
        status, bad_path = api("/api/workspaces", {"name": "Not a project", "host": HOST, "path": "/tmp"})
        check(status == 400 and "not a Seeknal project" in bad_path.get("error", ""), "non-project remote path rejected", bad_path)
        record("invalid remote registrations rejected", {"host_error": bad_host["error"], "path_error": bad_path["error"]})

        status, saved = api("/api/workspaces", {"name": "Remote commerce (FK-Legion)", "host": HOST, "path": REMOTE_PATH})
        check(status == 200, "remote workspace saved", saved)
        remote = saved["workspace"]
        check(remote["kind"] == "ssh" and remote["path"].startswith("/") and remote["host"] == HOST, "canonical remote entry", remote)
        status, local_saved = api("/api/workspaces", {"name": "Local commerce copy", "path": str(local)})
        check(status == 200, "local workspace saved", local_saved)
        record("remote workspace registered over SSH", remote)

        started = time.monotonic()
        status, opened = api("/api/workspaces/open", {"id": remote["id"]})
        check(status == 202 and opened["workspace"]["kind"] == "ssh", "remote open accepted", opened)
        wait(lambda: current_workspace().get("kind") == "ssh", 900, "remote workspace", interval=1)
        state = api("/api/state")[1]
        check(state.get("remote", {}).get("host") == HOST, "state reports the SSH host", state.get("remote"))
        check(state["project_path"] == remote["path"], "state keeps the canonical remote path for bindings", state["project_path"])
        check(state["seeknal_version"] == "2.14.0", "remote Seeknal version", state["seeknal_version"])
        files = {item["path"] for item in state["files"]}
        check("seeknal/transforms/customer_ltv.yml" in files, "remote project files listed", sorted(files))
        record("switched to remote workspace", {
            "seconds": round(time.monotonic() - started, 1), "project_path": state["project_path"],
            "runtime_id": state["runtime_id"], "files": sorted(files),
        })

        status, file_payload = api("/api/file?path=seeknal_project.yml")
        check(status == 200 and "remote_commerce_demo" in file_payload["content"], "remote file read through tunnel", file_payload)
        record("remote file content served", {"first_line": file_payload["content"].splitlines()[0]})

        newest = f"find {REMOTE_PATH}/target -type f -printf '%T@ %P\\n' 2>/dev/null | sort -n | tail -1"
        before = ssh(newest) or "none"
        job = run_job("run")
        check(job.get("status") in {"succeeded", "success"}, "remote pipeline run succeeded", job)
        after = ssh(newest)
        check(after not in {"", before}, "remote target state changed on the SSH host", {"before": before, "after": after})
        record("pipeline executed on remote host", {"job": job.get("id"), "status": job.get("status"), "remote_state_mtime": [before, after]})

        graph = api("/api/graph")[1]
        statuses = {node["id"]: node["status"] for node in graph.get("nodes", [])}
        record("remote lineage graph", statuses)

        orders = repl_count("orders")
        check(orders == 9, "remote REPL sees the 9 remote-only orders", orders)
        record("remote SQL REPL count", {"orders": orders})

        service_logs = re.findall(r"Service log: (.+)", log_path.read_text())
        check(bool(service_logs), "service log path printed", log_path.read_text()[-800:])
        log_text = Path(service_logs[-1]).read_text(errors="replace")
        ports = re.findall(r"backend listening on remote 127\.0\.0\.1:(\d+)", log_text)
        check(bool(ports), "remote port logged", log_text[-800:])
        remote_port = ports[-1]
        unauthenticated = ssh(f"curl -s -o /dev/null -w '%{{http_code}}' http://127.0.0.1:{remote_port}/api/state")
        processes = ssh("pgrep -af '[s]eeknal-studio/server/.*/server.py' || true")
        check(unauthenticated == "403", "remote backend rejects requests without the session token", unauthenticated)
        record("remote backend requires session token", {"remote_port": remote_port, "no_token_status": unauthenticated, "processes": processes.splitlines()})
        try:
            with urllib.request.urlopen(BASE + "/api/state", timeout=10) as response:
                gateway_status = response.status
        except urllib.error.HTTPError as error:
            gateway_status = error.code
        check(gateway_status == 403, "local gateway rejects requests without the browser secret", gateway_status)
        check(ACCESS[TOKEN_HEADER] not in log_path.read_text() + log_text, "browser secret leaked into a log", None)
        record("local gateway requires the per-launch browser secret", {"no_secret_status": gateway_status})

        # Half-open connection (laptop sleep / network change): freeze the local
        # SSH session so heartbeats stop without closing the TCP connection.
        listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True).stdout
        local_serve = [line.split()[0] for line in listing.splitlines()
                       if line.split()[1:2] == ["ssh"] and "remote_host.py serve" in line]
        check(len(local_serve) == 1, "exactly one local SSH serve session",
              [line[:160] for line in listing.splitlines() if "remote_host.py serve" in line])
        serve_pid = int(local_serve[0])
        backend_pid = processes.split()[0]
        before_runtime = api("/api/state")[1]["runtime_id"]
        frozen = time.monotonic()
        os.kill(serve_pid, signal.SIGSTOP)
        try:
            remaining = "unknown"
            while time.monotonic() - frozen < 120:
                remaining = ssh(f"kill -0 {backend_pid} 2>/dev/null && ps -o pid,etime,args -p {backend_pid} | tail -1")
                if not remaining:
                    break
                time.sleep(2)
            check(not remaining, "remote backend stopped after heartbeat loss", {
                "backend_pid": backend_pid, "remaining": remaining,
                "remote": ssh("pgrep -af '[r]emote_host.py serve|[s]eeknal-studio/server/.*/server.py' || true"),
            })
            stopped_after = round(time.monotonic() - frozen, 1)
        finally:
            os.kill(serve_pid, signal.SIGCONT)
        reconnected = wait(
            lambda: (lambda value: value if value.get("remote") and value.get("runtime_id") != before_runtime else None)(api("/api/state")[1]),
            300, "launcher reconnect", interval=1,
        )
        record("silent connection stops remote backend; launcher reconnects", {
            "remote_backend_stopped_after_seconds": stopped_after,
            "old_runtime": before_runtime, "new_runtime": reconnected["runtime_id"],
        })

        status, back = api("/api/workspaces/open", {"id": local_saved["workspace"]["id"]})
        check(status == 202, "return to local accepted", back)
        wait(lambda: current_workspace().get("kind") == "local" and current_workspace().get("path") == str(local.resolve()), 120, "local workspace")
        local_after = api("/api/state")[1]
        check("remote" not in local_after and local_after["project_path"] == str(local.resolve()), "local backend restored", local_after.get("project_path"))
        local_orders = repl_count("orders") if run_job("run").get("status") in {"succeeded", "success"} else None
        check(local_orders == 6, "local REPL sees 6 orders", local_orders)
        stopped = wait(lambda: not ssh("pgrep -af '[s]eeknal-studio/server/.*/server.py' || true"), 30, "remote backend shutdown")
        record("returned to local workspace and remote backend stopped", {
            "project_path": local_after["project_path"], "orders": local_orders, "remote_processes_after": "none" if stopped else "",
        })
        saved_entries = api("/api/workspaces")[1]["entries"]
        check({entry["kind"] for entry in saved_entries} == {"ssh", "local"}, "both registrations survive switching", saved_entries)
        record("registrations persisted", [{key: entry.get(key) for key in ("name", "kind", "location", "path")} for entry in saved_entries])
        receipt["result"] = "pass"
        return 0
    except BaseException as error:
        receipt["result"] = "fail"
        receipt["error"] = str(error)
        raise
    finally:
        os.killpg(launcher.pid, signal.SIGTERM)
        try:
            launcher.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(launcher.pid, signal.SIGKILL)
        receipt["remote_processes_after_exit"] = ssh("pgrep -af '[s]eeknal-studio/server/.*/server.py' || true")
        (EVIDENCE / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
