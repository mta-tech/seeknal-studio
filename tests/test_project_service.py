from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import launch
from project_service import ProjectService, validate_project


class ProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.current = self.project("current")
        self.other = self.project("other")
        self.config = self.root / "private" / "projects.json"
        self.control = self.root / "private" / "switch.json"

    def tearDown(self):
        self.temporary.cleanup()

    def project(self, name: str) -> Path:
        project = self.root / name
        project.mkdir()
        (project / "seeknal_project.yml").write_text(f"name: {name}\n")
        return project.resolve()

    def service(self, busy=lambda: []):
        return ProjectService(
            self.current, busy=busy, config_path=self.config,
            control_path=self.control, control_nonce="test-nonce",
        )

    def test_validation_requires_real_directory_and_regular_project_file(self):
        self.assertEqual(self.other, validate_project(str(self.other)))
        with self.assertRaisesRegex(ValueError, "not a Seeknal project"):
            validate_project(str(self.root))
        linked = self.root / "linked"
        linked.symlink_to(self.other, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            validate_project(str(linked))

    def test_switch_request_is_private_and_persists_only_project_paths(self):
        service = self.service()
        selected = service.prepare_switch(str(self.other))
        service.queue_switch(selected)
        self.assertEqual(0, self.control.stat().st_mode & 0o077)
        self.assertEqual(0, self.config.stat().st_mode & 0o077)
        request = json.loads(self.control.read_text())
        self.assertEqual({"nonce": "test-nonce", "project": str(self.other)}, request)
        settings = json.loads(self.config.read_text())
        self.assertEqual(str(self.other), settings["last"])
        self.assertEqual([str(self.other), str(self.current)], settings["recent"])

    def test_settings_override_is_shared_by_persistence_and_launcher_recall(self):
        override = self.root / "isolated" / "projects.json"
        with patch.dict(os.environ, {"SEEKNAL_STUDIO_PROJECTS_FILE": str(override)}):
            service = ProjectService(
                self.current, busy=lambda: [],
                control_path=self.control, control_nonce="test-nonce",
            )
            service.queue_switch(service.prepare_switch(str(self.other)))
            self.assertEqual(override, service.config_path)
            self.assertEqual(self.other, launch.remembered_project())
        self.assertFalse(self.config.exists())

    def test_active_work_and_standalone_mode_fail_without_changing_project(self):
        busy = self.service(lambda: ["pipeline job", "REPL query"])
        with self.assertRaisesRegex(RuntimeError, "finish active work"):
            busy.prepare_switch(str(self.other))
        self.assertFalse(self.control.exists())
        standalone = ProjectService(self.current, busy=lambda: [], config_path=self.config)
        with self.assertRaisesRegex(RuntimeError, "managed desktop launcher"):
            standalone.prepare_switch(str(self.other))
        self.assertEqual(self.current, standalone.current)

    def test_cancelled_native_picker_keeps_current_project(self):
        service = ProjectService(self.current, busy=lambda: [], platform="darwin")
        process = Mock()
        process.communicate.return_value = ("", "execution error: User canceled. (-128)")
        process.returncode = 1
        process.poll.return_value = 1
        with patch("project_service.subprocess.Popen", return_value=process) as popen:
            result = service.browse()
        self.assertEqual({"path": None, "cancelled": True, "supported": True}, result)
        self.assertEqual(self.current, service.current)
        self.assertNotIn("System Events", popen.call_args.args[0])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_close_reaps_blocking_picker_and_its_descendant(self):
        pid_file = self.root / "picker-child.pid"
        script = (
            "import pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
        )
        service = ProjectService(self.current, busy=lambda: [], platform="darwin")
        errors = []

        def browse():
            try:
                service.browse()
            except RuntimeError as error:
                errors.append(str(error))

        with patch.object(
            service, "_picker_command",
            return_value=[sys.executable, "-c", script, str(pid_file)],
        ):
            thread = threading.Thread(target=browse)
            thread.start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not pid_file.is_file():
                time.sleep(0.02)
            self.assertTrue(pid_file.is_file())
            with service._picker_lock:
                picker_pid = service._picker.pid
            child_pid = int(pid_file.read_text())
            service.close()
            service.close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertEqual(self.current, service.current)

        def alive(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (alive(picker_pid) or alive(child_pid)):
            time.sleep(0.05)
        self.assertFalse(alive(picker_pid))
        self.assertFalse(alive(child_pid))

    def test_non_macos_picker_has_typed_path_fallback(self):
        service = ProjectService(self.current, busy=lambda: [], platform="linux")
        self.assertEqual(
            {"path": None, "cancelled": False, "supported": False}, service.browse()
        )


if __name__ == "__main__":
    unittest.main()
