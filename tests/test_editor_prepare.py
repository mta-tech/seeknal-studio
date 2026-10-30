from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def load_server() -> Any:
    spec = importlib.util.spec_from_file_location("studio_editor_prepare_server", ROOT / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_engine_bridge() -> Any:
    spec = importlib.util.spec_from_file_location(
        "studio_editor_prepare_engine_bridge", ROOT / "engine_bridge.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EngineBridgePathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name).resolve()
        (self.project / ".seeknal" / "drafts").mkdir(parents=True)
        (self.project / "seeknal" / "transforms").mkdir(parents=True)
        self.engine = load_engine_bridge()

    def tearDown(self):
        self.temporary.cleanup()

    def test_draft_details_rejects_lexical_draft_symlink(self):
        real = self.project / ".seeknal" / "drafts" / "real.yml"
        real.write_text("kind: transform\nname: real\ntransform: SELECT 1\n")
        linked = self.project / ".seeknal" / "drafts" / "linked.yml"
        linked.symlink_to(real)

        with self.assertRaisesRegex(ValueError, "symlink"):
            self.engine.draft_details(
                self.project, linked.relative_to(self.project).as_posix(),
            )

    def test_draft_details_rejects_lexical_destination_symlink(self):
        draft = self.project / ".seeknal" / "drafts" / "destination.yml"
        draft.write_text("kind: transform\nname: destination\ntransform: SELECT 1\n")
        real = self.project / "seeknal" / "transforms" / "real.yml"
        real.write_text("kind: transform\nname: real\ntransform: SELECT 1\n")
        destination = self.project / "seeknal" / "transforms" / "destination.yml"
        destination.symlink_to(real)

        with self.assertRaisesRegex(ValueError, "symlink"):
            self.engine.draft_details(
                self.project, draft.relative_to(self.project).as_posix(),
            )

    def test_editor_details_rejects_lexical_source_symlink(self):
        real = self.project / "seeknal" / "transforms" / "real.yml"
        content = "kind: transform\nname: linked\ntransform: SELECT 1\n"
        real.write_text(content)
        linked = self.project / "seeknal" / "transforms" / "linked.yml"
        linked.symlink_to(real)

        with self.assertRaisesRegex(ValueError, "symlink"):
            self.engine.editor_details(
                self.project, linked.relative_to(self.project).as_posix(), content,
            )

    def test_validation_fails_closed_without_captured_sdk_preview_result(self):
        draft = self.project / ".seeknal" / "drafts" / "missing_result.yml"
        draft.write_text("kind: transform\nname: missing_result\ntransform: SELECT 1\n")

        with patch("seeknal.workflow.dry_run.dry_run_yaml", return_value=None):
            result = self.engine.validate_draft(
                self.project, draft.relative_to(self.project).as_posix(),
            )

        self.assertFalse(result["validation_passed"])
        self.assertEqual("unavailable", result["preview_status"])
        self.assertIn("no trustworthy", result["validation_error"])

    def test_validation_fails_closed_on_empty_sdk_result_dict(self):
        draft = self.project / ".seeknal" / "drafts" / "empty_result.yml"
        draft.write_text("kind: transform\nname: empty_result\ntransform: SELECT 1\n")

        with patch("seeknal.workflow.dry_run.execute_preview", return_value={}):
            result = self.engine.validate_draft(
                self.project, draft.relative_to(self.project).as_posix(),
            )

        self.assertFalse(result["validation_passed"])
        self.assertEqual("unavailable", result["preview_status"])
        self.assertIn("unrecognized", result["validation_error"])

    def test_recognized_definition_only_sdk_result_remains_valid(self):
        import seeknal.workflow.dry_run as native_dry_run

        draft = self.project / ".seeknal" / "drafts" / "model_definition.yml"
        draft.write_text("kind: model\nname: model_definition\n")
        native_result = {"duration": 0.1, "row_count": 0, "model_info": True}

        def run_captured_preview(*_args: object, **_kwargs: object) -> None:
            native_dry_run.execute_preview({}, draft, 10, 30)

        with (
            patch("seeknal.workflow.dry_run.execute_preview", return_value=native_result),
            patch(
                "seeknal.workflow.dry_run.dry_run_yaml", side_effect=run_captured_preview,
            ),
        ):
            result = self.engine.validate_draft(
                self.project, draft.relative_to(self.project).as_posix(),
            )

        self.assertTrue(result["validation_passed"])
        self.assertFalse(result["preview_available"])
        self.assertEqual("definition_only", result["preview_status"])
        self.assertEqual("Definition checked; no data preview", result["validation_message"])


class EditorPrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = Path(cls.temporary.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project)
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
        cls.server.AGENT.close()
        cls.server.REPL.close()
        cls.server.PROJECTS.close()
        if cls.previous_project is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = cls.previous_project
        cls.temporary.cleanup()

    def request(self, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={
                "Origin": f"http://127.0.0.1:{self.port}",
                "Content-Type": "application/json",
            },
            method="POST" if data is not None else "GET",
        )
        try:
            with urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def wait_job(self, job_id: str) -> dict:
        deadline = time.time() + 30
        while time.time() < deadline:
            status, job = self.request(f"/api/jobs/{job_id}")
            self.assertEqual(200, status)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.05)
        self.fail("editor lifecycle job did not finish")

    def test_active_source_is_snapshotted_validated_and_only_changed_on_apply(self):
        relative = "seeknal/sources/customers.yml"
        active = self.project / relative
        original = active.read_text()
        edited = original.replace("Customer master data", "Prepared customer master data")
        try:
            status, prepared = self.request("/api/editor/prepare", {
                "path": relative, "content": edited, "original_content": original,
            })
            self.assertEqual(201, status, prepared)
            snapshot = prepared["snapshot"]
            self.assertEqual("active", snapshot["mode"])
            self.assertEqual(relative, snapshot["source_path"])
            self.assertEqual(relative, snapshot["destination"])
            self.assertEqual(original, active.read_text())
            self.assertEqual(edited, (self.project / snapshot["path"]).read_text())
            self.assertTrue(Path(snapshot["path"]).name.startswith(".editor-"))
            _, state = self.request("/api/state")
            _, drafts = self.request("/api/drafts")
            self.assertNotIn(snapshot["path"], {item["path"] for item in state["files"]})
            self.assertNotIn(snapshot["path"], {item["path"] for item in drafts["drafts"]})

            status, validation = self.request("/api/jobs", {
                "action": "validate-draft",
                "path": snapshot["path"],
                "content_hash": snapshot["content_hash"],
                "destination_hash": snapshot["destination_hash"],
            })
            self.assertEqual(202, status, validation)
            validation = self.wait_job(validation["id"])
            self.assertEqual("succeeded", validation["status"], validation["logs"])
            self.assertEqual(0, validation["native_exit_code"])
            self.assertEqual("available", validation["preview_status"])
            self.assertEqual(original, active.read_text())

            status, applying = self.request("/api/jobs", {
                "action": "apply-draft",
                "path": snapshot["path"],
                "content_hash": snapshot["content_hash"],
                "destination_hash": snapshot["destination_hash"],
                "confirmed": True,
            })
            self.assertEqual(202, status, applying)
            applied = self.wait_job(applying["id"])
            self.assertEqual("succeeded", applied["status"], applied["logs"])
            self.assertEqual(edited, active.read_text())
            self.assertFalse((self.project / snapshot["path"]).exists())
        finally:
            active.write_text(original)
            for path in (self.project / ".seeknal" / "drafts").glob("*customers*"):
                path.unlink(missing_ok=True)

    def test_dirty_managed_draft_is_atomically_updated_without_touching_active(self):
        draft = self.project / ".seeknal" / "drafts" / "draft_transform_editor_new.yml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        original = "kind: transform\nname: editor_new\ntransform: SELECT 1\n"
        edited = original.replace("SELECT 1", "SELECT 2")
        draft.write_text(original)
        try:
            status, prepared = self.request("/api/editor/prepare", {
                "path": draft.relative_to(self.project).as_posix(),
                "content": edited,
                "original_content": original,
            })
            self.assertEqual(200, status, prepared)
            snapshot = prepared["snapshot"]
            self.assertEqual("draft", snapshot["mode"])
            self.assertEqual(draft.relative_to(self.project).as_posix(), snapshot["path"])
            self.assertEqual(edited, draft.read_text())
            self.assertFalse((self.project / snapshot["destination"]).exists())
            self.assertIn("SELECT 2", prepared["diff"]["diff"])

            saved_mtime = draft.stat().st_mtime_ns
            status, unchanged = self.request("/api/editor/prepare", {
                "path": draft.relative_to(self.project).as_posix(),
                "content": edited,
                "original_content": edited,
            })
            self.assertEqual(200, status, unchanged)
            self.assertTrue(unchanged["unchanged"])
            self.assertEqual(snapshot["path"], unchanged["snapshot"]["path"])
            self.assertEqual(saved_mtime, draft.stat().st_mtime_ns)
        finally:
            draft.unlink(missing_ok=True)

    def test_invalid_transform_preview_fails_closed_despite_native_zero_exit(self):
        draft = self.project / ".seeknal" / "drafts" / "draft_transform_invalid_preview.yml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(
            "kind: transform\n"
            "name: invalid_preview\n"
            "transform: |\n"
            "  SELECT SUM(o.no_such_amount) FROM input_0 AS o\n"
            "inputs:\n"
            "  - ref: source.orders\n"
        )
        try:
            relative = draft.relative_to(self.project).as_posix()
            digest = self.server.file_hash(draft)
            store = self.server.JobStore()
            store.validated_drafts.add((relative, digest))
            job = store.create("validate-draft", path=relative, content_hash=digest)
            deadline = time.time() + 30
            while time.time() < deadline:
                result = store.get(job["id"])
                if result["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            else:
                self.fail("invalid transform validation did not finish")
            self.assertEqual("failed", result["status"], result["logs"])
            self.assertEqual(0, result["native_exit_code"])
            self.assertEqual(1, result["exit_code"])
            self.assertEqual("unavailable", result["preview_status"])
            self.assertIn("could not execute", result["validation_error"].lower())
            self.assertIn("no_such_amount", result["logs"])
            self.assertNotIn((relative, digest), store.validated_drafts)
        finally:
            draft.unlink(missing_ok=True)

    def test_native_warning_text_inside_valid_sql_does_not_fail_validation(self):
        draft = self.project / ".seeknal" / "drafts" / "draft_transform_warning_literal.yml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(
            "kind: transform\n"
            "name: warning_literal\n"
            "transform: |\n"
            "  SELECT '[WARN] Could not execute transform: literal only' AS message\n"
        )
        try:
            relative = draft.relative_to(self.project).as_posix()
            digest = self.server.file_hash(draft)
            store = self.server.JobStore()
            job = store.create("validate-draft", path=relative, content_hash=digest)
            deadline = time.time() + 30
            while time.time() < deadline:
                result = store.get(job["id"])
                if result["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            else:
                self.fail("valid warning-literal transform validation did not finish")
            self.assertEqual("succeeded", result["status"], result["logs"])
            self.assertEqual(0, result["native_exit_code"])
            self.assertEqual("available", result["preview_status"])
            self.assertIn((relative, digest), store.validated_drafts)
        finally:
            draft.unlink(missing_ok=True)

    def test_source_schema_only_fallback_remains_valid(self):
        draft = self.project / ".seeknal" / "drafts" / "draft_source_schema_only.yml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(
            "kind: source\n"
            "name: schema_only\n"
            "source: csv\n"
            "table: data/does-not-exist.csv\n"
            "columns:\n"
            "  customer_id:\n"
            "    dtype: string\n"
        )
        try:
            relative = draft.relative_to(self.project).as_posix()
            digest = self.server.file_hash(draft)
            store = self.server.JobStore()
            job = store.create("validate-draft", path=relative, content_hash=digest)
            deadline = time.time() + 30
            while time.time() < deadline:
                result = store.get(job["id"])
                if result["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.05)
            else:
                self.fail("source schema-only validation did not finish")
            self.assertEqual("succeeded", result["status"], result["logs"])
            self.assertEqual(0, result["native_exit_code"])
            self.assertEqual("schema_only", result["preview_status"])
            self.assertEqual(
                "Definition checked; no data preview", result["validation_message"],
            )
            self.assertIn("Schema (no data available", result["logs"])
            self.assertIn((relative, digest), store.validated_drafts)
        finally:
            draft.unlink(missing_ok=True)

    def test_wrong_base_is_conflict_and_preserves_newer_disk_content(self):
        relative = "seeknal/transforms/customer_ltv.yml"
        target = self.project / relative
        stale = target.read_text()
        newer = stale + "\n# newer disk edit\n"
        target.write_text(newer)
        try:
            status, response = self.request("/api/editor/prepare", {
                "path": relative,
                "content": stale.replace("Customer lifetime", "Changed lifetime"),
                "original_content": stale,
            })
            self.assertEqual(409, status, response)
            self.assertEqual("editor_conflict", response["error_code"])
            self.assertEqual(newer, target.read_text())
        finally:
            target.write_text(stale)

    def test_invalid_yaml_and_identity_rename_are_rejected_without_snapshot(self):
        relative = "seeknal/transforms/customer_ltv.yml"
        active = self.project / relative
        original = active.read_text()
        drafts = self.project / ".seeknal" / "drafts"
        before = {path.name for path in drafts.glob("*")}

        status, invalid = self.request("/api/editor/prepare", {
            "path": relative,
            "content": "kind: transform\nname: [broken\n",
            "original_content": original,
        })
        self.assertEqual(400, status, invalid)
        self.assertIn("invalid", invalid["error"].lower())

        renamed = original.replace("name: customer_ltv", "name: renamed_ltv")
        status, rejected = self.request("/api/editor/prepare", {
            "path": relative, "content": renamed, "original_content": original,
        })
        self.assertEqual(400, status, rejected)
        self.assertIn("rename", rejected["error"].lower())
        self.assertEqual(original, active.read_text())
        self.assertEqual(before, {path.name for path in drafts.glob("*")})

    def test_unchanged_active_file_is_a_no_action_and_file_get_has_hash(self):
        relative = "seeknal/transforms/revenue_daily.yml"
        content = (self.project / relative).read_text()
        drafts = self.project / ".seeknal" / "drafts"
        before = {path.name for path in drafts.glob("*")}
        status, prepared = self.request("/api/editor/prepare", {
            "path": relative, "content": content, "original_content": content,
        })
        self.assertEqual(200, status, prepared)
        self.assertEqual({
            "unchanged": True, "mode": "active", "source_path": relative,
        }, prepared)
        self.assertEqual(before, {path.name for path in drafts.glob("*")})

        status, fetched = self.request(f"/api/file?path={relative}")
        self.assertEqual(200, status)
        self.assertEqual(self.server.file_hash(self.project / relative), fetched["content_hash"])


if __name__ == "__main__":
    unittest.main()
