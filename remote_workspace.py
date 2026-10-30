#!/usr/bin/env python3
"""SSH remote workspaces: validation, probing, bootstrap, and tunnelled sessions.

A remote workspace runs the unchanged Studio backend on the SSH host, bound to
the remote loopback interface and protected by a per-session access token.  The
local launcher reaches it only through an SSH local port forward.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import http.client
import io
import json
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
from typing import IO, Optional


ROOT = Path(__file__).resolve().parent
TOKEN_HEADER = "X-Seeknal-Studio-Token"
REMOTE_CACHE = "~/.cache/seeknal-studio"
READY_MARKER = b"SEEKNAL_STUDIO_READY "
BUSY_MARKER = b"SEEKNAL_STUDIO_BUSY "
PROJECT_BUSY_EXIT = 5
HEARTBEAT_SECONDS = 10
RUNTIME_SETUP_SECONDS = 900
# Files the remote backend needs. Static UI files are included so the remote
# service remains a complete Studio backend, although the local gateway serves
# the UI the user actually sees.
SERVER_FILES = (
    "server.py", "engine_bridge.py", "agent_runtime.py", "agent_worker.py",
    "project_service.py", "environment_service.py", "repl_service.py", "repl_worker.py",
    "runtime_resolver.py", "remote_workspace.py", "remote_host.py", "studio_lint.py",
    "index.html", "styles.css", "app.js", "requirements-runtime.lock.txt",
)
_HOST = re.compile(r"(?:[A-Za-z0-9._-]{1,64}@)?[A-Za-z0-9][A-Za-z0-9._-]{0,252}")
_PATH_LIMIT = 4096

_PROBE_SCRIPT = r"""
import json, os, sys
path = os.path.expanduser(sys.argv[1])
try:
    if not os.path.isabs(path):
        raise ValueError("remote project path must be absolute")
    if os.path.islink(path) or not os.path.isdir(path):
        raise ValueError("remote project directory does not exist or is unsafe")
    real = os.path.realpath(path)
    definition = os.path.join(real, "seeknal_project.yml")
    if os.path.islink(definition) or not os.path.isfile(definition):
        raise ValueError("remote directory is not a Seeknal project")
    print(json.dumps({"ok": True, "path": real, "name": os.path.basename(real)}))
except ValueError as error:
    print(json.dumps({"ok": False, "error": str(error)}))
"""

_INSTALL_SCRIPT = r"""
import io, os, shutil, sys, tarfile, tempfile
target = os.path.expanduser(sys.argv[1])
allowed = set(sys.argv[2].split(","))
payload = sys.stdin.buffer.read()
if os.path.isfile(os.path.join(target, ".complete")):
    print("present")
    raise SystemExit(0)
parent = os.path.dirname(target)
os.makedirs(parent, mode=0o700, exist_ok=True)
staging = tempfile.mkdtemp(prefix=".install-", dir=parent)
try:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or member.name not in allowed:
                raise SystemExit("unexpected file in Studio server bundle: " + member.name)
            source = archive.extractfile(member)
            with open(os.path.join(staging, member.name), "wb") as stream:
                stream.write(source.read())
    open(os.path.join(staging, ".complete"), "w").close()
    try:
        os.rename(staging, target)
    except OSError:
        if not os.path.isfile(os.path.join(target, ".complete")):
            raise
    print("installed")
finally:
    shutil.rmtree(staging, ignore_errors=True)
