from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from project_service import ProjectService


ROOT = Path(__file__).resolve().parents[1]


def load_server():
    spec = importlib.util.spec_from_file_location("studio_sql_draft_server", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class SqlDraftRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = Path(cls.temporary.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project)
        cls.previous = os.environ.get("SEEKNAL_STUDIO_PROJECT")
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
        if cls.previous is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = cls.previous
        cls.temporary.cleanup()

    def post(self, path: str, payload: dict):
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def get(self, path: str):
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        with urlopen(request, timeout=20) as response:
            return response.status, json.load(response)

    def test_editor_content_becomes_unique_draft_without_overwriting_active_file(self):
        relative = "seeknal/transforms/customer_ltv.yml"
        active = self.project / relative
        original = active.read_text()
        status, first = self.post("/api/drafts/from-editor", {"path": relative, "content": original + "\n# draft\n"})
        status_two, second = self.post("/api/drafts/from-editor", {"path": relative, "content": original + "\n# draft two\n"})
        self.assertEqual((201, 201), (status, status_two))
        self.assertNotEqual(first["draft"]["path"], second["draft"]["path"])
        self.assertEqual(relative, first["destination"])
        self.assertEqual(original, active.read_text())
        self.assertTrue((self.project / first["draft"]["path"]).is_file())

    def test_editor_draft_rejects_content_for_a_different_active_file(self):
        relative = "seeknal/transforms/customer_ltv.yml"
        content = (self.project / relative).read_text().replace("name: customer_ltv", "name: other")
        status, result = self.post("/api/drafts/from-editor", {"path": relative, "content": content})
        self.assertEqual(400, status)
        self.assertIn("resolve", result["error"])

    def test_extract_returns_transform_sql_without_executing_it(self):
        relative = "seeknal/transforms/customer_ltv.yml"
        content = (self.project / relative).read_text()
        status, result = self.post("/api/repl/extract", {"path": relative, "content": content})
        self.assertEqual(200, status)
        self.assertIn("SELECT", result["sql"])
        self.assertIn('"transform_clean_orders"', result["repl_sql"])
        self.assertEqual(
            [{"from": "transform.clean_orders", "to": "transform_clean_orders"}],
            result["rewritten_refs"],
        )
        source_path = "seeknal/sources/orders.yml"
        status, result = self.post(
            "/api/repl/extract",
            {"path": source_path, "content": (self.project / source_path).read_text()},
        )
        self.assertEqual(400, status)
        self.assertIn("not a Seeknal transform", result["error"])

    def test_extract_ast_rewrites_only_declared_table_references(self):
        content = """kind: transform
name: customer_ltv
inputs:
  - ref: source.orders
transform: |
  SELECT 'source.orders' AS label
  FROM source.orders AS orders
  JOIN input_0 AS first_input ON first_input.order_id = orders.order_id
  JOIN external.audit AS audit ON audit.order_id = orders.order_id
"""
        status, result = self.post(
            "/api/repl/extract",
            {"path": "seeknal/transforms/customer_ltv.yml", "content": content},
        )
        self.assertEqual(200, status)
        self.assertIn("'source.orders'", result["repl_sql"])
        self.assertIn("external.audit", result["repl_sql"])
        self.assertNotIn("source.orders AS orders", result["repl_sql"])
        self.assertNotIn("input_0 AS first_input", result["repl_sql"])
        self.assertEqual(
            [
                {"from": "source.orders", "to": "source_orders"},
                {"from": "input_0", "to": "source_orders"},
            ],
            result["rewritten_refs"],
        )

    def test_extract_does_not_rewrite_cte_named_like_declared_input_alias(self):
        content = """kind: transform
name: customer_ltv
inputs:
  - ref: source.orders
transform: |
  WITH input_0 AS (SELECT 99 AS n)
  SELECT 'input_0' AS literal_name, n FROM input_0
"""
        status, result = self.post(
            "/api/repl/extract",
            {"path": "seeknal/transforms/customer_ltv.yml", "content": content},
        )
        self.assertEqual(200, status)
        self.assertEqual([], result["rewritten_refs"])
        self.assertIn("FROM input_0", result["repl_sql"])
        self.assertIn("'input_0'", result["repl_sql"])
        self.assertNotIn("source_orders", result["repl_sql"])

    def test_extract_rewrites_physical_source_inside_nested_alias_only(self):
        content = """kind: transform
name: customer_ltv
inputs:
  - ref: source.orders
transform: |
  SELECT nested.order_id
  FROM (SELECT order_id FROM source.orders) AS input_0
  JOIN (SELECT 1 AS order_id) AS nested ON nested.order_id = input_0.order_id
"""
        status, result = self.post(
            "/api/repl/extract",
            {"path": "seeknal/transforms/customer_ltv.yml", "content": content},
        )
        self.assertEqual(200, status)
        self.assertEqual(
            [{"from": "source.orders", "to": "source_orders"}],
            result["rewritten_refs"],
        )
        self.assertIn('FROM "source_orders"', result["repl_sql"])
        self.assertIn("AS input_0", result["repl_sql"])

    def test_extract_rewrites_unaliased_input_column_qualifier(self):
        content = """kind: transform
name: customer_ltv
inputs:
  - ref: source.orders
transform: SELECT input_0.order_id FROM input_0
"""
        status, result = self.post(
            "/api/repl/extract",
            {"path": "seeknal/transforms/customer_ltv.yml", "content": content},
        )
        self.assertEqual(200, status)
        self.assertIn('SELECT "source_orders".order_id FROM "source_orders"', result["repl_sql"])

    def test_extract_rewrites_unaliased_declared_ref_column_qualifier(self):
        content = """kind: transform
name: customer_ltv
inputs:
  - ref: source.orders
transform: SELECT source.orders.order_id FROM source.orders
"""
        status, result = self.post(
            "/api/repl/extract",
            {"path": "seeknal/transforms/customer_ltv.yml", "content": content},
        )
        self.assertEqual(200, status)
        self.assertIn('SELECT "source_orders".order_id FROM "source_orders"', result["repl_sql"])

    def test_editor_draft_rejects_seeknal_symlink_before_creating_outside_directory(self):
        temporary = tempfile.TemporaryDirectory()
        previous = os.environ.get("SEEKNAL_STUDIO_PROJECT")
        project = Path(temporary.name) / "project"
        outside = Path(temporary.name) / "outside"
        shutil.copytree(ROOT / "demo", project)
        internal = project / ".seeknal"
        if internal.exists():
            shutil.rmtree(internal)
        internal.symlink_to(outside, target_is_directory=True)
        os.environ["SEEKNAL_STUDIO_PROJECT"] = str(project)
        module = load_server()
        try:
            relative = "seeknal/transforms/customer_ltv.yml"
            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                module.create_editor_draft(relative, (project / relative).read_text())
            self.assertFalse(outside.exists())
        finally:
            module.REPL.close()
            if previous is None:
                os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
            else:
                os.environ["SEEKNAL_STUDIO_PROJECT"] = previous
            temporary.cleanup()

    def test_repl_startup_is_active_work_for_project_switching(self):
        with patch.object(self.server.REPL, "status", return_value={"state": "starting"}):
            self.assertIn("REPL startup", self.server.active_work())

    def test_starting_query_on_ready_repl_stays_tracked_as_active_work(self):
        query_id = "starting-query"
        with self.server.REPL_QUERY_LOCK:
            self.server.REPL_QUERY_IDS.add(query_id)
        try:
            with (
                patch.object(self.server.REPL, "status", return_value={"state": "ready"}),
                patch.object(self.server.REPL, "get", return_value={"query_id": query_id, "status": "starting"}),
            ):
                self.assertIn("REPL query", self.server.active_work())
            with self.server.REPL_QUERY_LOCK:
                self.assertIn(query_id, self.server.REPL_QUERY_IDS)
        finally:
            with self.server.REPL_QUERY_LOCK:
                self.server.REPL_QUERY_IDS.discard(query_id)

    def test_project_api_reports_binding_and_queues_managed_switch(self):
        status, current = self.get("/api/projects")
        self.assertEqual(200, status)
        self.assertEqual(str(self.project.resolve()), current["current"]["path"])
        self.assertFalse(current["can_switch"])
        other = Path(self.temporary.name) / "other"
        other.mkdir()
        (other / "seeknal_project.yml").write_text("name: other\n")
        control = Path(self.temporary.name) / "switch.json"
        config = Path(self.temporary.name) / "projects.json"
        original = self.server.PROJECTS
        self.server.PROJECTS = ProjectService(
            self.project, busy=lambda: [], config_path=config,
            control_path=control, control_nonce="http-test",
        )
        try:
            status, result = self.post("/api/projects/open", {"path": str(other)})
        finally:
            self.server.PROJECTS = original
        self.assertEqual(202, status)
        self.assertTrue(result["switching"])
        self.assertEqual(str(other.resolve()), json.loads(control.read_text())["project"])


if __name__ == "__main__":
    unittest.main()
