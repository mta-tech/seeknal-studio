from __future__ import annotations

import importlib.util
from http.server import BaseHTTPRequestHandler
import socketserver
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import io
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from project_service import ProjectService
from remote_workspace import (
    SERVER_FILES, TOKEN_HEADER, RemoteTarget, server_bundle, ssh_command,
    validate_host, validate_remote_path, validate_ssh_port,
)


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def load_launch():
    spec = importlib.util.spec_from_file_location("studio_launch_remote", ROOT / "launch.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port: int, path: str, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    merged = {"Content-Type": "application/json"} if data is not None else {}
    merged.update(headers or {})
    req = Request(f"http://127.0.0.1:{port}{path}", data=data, headers=merged,
                  method="POST" if data is not None else "GET")
    try:
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def wait_for(predicate, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, URLError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


class RemoteValidationTests(unittest.TestCase):
    def test_host_rejects_option_and_shell_shaped_values(self):
        for value in ("-oProxyCommand=touch /tmp/x", "user@host;rm -rf ~", "host name", "", "@host", None):
            with self.assertRaises(ValueError, msg=value):
                validate_host(value)
        self.assertEqual("fitra@100.73.79.64", validate_host(" fitra@100.73.79.64 "))
        self.assertEqual("legion", validate_host("legion"))

    def test_port_and_path_validation(self):
        self.assertIsNone(validate_ssh_port(""))
        self.assertEqual(22, validate_ssh_port("22"))
        self.assertEqual(2222, validate_ssh_port("2222"))
        for value in ("0", "65536", "22a", True, 1.5):
            with self.assertRaises(ValueError):
                validate_ssh_port(value)
        self.assertEqual("~/demo", validate_remote_path("~/demo"))
        for value in ("relative/demo", "/tmp/a\nb", "~other/demo", ""):
            with self.assertRaises(ValueError):
                validate_remote_path(value)

    def test_ssh_command_ends_options_before_host(self):
        target = RemoteTarget.checked("fitra@legion", "/srv/demo", "2222")
        command = ssh_command(target, "echo ok", "-N")
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(["-p", "2222"], command[command.index("-p"):command.index("-p") + 2])
        self.assertEqual(["-N", "--", "fitra@legion", "echo ok"], command[-4:])
        self.assertEqual("fitra@legion:2222:/srv/demo", target.location)

    def test_descriptor_round_trip_revalidates(self):
        target = RemoteTarget.checked("legion", "/srv/demo", None, workspace_id="w1", name="Demo")
        self.assertEqual(target, RemoteTarget.from_json(json.loads(json.dumps(target.to_json()))))
        with self.assertRaises(ValueError):
            RemoteTarget.from_json({**target.to_json(), "host": "-oProxyCommand=x"})

    def test_server_bundle_is_deterministic_and_complete(self):
        first, payload = server_bundle()
        second, _ = server_bundle()
        self.assertEqual(first, second)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            names = [member.name for member in archive.getmembers()]
            self.assertTrue(all(member.isfile() and member.mtime == 0 for member in archive.getmembers()))
        self.assertEqual(list(SERVER_FILES), names)


class RemoteRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.current = self.root / "current"
        self.current.mkdir()
        (self.current / "seeknal_project.yml").write_text("name: current\n")
        self.config = self.root / "private" / "projects.json"
        self.control = self.root / "private" / "switch.json"
        self.probes: list[RemoteTarget] = []

    def tearDown(self):
        self.temporary.cleanup()

    def probe(self, target: RemoteTarget) -> RemoteTarget:
        self.probes.append(target)
        if "missing" in target.path:
            raise ValueError("remote directory is not a Seeknal project")
        path = target.path.replace("~", "/home/fitra", 1)
        return RemoteTarget(target.host, path, target.ssh_port, target.workspace_id, target.name)

    def service(self, *, remote: RemoteTarget | None = None, busy=lambda: []) -> ProjectService:
        return ProjectService(
            None if remote else self.current, remote=remote, busy=busy,
            config_path=self.config, control_path=self.control, control_nonce="nonce",
            remote_probe=self.probe,
        )

    def test_save_canonicalizes_and_rejects_duplicates(self):
        service = self.service()
        saved = service.save_workspace("Legion", "~/demo", None, "fitra@legion", "")
        self.assertEqual("ssh", saved["kind"])
        self.assertEqual("/home/fitra/demo", saved["path"])
        self.assertEqual("fitra@legion:/home/fitra/demo", saved["location"])
        self.assertFalse(saved["current"])
        stored = json.loads(self.config.read_text())["workspaces"][0]
        self.assertEqual({"id", "name", "kind", "host", "path"}, set(stored))
        self.assertEqual(0o600, self.config.stat().st_mode & 0o777)
        with self.assertRaisesRegex(ValueError, "already saved"):
            service.save_workspace("Again", "/home/fitra/demo", None, "fitra@legion")
        with self.assertRaisesRegex(ValueError, "already saved"):
            service.save_workspace("Port 22", "/home/fitra/demo", None, "fitra@legion", "22")
        with self.assertRaisesRegex(ValueError, "not a Seeknal project"):
            service.save_workspace("Missing", "/home/fitra/missing", None, "fitra@legion")
        with self.assertRaisesRegex(ValueError, "SSH port requires"):
            service.save_workspace("Local", str(self.current), None, None, "2222")
        self.assertEqual(1, len(json.loads(self.config.read_text())["workspaces"]))

    def test_editing_remote_entry_to_local_drops_remote_fields(self):
        service = self.service()
        saved = service.save_workspace("Legion", "/srv/demo", None, "legion", "2222")
        self.assertEqual(2222, saved["ssh_port"])
        service.save_workspace("Now local", str(self.current), saved["id"])
        stored = json.loads(self.config.read_text())["workspaces"][0]
        self.assertEqual({"id", "name", "path"}, set(stored))

    def test_open_remote_probes_and_writes_remote_control_without_touching_last(self):
        service = self.service()
        saved = service.save_workspace("Legion", "/srv/demo", None, "legion")
        self.probes.clear()
        target, status = service.prepare_workspace_switch(saved["id"])
        assert isinstance(target, RemoteTarget)
        self.assertEqual(1, len(self.probes))
        self.assertEqual(saved["id"], target.workspace_id)
        service.queue_switch(target)
        self.assertNotIn("last", json.loads(self.config.read_text()))
        launch = load_launch()
        self.assertEqual(target, launch.read_switch(self.control, "nonce"))
        self.assertFalse(self.control.exists())
        self.assertEqual("ssh", status["kind"])

    def test_busy_remote_work_blocks_switch(self):
        service = self.service(busy=lambda: ["pipeline job"])
        saved = service.save_workspace("Legion", "/srv/demo", None, "legion")
        with self.assertRaisesRegex(RuntimeError, "pipeline job"):
            service.prepare_workspace_switch(saved["id"])

    def test_remote_current_is_reported_and_cannot_reopen(self):
        saved = self.service().save_workspace("Legion", "/srv/demo", None, "legion")
        remote = RemoteTarget("legion", "/srv/demo", None, saved["id"], "Legion")
        service = self.service(remote=remote)
        local = service.save_workspace("Local", str(self.current))
        status = service.workspaces_status()
        self.assertEqual(saved["id"], status["current"]["id"])
        self.assertTrue(status["current"]["current"])
        self.assertEqual("Workspace is currently open", status["current"]["reason"])
        self.assertTrue(next(e for e in status["entries"] if e["id"] == local["id"])["can_switch"])
        with self.assertRaisesRegex(ValueError, "already open"):
            service.prepare_workspace_switch(saved["id"])
        self.assertEqual("ssh", service.status()["current"]["kind"])
        project, _ = service.prepare_workspace_switch(local["id"])
        self.assertEqual(self.current.resolve(), project)

    def test_probe_results_are_reused_inside_the_work_lock(self):
        service = self.service()
        probed = service.probe_workspace_request("legion", "~/demo", "")
        self.assertEqual(1, len(self.probes))
        saved = service.save_workspace("Legion", "~/demo", None, "legion", "", probed)
        self.assertEqual(1, len(self.probes))
        self.assertEqual("/home/fitra/demo", saved["path"])
        ahead = service.probe_workspace(saved["id"])
        self.assertEqual(2, len(self.probes))
        target, _ = service.prepare_workspace_switch(saved["id"], ahead)
        self.assertEqual(2, len(self.probes))
        self.assertEqual(ahead, target)
        assert ahead is not None
        stale = RemoteTarget(ahead.host, "/home/fitra/old", ahead.ssh_port, ahead.workspace_id, ahead.name)
        target, _ = service.prepare_workspace_switch(saved["id"], stale)
        self.assertEqual(3, len(self.probes))
        self.assertEqual(ahead, target)
        self.assertIsNone(service.probe_workspace_request("", str(self.current)))

    def test_malformed_remote_entries_are_skipped_when_listing(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"workspaces": [
            {"id": "bad", "name": "Bad", "kind": "ssh", "host": "-oProxyCommand=x", "path": "/srv"},
            {"id": "odd", "name": "Odd", "kind": "ftp", "path": "/srv"},
        ]}))
        self.config.chmod(0o600)
        self.assertEqual([], self.service().workspaces_status()["entries"])


