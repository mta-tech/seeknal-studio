#!/usr/bin/env python3
"""Studio-owned authenticated supervisor for the local Seeknal agent worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import subprocess
import threading
import tempfile
import time
import uuid


DEFAULT_CONFIG = {
    "provider": "openai",
    "model": "deepseek-v4.1-flash:netra",
    "base_url": "https://ai.sumopod.com/v1",
    "api_key_file": str(Path("~/sumopod-ai-api-key").expanduser()),
}
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TURN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_QUESTION_ID = re.compile(r"^[0-9a-f]{32}$")
_EXPECTED_TOOLS = {
    "read_pipeline", "search_pipelines", "search_project_files", "draft_node", "write_draft",
    "ask_user",
}
TURN_ACTIVE_SECONDS = 600


def _fingerprint(project: Path) -> str:
    digest = hashlib.sha256()
    paths = [project / "seeknal_project.yml"]
    paths.extend((project / "seeknal").glob("**/*"))
    paths.extend((project / "data").glob("**/*"))
    for path in sorted(path for path in paths if path.is_file() and not path.is_symlink()):
        digest.update(path.relative_to(project).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class AgentRuntime:
    """Own one draft-only worker and expose asynchronous, pollable turns."""

    def __init__(
        self,
        project_path: Path,
        python: Path,
        worker_path: Path,
        signal_root: Path | None = None,
        sdk_root: Path | None = None,
        sdk_version: str | None = None,
        sdk_module_file: Path | None = None,
        execution_lock: threading.Lock | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.project = project_path.resolve()
        # Preserve the selected virtualenv executable path. Resolving a venv's
        # python symlink to its base interpreter drops that venv's site-packages.
        self.python = Path(os.path.abspath(os.path.expanduser(str(python))))
        self.worker_path = worker_path.resolve()
        # signal_root is retained only for explicit legacy test callers.
        self.sdk_root = (sdk_root or signal_root or self.python.parents[2]).resolve()
        self.sdk_version = sdk_version
        self.sdk_module_file = sdk_module_file.resolve() if sdk_module_file else None
        self.execution_lock = execution_lock or threading.Lock()
        self.config_path = (config_path or (
            Path.home() / "Library" / "Application Support" / "Seeknal Studio" / "agent.json"
        )).expanduser()
        self._lock = threading.RLock()
        self._startup_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._answer_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._pending: dict[str, tuple[int, queue.Queue]] = {}
        self._request_sessions: dict[str, tuple[int, str, str]] = {}
        self._generation = 0
        self._process_generation: int | None = None
        self._turns: dict[tuple[str, str], dict] = {}
        self._latest_turn: dict[str, str] = {}
        self._config_load_error: str | None = None
        self._config = self._load_config()
        self._identity: dict | None = None
        self._error: str | None = None
        self._nonce: str | None = None
        self._last_start_attempt = 0.0

    def _load_config(self) -> dict:
        config = dict(DEFAULT_CONFIG)
        if self.config_path.exists() or self.config_path.is_symlink():
            try:
                if self.config_path.is_symlink() or not self.config_path.is_file():
                    raise ValueError("saved agent configuration path is unsafe")
                if self.config_path.stat().st_mode & 0o077:
                    raise ValueError("saved agent configuration permissions must be owner-only")
                loaded = json.loads(self.config_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or set(loaded) != set(DEFAULT_CONFIG):
                    raise ValueError("saved agent configuration schema is invalid")
                if not all(isinstance(loaded[key], str) and loaded[key] for key in DEFAULT_CONFIG):
                    raise ValueError("saved agent configuration values are invalid")
                if (
                    loaded["provider"] != DEFAULT_CONFIG["provider"]
                    or loaded["model"] != DEFAULT_CONFIG["model"]
                    or loaded["base_url"].rstrip("/") != DEFAULT_CONFIG["base_url"]
                ):
                    raise ValueError("saved agent configuration authority is invalid")
                config.update(loaded)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
                self._config_load_error = str(error)
        config["sdk_root"] = str(self.sdk_root)
        config["expected_seeknal_version"] = "2.14.0"
        return config

    def configure(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("agent configuration must be an object")
        candidate = dict(self._config)
        for key in DEFAULT_CONFIG:
            if key in payload:
                candidate[key] = payload[key]
        if candidate["provider"] != "openai":
            raise ValueError("pilot provider must be openai-compatible")
        if candidate["model"] != DEFAULT_CONFIG["model"]:
            raise ValueError(f"pilot model must be {DEFAULT_CONFIG['model']}")
        if str(candidate["base_url"]).rstrip("/") != DEFAULT_CONFIG["base_url"]:
            raise ValueError("pilot endpoint must be the verified Sumopod endpoint")
        key_path = Path(str(candidate["api_key_file"])).expanduser().resolve()
        if not key_path.is_file() or key_path.is_symlink():
            raise ValueError("API key file is missing or unsafe")
        if key_path.stat().st_mode & 0o077:
            raise ValueError("API key file permissions must be owner-only (chmod 600)")
        persisted = {key: str(key_path) if key == "api_key_file" else candidate[key] for key in DEFAULT_CONFIG}
        parent = self.config_path.parent
        if parent.is_symlink() or self.config_path.is_symlink():
            raise ValueError("agent configuration path is unsafe")
        parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".agent-", dir=parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(persisted, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        os.chmod(self.config_path, 0o600)
        with self._lock:
            self._config = {
                **persisted,
                "sdk_root": str(self.sdk_root),
                "expected_seeknal_version": "2.14.0",
            }
            self._config_load_error = None
            self._error = None
            self._last_start_attempt = 0.0
            self._stop_worker()
        return self.status()

    def _child_environment(self) -> dict[str, str]:
        keep = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
        environment = {key: os.environ[key] for key in keep if key in os.environ}
        environment["PYTHONUNBUFFERED"] = "1"
        return environment

    def _validate_key_file(self) -> None:
        path = Path(str(self._config.get("api_key_file", ""))).expanduser().resolve()
        if not path.is_file() or path.is_symlink():
            raise RuntimeError("configured API key file is missing or unsafe")
        if path.stat().st_mode & 0o077:
            raise RuntimeError("API key file permissions must be owner-only (chmod 600)")
        if not path.read_text(encoding="utf-8").strip():
            raise RuntimeError("configured API key file is empty")

    def _key_file_valid(self) -> bool:
        try:
            self._validate_key_file()
            return True
        except (OSError, UnicodeError, RuntimeError):
            return False

    def _start_worker(self, timeout: float = 45) -> None:
        with self._startup_lock:
            try:
                self._start_worker_serialized(timeout)
            except Exception:
                with self._lock:
                    self._stop_worker()
                raise

    def _start_worker_serialized(self, timeout: float) -> None:
        with self._lock:
            if self._config_load_error:
                raise RuntimeError(self._config_load_error)
            self._validate_key_file()
            if self._process is not None and self._process.poll() is None and self._identity:
                if self._identity.get("project_fingerprint") == _fingerprint(self.project):
                    return
                # A project mutation invalidates the worker's authenticated
                # context. Stop it and require a fresh nonce/HMAC handshake;
                # never relabel the existing identity with the new fingerprint.
                self._stop_worker()
            if self._error and time.monotonic() - self._last_start_attempt < 30:
                raise RuntimeError(self._error)
            self._last_start_attempt = time.monotonic()
            self._stop_worker()
            if not self.project.is_dir() or not (self.project / "seeknal_project.yml").is_file():
                raise RuntimeError("selected project is not a Seeknal project")
            token = secrets.token_hex(32)
            nonce = secrets.token_hex(16)
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
            hello_queue: queue.Queue = queue.Queue(maxsize=1)
            hello_key = f"__hello__:{generation}"
            self._pending[hello_key] = (generation, hello_queue)
            self._reader = threading.Thread(
                target=self._read_stdout,
                args=(process, generation, hello_key), daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._drain_stderr, args=(process,), daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()
            self._write({
                "operation": "init",
                "project_path": str(self.project),
                "nonce": nonce,
                "token": token,
                "config": self._config,
            }, generation=generation)
        try:
            hello = hello_queue.get(timeout=timeout)
        except queue.Empty as error:
            self._stop_worker()
            raise RuntimeError("agent worker readiness timed out") from error
        finally:
            with self._lock:
                self._pending.pop(hello_key, None)
        if hello.get("type") == "fatal":
            self._error = hello.get("error", "agent worker failed")
            self._stop_worker()
            raise RuntimeError(self._error)
        project_path = str(self.project)
        fingerprint = _fingerprint(self.project)
        expected = hmac.new(
            token.encode(), f"{nonce}\n{project_path}\n{fingerprint}".encode(), hashlib.sha256
        ).hexdigest()
        if (
            hello.get("type") != "hello"
            or hello.get("nonce") != nonce
            or hello.get("project_path") != project_path
            or hello.get("project_fingerprint") != fingerprint
            or hello.get("provider") != self._config["provider"]
            or hello.get("model") != self._config["model"]
            or hello.get("model_verified") is not True
            or hello.get("tool_calling_verified") is not True
            or hello.get("capabilities", {}).get("draft_only") is not True
            or set(hello.get("capabilities", {}).get("tools", [])) != _EXPECTED_TOOLS
            or (self.sdk_version is not None and hello.get("sdk_version") != self.sdk_version)
            or (
                self.sdk_module_file is not None
                and hello.get("sdk_module_file") != str(self.sdk_module_file)
            )
            or (
                self.sdk_version is not None
                and hello.get("expected_sdk_version") != "2.14.0"
            )
            or not hmac.compare_digest(str(hello.get("proof", "")), expected)
        ):
            self._stop_worker()
            raise RuntimeError("agent worker identity verification failed")
        with self._lock:
            self._nonce = nonce
            self._identity = hello
            self._error = None

    def _read_stdout(
        self, process: subprocess.Popen, generation: int, hello_key: str,
    ) -> None:
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = hello_key if message.get("type") in {"hello", "fatal"} else message.get("request_id")
                waiter = None
                with self._lock:
                    pending = self._pending.get(key)
                    if pending is not None and pending[0] == generation:
                        if message.get("type") != "event":
                            waiter = pending[1]
                        else:
                            session_entry = self._request_sessions.get(str(key))
                            session_id = (
                                session_entry[1]
                                if session_entry is not None and session_entry[0] == generation
                                else None
                            )
                            turn_id = session_entry[2] if session_id and session_entry else None
                            turn = self._turns.get((session_id, turn_id)) if session_id and turn_id else None
                            if turn is not None and turn["status"] == "running":
                                event = message.get("event") or {}
                                data = event.get("data") or {}
                                question_id = data.get("question_id") if isinstance(data, dict) else None
                                if event.get("type") == "ask_user" and isinstance(question_id, str) \
                                        and _QUESTION_ID.fullmatch(question_id):
                                    turn["pending_question"] = data
                                elif event.get("type") == "ask_user_timeout" and turn.get("pending_question") \
                                        and turn["pending_question"].get("question_id") == question_id:
                                    turn["pending_question"] = None
                                turn["events"].append({
                                    "seq": len(turn["events"]),
                                    "type": event.get("type", "status"),
                                    "data": event.get("data") or {},
                                })
                if waiter is not None:
                    waiter.put(message)
        finally:
            with self._lock:
                waiters = [
                    waiter for pending_generation, waiter in self._pending.values()
                    if pending_generation == generation
                ]
                for waiter in waiters:
                    try:
                        waiter.put_nowait({"type": "fatal", "error": "agent worker exited"})
                    except queue.Full:
                        pass

    def _drain_stderr(self, process: subprocess.Popen) -> None:
        if process is None or process.stderr is None:
            return
        for _line in process.stderr:
            pass

    def _write(self, payload: dict, *, generation: int | None = None) -> None:
        with self._write_lock:
            process = self._process
            if process is None or process.stdin is None or process.poll() is not None:
                raise RuntimeError("agent worker is unavailable")
            if generation is not None and generation != self._process_generation:
                raise RuntimeError("agent worker generation changed")
            try:
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()
            except (OSError, ValueError) as error:
                raise RuntimeError("agent worker is unavailable") from error

    def status(self) -> dict:
        try:
            self._start_worker()
        except Exception as error:
            self._error = str(error)[-1000:]
        return self.peek_status()

    def peek_status(self) -> dict:
        """Return current readiness without starting or probing a provider."""
        with self._lock:
            identity = self._identity or {}
            current_fingerprint = _fingerprint(self.project)
            project_verified = bool(
                identity and identity.get("project_fingerprint") == current_fingerprint
            )
            return {
                "configured": not self._config_load_error and self._key_file_valid(),
                "ready": bool(project_verified and self._process and self._process.poll() is None),
                "healthy": bool(self._identity and self._process and self._process.poll() is None),
                "project_verified": project_verified,
                "project_path": str(self.project),
                "project_fingerprint": identity.get("project_fingerprint"),
                "provider": self._config["provider"],
                "model": self._config["model"],
                "model_verified": bool(identity.get("model_verified")),
                "tool_calling_verified": bool(identity.get("tool_calling_verified")),
                "sdk_version": identity.get("sdk_version"),
                "sdk_module_file": identity.get("sdk_module_file"),
                "expected_sdk_version": identity.get("expected_sdk_version", "2.14.0"),
                "identity_nonce": self._nonce,
                "capabilities": identity.get("capabilities", {"draft_only": True, "tools": []}),
                **({"error": self._error} if self._error else {}),
            }

    def start(self, question: str, session_id: str, turn_id: str | None = None) -> dict:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question is required")
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session_id")
        resolved_turn_id = turn_id or uuid.uuid4().hex
        if not isinstance(resolved_turn_id, str) or not _TURN_ID.fullmatch(resolved_turn_id):
            raise ValueError("invalid turn_id")
        key = (session_id, resolved_turn_id)
        with self._lock:
            existing = self._turns.get(key)
            if existing is not None:
                return json.loads(json.dumps(existing))
            latest_id = self._latest_turn.get(session_id)
            current = self._turns.get((session_id, latest_id)) if latest_id else None
            if current and current["status"] == "running":
                raise ValueError("session already has an active turn")
            turn = {
                "session_id": session_id,
                "turn_id": resolved_turn_id,
                "status": "running",
                "events": [{"seq": 0, "type": "status", "data": {"state": "running"}}],
                "answer": None,
                "error": None,
                "pending_question": None,
            }
            self._turns[key] = turn
            self._latest_turn[session_id] = resolved_turn_id
        try:
            if self._identity and self._identity.get("project_fingerprint") != _fingerprint(self.project):
                with self._lock:
                    self._stop_worker()
            self._start_worker()
        except Exception as error:
            with self._lock:
                failed = self._turns[key]
                if failed["status"] == "running":
                    failed["status"] = "failed"
                    failed["error"] = str(error)[-1000:]
                    failed["events"].append({
                        "seq": len(failed["events"]), "type": "error",
                        "data": {"message": failed["error"]},
                    })
            raise
        with self._lock:
            generation = self._process_generation
            if generation is None:
                raise RuntimeError("agent worker generation is unavailable")
        threading.Thread(
            target=self._execute_turn,
            args=(question.strip(), session_id, resolved_turn_id, generation),
            daemon=True,
        ).start()
        created = self.get(session_id, resolved_turn_id)
        if created is None:
            raise RuntimeError("agent turn was not created")
        return created

    def _execute_turn(
        self, question: str, session_id: str, turn_id: str, generation: int,
    ) -> None:
        request_id = uuid.uuid4().hex
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = (generation, waiter)
            self._request_sessions[request_id] = (generation, session_id, turn_id)
        try:
            with self.execution_lock:
                with self._lock:
                    turn = self._turns.get((session_id, turn_id))
                    if turn is None or turn["status"] != "running":
                        return
                self._write({
                    "operation": "ask",
                    "request_id": request_id,
                    "session_id": session_id,
                    "question": question,
                }, generation=generation)
                # Time spent waiting for the user to answer ask_user does not
                # count toward the turn's active-time limit.
                remaining = float(TURN_ACTIVE_SECONDS)
                last = time.monotonic()
                while True:
                    try:
                        response = waiter.get(timeout=1)
                        break
                    except queue.Empty:
                        now = time.monotonic()
                        with self._lock:
                            current = self._turns.get((session_id, turn_id))
                            waiting = bool(current and current.get("pending_question"))
                        if not waiting:
                            remaining -= now - last
                        last = now
                        if remaining <= 0:
                            # The worker is still inside the old turn; stop it
                            # so the next turn does not queue behind it.
                            with self._lock:
                                if self._process_generation == generation:
                                    self._stop_worker()
                            raise TimeoutError("agent turn timed out") from None
            with self._lock:
                turn = self._turns[(session_id, turn_id)]
                if turn["status"] == "cancelled":
                    return
                turn["pending_question"] = None
                if response.get("type") == "response" and response.get("ok"):
                    result = response.get("result") or {}
                    turn["events"].append({
                        "seq": len(turn["events"]), "type": "answer", "data": {"text": result.get("answer", "")}
                    })
                    turn["answer"] = result.get("answer", "")
                    turn["status"] = "succeeded"
                else:
                    turn["status"] = "failed"
                    turn["error"] = str(response.get("error", "agent turn failed"))[-1000:]
                    turn["events"].append({
                        "seq": len(turn["events"]), "type": "error", "data": {"message": turn["error"]}
                    })
        except Exception as error:
            with self._lock:
                turn = self._turns.get((session_id, turn_id))
                if turn and turn["status"] != "cancelled":
                    turn["status"] = "failed"
                    turn["pending_question"] = None
                    turn["error"] = str(error)[-1000:]
                    turn["events"].append({
                        "seq": len(turn["events"]), "type": "error", "data": {"message": turn["error"]}
                    })
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
                self._request_sessions.pop(request_id, None)

    def get(self, session_id: str, turn_id: str | None = None) -> dict | None:
        with self._lock:
            resolved_turn_id = turn_id or self._latest_turn.get(session_id)
            turn = self._turns.get((session_id, resolved_turn_id)) if resolved_turn_id else None
            return json.loads(json.dumps(turn)) if turn else None

    def answer(self, session_id: str, turn_id: str, question_id: str, answer: str) -> dict:
        """Deliver the user's answer to the turn's pending ask_user question."""
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session_id")
        if not isinstance(turn_id, str) or not _TURN_ID.fullmatch(turn_id):
            raise ValueError("invalid turn_id")
        if not isinstance(question_id, str) or not _QUESTION_ID.fullmatch(question_id):
            raise ValueError("invalid question_id")
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 2000:
            raise ValueError("answer must be 1-2000 characters")
        answer = answer.strip()

        def waiting_turn() -> dict:
            turn = self._turns.get((session_id, turn_id))
            pending = turn.get("pending_question") if turn else None
            if turn is None or turn["status"] != "running" or not pending \
                    or pending.get("question_id") != question_id:
                raise ValueError("this agent question is no longer waiting for an answer")
            return turn

        # Serialize answers so two clicks cannot both reach the worker; the
        # question stays pending until the write succeeds, so a failed write
        # can be retried.
        with self._answer_lock:
            with self._lock:
                waiting_turn()
                request = next((
                    (request_id, generation)
                    for request_id, (generation, request_session, request_turn) in self._request_sessions.items()
                    if request_session == session_id and request_turn == turn_id
                ), None)
                if request is None:
                    raise RuntimeError("agent turn is not attached to a worker")
            self._write({
                "operation": "answer", "request_id": request[0],
                "question_id": question_id, "answer": answer,
            }, generation=request[1])
            with self._lock:
                # The worker may already have resumed and finished the turn,
                # but if the question timed out first the answer was dropped.
                turn = self._turns[(session_id, turn_id)]
                if any(event["type"] == "ask_user_timeout" and event["data"].get("question_id") == question_id
                       for event in turn["events"]):
                    raise ValueError("the answer arrived after the question timed out")
                if (turn.get("pending_question") or {}).get("question_id") == question_id:
                    turn["pending_question"] = None
                turn["events"].append({
                    "seq": len(turn["events"]), "type": "user_answer",
                    "data": {"question_id": question_id, "text": answer},
                })
        resolved = self.get(session_id, turn_id)
        if resolved is None:
            raise RuntimeError("agent turn was not found")
        return resolved

    def ask(
        self, question: str, session_id: str, timeout: float = 600,
        turn_id: str | None = None,
    ) -> dict:
        started = self.start(question, session_id, turn_id=turn_id)
        resolved_turn_id = started["turn_id"]
        if started["status"] != "running":
            return started
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            turn = self.get(session_id, resolved_turn_id)
            if turn and turn["status"] != "running":
                return turn
            time.sleep(0.1)
        self.cancel(session_id, resolved_turn_id)
        raise TimeoutError("agent turn timed out")

    def cancel(self, session_id: str, turn_id: str | None = None) -> dict | None:
        with self._lock:
            resolved_turn_id = turn_id or self._latest_turn.get(session_id)
            turn = self._turns.get((session_id, resolved_turn_id)) if resolved_turn_id else None
            if turn is None:
                return None
            if turn["status"] == "running":
                turn["status"] = "cancelled"
                turn["pending_question"] = None
                turn["events"].append({
                    "seq": len(turn["events"]), "type": "status", "data": {"state": "cancelled"}
                })
                self._stop_worker()
            return json.loads(json.dumps(turn))

    def _stop_worker(self) -> None:
        process = self._process
        self._identity = None
        self._nonce = None
        self._process_generation = None
        if process is None:
            return
        self._process = None
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            else:
                # The worker may have exited while a resistant descendant in
                # its process group remains alive.  Reap the whole owned group.
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

    def close(self) -> None:
        with self._lock:
            for turn in self._turns.values():
                if turn["status"] == "running":
                    turn["status"] = "cancelled"
                    turn["pending_question"] = None
            self._stop_worker()
