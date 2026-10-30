from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from project_service import ProjectService


ROOT = Path(__file__).resolve().parents[1]


def load_server() -> Any:
    spec = importlib.util.spec_from_file_location("studio_workspace_server", ROOT / "server.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.current = self.project("current")
        self.other = self.project("other")
        self.third = self.project("third")
        self.config = self.root / "private" / "projects.json"
        self.control = self.root / "private" / "switch.json"

    def tearDown(self):
        self.temporary.cleanup()

    def project(self, name: str) -> Path:
        project = self.root / name
        project.mkdir()
        (project / "seeknal_project.yml").write_text(f"name: {name}\n")
        return project.resolve()

    def service(self, *, busy=lambda: [], managed: bool = True) -> ProjectService:
        return ProjectService(
            self.current,
            busy=busy,
            config_path=self.config,
            control_path=self.control if managed else None,
            control_nonce="workspace-nonce" if managed else None,
        )

    def settings(self) -> dict:
        return json.loads(self.config.read_text(encoding="utf-8"))

    def write_settings(self, value: object, mode: int = 0o600) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.config, mode)

    def test_create_rename_and_path_change_preserve_stable_id_and_other_fields(self):
        self.write_settings({"theme": "dark", "workspaces": []})
        service = self.service()
        created = service.save_workspace("  Analytics  ", str(self.current))
        workspace_id = created["id"]
        self.assertEqual("Analytics", created["name"])
        self.assertEqual(str(self.current), created["path"])
        self.assertFalse(created["can_switch"])

        renamed = service.save_workspace("Revenue", str(self.other), workspace_id)
        self.assertEqual(workspace_id, renamed["id"])
        self.assertEqual("Revenue", renamed["name"])
        self.assertEqual(str(self.other), renamed["path"])
        self.assertTrue(renamed["can_switch"])
        self.assertEqual("dark", self.settings()["theme"])
        self.assertEqual([workspace_id], [item["id"] for item in self.settings()["workspaces"]])

    def test_duplicate_canonical_project_and_invalid_names_are_rejected(self):
        service = self.service()
        service.save_workspace("Other", str(self.other))
        with self.assertRaisesRegex(ValueError, "already saved"):
            service.save_workspace("Duplicate", str(self.other / "."))
        for name in ("", "   ", "bad\nname", "x" * 101):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "workspace name"):
                    service.save_workspace(name, str(self.third))

    def test_missing_saved_project_remains_listed_and_can_be_repaired(self):
        service = self.service()
        created = service.save_workspace("Other", str(self.other))
        shutil.rmtree(self.other)
        entry = service.workspaces_status()["entries"][0]
        self.assertEqual(created["id"], entry["id"])
        self.assertFalse(entry["available"])
        self.assertFalse(entry["can_switch"])
        self.assertIn("does not exist", entry["reason"])

        repaired = service.save_workspace("Other repaired", str(self.third), created["id"])
        self.assertTrue(repaired["available"])
        self.assertEqual(str(self.third), repaired["path"])

    def test_current_fallback_and_current_workspace_match_use_canonical_path(self):
        service = self.service()
        fallback = service.workspaces_status()["current"]
        self.assertIsNone(fallback["id"])
        self.assertEqual(str(self.current), fallback["path"])
        saved = service.save_workspace("Current", str(self.current))
        current = service.workspaces_status()["current"]
        self.assertEqual(saved["id"], current["id"])
        self.assertEqual("Current", current["name"])

    def test_remove_forgets_only_metadata_and_never_deletes_project(self):
        service = self.service()
        saved = service.save_workspace("Other", str(self.other))
        self.assertEqual(
            {"removed": True, "id": saved["id"]}, service.remove_workspace(saved["id"])
        )
        self.assertTrue(self.other.is_dir())
        self.assertTrue((self.other / "seeknal_project.yml").is_file())
        self.assertEqual([], service.workspaces_status()["entries"])
        with self.assertRaisesRegex(ValueError, "workspace not found"):
            service.save_workspace("Stale edit", str(self.other), saved["id"])
        self.assertEqual([], service.workspaces_status()["entries"])

    def test_open_revalidates_saved_path_and_obeys_busy_and_managed_guards(self):
        service = self.service()
        saved = service.save_workspace("Other", str(self.other))
        busy = self.service(busy=lambda: ["pipeline job"])
        with self.assertRaisesRegex(RuntimeError, "finish active work"):
            busy.prepare_workspace_switch(saved["id"])
        standalone = self.service(managed=False)
        with self.assertRaisesRegex(RuntimeError, "managed desktop launcher"):
            standalone.prepare_workspace_switch(saved["id"])
        shutil.rmtree(self.other)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            service.prepare_workspace_switch(saved["id"])
        self.assertFalse(self.control.exists())

    def test_open_uses_control_nonce_and_preserves_workspaces_and_recent_limit(self):
        older = [self.project(f"older-{index}") for index in range(10)]
        self.write_settings({
            "future": {"preserve": True},
            "recent": [str(path) for path in older],
            "workspaces": [{"id": "other", "name": "Other", "path": str(self.other)}],
        })
        service = self.service()
        project, workspace = service.prepare_workspace_switch("other")
        service.queue_switch(project)
        self.assertEqual("other", workspace["id"])
        self.assertEqual(
            {"nonce": "workspace-nonce", "project": str(self.other)},
            json.loads(self.control.read_text(encoding="utf-8")),
        )
        settings = self.settings()
        self.assertEqual({"preserve": True}, settings["future"])
        self.assertEqual("other", settings["workspaces"][0]["id"])
        self.assertEqual(8, len(settings["recent"]))
        self.assertEqual([str(self.other), str(self.current)], settings["recent"][:2])

    def test_failed_control_write_does_not_reserve_switch(self):
        service = self.service()
        project = service.prepare_switch(str(self.other))
        original = service._atomic_json

        def fail_control(path: Path, value: dict, *, mode: int) -> None:
            if path == self.control:
                raise OSError("control write failed")
            original(path, value, mode=mode)

        with patch.object(service, "_atomic_json", side_effect=fail_control):
            with self.assertRaisesRegex(OSError, "control write failed"):
                service.queue_switch(project)
        service.queue_switch(project)
        self.assertEqual(str(self.other), json.loads(self.control.read_text())["project"])

    def test_concurrent_services_do_not_lose_workspace_updates(self):
        first = self.service()
        second = self.service()
        barrier = threading.Barrier(3)
        errors: list[BaseException] = []

        def save(service: ProjectService, name: str, path: Path) -> None:
            try:
                barrier.wait()
                service.save_workspace(name, str(path))
            except BaseException as error:
                errors.append(error)

        threads = [
            threading.Thread(target=save, args=(first, "Other", self.other)),
            threading.Thread(target=save, args=(second, "Third", self.third)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(
            {str(self.other), str(self.third)},
            {item["path"] for item in self.settings()["workspaces"]},
        )

    def test_mutations_refuse_unsafe_or_malformed_existing_configuration(self):
        service = self.service()
        self.write_settings({"workspaces": []}, mode=0o644)
        with self.assertRaisesRegex(RuntimeError, "owner-only"):
            service.save_workspace("Other", str(self.other))
        self.assertEqual(0o644, self.config.stat().st_mode & 0o777)

        self.write_settings({"workspaces": "wrong"})
        before = self.config.read_text(encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "must be a list"):
            service.save_workspace("Other", str(self.other))
        self.assertEqual(before, self.config.read_text(encoding="utf-8"))


class WorkspaceRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.project = cls.root / "current"
        cls.project.mkdir()
        (cls.project / "seeknal_project.yml").write_text("name: current\n")
        cls.other = cls.root / "other"
        cls.other.mkdir()
        (cls.other / "seeknal_project.yml").write_text("name: other\n")
        cls.config = cls.root / "projects.json"
        cls.control = cls.root / "switch.json"
        cls.previous_project = os.environ.get("SEEKNAL_STUDIO_PROJECT")
        os.environ["SEEKNAL_STUDIO_PROJECT"] = str(cls.project)
        cls.server = load_server()
        cls.httpd = cls.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server.HOSTS = {f"127.0.0.1:{cls.port}"}
        cls.server.ORIGINS = {f"http://127.0.0.1:{cls.port}"}
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server.REPL.close()
        if cls.previous_project is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = cls.previous_project
        cls.temporary.cleanup()

    def setUp(self):
        self.config.unlink(missing_ok=True)
        self.control.unlink(missing_ok=True)
        if not self.other.exists():
            self.other.mkdir()
            (self.other / "seeknal_project.yml").write_text("name: other\n")
        self.server.PROJECTS = ProjectService(
            self.project,
            busy=lambda: [],
            config_path=self.config,
            control_path=self.control,
            control_nonce="route-nonce",
        )

    def request(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Origin": f"http://127.0.0.1:{self.port}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers=headers,
            method="POST" if payload is not None else "GET",
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_crud_routes_and_id_open_contract(self):
        status, initial = self.request("/api/workspaces")
        self.assertEqual(200, status)
        self.assertTrue(initial["managed"])
        self.assertEqual(str(self.project.resolve()), initial["current"]["path"])
        self.assertIsNone(initial["current"]["id"])

        status, created = self.request(
            "/api/workspaces", {"name": "Other", "path": str(self.other)}
        )
        self.assertEqual(200, status)
        workspace = created["workspace"]
        self.assertTrue(workspace["can_switch"])
        self.assertRegex(workspace["id"], r"^[a-f0-9]{32}$")

        status, renamed = self.request(
            "/api/workspaces",
            {"id": workspace["id"], "name": "Renamed", "path": str(self.other)},
        )
        self.assertEqual(200, status)
        self.assertEqual("Renamed", renamed["workspace"]["name"])

        status, opened = self.request("/api/workspaces/open", {"id": workspace["id"]})
        self.assertEqual(202, status)
        self.assertTrue(opened["switching"])
        self.assertEqual(workspace["id"], opened["workspace"]["id"])
        self.assertEqual(
            {"nonce": "route-nonce", "project": str(self.other.resolve())},
            json.loads(self.control.read_text(encoding="utf-8")),
        )

        status, removed = self.request("/api/workspaces/remove", {"id": workspace["id"]})
        self.assertEqual(200, status)
        self.assertEqual({"removed": True, "id": workspace["id"]}, removed)
        self.assertTrue(self.other.is_dir())
        status, stale = self.request(
            "/api/workspaces",
            {"id": workspace["id"], "name": "Stale", "path": str(self.other)},
        )
        self.assertEqual(400, status)
        self.assertIn("workspace not found", stale["error"])
        self.assertEqual([], self.request("/api/workspaces")[1]["entries"])

    def test_unavailable_busy_and_standalone_open_statuses(self):
        _, created = self.request(
            "/api/workspaces", {"name": "Other", "path": str(self.other)}
        )
        workspace_id = created["workspace"]["id"]
        shutil.rmtree(self.other)
        status, listing = self.request("/api/workspaces")
        self.assertEqual(200, status)
        self.assertFalse(listing["entries"][0]["available"])
        status, unavailable = self.request("/api/workspaces/open", {"id": workspace_id})
        self.assertEqual(400, status)
        self.assertIn("does not exist", unavailable["error"])

        self.other.mkdir()
        (self.other / "seeknal_project.yml").write_text("name: other\n")
        self.server.PROJECTS = ProjectService(
            self.project,
            busy=lambda: ["pipeline job"],
            config_path=self.config,
            control_path=self.control,
            control_nonce="route-nonce",
        )
        status, busy = self.request("/api/workspaces/open", {"id": workspace_id})
        self.assertEqual(409, status)
        self.assertIn("active work", busy["error"])

        self.server.PROJECTS = ProjectService(
            self.project, busy=lambda: [], config_path=self.config
        )
        status, standalone = self.request("/api/workspaces/open", {"id": workspace_id})
        self.assertEqual(409, status)
        self.assertIn("managed desktop launcher", standalone["error"])

    def test_concurrent_workspace_and_legacy_open_admit_exactly_one_target(self):
        third = self.root / "third"
        third.mkdir(exist_ok=True)
        (third / "seeknal_project.yml").write_text("name: third\n")
        _, created = self.request(
            "/api/workspaces", {"name": "Other", "path": str(self.other)}
        )
        workspace_id = created["workspace"]["id"]
        barrier = threading.Barrier(3)
        results: list[tuple[str, int, dict]] = []

        def open_project(label: str, path: str, payload: dict) -> None:
            barrier.wait()
            status, body = self.request(path, payload)
            results.append((label, status, body))

        threads = [
            threading.Thread(
                target=open_project,
                args=("workspace", "/api/workspaces/open", {"id": workspace_id}),
            ),
            threading.Thread(
                target=open_project,
                args=("legacy", "/api/projects/open", {"path": str(third)}),
            ),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual([202, 409], sorted(status for _, status, _ in results))
        accepted = next(label for label, status, _ in results if status == 202)
        control = json.loads(self.control.read_text(encoding="utf-8"))
        expected = self.other.resolve() if accepted == "workspace" else third.resolve()
        self.assertEqual(str(expected), control["project"])
        rejected = next(body for _, status, body in results if status == 409)
        self.assertIn("already pending", rejected["error"])


if __name__ == "__main__":
    unittest.main()