"""


def validate_host(value: object) -> str:
    if not isinstance(value, str) or not _HOST.fullmatch(value.strip()):
        raise ValueError("SSH host must look like user@host or an SSH config alias")
    return value.strip()


def validate_ssh_port(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("SSH port must be between 1 and 65535")
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("SSH port must be between 1 and 65535") from error
    if str(port) != str(value).strip() or not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    return port


def validate_remote_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("remote project path is required")
    path = value.strip()
    if (
        len(path) > _PATH_LIMIT
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or not (path.startswith("/") or path == "~" or path.startswith("~/"))
    ):
        raise ValueError("remote project path must be absolute (/… or ~/…)")
    return path


@dataclass(frozen=True)
class RemoteTarget:
    host: str
    path: str
    ssh_port: Optional[int] = None
    workspace_id: Optional[str] = None
    name: Optional[str] = None

    @classmethod
    def checked(cls, host: object, path: object, ssh_port: object = None, **extra) -> "RemoteTarget":
        return cls(validate_host(host), validate_remote_path(path), validate_ssh_port(ssh_port), **extra)

    @property
    def location(self) -> str:
        port = f":{self.ssh_port}" if self.ssh_port else ""
        return f"{self.host}{port}:{self.path}"

    def same_location(self, other: "RemoteTarget") -> bool:
        # An explicit port 22 and the default port name the same location.
        def port(value: Optional[int]) -> Optional[int]:
            return None if value == 22 else value
        return (self.host, port(self.ssh_port), self.path) == (other.host, port(other.ssh_port), other.path)

    def to_json(self) -> dict:
        return {
            "host": self.host, "path": self.path, "ssh_port": self.ssh_port,
            "workspace_id": self.workspace_id, "name": self.name,
        }

    @classmethod
    def from_json(cls, value: object) -> "RemoteTarget":
        if not isinstance(value, dict):
            raise ValueError("remote workspace descriptor is invalid")
        workspace_id = value.get("workspace_id")
        name = value.get("name")
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise ValueError("remote workspace descriptor is invalid")
        if name is not None and not isinstance(name, str):
            raise ValueError("remote workspace descriptor is invalid")
        return cls.checked(
            value.get("host"), value.get("path"), value.get("ssh_port"),
            workspace_id=workspace_id, name=name,
        )


def ssh_command(target: RemoteTarget, remote_command: Optional[str] = None, *options: str) -> list[str]:
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
    ]
    if target.ssh_port:
        command += ["-p", str(target.ssh_port)]
    command += [*options, "--", target.host]
    if remote_command is not None:
        command.append(remote_command)
    return command


def _ssh_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    detail = lines[-1] if lines else "no diagnostic output"
    return detail[:300]


def _run(target: RemoteTarget, remote_command: str, *, data: bytes = b"", timeout: float) -> subprocess.CompletedProcess:
    try:
        completed = subprocess.run(
            ssh_command(target, remote_command), input=data, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"SSH command to {target.host} timed out") from error
    except OSError as error:
        raise RuntimeError("ssh client is unavailable") from error
    if completed.returncode == 255:
        raise RuntimeError(f"SSH connection to {target.host} failed: {_ssh_error(completed.stderr.decode(errors='replace'))}")
    return completed


def probe(target: RemoteTarget, *, timeout: float = 25) -> RemoteTarget:
    """Confirm the remote path is a Seeknal project and return its canonical form."""

    try:
        completed = _run(
            target, f"python3 - {shlex.quote(target.path)}",
            data=_PROBE_SCRIPT.encode(), timeout=timeout,
        )
    except RuntimeError as error:
        raise ValueError(str(error)) from error
    try:
        result = json.loads(completed.stdout.decode().strip().splitlines()[-1])
    except (IndexError, UnicodeError, json.JSONDecodeError) as error:
        detail = _ssh_error(completed.stderr.decode(errors="replace"))
        raise ValueError(f"remote host needs python3 to open a workspace ({detail})") from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError(str(result.get("error") if isinstance(result, dict) else "remote project check failed"))
    return RemoteTarget(
        target.host, validate_remote_path(result.get("path")), target.ssh_port,
        workspace_id=target.workspace_id, name=target.name,
    )


def server_bundle(root: Path = ROOT) -> tuple[str, bytes]:
    """Return a deterministic content hash and gzip tarball of the backend files."""

    digest = hashlib.sha256()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name in SERVER_FILES:
            content = (root / name).read_bytes()
            digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return digest.hexdigest(), buffer.getvalue()


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("127.0.0.1", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def unix_request(
    socket_path: str, method: str, path: str, headers: dict[str, str],
    body: Optional[bytes] = None, timeout: float = 120,
) -> tuple[int, str, bytes]:
    """HTTP over the private Unix socket that SSH forwards to the remote backend."""

    connection = _UnixHTTPConnection(socket_path, timeout)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", "application/json"), response.read()
    finally:
        connection.close()


def _terminate(process: Optional[subprocess.Popen], timeout: float = 8) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class RemoteSession:
    """One remote backend plus its local port forward."""

    def __init__(self, target: RemoteTarget, *, log: IO[str], socket_dir: Path, root: Path = ROOT) -> None:
        self.target = target
        self.log = log
        self.root = root
        self.token = secrets.token_urlsafe(32)
        # A Unix socket inside the launcher's private 0700 directory: unlike a
        # loopback TCP port, no other local user can connect to or pre-bind it.
        self._short_dir: Optional[str] = None
        socket_name = f"remote-{secrets.token_hex(4)}.sock"
        if len(str(socket_dir / socket_name)) > 100:
            # Unix socket paths are limited to ~104 bytes on macOS; a long
            # TMPDIR needs a short private 0700 directory instead.
            self._short_dir = tempfile.mkdtemp(prefix="seeknal-", dir="/tmp")
            socket_dir = Path(self._short_dir)
        self.socket_path = str(socket_dir / socket_name)
        self.remote_port: Optional[int] = None
        self.remote_log: Optional[str] = None
        self._host: Optional[subprocess.Popen] = None
        self._tunnel: Optional[subprocess.Popen] = None
        self._stopping = threading.Event()

    def _note(self, message: str) -> None:
        print(f"[remote {self.target.host}] {message}", file=self.log, flush=True)

    def start(self) -> None:
        try:
            directory = self._install()
            python = self._ensure_runtime(directory)
            self._serve(directory, python)
            self._forward()
        except BaseException:
            self.stop()
            raise

    def _install(self) -> str:
        digest, payload = server_bundle(self.root)
        directory = f"{REMOTE_CACHE}/server/{digest[:20]}"
        # A single-line base64 loader keeps the command valid for any login
        # shell (sh, bash, zsh, csh/tcsh) on the SSH host.
        loader = "import base64;exec(base64.b64decode('%s'))" % base64.b64encode(_INSTALL_SCRIPT.encode()).decode()
        command = " ".join([
            "python3", "-c", shlex.quote(loader),
            shlex.quote(directory), shlex.quote(",".join(SERVER_FILES)),
        ])
        completed = _run(self.target, command, data=payload, timeout=120)
        if completed.returncode:
            raise RuntimeError(f"Studio backend upload failed: {_ssh_error(completed.stderr.decode(errors='replace'))}")
        self._note(f"backend {digest[:20]} {completed.stdout.decode().strip()}")
        return directory

    def _ensure_runtime(self, directory: str) -> str:
        self._note("ensuring the pinned Seeknal runtime (first use downloads packages)")
        command = (
            f"cd {directory} && python3 remote_host.py ensure-runtime"
        )
        completed = _run(self.target, command, timeout=RUNTIME_SETUP_SECONDS)
        stderr = completed.stderr.decode(errors="replace")
        if stderr.strip():
            self.log.write(stderr)
            self.log.flush()
        if completed.returncode:
            raise RuntimeError(f"remote Seeknal runtime setup failed: {_ssh_error(stderr)}")
        try:
            result = json.loads(completed.stdout.decode().strip().splitlines()[-1])
            python = result["python"]
            version = result["seeknal_version"]
        except (IndexError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("remote Seeknal runtime setup returned no runtime identity") from error
        if not isinstance(python, str) or not python.startswith("/"):
            raise RuntimeError("remote Seeknal runtime path is invalid")
        self._note(f"runtime {python} (Seeknal {version})")
        return python

    def _serve(self, directory: str, python: str) -> None:
        command = (
            f"cd {directory} && exec python3 remote_host.py serve"
            f" --project {shlex.quote(self.target.path)} --runtime-python {shlex.quote(python)}"
        )
        self._host = subprocess.Popen(
            ssh_command(self.target, command),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
        )
        assert self._host.stdin is not None and self._host.stdout is not None
        # The token travels on the encrypted channel, never on a command line.
        self._host.stdin.write(self.token.encode() + b"\n")
        self._host.stdin.flush()
        # Heartbeats start now: the host's silence timer runs during startup too.
        threading.Thread(target=self._heartbeat, name="seeknal-ssh-heartbeat", daemon=True).start()
        holder: dict = {}

        def busy(record: dict) -> None:
            holder.update(record)
            self._note(f"another Studio session has this project open ({_describe_holder(record)}); waiting for it to close")

        line = self._ready_line(self._host.stdout, 150, busy)
        if not line:
            try:
                # stdout closes just before ssh exits; wait briefly for its code.
                code = self._host.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = None
            if code == PROJECT_BUSY_EXIT:
                raise RuntimeError(
                    f"Another Studio session already has this remote project open ({_describe_holder(holder)}). "
                    "Close it there, or wait about a minute after it disconnects, then try again."
                )
            raise RuntimeError(
                "remote Studio backend did not start"
                + (f" (exit {code})" if code is not None else " within 150 seconds")
                + "; see the service log"
            )
        try:
            ready = json.loads(line[len(READY_MARKER):])
            self.remote_port = int(ready["port"])
            self.remote_log = str(ready.get("log") or "")
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("remote Studio backend sent an invalid readiness record") from error
        self._note(f"backend listening on remote 127.0.0.1:{self.remote_port}; log {self.remote_log}")

    @staticmethod
    def _ready_line(stream: IO[bytes], timeout: float, on_busy=None) -> Optional[bytes]:
        # Shell startup files on the host may print before remote_host.py runs;
        # only a line carrying the readiness marker counts.
        found: list[bytes] = []

        def scan() -> None:
            for line in iter(stream.readline, b""):
                if line.startswith(READY_MARKER):
                    found.append(line.strip())
                    return
                if line.startswith(BUSY_MARKER) and on_busy is not None:
                    try:
                        record = json.loads(line[len(BUSY_MARKER):])
                    except ValueError:
                        record = {}
                    on_busy(record if isinstance(record, dict) else {})

        reader = threading.Thread(target=scan, daemon=True)
        reader.start()
        reader.join(timeout)
        return found[0] if found else None

    def _heartbeat(self) -> None:
        host = self._host
        while host is not None and not self._stopping.wait(HEARTBEAT_SECONDS):
            try:
                assert host.stdin is not None
                host.stdin.write(b"\n")
                host.stdin.flush()
            except (OSError, ValueError):
                return

    def _forward(self) -> None:
        assert self.remote_port is not None
        Path(self.socket_path).unlink(missing_ok=True)
        self._tunnel = subprocess.Popen(
            ssh_command(
                self.target, None, "-N", "-o", "ExitOnForwardFailure=yes",
                "-o", "StreamLocalBindUnlink=yes",
                "-L", f"{self.socket_path}:127.0.0.1:{self.remote_port}",
            ),
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.PIPE,
        )
        threading.Thread(target=self._copy_tunnel_stderr, args=(self._tunnel,), daemon=True).start()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and self._tunnel.poll() is None:
            if self.ready():
                self._note(f"forwarding {self.socket_path} -> remote 127.0.0.1:{self.remote_port}")
                return
            time.sleep(0.2)
        raise RuntimeError(f"SSH port forward did not become ready (exit {self._tunnel.poll()})")

    def _copy_tunnel_stderr(self, tunnel: subprocess.Popen) -> None:
        # ssh warns about TCP_NODELAY for every Unix-socket forward connection.
        assert tunnel.stderr is not None
        for line in iter(tunnel.stderr.readline, b""):
            text = line.decode(errors="replace")
            if "TCP_NODELAY" not in text:
                self._note(text.rstrip())

    def headers(self) -> dict[str, str]:
        assert self.remote_port is not None
        return {"Host": f"127.0.0.1:{self.remote_port}", TOKEN_HEADER: self.token}

    def ready(self) -> bool:
        try:
            status, _, _ = unix_request(self.socket_path, "GET", "/api/activity", self.headers(), timeout=3)
        except OSError:
            return False
        return status == 200

    def alive(self) -> bool:
        return all(process is not None and process.poll() is None for process in (self._host, self._tunnel))

    def gateway_environment(self) -> dict[str, str]:
        assert self.remote_port is not None
        return {
            "SEEKNAL_STUDIO_REMOTE": json.dumps(self.target.to_json()),
            "SEEKNAL_STUDIO_REMOTE_SOCKET": self.socket_path,
            "SEEKNAL_STUDIO_REMOTE_PORT": str(self.remote_port),
            "SEEKNAL_STUDIO_REMOTE_TOKEN": self.token,
        }

    def stop(self) -> None:
        self._stopping.set()
        _terminate(self._tunnel)
        self._tunnel = None
        Path(self.socket_path).unlink(missing_ok=True)
        if self._short_dir:
            shutil.rmtree(self._short_dir, ignore_errors=True)
            self._short_dir = None
        host = self._host
        self._host = None
        if host is None:
            return
        # Closing stdin tells remote_host.py to stop the backend and its workers.
        try:
            if host.stdin:
                host.stdin.close()
        except OSError:
            pass
        try:
            host.wait(timeout=12)
        except subprocess.TimeoutExpired:
            _terminate(host)
        if host.stdout:
            host.stdout.close()


def _describe_holder(holder: dict) -> str:
    client = holder.get("client") if isinstance(holder.get("client"), str) else None
    started = holder.get("started_at") if isinstance(holder.get("started_at"), str) else None
    parts = [f"opened from {client[:64]}" if client else None, f"since {started[:40]}" if started else None]
    return ", ".join(part for part in parts if part) or "details unavailable"
