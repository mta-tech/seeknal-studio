#!/usr/bin/env python3
"""Local gateway for an SSH remote workspace.

The UI, saved-workspace registry, and launcher switch channel stay local; every
project-bound API call is forwarded through the SSH port forward to the remote
Studio backend with its per-session access token.
"""

from __future__ import annotations

import http.client
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import signal
import threading
from urllib.parse import urlparse

from project_service import ProjectService
from remote_workspace import TOKEN_HEADER, RemoteTarget, unix_request


ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("SEEKNAL_STUDIO_PORT", "18766"))
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
STATIC = {"index.html", "styles.css", "app.js"}
MAX_BODY = 300_000
REMOTE = RemoteTarget.from_json(json.loads(os.environ.get("SEEKNAL_STUDIO_REMOTE", "null")))
UPSTREAM_SOCKET = os.environ.get("SEEKNAL_STUDIO_REMOTE_SOCKET", "")
UPSTREAM_HOST = f"127.0.0.1:{os.environ.get('SEEKNAL_STUDIO_REMOTE_PORT', '')}"
TOKEN = os.environ.pop("SEEKNAL_STUDIO_REMOTE_TOKEN", "")
NOTICE = os.environ.pop("SEEKNAL_STUDIO_NOTICE", None)
WORK_ACCEPTANCE_LOCK = threading.RLock()


def upstream(method: str, path: str, body: bytes | None = None, content_type: str | None = None,
             timeout: float = 120) -> tuple[int, str, bytes]:
    headers = {"Host": UPSTREAM_HOST, TOKEN_HEADER: TOKEN, "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    return unix_request(UPSTREAM_SOCKET, method, path, headers, body, timeout)


def remote_activity() -> list[str]:
    try:
        status, _, body = upstream("GET", "/api/activity", timeout=10)
        active = json.loads(body).get("active") if status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        # An unreachable host cannot report work; allow switching away from it.
        return []
    return [str(item) for item in active] if isinstance(active, list) else []


control_value = os.environ.get("SEEKNAL_STUDIO_CONTROL_FILE")
PROJECTS = ProjectService(
    None,
    busy=remote_activity,
    control_path=Path(control_value) if control_value else None,
    control_nonce=os.environ.get("SEEKNAL_STUDIO_CONTROL_NONCE"),
    remote=REMOTE,
    notice=NOTICE,
)


def remote_state(payload: dict) -> dict:
    # project_path stays the backend's canonical remote path: REPL and agent
    # bindings compare against it. The UI displays ``remote.location``.
    payload.update(
        remote={
            "host": REMOTE.host, "path": REMOTE.path, "ssh_port": REMOTE.ssh_port,
            "location": REMOTE.location, "workspace_id": REMOTE.workspace_id,
        },
        notice=NOTICE,
    )
    return payload


class Handler(SimpleHTTPRequestHandler):
    server_version = "SeeknalStudioGateway/0.1"

    def trusted(self) -> bool:
        return self.headers.get("Host") in HOSTS and self.headers.get("Origin") in {None, *ORIGINS}

    def send_body(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, payload: object, status: int = 200) -> None:
        self.send_body(status, "application/json; charset=utf-8", json.dumps(payload).encode())

    def body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 0 or length > MAX_BODY:
            raise ValueError("request too large")
        return self.rfile.read(length)

    def proxy(self, method: str, body: bytes | None = None) -> None:
        try:
            status, content_type, response = upstream(
                method, self.path, body, self.headers.get("Content-Type") if body is not None else None,
            )
        except (OSError, http.client.HTTPException) as error:
            self.send_json({"error": f"Remote workspace {REMOTE.host} is unreachable: {error}"}, 502)
            return
        if self.path == "/api/state" and status == 200:
            try:
                response = json.dumps(remote_state(json.loads(response))).encode()
            except ValueError:
                pass
        self.send_body(status, content_type, response)

    def do_GET(self) -> None:
        if not self.trusted():
            self.send_json({"error": "loopback origin required"}, 403)
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/projects":
            self.send_json(PROJECTS.status())
        elif parsed.path == "/api/workspaces":
            self.send_json(PROJECTS.workspaces_status())
        elif parsed.path.startswith("/api/"):
            self.proxy("GET")
        else:
            if parsed.path == "/":
                self.path = "/index.html"
            super().do_GET()

    def do_POST(self) -> None:
        if not self.trusted():
            self.send_json({"error": "loopback origin required"}, 403)
            return
        try:
            raw = self.body()
            if self.path not in {
                "/api/projects/browse", "/api/projects/open", "/api/workspaces",
                "/api/workspaces/remove", "/api/workspaces/open",
            }:
                self.proxy("POST", raw)
                return
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            if self.path == "/api/projects/browse":
                self.send_json(PROJECTS.browse())
            elif self.path == "/api/projects/open":
                with WORK_ACCEPTANCE_LOCK:
                    project = PROJECTS.prepare_switch(data.get("path"))
                    PROJECTS.queue_switch(project)
                self.send_json({"switching": True, "project": {"path": str(project), "name": project.name}}, 202)
            elif self.path == "/api/workspaces":
                probed = PROJECTS.probe_workspace_request(data.get("host"), data.get("path"), data.get("ssh_port"))
                with WORK_ACCEPTANCE_LOCK:
                    workspace = PROJECTS.save_workspace(
                        data.get("name"), data.get("path"), data.get("id"),
                        data.get("host"), data.get("ssh_port"), probed,
                    )
                self.send_json({"workspace": workspace})
            elif self.path == "/api/workspaces/remove":
                with WORK_ACCEPTANCE_LOCK:
                    self.send_json(PROJECTS.remove_workspace(data.get("id")))
            else:
                probed = PROJECTS.probe_workspace(data.get("id"))
                with WORK_ACCEPTANCE_LOCK:
                    target, workspace = PROJECTS.prepare_workspace_switch(data.get("id"), probed)
                    PROJECTS.queue_switch(target)
                self.send_json({"switching": True, "workspace": workspace}, 202)
        except RuntimeError as error:
            self.send_json({"error": str(error)}, 409)
        except (json.JSONDecodeError, ValueError) as error:
            self.send_json({"error": str(error)}, 400)

    def translate_path(self, path: str) -> str:
        relative = urlparse(path).path.lstrip("/") or "index.html"
        return str(ROOT / relative) if relative in STATIC else str(ROOT / "__not_found__")

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    if not UPSTREAM_SOCKET or not TOKEN:
        raise SystemExit("remote gateway requires an upstream socket and session token")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def shutdown(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        print(f"Seeknal Studio gateway: http://127.0.0.1:{PORT} -> {REMOTE.location}", flush=True)
        server.serve_forever()
    finally:
        PROJECTS.close()
        server.server_close()


if __name__ == "__main__":
    main()
