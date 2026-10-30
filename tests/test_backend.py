from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = ROOT / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location("studio_server_test", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class BackendUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.project = Path(cls.tempdir.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project)
        cls.old_project = os.environ.get("SEEKNAL_STUDIO_PROJECT")
        os.environ["SEEKNAL_STUDIO_PROJECT"] = str(cls.project)
        cls.server = load_server()

    @classmethod
    def tearDownClass(cls):
        if cls.old_project is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = cls.old_project
        cls.tempdir.cleanup()

    def test_project_has_six_nodes_and_five_edges(self):
        result = self.server.bridge("graph")
        self.assertEqual(6, len(result["nodes"]))
        self.assertEqual(5, len(result["edges"]))

    def test_safe_file_rejects_traversal_and_unknown_file(self):
        self.assertIsNone(self.server.safe_file("../signal/pyproject.toml"))
        self.assertIsNone(self.server.safe_file("data/orders.csv"))
        self.assertIsNotNone(self.server.safe_file("seeknal/transforms/customer_ltv.yml"))

    def test_preview_reads_real_parquet(self):
        result = self.server.bridge("preview", "transform.customer_ltv")
        self.assertEqual(3, result["row_count"])
        self.assertIn("lifetime_value", result["columns"])

    def test_cancelled_run_stays_cancelled_and_clears_freshness(self):
        original_seeknal = self.server.SEEKNAL
        original_commands = self.server.COMMANDS
        try:
            self.server.SEEKNAL = Path(sys.executable)
            self.server.COMMANDS = {
                "run": ["-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('started', flush=True); time.sleep(20)"]
            }
            store = self.server.JobStore()
            job = store.create("run", None)
            deadline = time.time() + 5
            while time.time() < deadline and store.get(job["id"])["status"] != "running":
                time.sleep(0.02)
            started = time.time()
            cancelled = store.cancel(job["id"])
            self.assertLess(time.time() - started, 4)
            self.assertEqual("cancelled", cancelled["status"])
            deadline = time.time() + 5
            while time.time() < deadline and store.get(job["id"])["exit_code"] is None:
                time.sleep(0.02)
            self.assertEqual("cancelled", store.get(job["id"])["status"])
            self.assertIsNone(store.last_successful_fingerprint)
        finally:
            self.server.SEEKNAL = original_seeknal
            self.server.COMMANDS = original_commands

    def test_gateway_proxy_uses_server_side_bearer_token(self):
        observed = {}

        class StubHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                observed["authorization"] = self.headers.get("Authorization")
                length = int(self.headers.get("Content-Length", "0"))
                observed["payload"] = json.loads(self.rfile.read(length))
                body = json.dumps({"answer": "stubbed", "events": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_arguments):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        import threading
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        old_url = os.environ.get("SEEKNAL_GATEWAY_URL")
        old_token = os.environ.get("SEEKNAL_GATEWAY_TOKEN")
        try:
            os.environ["SEEKNAL_GATEWAY_URL"] = f"http://127.0.0.1:{httpd.server_address[1]}"
            os.environ["SEEKNAL_GATEWAY_TOKEN"] = "test-only-token"
            result = self.server.gateway_request(
                "/ask", {"question": "draft a node", "session_id": "studio_test", "attachments": []}
            )
            self.assertEqual("stubbed", result["answer"])
            self.assertEqual("Bearer test-only-token", observed["authorization"])
            self.assertEqual("studio_test", observed["payload"]["session_id"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            if old_url is None:
                os.environ.pop("SEEKNAL_GATEWAY_URL", None)
            else:
                os.environ["SEEKNAL_GATEWAY_URL"] = old_url
            if old_token is None:
                os.environ.pop("SEEKNAL_GATEWAY_TOKEN", None)
            else:
                os.environ["SEEKNAL_GATEWAY_TOKEN"] = old_token

    def test_draft_jobs_bind_reviewed_content_and_destination(self):
        draft = self.server.PROJECT / ".seeknal" / "drafts" / "draft_transform_studio_test.yml"
        target = self.server.PROJECT / "seeknal" / "transforms" / "studio_test.yml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(
            "kind: transform\nname: studio_test\ntransform: |\n"
            "  SELECT order_id FROM input_0\ninputs:\n  - ref: source.orders\n"
        )
        store = self.server.JobStore()

        def wait(job_id):
            deadline = time.time() + 30
            while time.time() < deadline:
                job = store.get(job_id)
                if job["status"] not in {"queued", "running"}:
                    return job
                time.sleep(0.05)
            self.fail("draft job did not finish")

        try:
            details = self.server.draft_details(".seeknal/drafts/draft_transform_studio_test.yml")
            digest = details["content_hash"]
            rejected = wait(store.create(
                "apply-draft", path=details["path"], content_hash=digest,
                destination_hash=None, confirmed=False,
            )["id"])
            self.assertEqual("failed", rejected["status"])
            self.assertIn("confirmation", rejected["logs"])
            self.assertTrue(draft.is_file())
            self.assertFalse(target.exists())
            validated = wait(store.create("validate-draft", path=details["path"], content_hash=digest)["id"])
            self.assertEqual("succeeded", validated["status"], validated["logs"])
            applied = wait(store.create(
                "apply-draft", path=details["path"], content_hash=digest,
                destination_hash=None, confirmed=True,
            )["id"])
            self.assertEqual("succeeded", applied["status"], applied["logs"])
            self.assertTrue(target.is_file())
            self.assertFalse(draft.exists())
        finally:
            draft.unlink(missing_ok=True)
            target.unlink(missing_ok=True)

    def test_cancelled_run_marker_does_not_resurrect_after_restart(self):
        previous = self.server.STUDIO_STATE.read_bytes() if self.server.STUDIO_STATE.exists() else None
        original_seeknal = self.server.SEEKNAL
        original_commands = self.server.COMMANDS
        try:
            fingerprint = self.server.project_fingerprint()
            self.server.save_successful_fingerprint(fingerprint)
            store = self.server.JobStore()
            self.server.SEEKNAL = Path(sys.executable)
            self.server.COMMANDS = {"run": ["-c", "import time; time.sleep(20)"]}
            job = store.create("run", None)
            deadline = time.time() + 5
            while time.time() < deadline and store.get(job["id"])["status"] != "running":
                time.sleep(0.02)
            store.cancel(job["id"])
            self.assertIsNone(self.server.JobStore().last_successful_fingerprint)
            self.assertFalse(self.server.STUDIO_STATE.exists())
        finally:
            self.server.SEEKNAL = original_seeknal
            self.server.COMMANDS = original_commands
            if previous is None:
                self.server.STUDIO_STATE.unlink(missing_ok=True)
            else:
                self.server.STUDIO_STATE.parent.mkdir(parents=True, exist_ok=True)
                self.server.STUDIO_STATE.write_bytes(previous)


class BackendHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.project = Path(cls.tempdir.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project)
        cls.old_project = os.environ.get("SEEKNAL_STUDIO_PROJECT")
        os.environ["SEEKNAL_STUDIO_PROJECT"] = str(cls.project)
        cls.server = load_server()
        cls.server.JOBS.last_successful_fingerprint = cls.server.project_fingerprint()
        cls.httpd = cls.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server.HOSTS = {f"127.0.0.1:{cls.port}"}
        cls.server.ORIGINS = {f"http://127.0.0.1:{cls.port}"}
        import threading
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        if cls.old_project is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = cls.old_project
        cls.tempdir.cleanup()

    def request(self, path, *, method="GET", payload=None, origin=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Origin": origin or f"http://127.0.0.1:{self.port}"}
        if data:
            headers["Content-Type"] = "application/json"
        request = Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_state_and_wrong_origin(self):
        status, result = self.request("/api/state")
        self.assertEqual(200, status)
        self.assertEqual(6, len(result["files"]) - 1)
        status, _ = self.request("/api/state", origin="http://127.0.0.1:9999")
        self.assertEqual(403, status)

    def test_invalid_job_and_disconnected_agent(self):
        status, _ = self.request("/api/jobs", method="POST", payload={"action": "shell"})
        self.assertEqual(400, status)
        status, _ = self.request("/api/jobs", method="POST", payload={"action": ["run"]})
        self.assertEqual(400, status)
        status, _ = self.request("/api/jobs", method="POST", payload=["run"])
        self.assertEqual(400, status)
        status, result = self.request("/api/agent/status")
        self.assertEqual(200, status)
        self.assertFalse(result["configured"])

    def test_environment_busy_and_conflict_are_distinguishable(self):
        status, initial = self.request("/api/environment")
        self.assertEqual(200, status)
        payload = {
            "updates": {"WAREHOUSE_DSN": "must-not-be-returned"},
            "remove": [],
            "content_hash": initial["environment"]["content_hash"],
        }
        original_busy = self.server.ENVIRONMENT.busy
        self.server.ENVIRONMENT.busy = lambda: ["REPL query"]
        try:
            status, result = self.request("/api/environment", method="POST", payload=payload)
        finally:
            self.server.ENVIRONMENT.busy = original_busy
        self.assertEqual(409, status)
        self.assertEqual("environment_busy", result["error_code"])
        self.assertNotIn("must-not-be-returned", json.dumps(result))

        (self.project / ".env").write_text("EXTERNAL=changed\n", encoding="utf-8")
        status, result = self.request("/api/environment", method="POST", payload=payload)
        self.assertEqual(409, status)
        self.assertEqual("environment_conflict", result["error_code"])
        self.assertNotIn("must-not-be-returned", json.dumps(result))

    def test_save_is_rejected_while_execution_lock_is_held(self):
        path = "seeknal/transforms/customer_ltv.yml"
        _, current = self.request(f"/api/file?path={path}")
        self.server.JOBS.execution_lock.acquire()
        try:
            status, result = self.request(
                "/api/file", method="POST", payload={"path": path, "content": current["content"]}
            )
            self.assertEqual(409, status)
            self.assertIn("running", result["error"])
        finally:
            self.server.JOBS.execution_lock.release()

    def test_save_marks_preview_stale_without_losing_file(self):
        path = "seeknal/transforms/customer_ltv.yml"
        status, current = self.request(f"/api/file?path={path}")
        self.assertEqual(200, status)
        try:
            status, result = self.request("/api/file", method="POST", payload={"path": path, "content": current["content"] + "\n"})
            self.assertEqual(200, status)
            self.assertFalse(result["preview_fresh"])
            status, result = self.request("/api/preview?node=transform.customer_ltv")
            self.assertEqual(409, status)
            self.assertEqual([], result["rows"])
        finally:
            self.request("/api/file", method="POST", payload={"path": path, "content": current["content"]})
        status, result = self.request("/api/preview?node=transform.customer_ltv")
        self.assertEqual(200, status)
        self.assertTrue(result["fresh"])
        self.assertEqual(3, len(result["rows"]))


if __name__ == "__main__":
    unittest.main()
