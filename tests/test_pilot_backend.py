from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def load_server(project: Path):
    old = os.environ.get("SEEKNAL_STUDIO_PROJECT")
    os.environ["SEEKNAL_STUDIO_PROJECT"] = str(project)
    try:
        spec = importlib.util.spec_from_file_location("studio_pilot_server_test", ROOT / "server.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = old


class FakeAgent:
    mode = "owned"

    def __init__(self, project: Path):
        self.project = project.resolve()
        self.config = None
        self.sessions = {}
        self.closed = False

    def status(self):
        return {
            "mode": "owned", "configured": True, "healthy": True, "ready": True,
            "project_verified": True, "project_path": str(self.project),
            "project_fingerprint": "test-fingerprint", "identity_nonce": "test-nonce",
            "provider": "openai", "model": "deepseek-v4.1-flash:netra",
            "capabilities": {"draft_only": True, "tools": ["draft_node"]},
        }

    def configure(self, payload):
        self.config = dict(payload)
        return self.status()

    def start(self, question, session_id, turn_id):
        session = {
            "session_id": session_id, "turn_id": turn_id,
            "status": "running", "events": [],
        }
        self.sessions[session_id] = session
        return dict(session)

    def get(self, session_id, turn_id=None):
        session = dict(self.sessions[session_id])
        return session if turn_id is None or session["turn_id"] == turn_id else None

    def cancel(self, session_id, turn_id=None):
        if turn_id is not None and self.sessions[session_id]["turn_id"] != turn_id:
            return None
        self.sessions[session_id]["status"] = "cancelled"
        return dict(self.sessions[session_id])

    def close(self):
        self.closed = True


class PilotBackendHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.project = Path(cls.tempdir.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project)
        cls.server = load_server(cls.project)
        cls.agent = FakeAgent(cls.project)
        cls.original_agent = cls.server.AGENT
        cls.server.AGENT = cls.agent
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server.HOSTS = {f"127.0.0.1:{cls.port}"}
        cls.server.ORIGINS = {f"http://127.0.0.1:{cls.port}"}
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server.AGENT = cls.original_agent
        cls.tempdir.cleanup()

    def request(self, path, *, method="GET", payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Origin": f"http://127.0.0.1:{self.port}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers=headers, method=method,
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_status_and_state_attest_selected_project(self):
        status, agent = self.request("/api/agent/status")
        self.assertEqual(200, status)
        self.assertTrue(agent["project_verified"])
        self.assertEqual(str(self.project.resolve()), agent["project_path"])
        self.assertEqual("deepseek-v4.1-flash:netra", agent["model"])
        status, state = self.request("/api/state")
        self.assertEqual(200, status)
        self.assertEqual(
            {"configured": True, "project_verified": True, "mode": "owned"},
            state["gateway"],
        )

    def test_owned_agent_session_lifecycle(self):
        turn_id = "2f881d98-827d-4ca4-ab9b-c6f4951c017f"
        status, started = self.request(
            "/api/agent/ask", method="POST",
            payload={
                "question": "Create a draft", "session_id": "pilot_01",
                "turn_id": turn_id,
            },
        )
        self.assertEqual(202, status)
        self.assertEqual("running", started["status"])
        self.assertEqual(turn_id, started["turn_id"])
        status, current = self.request(
            f"/api/agent/sessions/pilot_01?turn_id={turn_id}"
        )
        self.assertEqual(200, status)
        self.assertEqual("pilot_01", current["session_id"])
        status, cancelled = self.request(
            "/api/agent/cancel", method="POST",
            payload={"session_id": "pilot_01", "turn_id": turn_id},
        )
        self.assertEqual(200, status)
        self.assertEqual("cancelled", cancelled["status"])

    def test_turn_id_is_required_and_old_turn_is_not_accepted_as_new(self):
        first_turn = "e691b28b-dd6f-4f13-8d7c-385362764b2a"
        status, _first = self.request(
            "/api/agent/ask", method="POST",
            payload={
                "question": "first", "session_id": "stable_session",
                "turn_id": first_turn,
            },
        )
        self.assertEqual(202, status)
        self.agent.sessions["stable_session"].update(
            status="succeeded", answer="old answer",
        )
        second_turn = "2dc374c0-8ef6-49c7-b184-91d761cd14a9"
        original_start = self.agent.start
        self.agent.start = lambda _question, _session, _turn: dict(
            self.agent.sessions["stable_session"]
        )
        try:
            status, rejected = self.request(
                "/api/agent/ask", method="POST",
                payload={
                    "question": "second", "session_id": "stable_session",
                    "turn_id": second_turn,
                },
            )
            self.assertEqual(400, status)
            self.assertIn("turn_id", rejected["error"])
        finally:
            self.agent.start = original_start
        status, current = self.request(
            f"/api/agent/sessions/stable_session?turn_id={first_turn}"
        )
        self.assertEqual(200, status)
        self.assertEqual(first_turn, current["turn_id"])
        self.assertEqual("old answer", current["answer"])

    def test_configuration_is_sanitized_and_backend_only(self):
        payload = {
            "provider": "openai", "model": "deepseek-v4.1-flash:netra",
            "base_url": "https://ai.sumopod.com/v1",
            "key_path": "~/sumopod-ai-api-key",
        }
        status, response = self.request("/api/agent/config", method="POST", payload=payload)
        self.assertEqual(200, status)
        self.assertNotIn("key_path", response)
        self.assertNotIn("api_key", json.dumps(response).lower())
        self.assertEqual(payload, self.agent.config)

    def test_failed_configuration_returns_non_success(self):
        original = self.agent.configure
        self.agent.configure = lambda _payload: (_ for _ in ()).throw(
            RuntimeError("probe failed")
        )
        try:
            status, response = self.request(
                "/api/agent/config", method="POST",
                payload={"key_path": "~/alternate-key"},
            )
            self.assertEqual(400, status)
            self.assertEqual("probe failed", response["error"])
        finally:
            self.agent.configure = original


class PilotBackendUnitTests(unittest.TestCase):
    def test_configure_does_not_label_failed_probe_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            shutil.copytree(ROOT / "demo", project)
            server = load_server(project)

            class FailedRuntime:
                def __init__(self):
                    self.closed = False

                def configure(self, _payload):
                    return {
                        "configured": True, "healthy": False, "ready": False,
                        "project_verified": False, "error": "probe failed",
                    }

                def close(self):
                    self.closed = True

            runtime = FailedRuntime()
            service = server.AgentService()
            with mock.patch.object(service, "_create_runtime", return_value=runtime):
                with self.assertRaisesRegex(RuntimeError, "probe failed"):
                    service.configure({"key_path": "~/alternate-key"})
            status = service.status()
            self.assertEqual("failed", status["state"])
            self.assertFalse(status["ready"])
            self.assertFalse(status["project_verified"])
            self.assertEqual("probe failed", status["error"])
            self.assertTrue(runtime.closed)
            self.assertIsNone(service.runtime)

    def test_http_is_ready_while_agent_provider_probe_is_still_running(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            shutil.copytree(ROOT / "demo", project)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            script = f"""
import sys, time
sys.path.insert(0, {str(ROOT)!r})
import server

class SlowRuntime:
    mode = 'owned'
    def start_default(self):
        time.sleep(4)
    def status(self):
        return {{'mode':'owned','configured':True,'healthy':False,'ready':False,
                 'project_verified':False,'state':'configuring'}}
    def close(self):
        pass

server.AGENT = SlowRuntime()
server.main()
"""
            environment = os.environ.copy()
            environment["SEEKNAL_STUDIO_PROJECT"] = str(project)
            environment["SEEKNAL_STUDIO_PORT"] = str(port)
            process = subprocess.Popen(
                [sys.executable, "-c", script], cwd=ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            started = time.monotonic()
            try:
                deadline = started + 2
                response = None
                while time.monotonic() < deadline:
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=0.3) as stream:
                            response = json.load(stream)
                            break
                    except OSError:
                        if process.poll() is not None:
                            self.fail(f"server exited early: {process.stderr.read()}")
                        time.sleep(0.03)
                self.assertIsNotNone(response, "HTTP startup waited for the slow provider probe")
                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual("owned", response["gateway"]["mode"])
                self.assertFalse(response["gateway"]["project_verified"])
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=6)
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def test_job_termination_kills_term_resistant_descendant_after_parent_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            descendant_pid_file = Path(directory) / "descendant.pid"
            parent_program = (
                "import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
                f"open({str(descendant_pid_file)!r},'w').write(str(child.pid)); "
                "print('descendant started', flush=True); time.sleep(60)"
            )
            with tempfile.TemporaryDirectory() as project_directory:
                project = Path(project_directory) / "demo"
                shutil.copytree(ROOT / "demo", project)
                server = load_server(project)
                original_seeknal = server.SEEKNAL
                original_commands = server.COMMANDS
                descendant_pid = None
                try:
                    server.SEEKNAL = Path(sys.executable)
                    server.COMMANDS = {"run": ["-c", parent_program]}
                    store = server.JobStore()
                    job = store.create("run")
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline and not descendant_pid_file.is_file():
                        self.assertNotEqual("failed", store.get(job["id"])["status"])
                        time.sleep(0.05)
                    self.assertTrue(descendant_pid_file.is_file())
                    descendant_pid = int(descendant_pid_file.read_text())
                    cancelled = store.cancel(job["id"])
                    self.assertEqual("cancelled", cancelled["status"])

                    deadline = time.monotonic() + 5
                    alive_state = ""
                    while time.monotonic() < deadline:
                        state = subprocess.run(
                            ["ps", "-o", "stat=", "-p", str(descendant_pid)],
                            capture_output=True, text=True, check=False,
                        ).stdout.strip()
                        alive_state = state
                        if not state or state.startswith("Z"):
                            break
                        time.sleep(0.05)
                    self.assertTrue(
                        not alive_state or alive_state.startswith("Z"),
                        f"TERM-resistant descendant remains alive: {alive_state}",
                    )
                finally:
                    server.SEEKNAL = original_seeknal
                    server.COMMANDS = original_commands
                    if descendant_pid is not None:
                        try:
                            os.kill(descendant_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_signal_during_agent_bootstrap_closes_and_reaps_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            project = temporary / "demo"
            shutil.copytree(ROOT / "demo", project)
            worker_pid_file = temporary / "worker.pid"
            closed_file = temporary / "closed"
            script = f"""
import os, signal, subprocess, sys, time
sys.path.insert(0, {str(ROOT)!r})
import server

class SlowRuntime:
    def __init__(self):
        self.worker = None

    def start_default(self):
        self.worker = subprocess.Popen(
            [sys.executable, '-c', 'import time; time.sleep(60)'],
            start_new_session=True,
        )
        open({str(worker_pid_file)!r}, 'w').write(str(self.worker.pid))
        while True:
            time.sleep(1)

    def close(self):
        if self.worker is not None and self.worker.poll() is None:
            os.killpg(self.worker.pid, signal.SIGTERM)
            self.worker.wait(timeout=5)
        open({str(closed_file)!r}, 'w').write('closed')

server.AGENT = SlowRuntime()
server.main()
"""
            environment = os.environ.copy()
            environment["SEEKNAL_STUDIO_PROJECT"] = str(project)
            environment["SEEKNAL_STUDIO_PORT"] = "0"
            process = subprocess.Popen(
                [sys.executable, "-c", script], cwd=ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            worker_pid = None
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        self.fail(f"bootstrap process exited early: {process.stderr.read()}")
                    if worker_pid_file.is_file():
                        published_pid = worker_pid_file.read_text().strip()
                        if published_pid.isdecimal() and int(published_pid) > 0:
                            worker_pid = int(published_pid)
                            break
                    time.sleep(0.05)
                if worker_pid is None:
                    self.fail("fake worker did not publish its PID")
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=6)
                self.assertEqual(0, process.returncode, process.stderr.read())
                self.assertTrue(closed_file.is_file(), "agent close was not called")
                with self.assertRaises(ProcessLookupError):
                    os.kill(worker_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if worker_pid is not None:
                    try:
                        os.killpg(worker_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

    def test_alternate_private_key_configures_before_readiness_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            shutil.copytree(ROOT / "demo", project)
            server = load_server(project)
            alternate_key = Path(directory) / "alternate-key"
            alternate_key.write_text("test-only-secret")
            alternate_key.chmod(0o600)

            class PersistentRuntime:
                persisted = None
                configure_calls = 0

                def __init__(self):
                    self.config = dict(self.persisted or {"api_key_file": "/missing/default-key"})
                    self.closed = False

                def status(self):
                    ready = Path(self.config["api_key_file"]).is_file()
                    return {
                        "configured": ready, "healthy": ready, "ready": ready,
                        "project_verified": ready,
                        **({} if ready else {"error": "configured key is missing"}),
                    }

                def configure(self, payload):
                    type(self).configure_calls += 1
                    self.config.update(payload)
                    type(self).persisted = dict(self.config)
                    return self.status()

                def close(self):
                    self.closed = True

            old_mode = os.environ.get("SEEKNAL_STUDIO_AGENT_MODE")
            os.environ["SEEKNAL_STUDIO_AGENT_MODE"] = "owned"
            try:
                first = server.AgentService()
                with mock.patch.object(first, "_create_runtime", side_effect=PersistentRuntime):
                    configured = first.configure({
                        "provider": "openai",
                        "model": "deepseek-v4.1-flash:netra",
                        "base_url": "https://ai.sumopod.com/v1",
                        "key_path": str(alternate_key),
                    })
                self.assertTrue(configured["ready"])
                self.assertEqual(str(alternate_key), first.runtime.config["api_key_file"])
                first.close()

                restarted = server.AgentService()
                with mock.patch.object(restarted, "_create_runtime", side_effect=PersistentRuntime):
                    status = restarted.start_default()
                self.assertTrue(status["ready"])
                self.assertEqual(str(alternate_key), restarted.runtime.config["api_key_file"])
                self.assertEqual(1, PersistentRuntime.configure_calls)
                restarted.close()
            finally:
                if old_mode is None:
                    os.environ.pop("SEEKNAL_STUDIO_AGENT_MODE", None)
                else:
                    os.environ["SEEKNAL_STUDIO_AGENT_MODE"] = old_mode

    def test_owned_mode_is_default_even_with_legacy_url_present(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            shutil.copytree(ROOT / "demo", project)
            old_url = os.environ.get("SEEKNAL_GATEWAY_URL")
            old_mode = os.environ.get("SEEKNAL_STUDIO_AGENT_MODE")
            try:
                os.environ["SEEKNAL_GATEWAY_URL"] = "http://127.0.0.1:9999"
                os.environ.pop("SEEKNAL_STUDIO_AGENT_MODE", None)
                server = load_server(project)
                self.assertEqual("owned", server.AgentService().mode)
                os.environ["SEEKNAL_STUDIO_AGENT_MODE"] = "external"
                self.assertEqual("external", server.AgentService().mode)
            finally:
                if old_url is None:
                    os.environ.pop("SEEKNAL_GATEWAY_URL", None)
                else:
                    os.environ["SEEKNAL_GATEWAY_URL"] = old_url
                if old_mode is None:
                    os.environ.pop("SEEKNAL_STUDIO_AGENT_MODE", None)
                else:
                    os.environ["SEEKNAL_STUDIO_AGENT_MODE"] = old_mode

    def test_error_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "demo"
            shutil.copytree(ROOT / "demo", project)
            server = load_server(project)
            message = server.safe_agent_error(
                RuntimeError("Authorization: secret-value https://x.test/?token=secret")
            )
            self.assertNotIn("secret-value", message)
            self.assertNotIn("token=secret", message)
            self.assertIn("[redacted]", message)

    def test_bundle_manifest_includes_runtime_without_credentials(self):
        source = (ROOT / "build_app.py").read_text()
        self.assertIn("'agent_runtime.py'", source)
        self.assertIn("'agent_worker.py'", source)
        self.assertNotIn("sumopod-ai-api-key", source)


if __name__ == "__main__":
    unittest.main()
