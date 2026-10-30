#!/usr/bin/env python3
"""Persistent JSON-lines worker around Seeknal's real SQL REPL."""

from __future__ import annotations

from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from importlib import metadata
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any

from environment_service import HOST_CONTROL_ENV


MAX_ROWS = 500
MAX_REQUEST_BYTES = 128_000
_TABLE_NAME = re.compile(
    r'^(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+")'
    r'(?:\.(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+")){0,2}$'
)
_PROVIDER_ENV = re.compile(
    r"(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE_API|MISTRAL|COHERE|TOGETHER|GROQ|SUMOPOD)",
    re.IGNORECASE,
)
_PROVIDER_ENV_EXACT = {
    "FIRECRAWL_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HF_TOKEN",
    "IBA_ENGINE_API_KEY",
    "IBA_STORAGE_API_KEY",
}


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, datetime_time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _safe_error(error: BaseException) -> str:
    try:
        from seeknal.db_utils import sanitize_error_message  # pyright: ignore[reportMissingImports]

        message = sanitize_error_message(str(error))
    except Exception:
        message = str(error)
    return " ".join(message.split())[-2000:] or error.__class__.__name__


def _is_provider_environment(name: str) -> bool:
    return bool(
        name in _PROVIDER_ENV_EXACT
        or name.startswith("SEEKNAL_ASK_")
        or _PROVIDER_ENV.search(name)
    )


def _is_private_studio_environment(name: str) -> bool:
    return name.startswith("SEEKNAL_STUDIO_") or name in HOST_CONTROL_ENV


def _normalize_single_statement(sql: str) -> str:
    """Return one parser-confirmed DuckDB statement without its terminator."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ParseError

    try:
        parsed = sqlglot.parse(sql, read="duckdb")
    except ParseError as error:
        raise ValueError("SQL query could not be parsed") from error
    # SQLGlot represents a comment after a terminating semicolon as a harmless
    # trailing Semicolon node. Preserve the statement while discarding only
    # that parser-confirmed terminator/comment node.
    if len(parsed) == 2 and isinstance(parsed[1], exp.Semicolon) and parsed[1].comments:
        parsed = parsed[:1]
    if len(parsed) != 1 or parsed[0] is None:
        raise ValueError("Only one SQL statement is allowed")
    normalized = parsed[0].sql(dialect="duckdb", comments=True).strip()
    if not normalized:
        raise ValueError("SQL query is required")
    return normalized


def _load_project_environment(project_path: Path) -> None:
    """Load only the selected project's dotenv values needed by profiles."""
    env_file = project_path / ".env"
    if not env_file.is_file() or env_file.is_symlink():
        return
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    values = dotenv_values(env_file)
    for raw_name, value in values.items():
        name = str(raw_name)
        if value is None or _is_provider_environment(name) or _is_private_studio_environment(name):
            continue
        os.environ.setdefault(name, value)
    # Defense in depth: provider and Studio host-control capabilities are never
    # needed by the project-scoped SQL process.
    for name in list(os.environ):
        if _is_provider_environment(name) or _is_private_studio_environment(name):
            os.environ.pop(name, None)


