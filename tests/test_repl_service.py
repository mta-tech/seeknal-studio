from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "pilot" / "workspace"
sys.path.insert(0, str(ROOT))

from runtime_resolver import resolve_runtime  # noqa: E402
from repl_service import ReplService  # noqa: E402
from repl_worker import _load_project_environment  # noqa: E402

RUNTIME = resolve_runtime()
PYTHON = RUNTIME.python


def wait_for(predicate, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last and last.get("status", last.get("state")) not in {"queued", "starting", "running"}:
            return last
        time.sleep(0.05)
    raise AssertionError(f"timed out; last value: {last}")


def require_snapshot(value: dict | None) -> dict:
    assert value is not None
    return value


@unittest.skipUnless(PYTHON.is_file(), "Seeknal virtualenv is unavailable")
class ReplServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = threading.Lock()
        self.seen_environment: list[Path] = []

        def environment_factory(project: Path) -> dict[str, str]:
            self.seen_environment.append(Path(project))
            return {
                "HOME": os.environ.get("HOME", ""),
                "OPENAI_API_KEY": "must-not-reach-worker",
                "SEEKNAL_STUDIO_CONTROL_FILE": "/private/control.json",
                "SEEKNAL_STUDIO_CONTROL_NONCE": "private-control-nonce",
                "SEEKNAL_STUDIO_BASELINE_RECEIPT": "/private/receipt.json",
                "SEEKNAL_STUDIO_BASELINE_NONCE": "private-baseline-nonce",
                "SEEKNAL_STUDIO_PROJECTS_FILE": "/private/projects.json",
                "SEEKNAL_GATEWAY_URL": "https://gateway.private.test",
                "SEEKNAL_GATEWAY_TOKEN": "private-gateway-token",
                "SEEKNAL_PROJECT_PATH": "/private/host-project",
                "WAREHOUSE_DSN": "postgresql://reader@db/warehouse",
                "ATLAS_API_URL": "https://atlas.example.test",
            }

        self.service = ReplService(
            PILOT,
            PYTHON,
            ROOT / "repl_worker.py",
            environment_factory,
            self.lock,
        )

    def tearDown(self) -> None:
        self.service.close()

    def start_ready(self) -> dict:
        self.service.start()
        status = wait_for(self.service.status)
        self.assertEqual(status["state"], "ready", status)
        return status

    def execute(self, sql: str) -> dict:
        query = self.service.execute(sql)
        result = wait_for(lambda: self.service.get(query["query_id"]))
        self.assertEqual(result["status"], "succeeded", result)
        return result

    def test_real_repl_persists_and_queries_pilot_outputs(self) -> None:
        status = self.start_ready()
        first = self.execute("SELECT count(*) AS n FROM orders")
        second = self.execute("SELECT sum(amount) AS total FROM orders WHERE status = 'paid'")
        self.assertEqual(first["rows"], [[6]])
        self.assertGreater(second["rows"][0][0], 0)
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(status["generation"], first["generation"])
        self.assertEqual(self.seen_environment, [PILOT])

    def test_tables_schema_invalid_sql_and_bounded_rows(self) -> None:
        tables = self.execute(".tables")
        names = {row[0] for row in tables["rows"]}
        self.assertIn("orders", names)
        self.assertIn("customer_value_pilot", names)
        schema = self.execute(".schema orders")
        self.assertEqual(schema["kind"], "schema")
        self.assertIn("order_id", {row[0] for row in schema["rows"]})

        rejected = self.service.execute("DROP TABLE orders")
        rejected = wait_for(lambda: self.service.get(rejected["query_id"]))
        self.assertEqual(rejected["status"], "failed")
        self.assertIn("read-only", rejected["error"])

        bounded = self.execute("SELECT range AS n FROM range(1000)")
        self.assertEqual(bounded["row_count"], 500)
        self.assertTrue(bounded["truncated"])

    def test_single_statement_terminator_strings_and_comments_are_parser_safe(self) -> None:
        self.start_ready()
        terminated = self.execute(
            "SELECT order_id, customer_id, amount, status "
            "FROM orders ORDER BY order_id;"
        )
        self.assertGreater(terminated["row_count"], 0)
        literal = self.execute("SELECT ';' AS marker;")
        self.assertEqual(literal["rows"], [[";"]])
        commented = self.execute("SELECT 1 AS n /* internal ; remains a comment */;")
        self.assertEqual(commented["rows"], [[1]])
        trailing_comment = self.execute("SELECT 2 AS n; -- trailing ; comment")
        self.assertEqual(trailing_comment["rows"], [[2]])

        multiple = self.service.execute("SELECT 1; SELECT 2")
        multiple = wait_for(lambda: self.service.get(multiple["query_id"]))
        self.assertEqual(multiple["status"], "failed")
        self.assertIn("one SQL statement", multiple["error"])

    def test_reload_changes_generation_and_close_reaps_worker(self) -> None:
        before = self.start_ready()["generation"]
        self.service.reload()
        after = wait_for(self.service.status)
        self.assertEqual(after["state"], "ready", after)
        self.assertGreater(after["generation"], before)
        process = self.service._process
        assert process is not None
        self.service.close()
        self.assertIsNotNone(process.poll())
        self.assertEqual(self.service.status()["state"], "stopped")

    def test_run_change_requires_reload_and_marks_old_results_historical(self) -> None:
        import duckdb

        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            shutil.copytree(PILOT, project)
            service = ReplService(
                project,
                PYTHON,
                ROOT / "repl_worker.py",
                lambda: {"HOME": os.environ.get("HOME", "")},
                threading.Lock(),
            )
            try:
                service.start()
                ready = wait_for(service.status)
                self.assertFalse(ready["needs_reload"])
                old = service.execute("SELECT count(*) AS n FROM orders")
                old = wait_for(lambda: service.get(old["query_id"]))
                self.assertTrue(old["current"])

                output = project / "target" / "intermediate" / "transform_refresh_probe.parquet"
                escaped = str(output).replace("'", "''")
                connection = duckdb.connect(":memory:")
                try:
                    connection.execute(f"COPY (SELECT 42 AS answer) TO '{escaped}' (FORMAT PARQUET)")
                finally:
                    connection.close()
                state_path = project / "target" / "run_state.json"
                state = json.loads(state_path.read_text())
                state["run_id"] = "refresh_probe_run"
                state["nodes"]["transform.refresh_probe"] = {
                    "status": "success",
                    "metadata": {"intermediate_path": str(output)},
                }
                state_path.write_text(json.dumps(state))

                stale_status = service.status()
                self.assertTrue(stale_status["needs_reload"])
                self.assertEqual(stale_status["bound_run_id"], ready["bound_run_id"])
                self.assertEqual(stale_status["current_run_id"], "refresh_probe_run")
                old_snapshot = require_snapshot(service.get(old["query_id"]))
                self.assertTrue(old_snapshot["historical"])
                self.assertFalse(old_snapshot["current"])

                rejected = service.execute("SELECT * FROM refresh_probe")
                rejected = wait_for(lambda: service.get(rejected["query_id"]))
                self.assertEqual(rejected["status"], "failed")
                self.assertEqual(rejected["error_code"], "reload_required")

                service.reload()
                refreshed = wait_for(service.status)
                self.assertFalse(refreshed["needs_reload"])
                tables = service.execute(".tables")
                tables = wait_for(lambda: service.get(tables["query_id"]))
                self.assertEqual(tables["status"], "succeeded", tables)
                self.assertIn("refresh_probe", {row[0] for row in tables["rows"]})
                self.assertEqual(tables["run_id"], "refresh_probe_run")
                self.assertTrue(tables["current"])
            finally:
                service.close()

    def test_execution_waits_for_shared_lock_and_can_cancel_queued_query(self) -> None:
        self.start_ready()
        self.lock.acquire()
        try:
            query = self.service.execute("SELECT 1 AS n")
            time.sleep(0.1)
            pending = require_snapshot(self.service.get(query["query_id"]))
            self.assertEqual(pending["status"], "queued")
            cancelled = require_snapshot(self.service.cancel(query["query_id"]))
            self.assertEqual(cancelled["status"], "cancelled")
        finally:
            self.lock.release()
        time.sleep(0.1)
        final = require_snapshot(self.service.get(query["query_id"]))
        self.assertEqual(final["status"], "cancelled")

    def test_running_query_cancellation_restarts_clean_worker(self) -> None:
        before = self.start_ready()["generation"]
        query = self.service.execute("SELECT sum(i) FROM range(1000000000000) AS values(i)")
        deadline = time.monotonic() + 10
        current: dict = {}
        while time.monotonic() < deadline:
            current = require_snapshot(self.service.get(query["query_id"]))
            if current["status"] == "running":
                break
            time.sleep(0.02)
        else:
            self.fail(f"query did not enter running state: {current}")
        cancelled = require_snapshot(self.service.cancel(query["query_id"]))
        self.assertEqual(cancelled["status"], "cancelled")
        recovered = self.execute("SELECT 1 AS n")
        self.assertEqual(recovered["rows"], [[1]])
        self.assertGreater(recovered["generation"], before)

    def test_oversized_input_and_provider_credentials_are_rejected_or_isolated(self) -> None:
        with self.assertRaisesRegex(ValueError, "too large"):
            self.service.execute("SELECT '" + ("x" * 100_001) + "'")
        child = self.service._child_environment()
        self.assertNotIn("OPENAI_API_KEY", child)
        for name in (
            "SEEKNAL_STUDIO_CONTROL_FILE",
            "SEEKNAL_STUDIO_CONTROL_NONCE",
            "SEEKNAL_STUDIO_BASELINE_RECEIPT",
            "SEEKNAL_STUDIO_BASELINE_NONCE",
            "SEEKNAL_STUDIO_PROJECTS_FILE",
            "SEEKNAL_GATEWAY_URL",
            "SEEKNAL_GATEWAY_TOKEN",
            "SEEKNAL_PROJECT_PATH",
        ):
            self.assertNotIn(name, child)
        self.assertEqual(child["WAREHOUSE_DSN"], "postgresql://reader@db/warehouse")
        self.assertEqual(child["ATLAS_API_URL"], "https://atlas.example.test")

    def test_only_selected_project_dotenv_is_loaded_without_provider_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / ".env").write_text(
                "WAREHOUSE_DSN=postgresql://reader:secret@db/warehouse\n"
                "ATLAS_API_URL=https://atlas.project.example.test\n"
                "OPENAI_API_KEY=must-not-load\n"
                "SEEKNAL_ASK_MODEL=must-not-load\n"
                "SEEKNAL_STUDIO_CONTROL_FILE=/private/control.json\n"
                "SEEKNAL_STUDIO_CONTROL_NONCE=private-control-nonce\n"
                "SEEKNAL_STUDIO_BASELINE_RECEIPT=/private/receipt.json\n"
                "SEEKNAL_STUDIO_BASELINE_NONCE=private-baseline-nonce\n"
                "SEEKNAL_STUDIO_PROJECTS_FILE=/private/projects.json\n"
                "SEEKNAL_GATEWAY_URL=https://project-gateway.private.test\n"
                "SEEKNAL_GATEWAY_TOKEN=project-gateway-token\n"
                "SEEKNAL_PROJECT_PATH=/private/project-override\n"
            )
            with patch.dict(
                os.environ,
                {
                    "SEEKNAL_STUDIO_CONTROL_FILE": "/inherited/private-control.json",
                    "SEEKNAL_GATEWAY_URL": "https://inherited-gateway.private.test",
                    "SEEKNAL_GATEWAY_TOKEN": "inherited-gateway-token",
                    "SEEKNAL_PROJECT_PATH": "/inherited/private-project",
                },
                clear=False,
            ):
                os.environ.pop("WAREHOUSE_DSN", None)
                os.environ.pop("ATLAS_API_URL", None)
                os.environ.pop("OPENAI_API_KEY", None)
                os.environ.pop("SEEKNAL_ASK_MODEL", None)
                _load_project_environment(project)
                self.assertEqual(
                    os.environ["WAREHOUSE_DSN"],
                    "postgresql://reader:secret@db/warehouse",
                )
                self.assertEqual(
                    os.environ["ATLAS_API_URL"],
                    "https://atlas.project.example.test",
                )
                self.assertNotIn("OPENAI_API_KEY", os.environ)
                self.assertNotIn("SEEKNAL_ASK_MODEL", os.environ)
                for name in (
                    "SEEKNAL_STUDIO_CONTROL_FILE",
                    "SEEKNAL_STUDIO_CONTROL_NONCE",
                    "SEEKNAL_STUDIO_BASELINE_RECEIPT",
                    "SEEKNAL_STUDIO_BASELINE_NONCE",
                    "SEEKNAL_STUDIO_PROJECTS_FILE",
                    "SEEKNAL_GATEWAY_URL",
                    "SEEKNAL_GATEWAY_TOKEN",
                    "SEEKNAL_PROJECT_PATH",
                ):
                    self.assertNotIn(name, os.environ)


if __name__ == "__main__":
    unittest.main()
