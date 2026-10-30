#!/usr/bin/env python3
"""Asynchronous supervisor for a project-scoped Seeknal REPL worker."""

from __future__ import annotations

from collections import deque
import hashlib
import inspect
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from typing import Any, Callable

from environment_service import HOST_CONTROL_ENV


MAX_SQL_BYTES = 100_000
STARTUP_TIMEOUT = 60.0
QUERY_TIMEOUT = 600.0
_PROVIDER_SECRET = re.compile(
    r"(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE_API|MISTRAL|COHERE|TOGETHER|GROQ|SUMOPOD).*?(?:KEY|TOKEN|SECRET)?$",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(r"(?i)(password|token|secret|api[_-]?key)=([^\s&;]+)")


def _project_value(project: Any, *names: str) -> Any:
    if isinstance(project, dict):
        for name in names:
            if project.get(name) is not None:
                return project[name]
    for name in names:
        value = getattr(project, name, None)
        if value is not None:
            return value
    return None


def _project_path(project: Any) -> Path:
    if isinstance(project, (str, os.PathLike)):
        raw = project
    else:
        raw = _project_value(project, "project_path", "path", "root")
    if raw is None:
        raise ValueError("project path is required")
    return Path(raw).expanduser().resolve()


def _redact(value: object) -> str:
    return _SECRET_VALUE.sub(r"\1=[redacted]", " ".join(str(value).split()))[-2000:]


class ReplService:
    """Own one persistent worker and expose pollable asynchronous queries."""

    def __init__(
        self,
        project: Any,
        python: Path,
        worker_path: Path,
        environment_factory: Callable[..., dict[str, str]],
        execution_lock: threading.Lock,
        sdk_version: str | None = None,
        sdk_module_file: Path | None = None,
    ) -> None:
        self.project_source = project
        self.project = _project_path(project)
        raw_profile = _project_value(project, "profile_path", "profile")
        self.profile_path = Path(raw_profile).expanduser().resolve() if raw_profile else None
        environment = _project_value(project, "environment", "env_name")
        self.environment = str(environment) if environment else None
        # Keep the lexical venv entrypoint; resolving it can execute the base
        # interpreter without the managed SDK environment.
        self.python = Path(os.path.abspath(os.path.expanduser(str(python))))
        self.worker_path = Path(worker_path).resolve()
        self.sdk_version = sdk_version
        self.sdk_module_file = sdk_module_file.resolve() if sdk_module_file else None
        self._sdk_identity: dict[str, str] | None = None
        self.environment_factory = environment_factory
        self.execution_lock = execution_lock
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._process_generation: int | None = None
        self._generation = 0
        self._state = "stopped"
        self._error: str | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._pending: dict[str, tuple[int, queue.Queue[dict[str, Any]]]] = {}
        self._queries: dict[str, dict[str, Any]] = {}
        self._active_query: str | None = None
        self._session_id = uuid.uuid4().hex
        self._bound_provenance: dict[str, str | None] | None = None
        self._closed = False

    def _run_state_path(self) -> Path:
        if self.environment:
            environment_state = (
                self.project / "target" / "environments" / self.environment / "run_state.json"
            )
            if environment_state.is_file():
                return environment_state
        return self.project / "target" / "run_state.json"

    def _output_provenance(self) -> dict[str, str | None]:
        state_path = self._run_state_path()
        try:
            content = state_path.read_bytes()
        except OSError:
            content = b""
        signature = hashlib.sha256(
            state_path.relative_to(self.project).as_posix().encode() + b"\0" + content
        ).hexdigest()
        run_id = None
        if content:
            try:
                payload = json.loads(content)
                value = payload.get("run_id") if isinstance(payload, dict) else None
                run_id = str(value) if value is not None else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
        return {"run_id": run_id, "output_signature": signature}

    def status(self) -> dict[str, Any]:
        with self._lock:
            current = self._output_provenance()
            bound = self._bound_provenance
            process_alive = self._process is not None and self._process.poll() is None
            state = self._state
            if state == "ready" and not process_alive:
                state = "error"
            result = {
                "state": state,
                "ready": state == "ready" and process_alive,
                "healthy": process_alive and state in {"starting", "ready"},
                "project_path": str(self.project),
                "profile_path": str(self.profile_path) if self.profile_path else None,
                "environment": self.environment,
                "generation": self._process_generation,
                "session_id": self._session_id,
                "bound_run_id": bound.get("run_id") if bound else None,
                "bound_output_signature": bound.get("output_signature") if bound else None,
                "current_run_id": current["run_id"],
                "current_output_signature": current["output_signature"],
                "needs_reload": bool(
                    bound and bound.get("output_signature") != current["output_signature"]
                ),
                "sdk_version": (self._sdk_identity or {}).get("sdk_version"),
                "sdk_module_file": (self._sdk_identity or {}).get("sdk_module_file"),
            }
            if self._error:
                result["error"] = self._error
            if self._stderr:
                result["stderr"] = list(self._stderr)
            return result

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("REPL service is closed")
            if self._state in {"starting", "ready"} and self._process is not None and self._process.poll() is None:
                return self.status()
            self._state = "starting"
            self._error = None
        threading.Thread(target=self._bootstrap, name="seeknal-repl-bootstrap", daemon=True).start()
        return self.status()

    def _bootstrap(self) -> None:
        try:
            with self.execution_lock:
                self._ensure_worker()
        except Exception as error:
            with self._lock:
                if not self._closed:
                    self._stop_worker_locked("REPL initialization failed")
                    self._state = "error"
                    self._error = _redact(error)

    def _ensure_worker(self) -> tuple[subprocess.Popen[str], int]:
        with self._start_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("REPL service is closed")
                if self._process is not None and self._process.poll() is None and self._state == "ready":
                    generation = self._process_generation
                    if generation is None:
                        raise RuntimeError("REPL worker generation is unavailable")
                    return self._process, generation
                self._state = "starting"
                self._stop_worker_locked("REPL worker restarted")
                self._validate_inputs()
                process = subprocess.Popen(
                    [str(self.python), str(self.worker_path)],
                    cwd=self.project,
                    env=self._child_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                self._process = process
                self._generation += 1
                generation = self._generation
                self._process_generation = generation
                self._reader = threading.Thread(
                    target=self._read_stdout, args=(process, generation), daemon=True,
                )
                self._stderr_reader = threading.Thread(
                    target=self._drain_stderr, args=(process, generation), daemon=True,
                )
                self._reader.start()
                self._stderr_reader.start()
            response = self._request(
                {
                    "operation": "init",
                    "project_path": str(self.project),
                    "profile_path": str(self.profile_path) if self.profile_path else None,
                    "environment": self.environment,
                },
                generation,
                STARTUP_TIMEOUT,
            )
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "REPL initialization failed"))
            result = response.get("result") or {}
            if result.get("project_path") != str(self.project):
                raise RuntimeError("REPL worker project identity mismatch")
            if (
                self.sdk_version is not None
                and result.get("sdk_version") != self.sdk_version
            ):
                raise RuntimeError("REPL worker SDK version mismatch")
            if (
                self.sdk_module_file is not None
                and result.get("sdk_module_file") != str(self.sdk_module_file)
            ):
                raise RuntimeError("REPL worker SDK module mismatch")
            with self._lock:
                if self._process is not process or self._process_generation != generation:
                    raise RuntimeError("REPL worker generation changed during startup")
                self._state = "ready"
                self._error = None
                self._sdk_identity = {
                    "sdk_version": str(result.get("sdk_version", "")),
                    "sdk_module_file": str(result.get("sdk_module_file", "")),
                }
                self._bound_provenance = self._output_provenance()
            return process, generation

    def _validate_inputs(self) -> None:
        if not self.project.is_dir() or not (self.project / "seeknal_project.yml").is_file():
            raise RuntimeError("selected project is not a Seeknal project")
        if not self.python.is_file():
            raise RuntimeError("Seeknal Python runtime is missing or unsafe")
        if not self.worker_path.is_file() or self.worker_path.is_symlink():
            raise RuntimeError("REPL worker is missing or unsafe")
        if self.profile_path is not None and (
            not self.profile_path.is_file() or self.profile_path.is_symlink()
        ):
            raise RuntimeError("profile path is missing or unsafe")

    def _child_environment(self) -> dict[str, str]:
        base_keys = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
        environment = {key: os.environ[key] for key in base_keys if key in os.environ}
        try:
            parameters = inspect.signature(self.environment_factory).parameters
            supplied = self.environment_factory(self.project_source) if parameters else self.environment_factory()
        except (TypeError, ValueError):
            supplied = self.environment_factory()
        if supplied is None:
            supplied = {}
        if not isinstance(supplied, dict):
            raise RuntimeError("REPL environment factory must return a mapping")
        for key, value in supplied.items():
            key = str(key)
            if (
                key.startswith("SEEKNAL_STUDIO_")
                or key in HOST_CONTROL_ENV
                or _PROVIDER_SECRET.fullmatch(key)
                or key in {"SIGNAL_ENV", "SEEKNAL_SIGNAL_ENV"}
                or key in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}
            ):
                continue
            if isinstance(value, str):
                environment[key] = value
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    def execute(self, sql: str) -> dict[str, Any]:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("SQL query is required")
        if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
            raise ValueError("SQL query is too large")
        query_id = uuid.uuid4().hex
        query = {
            "query_id": query_id,
            "status": "queued",
            "sql": sql,
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
            "kind": None,
            "text": None,
            "error": None,
            "generation": None,
            "session_id": self._session_id,
            "run_id": None,
            "output_signature": None,
            "historical": False,
            "current": None,
            "error_code": None,
        }
        with self._lock:
            if self._closed:
                raise RuntimeError("REPL service is closed")
            self._queries[query_id] = query
        threading.Thread(
            target=self._execute_query, args=(query_id,), name=f"seeknal-repl-{query_id[:8]}", daemon=True,
        ).start()
        return self.get(query_id) or query

    def _execute_query(self, query_id: str) -> None:
        try:
            with self.execution_lock:
                with self._lock:
                    query = self._queries.get(query_id)
                    if query is None or query["status"] == "cancelled":
                        return
                    query["status"] = "starting"
                    query["started_at"] = time.time()
                    self._active_query = query_id
                _process, generation = self._ensure_worker()
                with self._lock:
                    query = self._queries[query_id]
                    if query["status"] == "cancelled":
                        return
                    query["status"] = "running"
                    query["generation"] = generation
                    bound = self._bound_provenance or self._output_provenance()
                    query["run_id"] = bound["run_id"]
                    query["output_signature"] = bound["output_signature"]
                    sql = query["sql"]
                current = self._output_provenance()
                if current["output_signature"] != bound["output_signature"]:
                    with self._lock:
                        query = self._queries[query_id]
                        query["status"] = "failed"
                        query["error_code"] = "reload_required"
                        query["error"] = "Pipeline outputs changed; reload the REPL before querying."
                        query["historical"] = True
                        query["current"] = False
                        query["finished_at"] = time.time()
                    return
                response = self._request(
                    {"operation": "query", "sql": sql}, generation, QUERY_TIMEOUT,
                )
            with self._lock:
                query = self._queries[query_id]
                if query["status"] == "cancelled":
                    return
                query["finished_at"] = time.time()
                if response.get("ok"):
                    result = response.get("result") or {}
                    for key in ("columns", "rows", "row_count", "truncated", "kind", "text", "duration_ms"):
                        query[key] = result.get(key, query[key])
                    query["status"] = "succeeded"
                    query["historical"] = False
                    query["current"] = True
                    if query["kind"] == "quit":
                        self._stop_worker_locked("REPL closed by client")
                        self._state = "stopped"
                else:
                    query["status"] = "failed"
                    query["error"] = _redact(response.get("error", "REPL query failed"))
        except Exception as error:
            with self._lock:
                query = self._queries.get(query_id)
                if query is not None and query["status"] != "cancelled":
                    query["status"] = "failed"
                    query["error"] = _redact(error)
                    query["finished_at"] = time.time()
                    self._stop_worker_locked("REPL query failed")
                    self._state = "error"
                    self._error = _redact(error)
        finally:
            with self._lock:
                query = self._queries.get(query_id)
                if query is not None and query["finished_at"] is None and query["status"] != "running":
                    query["finished_at"] = time.time()
                if self._active_query == query_id:
                    self._active_query = None

    def get(self, query_id: str) -> dict[str, Any] | None:
        with self._lock:
            query = self._queries.get(query_id)
            if query is None:
                return None
            snapshot = json.loads(json.dumps(query))
            if snapshot.get("output_signature"):
                current = self._output_provenance()
                historical = snapshot["output_signature"] != current["output_signature"]
                snapshot["historical"] = historical
                snapshot["current"] = not historical
            return snapshot

    def cancel(self, query_id: str) -> dict[str, Any] | None:
        with self._lock:
            query = self._queries.get(query_id)
            if query is None:
                return None
            if query["status"] in {"queued", "starting", "running"}:
                query["status"] = "cancelled"
                query["finished_at"] = time.time()
                if self._active_query == query_id:
                    self._stop_worker_locked("REPL query cancelled")
                    self._state = "stopped"
            return json.loads(json.dumps(query))

    def reload(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("REPL service is closed")
            if self._active_query:
                active = self._queries.get(self._active_query)
                if active and active["status"] in {"starting", "running"}:
                    active["status"] = "cancelled"
                    active["finished_at"] = time.time()
            self._stop_worker_locked("REPL reloaded")
            self._state = "stopped"
            self._error = None
            self._bound_provenance = None
        return self.start()

    def _request(self, payload: dict[str, Any], generation: int, timeout: float) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = (generation, waiter)
        try:
            self._write({**payload, "request_id": request_id}, generation)
            try:
                return waiter.get(timeout=timeout)
            except queue.Empty as error:
                raise TimeoutError("REPL worker response timed out") from error
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _write(self, payload: dict[str, Any], generation: int) -> None:
        with self._write_lock:
            with self._lock:
                process = self._process
                if process is None or process.stdin is None or process.poll() is not None:
                    raise RuntimeError("REPL worker is unavailable")
                if self._process_generation != generation:
                    raise RuntimeError("REPL worker generation changed")
                stream = process.stdin
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
            stream.flush()

    def _read_stdout(self, process: subprocess.Popen[str], generation: int) -> None:
        if process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("request_id")
                with self._lock:
                    pending = self._pending.get(str(request_id))
                if pending is not None and pending[0] == generation:
                    try:
                        pending[1].put_nowait(message)
                    except queue.Full:
                        pass
        finally:
            with self._lock:
                waiters = [waiter for item_generation, waiter in self._pending.values() if item_generation == generation]
                if self._process is process and self._process_generation == generation and not self._closed:
                    self._state = "error"
                    self._error = "REPL worker exited"
            for waiter in waiters:
                try:
                    waiter.put_nowait({"ok": False, "error": "REPL worker exited"})
                except queue.Full:
                    pass

    def _drain_stderr(self, process: subprocess.Popen[str], generation: int) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            with self._lock:
                if self._process_generation == generation:
                    self._stderr.append(_redact(line))

    def _stop_worker_locked(self, reason: str) -> None:
        process = self._process
        generation = self._process_generation
        self._process = None
        self._process_generation = None
        waiters = [waiter for item_generation, waiter in self._pending.values() if item_generation == generation]
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=3)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        for waiter in waiters:
            try:
                waiter.put_nowait({"ok": False, "error": reason})
            except queue.Full:
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for query in self._queries.values():
                if query["status"] in {"queued", "starting", "running"}:
                    query["status"] = "cancelled"
                    query["finished_at"] = time.time()
            self._stop_worker_locked("REPL service closed")
            self._state = "stopped"