class Worker:
    def __init__(self) -> None:
        self.repl = None
        self.project_path: Path | None = None
        self.profile_path: Path | None = None
        self.environment: str | None = None

    def initialize(self, request: dict[str, Any]) -> dict[str, Any]:
        project_path = Path(str(request["project_path"])).resolve()
        if not project_path.is_dir() or not (project_path / "seeknal_project.yml").is_file():
            raise ValueError("selected project is not a Seeknal project")
        raw_profile = request.get("profile_path")
        profile_path = Path(str(raw_profile)).expanduser().resolve() if raw_profile else None
        if profile_path is not None and (not profile_path.is_file() or profile_path.is_symlink()):
            raise ValueError("profile path is missing or unsafe")
        environment = request.get("environment")
        if environment is not None and not isinstance(environment, str):
            raise ValueError("environment must be a string")
        _load_project_environment(project_path)
        self.project_path = project_path
        self.profile_path = profile_path
        self.environment = environment
        self._replace_repl()
        import seeknal
        if seeknal.__file__ is None:
            raise RuntimeError("Seeknal SDK module has no file identity")

        return {
            "project_path": str(project_path),
            "profile_path": str(profile_path) if profile_path else None,
            "environment": environment,
            "sdk_version": metadata.version("seeknal"),
            "sdk_module_file": str(Path(seeknal.__file__).resolve()),
        }

    def _replace_repl(self) -> None:
        from seeknal.cli.repl import REPL  # pyright: ignore[reportMissingImports]

        previous = self.repl
        self.repl = REPL(
            project_path=self.project_path,
            profile_path=self.profile_path,
            env_name=self.environment,
            skip_history=True,
        )
        if previous is not None:
            try:
                previous.conn.close()
            except Exception:
                pass
            for connection in getattr(previous, "_starrocks_connections", {}).values():
                try:
                    connection.close()
                except Exception:
                    pass

    def query(self, text: str) -> dict[str, Any]:
        if self.repl is None:
            raise RuntimeError("REPL is not initialized")
        stripped = text.strip()
        if not stripped:
            raise ValueError("SQL query is required")
        if stripped.startswith("."):
            return self._meta(stripped)

        from seeknal.cli.repl import validate_sql  # pyright: ignore[reportMissingImports]

        is_valid, error = validate_sql(stripped)
        if not is_valid:
            raise ValueError(f"Query rejected: {error}")
        statement = _normalize_single_statement(stripped)
        started = time.monotonic()
        cursor = self.repl.conn.execute(
            f"SELECT * FROM ({statement}) AS _q LIMIT {MAX_ROWS + 1}"
        )
        columns = [item[0] for item in cursor.description]
        rows = cursor.fetchall()
        truncated = len(rows) > MAX_ROWS
        visible = rows[:MAX_ROWS]
        return {
            "kind": "table",
            "columns": columns,
            "rows": _json_value(visible),
            "row_count": len(visible),
            "truncated": truncated,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    def _meta(self, command: str) -> dict[str, Any]:
        repl = self.repl
        if repl is None:
            raise RuntimeError("REPL is not initialized")
        started = time.monotonic()
        if command == ".help":
            text = (
                ".tables — list registered tables\n"
                ".schema <table> — describe a table\n"
                ".reload — reload project outputs\n"
                ".quit — close this REPL worker"
            )
            return self._text_result("help", text, started)
        if command == ".tables":
            rows = repl.conn.execute(
                "SELECT table_catalog, table_schema, table_name "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
                "ORDER BY table_catalog, table_schema, table_name"
            ).fetchall()
            names = [
                table if catalog == "memory" and schema == "main" else f"{catalog}.{schema}.{table}"
                for catalog, schema, table in rows
            ]
            return {
                "kind": "tables",
                "columns": ["table"],
                "rows": [[name] for name in names[:MAX_ROWS]],
                "row_count": min(len(names), MAX_ROWS),
                "truncated": len(names) > MAX_ROWS,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
        if command.startswith(".schema"):
            table = command[len(".schema"):].strip()
            if not table:
                raise ValueError("Usage: .schema <table_name>")
            if not _TABLE_NAME.fullmatch(table):
                raise ValueError("table name contains unsupported characters")
            cursor = repl.conn.execute(f"DESCRIBE {table}")
            rows = cursor.fetchall()
            columns = [item[0] for item in cursor.description]
            return {
                "kind": "schema",
                "columns": columns,
                "rows": _json_value(rows[:MAX_ROWS]),
                "row_count": min(len(rows), MAX_ROWS),
                "truncated": len(rows) > MAX_ROWS,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
        if command == ".reload":
            self._replace_repl()
            return self._text_result("reload", "Project outputs reloaded.", started)
        if command in {".quit", ".exit"}:
            return self._text_result("quit", "REPL closed.", started)
        raise ValueError(f"Unknown command: {command.split()[0]}")

    @staticmethod
    def _text_result(kind: str, text: str, started: float) -> dict[str, Any]:
        return {
            "kind": kind,
            "text": text,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }

    def close(self) -> None:
        if self.repl is None:
            return
        try:
            self.repl.conn.close()
        except Exception:
            pass
        for connection in getattr(self.repl, "_starrocks_connections", {}).values():
            try:
                connection.close()
            except Exception:
                pass
        self.repl = None


def main() -> int:
    worker = Worker()
    for raw_line in sys.stdin.buffer:
        if len(raw_line) > MAX_REQUEST_BYTES:
            _write({"type": "response", "request_id": None, "ok": False, "error": "request is too large"})
            continue
        request_id = None
        try:
            request = json.loads(raw_line)
            request_id = request.get("request_id")
            operation = request.get("operation")
            if operation == "init":
                result = worker.initialize(request)
            elif operation == "query":
                sql = request.get("sql")
                if not isinstance(sql, str):
                    raise ValueError("SQL query must be a string")
                result = worker.query(sql)
            elif operation == "reload":
                worker._replace_repl()
                result = {"reloaded": True}
            elif operation == "close":
                worker.close()
                result = {"closed": True}
            else:
                raise ValueError("unsupported REPL operation")
            _write({"type": "response", "request_id": request_id, "ok": True, "result": result})
            if operation == "close" or (operation == "query" and str(request.get("sql", "")).strip() in {".quit", ".exit"}):
                return 0
        except Exception as error:
            _write({"type": "response", "request_id": request_id, "ok": False, "error": _safe_error(error)})
    worker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
