from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from project_lock import NO_RECORD, ProjectBusyError, ProjectLock, describe, sanitize


ROOT = Path(__file__).resolve().parents[1]
HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from project_lock import ProjectLock
lease = ProjectLock(Path(sys.argv[2])).acquire(sys.argv[3])
print("held", flush=True)
time.sleep(120)
"""
PROBE = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from project_lock import ProjectBusyError, ProjectLock
try:
    ProjectLock(Path(sys.argv[2])).acquire("probe").release()
    print("acquired")
except ProjectBusyError as error:
    print("busy: " + str(error))
"""


def hold(project: Path, action: str = "hold-run") -> subprocess.Popen:
    """A second process, as another Studio backend would be, holding the project lock."""
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(ROOT), str(project), action],
        stdout=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    if process.stdout.readline().strip() != "held":
        process.kill()
        raise AssertionError("holder process did not take the lock")
    return process


def probe(project: Path) -> str:
    return subprocess.run(
        [sys.executable, "-c", PROBE, str(ROOT), str(project)], capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def stop(process: subprocess.Popen, sig: int = signal.SIGKILL) -> None:
    if process.poll() is None:
        process.send_signal(sig)
    process.wait(timeout=10)
    if process.stdout is not None:
        process.stdout.close()


class ProjectLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.project.mkdir()
        self.lock_path = self.project / ".seeknal" / "studio.lock"

    def tearDown(self):
        self.temporary.cleanup()

    def test_second_process_is_refused_with_the_holder_named_until_the_holder_dies(self):
        holder = hold(self.project, "run")
        try:
            with self.assertRaises(ProjectBusyError) as raised:
                ProjectLock(self.project).acquire("save")
            message = str(raised.exception)
            self.assertIn("Another Studio is running 'run' in this project", message)
            self.assertIn(f"pid {holder.pid}", message)
            self.assertIn(" on ", message)
            self.assertIn("since ", message)
            self.assertIn("Try again when it finishes.", message)
            self.assertEqual(holder.pid, raised.exception.holder["pid"])
            # The waiter's attempt never truncates the holder's record.
            self.assertEqual("run", json.loads(self.lock_path.read_text().splitlines()[0])["action"])
        finally:
            stop(holder)
        # The OS releases flock when the holder is killed.
        with ProjectLock(self.project).acquire("save"):
            self.assertTrue(probe(self.project).startswith("busy: Another Studio is running 'save'"))
        self.assertEqual("acquired", probe(self.project))

    def test_one_process_shares_the_lock_between_overlapping_operations(self):
        lock = ProjectLock(self.project)
        first = lock.acquire("run")
        second = lock.acquire("save")
        first.release()
        first.release()  # idempotent
        self.assertTrue(lock.held())
        self.assertTrue(probe(self.project).startswith("busy:"))
        second.release()
        self.assertFalse(lock.held())
        self.assertEqual("acquired", probe(self.project))

    def test_symlinked_lock_file_is_refused_and_its_target_untouched(self):
        outside = Path(self.temporary.name) / "victim.txt"
        outside.write_text("keep me\n")
        self.lock_path.parent.mkdir()
        self.lock_path.symlink_to(outside)
        with self.assertRaisesRegex(ProjectBusyError, "could not open the project lock"):
            ProjectLock(self.project).acquire("run")
        self.assertEqual("keep me\n", outside.read_text())

    def test_symlinked_internal_directory_is_refused(self):
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".seeknal").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ProjectBusyError):
            ProjectLock(self.project).acquire("run")
        self.assertEqual([], list(elsewhere.iterdir()))

    def test_hard_link_and_fifo_are_refused(self):
        self.lock_path.parent.mkdir()
        victim = Path(self.temporary.name) / "victim.txt"
        victim.write_text("keep me\n")
        os.link(victim, self.lock_path)
        with self.assertRaisesRegex(ProjectBusyError, "not a regular file"):
            ProjectLock(self.project).acquire("run")
        self.assertEqual("keep me\n", victim.read_text())
        self.lock_path.unlink()
        os.mkfifo(self.lock_path)
        with self.assertRaisesRegex(ProjectBusyError, "not a regular file"):
            ProjectLock(self.project).acquire("run")

    def run_with_umask(self, project: Path, umask: str) -> None:
        script = (
            "import os, sys; sys.path.insert(0, sys.argv[1]); os.umask(int(sys.argv[3], 8));"
            "from pathlib import Path; from project_lock import ProjectLock;"
            "ProjectLock(Path(sys.argv[2])).acquire('run').release()"
        )
        subprocess.run([sys.executable, "-c", script, str(ROOT), str(project), umask], check=True)

    def test_new_lock_file_is_never_open_to_other_accounts(self):
        for umask, directory_mode, file_mode in (("002", 0o770, 0o660), ("022", 0o750, 0o640)):
            with self.subTest(umask=umask):
                project = Path(self.temporary.name) / f"umask-{umask}"
                project.mkdir()
                self.run_with_umask(project, umask)
                self.assertEqual(directory_mode, stat.S_IMODE((project / ".seeknal").stat().st_mode))
                self.assertEqual(file_mode, stat.S_IMODE((project / ".seeknal" / "studio.lock").stat().st_mode))

    def test_existing_lock_file_loses_other_permissions_but_keeps_group(self):
        for umask, before, after in (("022", 0o644, 0o640), ("002", 0o666, 0o660), ("002", 0o604, 0o600)):
            with self.subTest(umask=umask, before=oct(before)):
                project = Path(self.temporary.name) / f"existing-{umask}-{before:o}"
                (project / ".seeknal").mkdir(parents=True)
                lock = project / ".seeknal" / "studio.lock"
                lock.write_text("")
                lock.chmod(before)
                self.run_with_umask(project, umask)
                self.assertEqual(after, stat.S_IMODE(lock.stat().st_mode))

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_read_only_lock_file_still_excludes_other_processes(self):
        ProjectLock(self.project).acquire("run").release()
        self.lock_path.chmod(0o440)
        with ProjectLock(self.project).acquire("run"):
            self.assertEqual("busy: " + NO_RECORD, probe(self.project))

    def test_held_lock_without_a_record_is_explained(self):
        holder = hold(self.project, "run")
        try:
            self.lock_path.write_text("")
            with self.assertRaises(ProjectBusyError) as raised:
                ProjectLock(self.project).acquire("save")
            self.assertEqual(NO_RECORD, str(raised.exception))
            self.assertIn("released when that process exits", str(raised.exception))
            self.assertEqual({}, raised.exception.holder)
        finally:
            stop(holder)

    def test_hostile_holder_records_are_sanitized_not_fatal(self):
        records = {
            "huge time": b'{"started_at": 1e300, "pid": 7, "action": "run"}',
            "infinite time": b'{"started_at": Infinity, "pid": 7}',
            "nan time": b'{"started_at": NaN, "user": "u", "host": "h"}',
            "huge int time": b'{"started_at": 1000000000000000000000000000000}',
            "negative huge": b'{"started_at": -1e400, "pid": -5}',
            "ansi and newlines": json.dumps({
                "user": "\u001b[31mevil\u001b[0m", "host": "a\nb\rc\u0007", "action": "run\u001b]0;title\u0007",
                "pid": True,
            }).encode(),
            "long fields": json.dumps({"user": "u" * 500, "host": "h" * 500, "action": "x" * 500, "pid": 2**40}).encode(),
            "over the read limit": json.dumps({"user": "u" * 10_000}).encode(),
            "deep nesting": b"[" * 4000,
            "wrong types": b'{"user": ["x"], "host": {"a": 1}, "pid": "12", "started_at": "now", "action": 5}',
            "not an object": b'"just a string"',
            "invalid utf8": b'{"user": "' + bytes([0xff, 0xfe]) + b'"}',
        }
        holder = hold(self.project, "run")
        try:
            for label, record in records.items():
                with self.subTest(record=label):
                    self.lock_path.write_bytes(record + b"\n")
                    with self.assertRaises(ProjectBusyError) as raised:
                        ProjectLock(self.project).acquire("save")
                    message = str(raised.exception)
                    self.assertTrue(message.isprintable(), repr(message))
                    self.assertLessEqual(len(message), 400)
                    self.assertTrue(message.startswith("Another Studio is") or message == NO_RECORD, message)
                    for key, value in raised.exception.holder.items():
                        self.assertIn(key, {"user", "host", "action", "pid", "since"})
                        if isinstance(value, str):
                            self.assertTrue(value.isprintable() and len(value) <= 64, repr(value))
        finally:
            stop(holder)
        self.assertEqual({"pid": 7, "action": "run"}, sanitize({"started_at": float("inf"), "pid": 7, "action": "run"}))
        self.assertEqual("Another Studio is working in this project (pid 7). Try again when it finishes.", describe({"pid": 7}))


