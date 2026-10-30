#!/usr/bin/env python3
"""Project discovery, selection persistence, and launcher switch requests."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from typing import Callable, Optional, Union

import fcntl

from remote_workspace import RemoteTarget, probe as probe_remote

SwitchTarget = Union[Path, RemoteTarget]


_CONFIG_LOCKS_GUARD = threading.Lock()
_CONFIG_LOCKS: dict[Path, threading.RLock] = {}
_WORKSPACE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_WORKSPACE_NAME_LIMIT = 100


def default_config_path() -> Path:
    override = os.environ.get("SEEKNAL_STUDIO_PROJECTS_FILE")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Seeknal Studio" / "projects.json"
    return Path.home() / ".config" / "seeknal-studio" / "projects.json"


def validate_project(value: object) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("project path is required")
    candidate = Path(value).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("project directory does not exist or is unsafe")
    project = candidate.resolve()
    definition = project / "seeknal_project.yml"
    if definition.is_symlink() or not definition.is_file():
        raise ValueError("selected directory is not a Seeknal project")
    return project


class ProjectService:
    def __init__(
        self,
        current: Optional[Path],
        *,
        busy: Callable[[], list[str]],
        config_path: Path | None = None,
        control_path: Path | None = None,
        control_nonce: str | None = None,
        platform: str | None = None,
        remote: RemoteTarget | None = None,
        notice: str | None = None,
        remote_probe: Callable[[RemoteTarget], RemoteTarget] = probe_remote,
    ) -> None:
        if (current is None) == (remote is None):
            raise ValueError("exactly one of a local project or a remote workspace is current")
        # ``current`` is the open local project; ``remote`` the open SSH workspace.
        self.current = current.resolve() if current is not None else None
        self.remote = remote
        self.notice = notice
        self.remote_probe = remote_probe
        self.instance = uuid.uuid4().hex
        self.busy = busy
        self.config_path = (config_path or default_config_path()).expanduser()
        self.control_path = control_path
        self.control_nonce = control_nonce
        self.platform = platform or sys.platform
        self._picker_lock = threading.Lock()
        self._picker: subprocess.Popen[str] | None = None
        self._switch_lock = threading.Lock()
        self._switch_pending = False
        self._closed = False

    @property
    def managed(self) -> bool:
        return bool(self.control_path and self.control_nonce)

    @property
    def switch_pending(self) -> bool:
        with self._switch_lock:
            return self._switch_pending

    @staticmethod
    def _entry(path: Path) -> dict[str, str]:
        return {"path": str(path), "name": path.name}

    def _load(self) -> dict:
        try:
            if self.config_path.is_symlink() or not self.config_path.is_file():
                return {}
            if self.config_path.stat().st_mode & 0o077:
                return {}
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _load_for_write(self) -> dict:
        if self.config_path.is_symlink():
            raise RuntimeError("private Studio configuration path is unsafe")
        if not self.config_path.exists():
            return {}
        try:
            if not self.config_path.is_file():
                raise RuntimeError("private Studio configuration path is unsafe")
            if self.config_path.stat().st_mode & 0o077:
                raise RuntimeError("private Studio configuration permissions must be owner-only")
            value = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("private Studio configuration is unreadable") from error
        if not isinstance(value, dict):
            raise RuntimeError("private Studio configuration must be a JSON object")
        return value

    @contextmanager
    def _locked_config(self):
        canonical = self.config_path.absolute()
        with _CONFIG_LOCKS_GUARD:
            lock = _CONFIG_LOCKS.setdefault(canonical, threading.RLock())
        with lock:
            if self.config_path.parent.is_symlink():
                raise RuntimeError("private Studio state path is unsafe")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.config_path.with_name(f".{self.config_path.name}.lock")
            if lock_path.is_symlink():
                raise RuntimeError("private Studio configuration lock is unsafe")
            flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    @staticmethod
    def _workspace_id(value: object, *, required: bool = True) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str) or not _WORKSPACE_ID.fullmatch(value):
            raise ValueError("workspace id is invalid")
        return value

    @staticmethod
    def _workspace_name(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("workspace name is required")
        name = value.strip()
        if (
            not name
            or len(name) > _WORKSPACE_NAME_LIMIT
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise ValueError("workspace name must be 1-100 printable characters")
        return name

    @staticmethod
    def _workspace_values(settings: dict, *, strict: bool) -> list[dict]:
        raw = settings.get("workspaces", [])
        if not isinstance(raw, list):
            if strict:
                raise RuntimeError("private Studio workspaces configuration must be a list")
            return []
        values: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                if strict:
                    raise RuntimeError("private Studio workspace entry must be an object")
                continue
            workspace_id = entry.get("id")
            name = entry.get("name")
            path = entry.get("path")
            remote_valid = True
            if entry.get("kind", "local") == "ssh":
                try:
                    RemoteTarget.checked(entry.get("host"), path, entry.get("ssh_port"))
                except ValueError:
                    remote_valid = False
            elif entry.get("kind", "local") != "local":
                remote_valid = False
            valid = remote_valid and (
                isinstance(workspace_id, str)
                and _WORKSPACE_ID.fullmatch(workspace_id) is not None
                and isinstance(name, str)
                and bool(name.strip())
                and len(name.strip()) <= _WORKSPACE_NAME_LIMIT
                and not any(ord(character) < 32 or ord(character) == 127 for character in name)
                and isinstance(path, str)
                and bool(path.strip())
                and "\0" not in path
            )
            if not valid:
                if strict:
                    raise RuntimeError("private Studio workspace entry is malformed")
                continue
            values.append(dict(entry))
        return values

    @staticmethod
    def _saved_canonical(value: str) -> Path:
        return Path(value).expanduser().resolve(strict=False)

    @staticmethod
    def _remote_target(entry: dict) -> RemoteTarget | None:
        if entry.get("kind") != "ssh":
            return None
        return RemoteTarget.checked(
            entry.get("host"), entry["path"], entry.get("ssh_port"),
            workspace_id=entry["id"], name=entry["name"],
        )

    def _switch_reason(self, current: bool, active: list[str], pending: bool) -> str | None:
        if current:
            return "Workspace is currently open"
        if not self.managed:
            return "Project switching requires the managed desktop launcher"
        if active:
            return f"Finish active work before switching projects: {', '.join(active)}"
        if pending:
            return "A project switch is already pending"
        return None

    def _remote_status(self, entry: dict, target: RemoteTarget, active: list[str]) -> dict:
        # Remote availability is checked when saving and opening, not on every
        # listing, so a slow or sleeping SSH host never stalls the UI.
        current = self.remote is not None and self.remote.same_location(target)
        pending = self.switch_pending
        return {
            "id": entry["id"],
            "name": entry["name"],
            "path": target.path,
            "kind": "ssh",
            "host": target.host,
            "ssh_port": target.ssh_port,
            "location": target.location,
            "current": current,
            "available": True,
            "reason": self._switch_reason(current, active, pending),
            "can_switch": self.managed and not current and not active and not pending,
        }

    def _workspace_status(self, entry: dict, active: list[str]) -> dict:
        target = self._remote_target(entry)
        if target is not None:
            return self._remote_status(entry, target, active)
        saved_path = entry["path"]
        canonical = self._saved_canonical(saved_path)
        available = True
        reason = None
        try:
            project = validate_project(saved_path)
        except ValueError as error:
            available = False
            project = canonical
            reason = str(error)
        current = project == self.current
        pending = self.switch_pending
        can_switch = available and self.managed and not current and not active and not pending
        if reason is None:
            reason = self._switch_reason(current, active, pending)
        return {
            "id": entry["id"],
            "name": entry["name"],
            "path": str(canonical),
            "kind": "local",
            "current": current,
            "available": available,
            "reason": reason,
            "can_switch": can_switch,
        }

    def workspaces_status(self) -> dict:
        entries = self._workspace_values(self._load(), strict=False)
        active = self.busy()
        rendered = [self._workspace_status(entry, active) for entry in entries]
        current = next(
            (
                entry
                for entry in rendered
                if entry["available"] and entry["current"]
            ),
            None,
        )
        if current is None:
            current = {
                "id": None,
                "name": None,
                "path": str(self.current) if self.current is not None else None,
                "kind": "local",
                "current": True,
                "available": True,
                "reason": "Current project is not saved as a workspace",
                "can_switch": False,
            }
            if self.remote is not None:
                current.update(
                    name=self.remote.name, path=self.remote.path, kind="ssh",
                    host=self.remote.host, ssh_port=self.remote.ssh_port,
                    location=self.remote.location,
                )
        return {
            "current": current,
            "entries": rendered,
            "managed": self.managed,
            "can_switch": self.managed and not active and not self.switch_pending,
            "instance": self.instance,
            "notice": self.notice,
        }

    def probe_workspace_request(self, host: object, path: object, ssh_port: object = None) -> RemoteTarget | None:
        """Check a remote registration over SSH; call before taking work locks."""

        if host in (None, ""):
            return None
        return self.remote_probe(RemoteTarget.checked(host, path, ssh_port))

    def probe_workspace(self, workspace_id: object) -> RemoteTarget | None:
        """Check a saved remote workspace over SSH; call before taking work locks."""

        checked_id = self._workspace_id(workspace_id)
        entry = next(
            (item for item in self._workspace_values(self._load(), strict=False) if item["id"] == checked_id),
            None,
        )
        target = self._remote_target(entry) if entry is not None else None
        return self.remote_probe(target) if target is not None else None

    def save_workspace(
        self, name: object, path: object, workspace_id: object = None,
        host: object = None, ssh_port: object = None, probed: RemoteTarget | None = None,
    ) -> dict:
        checked_name = self._workspace_name(name)
        checked_id = self._workspace_id(workspace_id, required=False)
        if host not in (None, ""):
            return self._save_remote_workspace(checked_name, checked_id, host, path, ssh_port, probed)
        if ssh_port not in (None, ""):
            raise ValueError("SSH port requires an SSH host")
        project = validate_project(path)
        with self._locked_config():
            settings = self._load_for_write()
            workspaces = self._workspace_values(settings, strict=True)
            target_id = checked_id or uuid.uuid4().hex
            existing = next((entry for entry in workspaces if entry["id"] == target_id), None)
            if checked_id is not None and existing is None:
                raise ValueError("workspace not found")
            for entry in workspaces:
                if (
                    entry["id"] != target_id
                    and entry.get("kind", "local") == "local"
                    and self._saved_canonical(entry["path"]) == project
                ):
                    raise ValueError("project directory is already saved as another workspace")
            replacement = {
                key: value
                for key, value in (existing or {}).items()
                if key not in {"kind", "host", "ssh_port"}
            }
            replacement.update(id=target_id, name=checked_name, path=str(project))
            if existing is None:
                workspaces.append(replacement)
            else:
                workspaces[workspaces.index(existing)] = replacement
            settings["workspaces"] = workspaces
            self._atomic_json(self.config_path, settings, mode=0o600)
        return self._workspace_status(replacement, self.busy())

    def _save_remote_workspace(
        self, name: str, checked_id: str | None, host: object, path: object, ssh_port: object,
        probed: RemoteTarget | None,
    ) -> dict:
        requested = RemoteTarget.checked(host, path, ssh_port)
        # Probe outside the configuration lock: SSH may take seconds.
        if probed is not None and (probed.host, probed.ssh_port) == (requested.host, requested.ssh_port):
            target = probed
        else:
            target = self.remote_probe(requested)
        with self._locked_config():
            settings = self._load_for_write()
            workspaces = self._workspace_values(settings, strict=True)
            target_id = checked_id or uuid.uuid4().hex
            existing = next((entry for entry in workspaces if entry["id"] == target_id), None)
            if checked_id is not None and existing is None:
                raise ValueError("workspace not found")
            for entry in workspaces:
                other = self._remote_target(entry)
                if entry["id"] != target_id and other is not None and other.same_location(target):
                    raise ValueError("remote project is already saved as another workspace")
            replacement = {
                **(existing or {}),
                "id": target_id,
                "name": name,
                "kind": "ssh",
                "host": target.host,
                "path": target.path,
            }
            if target.ssh_port:
                replacement["ssh_port"] = target.ssh_port
            else:
                replacement.pop("ssh_port", None)
            if existing is None:
                workspaces.append(replacement)
            else:
                workspaces[workspaces.index(existing)] = replacement
            settings["workspaces"] = workspaces
            self._atomic_json(self.config_path, settings, mode=0o600)
        return self._workspace_status(replacement, self.busy())

    def remove_workspace(self, workspace_id: object) -> dict:
        checked_id = self._workspace_id(workspace_id)
        with self._locked_config():
            settings = self._load_for_write()
            workspaces = self._workspace_values(settings, strict=True)
            remaining = [entry for entry in workspaces if entry["id"] != checked_id]
            if len(remaining) == len(workspaces):
                raise ValueError("workspace not found")
            settings["workspaces"] = remaining
            self._atomic_json(self.config_path, settings, mode=0o600)
        return {"removed": True, "id": checked_id}

    def prepare_workspace_switch(
        self, workspace_id: object, probed: RemoteTarget | None = None,
    ) -> tuple[SwitchTarget, dict]:
        checked_id = self._workspace_id(workspace_id)
        workspace = next(
            (
                entry
                for entry in self._workspace_values(self._load(), strict=False)
                if entry["id"] == checked_id
            ),
            None,
        )
        if workspace is None:
            raise ValueError("workspace not found")
        remote = self._remote_target(workspace)
        if remote is None:
            project = self.prepare_switch(workspace["path"])
            return project, self._workspace_status(workspace, [])
        if self.remote is not None and self.remote.same_location(remote):
            raise ValueError("selected project is already open")
        self._check_switch_allowed()
        if probed is None or probed.workspace_id != remote.workspace_id or not probed.same_location(remote):
            probed = self.remote_probe(remote)
        return probed, self._workspace_status(workspace, [])

    def _current_entry(self) -> dict:
        if self.current is not None:
            return self._entry(self.current)
        assert self.remote is not None
        return {
            "path": self.remote.path,
            "name": self.remote.name or Path(self.remote.path).name,
            "kind": "ssh",
            "host": self.remote.host,
            "location": self.remote.location,
        }

    def status(self) -> dict:
        recent = []
        seen = {self.current}
        for raw in self._load().get("recent", []):
            try:
                path = validate_project(raw)
            except ValueError:
                continue
            if path not in seen:
                seen.add(path)
                recent.append(self._entry(path))
        return {
            "current": self._current_entry(),
            "recent": recent[:8],
            "can_switch": self.managed and not self.switch_pending,
            "managed": self.managed,
        }

    def browse(self) -> dict:
        if self.platform != "darwin":
            return {"path": None, "cancelled": False, "supported": False}
        try:
            with self._picker_lock:
                if self._closed:
                    raise RuntimeError("project service is closed")
                if self._picker is not None and self._picker.poll() is None:
                    raise RuntimeError("native folder picker is already open")
                process = subprocess.Popen(
                    self._picker_command(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=False,
                    start_new_session=True,
                )
                self._picker = process
            try:
                stdout, stderr = process.communicate(timeout=300)
            except subprocess.TimeoutExpired as error:
                self._terminate_picker(process)
                raise RuntimeError("native folder picker timed out") from error
            finally:
                with self._picker_lock:
                    if self._picker is process:
                        self._picker = None
        except OSError as error:
            raise RuntimeError("native folder picker failed") from error
        if process.returncode:
            if "cancel" in stderr.lower():
                return {"path": None, "cancelled": True, "supported": True}
            raise RuntimeError("native folder picker failed")
        path = stdout.strip().rstrip("/")
        return {"path": path or None, "cancelled": not bool(path), "supported": True}

    @staticmethod
    def _picker_command() -> list[str]:
        return [
            "osascript",
            "-e",
            'POSIX path of (choose folder with prompt "Open Seeknal Project Directory")',
        ]

    @staticmethod
    def _terminate_picker(process: subprocess.Popen[str]) -> None:
        process_group = process.pid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=2)

    def close(self) -> None:
        with self._picker_lock:
            self._closed = True
            process = self._picker
        if process is not None:
            self._terminate_picker(process)

    def _check_switch_allowed(self) -> None:
        active = self.busy()
        if active:
            raise RuntimeError(f"finish active work before switching projects: {', '.join(active)}")
        if not self.managed:
            raise RuntimeError("project switching requires the managed desktop launcher")

    def prepare_switch(self, value: object) -> Path:
        project = validate_project(value)
        if project == self.current:
            raise ValueError("selected project is already open")
        self._check_switch_allowed()
        return project

    def queue_switch(self, project: SwitchTarget) -> None:
        assert self.control_path is not None and self.control_nonce is not None
        with self._switch_lock:
            if self._switch_pending:
                raise RuntimeError("a project switch is already pending")
            if isinstance(project, RemoteTarget):
                # Remote workspaces are not remembered as the startup project:
                # launching Studio must never depend on network reachability.
                request = {"nonce": self.control_nonce, "remote": project.to_json()}
            else:
                self._persist(project)
                request = {"nonce": self.control_nonce, "project": str(project)}
            self._atomic_json(self.control_path, request, mode=0o600)
            self._switch_pending = True

    def _persist(self, project: Path) -> None:
        with self._locked_config():
            settings = self._load_for_write()
            recent = settings.get("recent", [])
            if not isinstance(recent, list):
                raise RuntimeError("private Studio recent projects configuration must be a list")
            paths = [project] + ([self.current] if self.current is not None else [])
            for raw in recent:
                try:
                    path = validate_project(raw)
                except ValueError:
                    continue
                if path not in paths:
                    paths.append(path)
            settings.update(last=str(project), recent=[str(path) for path in paths[:8]])
            self._atomic_json(self.config_path, settings, mode=0o600)

    @staticmethod
    def _atomic_json(path: Path, value: dict, *, mode: int) -> None:
        if path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError("private Studio state path is unsafe")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, mode)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


def new_control_nonce() -> str:
    return secrets.token_hex(32)
