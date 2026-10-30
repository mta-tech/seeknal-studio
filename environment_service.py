#!/usr/bin/env python3
"""Safe, write-only management of a Seeknal project's local ``.env`` file."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Callable


MAX_ENV_BYTES = 300_000
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
ENV_ASSIGNMENT = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]{0,127})(?P<spacing>\s*)="
)
PROVIDER_ENV = re.compile(
    r"(?:OPENAI|ANTHROPIC|GEMINI|GOOGLE_API|MISTRAL|COHERE|TOGETHER|GROQ|SUMOPOD)",
    re.IGNORECASE,
)
PROVIDER_ENV_EXACT = {
    "FIRECRAWL_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HF_TOKEN",
    "IBA_ENGINE_API_KEY",
    "IBA_STORAGE_API_KEY",
}
PYTHON_CONTROL_ENV = {
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "VIRTUAL_ENV",
    "__PYVENV_LAUNCHER__",
}
HOST_CONTROL_ENV = {
    "SEEKNAL_GATEWAY_URL",
    "SEEKNAL_GATEWAY_TOKEN",
    "SEEKNAL_PROJECT_PATH",
}


def _content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def protected_reason(key: str) -> str | None:
    if key.startswith("SEEKNAL_STUDIO_") or key in HOST_CONTROL_ENV:
        return "Studio control variable"
    if key in PYTHON_CONTROL_ENV or key.startswith("PYTHON"):
        return "Python runtime variable"
    if key.startswith("SEEKNAL_ASK_") or key in PROVIDER_ENV_EXACT or PROVIDER_ENV.search(key):
        return "model provider variable"
    return None


def _quote(value: str) -> str:
    if not value:
        raise ValueError("environment values cannot be empty; use remove instead")
    if any(character in value for character in ("\0", "\r", "\n")):
        raise ValueError("environment values must be a single line")
    # python-dotenv expands ${NAME} even inside quotes. Reject it rather than
    # silently persisting a value that the SDK will interpret differently.
    if "${" in value:
        raise ValueError("environment values cannot contain ${...} expansion")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _has_closing_quote(raw_value: str) -> bool:
    quote = raw_value[0]
    escaped = False
    for character in raw_value[1:]:
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == quote:
            return True
    return False


def _ensure_single_line_assignments(text: str) -> None:
    for line in text.splitlines():
        match = ENV_ASSIGNMENT.match(line)
        if not match:
            continue
        raw_value = line[match.end():].lstrip()
        if raw_value.startswith(("'", '\"')) and not _has_closing_quote(raw_value):
            raise RuntimeError(
                "project .env contains a multiline quoted value; edit it outside Studio"
            )


class EnvironmentConflictError(RuntimeError):
    """The project environment changed after the browser loaded it."""


class EnvironmentBusyError(RuntimeError):
    """Active Studio work prevents a safe environment update."""


class EnvironmentService:
    def __init__(
        self,
        project: Path,
        *,
        busy: Callable[[], list[str]],
        runtime: Callable[[], dict[str, object]],
    ) -> None:
        self.project = project.resolve()
        self.path = self.project / ".env"
        self.busy = busy
        self.runtime = runtime
        self._lock = threading.Lock()

    def _read(self) -> bytes:
        if self.path.is_symlink():
            raise RuntimeError("project .env path is unsafe")
        if not self.path.exists():
            return b""
        if not self.path.is_file():
            raise RuntimeError("project .env path is unsafe")
        content = self.path.read_bytes()
        if len(content) > MAX_ENV_BYTES:
            raise RuntimeError("project .env exceeds 300 KB")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RuntimeError("project .env must be UTF-8") from error
        return content

    @staticmethod
    def _entries(content: bytes) -> list[dict[str, Any]]:
        text = content.decode("utf-8")
        entries: dict[str, dict[str, Any]] = {}
        for line in text.splitlines():
            match = ENV_ASSIGNMENT.match(line)
            if not match:
                continue
            key = match.group("key")
            raw_value = line[match.end():].strip()
            reason = protected_reason(key)
            entries[key] = {
                "key": key,
                "has_value": bool(raw_value and raw_value not in {"''", '\"\"'}),
                "editable": reason is None,
                "removable": True,
                **({"protected_reason": reason} if reason else {}),
            }
        return [entries[key] for key in sorted(entries)]

    def status(self) -> dict[str, Any]:
        content = self._read()
        exists = self.path.exists()
        permissions = f"{self.path.stat().st_mode & 0o777:04o}" if exists else None
        return {
            "project": {"name": self.project.name, "path": str(self.project)},
            "environment": {
                "path": str(self.path),
                "exists": exists,
                "content_hash": _content_hash(content),
                "entries": self._entries(content),
                "permissions": permissions,
            },
            "runtime": self.runtime(),
            "busy": self.busy(),
            "reload": {
                "required": False,
                "message": (
                    "Saved values apply to new Seeknal CLI processes. "
                    "A running REPL is reloaded after a successful update."
                ),
            },
        }

    def update(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("environment update must be an object")
        updates = payload.get("updates", {})
        remove = payload.get("remove", [])
        expected_hash = payload.get("content_hash")
        if not isinstance(updates, dict) or not isinstance(remove, list):
            raise ValueError("updates must be an object and remove must be a list")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            raise ValueError("a valid content_hash is required")
        if len(updates) + len(remove) > 200:
            raise ValueError("too many environment changes")

        normalized_updates: dict[str, str] = {}
        for raw_key, raw_value in updates.items():
            key = str(raw_key)
            if ENV_KEY.fullmatch(key) is None:
                raise ValueError(f"invalid environment key: {key}")
            reason = protected_reason(key)
            if reason:
                raise ValueError(f"cannot override {reason}: {key}")
            if not isinstance(raw_value, str):
                raise ValueError(f"environment value must be a string: {key}")
            normalized_updates[key] = _quote(raw_value)

        normalized_remove: set[str] = set()
        for raw_key in remove:
            if not isinstance(raw_key, str) or ENV_KEY.fullmatch(raw_key) is None:
                raise ValueError("remove contains an invalid environment key")
            normalized_remove.add(raw_key)
        overlap = normalized_remove.intersection(normalized_updates)
        if overlap:
            raise ValueError(f"environment key cannot be updated and removed: {sorted(overlap)[0]}")
        if not normalized_updates and not normalized_remove:
            raise ValueError("at least one environment change is required")

        with self._lock:
            active = self.busy()
            if active:
                raise EnvironmentBusyError(
                    f"finish active work before changing the environment: {', '.join(active)}"
                )
            content = self._read()
            if _content_hash(content) != expected_hash:
                raise EnvironmentConflictError("project .env changed; reload before saving")
            text = content.decode("utf-8")
            _ensure_single_line_assignments(text)
            retained: list[str] = []
            replaced: set[str] = set()
            for line in text.splitlines():
                match = ENV_ASSIGNMENT.match(line)
                key = match.group("key") if match else None
                if key in normalized_remove:
                    continue
                if key in normalized_updates:
                    if key not in replaced:
                        retained.append(f"{key}={normalized_updates[key]}")
                        replaced.add(key)
                    continue
                retained.append(line)
            for key in sorted(normalized_updates.keys() - replaced):
                retained.append(f"{key}={normalized_updates[key]}")
            rendered = ("\n".join(retained).rstrip("\n") + "\n").encode("utf-8") if retained else b""
            if len(rendered) > MAX_ENV_BYTES:
                raise ValueError("project .env exceeds 300 KB")
            self._atomic_write(rendered)
        result = self.status()
        result["saved"] = True
        return result

    def _atomic_write(self, content: bytes) -> None:
        if self.path.is_symlink() or self.project.is_symlink():
            raise RuntimeError("project .env path is unsafe")
        descriptor, name = tempfile.mkstemp(prefix=".env.studio-", dir=self.project)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