def load_server(project: Path):
    with patch.dict(os.environ, {
        "SEEKNAL_STUDIO_PROJECT": str(project),
        "SEEKNAL_STUDIO_PROJECTS_FILE": str(project.parent / "projects.json"),
    }):
        spec = importlib.util.spec_from_file_location("studio_project_lock_server", ROOT / "server.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
    return module


class ServerProjectLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.project = Path(cls.tempdir.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project, ignore=shutil.ignore_patterns("target", ".seeknal"))
        cls.server = load_server(cls.project)
        cls.httpd = cls.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server.HOSTS = {f"127.0.0.1:{cls.port}"}
        cls.server.ORIGINS = {f"http://127.0.0.1:{cls.port}"}
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server.PROJECTS.close()
        cls.tempdir.cleanup()

    def setUp(self):
        self.commands = patch.multiple(self.server, SEEKNAL=Path(sys.executable), COMMANDS={
            "run": ["-c", "import sys, time; time.sleep(float(sys.argv[1]) if len(sys.argv) > 1 else 0)"],
            "validate": ["-c", "raise SystemExit(3)"],
            "plan": ["-c", "import time; time.sleep(30)"],
            "lineage": ["-c", "pass"],
        })
        self.commands.start()
        self.addCleanup(self.commands.stop)

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST" if data is not None else "GET",
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def wait_job(self, job_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = self.server.JOBS.get(job_id)
            if job["status"] in {"succeeded", "failed", "cancelled"} and not self.server.PROJECT_LOCK.held():
                return job
            time.sleep(0.05)
        raise AssertionError(f"job {job_id} did not finish: {self.server.JOBS.get(job_id)}")

    def assert_busy(self, status, payload, action="hold-run"):
        self.assertEqual(409, status, payload)
        self.assertEqual("project_busy", payload["error_code"])
        self.assertLessEqual(set(payload["holder"]), {"user", "host", "action", "pid", "since"})
        self.assertIn(f"Another Studio is running '{action}' in this project", payload["error"])
        self.assertIn("pid ", payload["error"])

    def test_jobs_saves_and_draft_writes_are_refused_while_another_process_holds_the_project(self):
        path = "seeknal/transforms/customer_ltv.yml"
        target = self.project / path
        original = target.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        environment_hash = self.request("/api/environment")[1]["environment"]["content_hash"]
        drafts_before = sorted((self.project / ".seeknal").rglob("draft*"))
        jobs_before = len(self.server.JOBS.list())
        holder = hold(self.project)
        try:
            for action in ("run", "validate", "plan", "lineage"):
                with self.subTest(action=action):
                    self.assert_busy(*self.request("/api/jobs", {"action": action}))
            self.assert_busy(*self.request("/api/jobs", {"action": "draft", "node_type": "transform", "name": "blocked_draft"}))
            self.assert_busy(*self.request("/api/file", {"path": path, "content": "changed\n", "base_hash": digest}))
            content = original.decode()
            self.assert_busy(*self.request("/api/editor/prepare", {
                "path": path, "content": content + "\n# edit\n", "original_content": content,
            }))
            self.assert_busy(*self.request("/api/drafts/from-editor", {"path": path, "content": content}))
            self.assert_busy(*self.request("/api/environment", {
                "updates": {"STUDIO_LOCK_TEST": "x"}, "remove": [], "content_hash": environment_hash,
            }))
            self.assertEqual(jobs_before, len(self.server.JOBS.list()))
            self.assertEqual(original, target.read_bytes())
            self.assertEqual(drafts_before, sorted((self.project / ".seeknal").rglob("draft*")))
            self.assertNotIn("STUDIO_LOCK_TEST", (self.project / ".env").read_text() if (self.project / ".env").exists() else "")
            # In-process work is free again: the refusal did not leak the execution lock.
            self.assertFalse(self.server.JOBS.execution_lock.locked())
        finally:
            stop(holder)
        status, saved = self.request("/api/file", {"path": path, "content": original.decode(), "base_hash": digest})
        self.assertEqual(200, status, saved)

    def test_apply_is_refused_while_another_process_holds_the_project(self):
        destination = self.project / "seeknal/transforms/customer_ltv.yml"
        drafts = self.project / ".seeknal" / "drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        draft = drafts / "draft_transform_customer_ltv.yml"
        draft.write_text(destination.read_text() + "\n# reviewed change\n")
        relative = draft.relative_to(self.project).as_posix()
        content_hash = hashlib.sha256(draft.read_bytes()).hexdigest()
        destination_before = destination.read_bytes()
        self.server.JOBS.validated_drafts.add((relative, content_hash))
        holder = hold(self.project, "apply-draft")
        try:
            self.assert_busy(*self.request("/api/jobs", {
                "action": "apply-draft", "path": relative, "content_hash": content_hash,
                "destination_hash": hashlib.sha256(destination_before).hexdigest(), "confirmed": True,
            }), action="apply-draft")
            self.assertEqual(destination_before, destination.read_bytes())
        finally:
            stop(holder)
            self.server.JOBS.validated_drafts.discard((relative, content_hash))
            draft.unlink()

    def test_lock_is_held_for_the_job_and_released_after_success_failure_and_cancel(self):
        with patch.dict(self.server.COMMANDS, {"run": ["-c", "import time; time.sleep(1.5)"]}):
            job = self.server.JOBS.create("run")
            deadline = time.monotonic() + 10
            while self.server.JOBS.get(job["id"])["status"] != "running" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(probe(self.project).startswith("busy: Another Studio is running 'run'"))
            self.assertEqual("succeeded", self.wait_job(job["id"])["status"])
        self.assertEqual("acquired", probe(self.project))

        self.assertEqual("failed", self.wait_job(self.server.JOBS.create("validate")["id"])["status"])
        self.assertEqual("acquired", probe(self.project))

        job = self.server.JOBS.create("plan")
        deadline = time.monotonic() + 10
        while self.server.JOBS.get(job["id"])["status"] != "running" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.server.JOBS.cancel(job["id"])
        self.assertEqual("cancelled", self.wait_job(job["id"])["status"])
        self.assertEqual("acquired", probe(self.project))

    def test_orphaned_job_process_keeps_the_lock_until_it_exits(self):
        # Simulates the backend dying mid-run: the job's own process still holds
        # the inherited lock descriptor, so another Studio cannot start.
        with patch.dict(self.server.COMMANDS, {"run": ["-c", "import time; time.sleep(1.5)"]}):
            lease = self.server.PROJECT_LOCK.acquire("run")
            try:
                process = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(1.5)"],
                    pass_fds=self.server.PROJECT_LOCK.inherited_fds(),
                )
            finally:
                lease.release()  # the backend's own descriptor is gone
            self.assertFalse(self.server.PROJECT_LOCK.held())
            self.assertTrue(probe(self.project).startswith("busy:"))
            process.wait(timeout=10)
        self.assertEqual("acquired", probe(self.project))

    def test_hostile_record_still_gets_a_409_with_only_sanitized_fields(self):
        holder = hold(self.project)
        try:
            (self.project / ".seeknal" / "studio.lock").write_bytes(
                b'{"started_at": 1e300, "pid": 99, "user": "\\u001b[2Jx", "host": "h", "action": "run", "extra": "secret"}\n'
            )
            status, payload = self.request("/api/jobs", {"action": "run"})
            self.assertEqual(409, status, payload)
            self.assertEqual({"user": "[2Jx", "host": "h", "action": "run", "pid": 99}, payload["holder"])
            self.assertIn("Another Studio is running 'run' in this project ([2Jx on h, pid 99)", payload["error"])
        finally:
            stop(holder)

    def test_lease_is_released_when_job_creation_fails_after_acquiring(self):
        jobs_before = len(self.server.JOBS.list())
        with patch.object(self.server.uuid, "uuid4", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.server.JOBS.create("run")
        self.assertFalse(self.server.PROJECT_LOCK.held())
        self.assertEqual(jobs_before, len(self.server.JOBS.list()))
        self.assertEqual("acquired", probe(self.project))

    def test_queued_jobs_in_one_backend_share_the_lock_until_the_last_ends(self):
        with patch.dict(self.server.COMMANDS, {"run": ["-c", "import time; time.sleep(1)"]}):
            first = self.server.JOBS.create("run")
            second = self.server.JOBS.create("run")
            deadline = time.monotonic() + 15
            while self.server.JOBS.get(first["id"])["status"] != "succeeded" and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(probe(self.project).startswith("busy:"))
            self.assertEqual("succeeded", self.wait_job(second["id"])["status"])
        self.assertEqual("acquired", probe(self.project))


if __name__ == "__main__":
    unittest.main()