FAKE_SSH = r"""#!/usr/bin/env python3
import json, sys, time
args = sys.argv[1:]
command = args[args.index("--") + 2] if len(args) > args.index("--") + 2 else ""
if command.startswith("python3 - "):
    sys.stdin.read()
    print(json.dumps({"ok": True, "path": "/srv/demo", "name": "demo"}))
elif "-N" in args:
    time.sleep(60)
else:
    sys.stdin.buffer.read()
    print("simulated remote upload failure", file=sys.stderr)
    sys.exit(1)
"""


class RemoteLauncherFallbackTests(unittest.TestCase):
    def test_failed_remote_start_falls_back_to_local_with_notice(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "ssh").write_text(FAKE_SSH)
            (bin_dir / "ssh").chmod(0o755)
            project = root / "local"
            project.mkdir()
            (project / "seeknal_project.yml").write_text("name: local\n")
            port = free_port()
            env = {key: value for key, value in os.environ.items() if key not in {"ATLAS_API_URL", "ATLAS_PORTAL_URL"}}
            env.update(
                PATH=f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}",
                SEEKNAL_STUDIO_PROJECTS_FILE=str(root / "projects.json"),
                SEEKNAL_STUDIO_AGENT_MODE="external", BROWSER="true",
            )
            process = subprocess.Popen(
                [str(PYTHON), str(ROOT / "launch.py"), "--browser", "--port", str(port), "--project", str(project)],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                first = wait_for(lambda: request(port, "/api/workspaces")[1])
                status, saved = request(port, "/api/workspaces", {"name": "Legion", "host": "legion", "path": "/srv/demo"})
                self.assertEqual(200, status, saved)
                status, opened = request(port, "/api/workspaces/open", {"id": saved["workspace"]["id"]})
                self.assertEqual(202, status, opened)
                after = wait_for(lambda: (lambda value: value if value["instance"] != first["instance"] else None)(
                    request(port, "/api/workspaces")[1]
                ), timeout=60)
                self.assertEqual("local", after["current"]["kind"])
                self.assertEqual(str(project.resolve()), after["current"]["path"])
                self.assertIn("Could not open remote workspace Legion", after["notice"])
                self.assertIn("simulated remote upload failure", after["notice"])
                state = request(port, "/api/state")[1]
                self.assertEqual(after["notice"], state["notice"])
            finally:
                process.terminate()
                process.communicate(timeout=30)
            self.assertEqual(0, process.returncode)


class Upstream(BaseHTTPRequestHandler):
    token = "t" * 43
    seen: list[dict] = []

    def reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode() if length else None
        Upstream.seen.append({"method": method, "path": self.path, "host": self.headers.get("Host"),
                              "token": self.headers.get(TOKEN_HEADER), "body": body})
        if self.headers.get(TOKEN_HEADER) != self.token:
            self.reply(403, {"error": "loopback origin required"})
        elif self.path == "/api/state":
            self.reply(200, {"project": "demo", "project_path": "/srv/demo", "runtime_id": "r1", "files": []})
        elif self.path == "/api/activity":
            self.reply(200, {"active": ["pipeline job"]})
        elif self.path == "/api/jobs":
            self.reply(202, {"id": "job1", "echo": json.loads(body or "{}")})
        else:
            self.reply(404, {"error": "unknown endpoint"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
        del format, args


class RemoteGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        socket_path = str(self.root / "upstream.sock")
        self.upstream = socketserver.ThreadingUnixStreamServer(socket_path, Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.port = free_port()
        target = RemoteTarget("legion", "/srv/demo", None, "w1", "Legion")
        env = dict(os.environ)
        env.update(
            SEEKNAL_STUDIO_PORT=str(self.port),
            SEEKNAL_STUDIO_PROJECTS_FILE=str(self.root / "projects.json"),
            SEEKNAL_STUDIO_CONTROL_FILE=str(self.root / "switch.json"),
            SEEKNAL_STUDIO_CONTROL_NONCE="nonce",
            SEEKNAL_STUDIO_REMOTE=json.dumps(target.to_json()),
            SEEKNAL_STUDIO_REMOTE_SOCKET=socket_path,
            SEEKNAL_STUDIO_REMOTE_PORT="40123",
            SEEKNAL_STUDIO_REMOTE_TOKEN=Upstream.token,
        )
        Upstream.seen = []
        self.gateway = subprocess.Popen([str(PYTHON), str(ROOT / "remote_gateway.py")], cwd=ROOT, env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        wait_for(lambda: request(self.port, "/api/workspaces")[0] == 200)
        Upstream.seen = []

    def tearDown(self):
        self.gateway.terminate()
        self.gateway.communicate(timeout=10)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temporary.cleanup()

    def test_state_is_proxied_with_token_and_annotated(self):
        status, state = request(self.port, "/api/state")
        self.assertEqual(200, status)
        self.assertEqual("/srv/demo", state["project_path"])
        self.assertEqual("legion:/srv/demo", state["remote"]["location"])
        self.assertEqual("legion", state["remote"]["host"])
        self.assertEqual({"host": "127.0.0.1:40123", "token": Upstream.token},
                         {key: Upstream.seen[-1][key] for key in ("host", "token")})

    def test_post_body_and_status_pass_through(self):
        status, job = request(self.port, "/api/jobs", {"action": "run"})
        self.assertEqual(202, status)
        self.assertEqual({"action": "run"}, job["echo"])
        status, _ = request(self.port, "/api/nope")
        self.assertEqual(404, status)

    def test_registry_is_local_and_remote_activity_blocks_switch(self):
        status, workspaces = request(self.port, "/api/workspaces")
        self.assertEqual(200, status)
        self.assertEqual("ssh", workspaces["current"]["kind"])
        self.assertEqual("legion", workspaces["current"]["host"])
        self.assertFalse(workspaces["can_switch"])
        self.assertFalse(any(seen["path"] == "/api/workspaces" for seen in Upstream.seen))

    def test_untrusted_origin_is_rejected_before_proxying(self):
        status, _ = request(self.port, "/api/state", headers={"Origin": "http://evil.invalid"})
        self.assertEqual(403, status)
        self.assertEqual([], Upstream.seen)

    def test_unreachable_upstream_reports_502(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        status, payload = request(self.port, "/api/graph")
        self.assertEqual(502, status)
        self.assertIn("unreachable", payload["error"])
        # Unreachable hosts report no active work, so the user can switch away.
        self.assertTrue(request(self.port, "/api/workspaces")[1]["can_switch"])


class InstallLoaderTests(unittest.TestCase):
    def test_single_line_loader_installs_under_sh_and_csh(self):
        from remote_workspace import _INSTALL_SCRIPT
        import base64
        import shlex
        _, payload = server_bundle()
        loader = "import base64;exec(base64.b64decode('%s'))" % base64.b64encode(_INSTALL_SCRIPT.encode()).decode()
        for shell in ("/bin/sh", "/bin/csh"):
            if not Path(shell).exists():
                continue
            with tempfile.TemporaryDirectory() as name:
                target = Path(name) / "server" / "abc"
                command = " ".join(["python3", "-c", shlex.quote(loader), shlex.quote(str(target)), shlex.quote(",".join(SERVER_FILES))])
                result = subprocess.run([shell, "-c", command], input=payload, capture_output=True, timeout=60)
                self.assertEqual(0, result.returncode, (shell, result.stderr))
                self.assertEqual(b"installed", result.stdout.strip())
                self.assertTrue((target / ".complete").is_file())
                self.assertEqual(sorted(SERVER_FILES), sorted(p.name for p in target.iterdir() if p.name != ".complete"))


class ProjectLockTests(unittest.TestCase):
    def test_second_session_learns_who_holds_the_project(self):
        holder_script = (
            "import sys, time; sys.path.insert(0, sys.argv[1]); import remote_host;"
            "guard, _ = remote_host._project_lock(sys.argv[2], time.monotonic() + 5);"
            "print('held' if guard else 'missed', flush=True); time.sleep(30)"
        )
        waiter_script = (
            "import json, sys, time; sys.path.insert(0, sys.argv[1]); import remote_host;"
            "guard, holder = remote_host._project_lock(sys.argv[2], time.monotonic() + 1);"
            "print('RESULT ' + json.dumps({'acquired': guard is not None, 'holder': holder}), flush=True)"
        )
        with tempfile.TemporaryDirectory() as home:
            project = str(Path(home) / "project")
            base = {"HOME": home, "PATH": os.environ.get("PATH", "")}
            holder = subprocess.Popen(
                [str(PYTHON), "-c", holder_script, str(ROOT), project],
                env={**base, "SSH_CLIENT": "10.0.0.7 51234 22"}, stdout=subprocess.PIPE, text=True,
            )
            try:
                self.assertEqual("held", holder.stdout.readline().strip())
                waiter = subprocess.run(
                    [str(PYTHON), "-c", waiter_script, str(ROOT), project],
                    env={**base, "SSH_CLIENT": "10.0.0.9 40000 22"}, capture_output=True, text=True, timeout=30,
                )
            finally:
                holder.kill()
                holder.wait()
        lines = waiter.stdout.splitlines()
        busy = [line for line in lines if line.startswith("SEEKNAL_STUDIO_BUSY ")]
        self.assertEqual(1, len(busy), waiter.stdout)
        self.assertEqual("10.0.0.7", json.loads(busy[0].split(" ", 1)[1])["client"])
        result = json.loads(next(line for line in lines if line.startswith("RESULT ")).split(" ", 1)[1])
        self.assertFalse(result["acquired"])
        self.assertEqual("10.0.0.7", result["holder"]["client"])
        self.assertIn("started_at", result["holder"])

    def test_busy_project_error_names_the_holder(self):
        import remote_workspace
        from unittest import mock
        busy = "SEEKNAL_STUDIO_BUSY " + json.dumps({"client": "10.0.0.7", "started_at": "2026-10-30 09:00 UTC"})
        fake_host = f"import sys; sys.stdin.readline(); print({busy!r}, flush=True); sys.exit(5)"
        with tempfile.TemporaryDirectory() as name:
            log = open(Path(name) / "service.log", "w+")
            self.addCleanup(log.close)
            session = remote_workspace.RemoteSession(
                RemoteTarget("fitra@example.invalid", "/srv/project", None, None, None), log=log, socket_dir=Path(name),
            )
            self.addCleanup(session._stopping.set)
            with mock.patch.object(remote_workspace, "ssh_command", lambda *_args: [str(PYTHON), "-c", fake_host]):
                with self.assertRaisesRegex(RuntimeError, "already has this remote project open") as raised:
                    session._serve("/tmp/unused", "/usr/bin/python3")
            self.assertIn("opened from 10.0.0.7, since 2026-10-30 09:00 UTC", str(raised.exception))
            session._host.stdin.close()
            session._host.stdout.close()
            log.seek(0)
            self.assertIn("waiting for it to close", log.read())

    def test_client_reports_the_busy_holder_while_waiting(self):
        from remote_workspace import RemoteSession, _describe_holder
        stream = io.BytesIO(
            b"motd noise\n"
            b'SEEKNAL_STUDIO_BUSY {"client": "10.0.0.7", "started_at": "2026-10-30 09:00 UTC"}\n'
            b'SEEKNAL_STUDIO_READY {"port": 1}\n'
        )
        seen = []
        line = RemoteSession._ready_line(stream, 5, seen.append)
        self.assertTrue(line.startswith(b"SEEKNAL_STUDIO_READY"))
        self.assertEqual([{"client": "10.0.0.7", "started_at": "2026-10-30 09:00 UTC"}], seen)
        self.assertEqual("opened from 10.0.0.7, since 2026-10-30 09:00 UTC", _describe_holder(seen[0]))
        self.assertEqual("details unavailable", _describe_holder({"client": 5}))


class ServerTokenTests(unittest.TestCase):
    def test_server_requires_access_token_when_configured(self):
        token = "s" * 43
        with tempfile.TemporaryDirectory() as name:
            port_file = Path(name) / "port"
            env = dict(os.environ)
            env.update(
                SEEKNAL_STUDIO_PORT="0", SEEKNAL_STUDIO_PORT_FILE=str(port_file),
                SEEKNAL_STUDIO_PROJECT=str(ROOT / "demo"),
                SEEKNAL_STUDIO_ACCESS_TOKEN=token, SEEKNAL_STUDIO_AGENT_MODE="external",
                SEEKNAL_STUDIO_PROJECTS_FILE=os.devnull,
            )
            process = subprocess.Popen([str(PYTHON), str(ROOT / "server.py")], cwd=ROOT, env=env,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            port = int(wait_for(lambda: port_file.is_file() and port_file.read_text().strip()))
            self.assertEqual(0o600, port_file.stat().st_mode & 0o777)
            self._check(process, port, token)

    def _check(self, process, port: int, token: str):
        try:
            wait_for(lambda: request(port, "/api/activity", headers={TOKEN_HEADER: token})[0] == 200)
            self.assertEqual(403, request(port, "/api/activity")[0])
            self.assertEqual(403, request(port, "/api/activity", headers={TOKEN_HEADER: "x" * 43})[0])
            self.assertEqual({"active": []}, request(port, "/api/activity", headers={TOKEN_HEADER: token})[1])
        finally:
            process.terminate()
            process.communicate(timeout=15)


if __name__ == "__main__":
    unittest.main()
