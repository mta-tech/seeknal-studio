#!/usr/bin/env python3
"""Remote side of an SSH workspace: runtime setup and a stdin-bound backend.

Runs with the SSH host's system python3 (stdlib only).  ``serve`` keeps the
Studio backend alive only while the controlling SSH session holds stdin open.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


HERE = Path(__file__).resolve().parent
CACHE = Path.home() / ".cache" / "seeknal-studio"
LOCK = HERE / "requirements-runtime.lock.txt"
SEEKNAL_VERSION = "2.14.0"
PYTHON_VERSION = "3.11"
TOKEN_HEADER = "X-Seeknal-Studio-Token"


def _uv() -> str | None:
    for candidate in (os.environ.get("UV"), str(Path.home() / ".local" / "bin" / "uv"),
                      shutil.which("uv"), str(Path.home() / ".cargo" / "bin" / "uv")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _seeknal_version(python: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(python), "-I", "-c", "import importlib.metadata as m; print(m.version('seeknal'))"],
            capture_output=True, text=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def ensure_runtime() -> int:
    lock = LOCK.read_bytes()
    root = CACHE / "runtimes" / hashlib.sha256(lock).hexdigest()[:16]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    python = root / "venv" / "bin" / "python"
    with open(root / ".lock", "w") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if not ((root / ".complete").is_file() and _seeknal_version(python) == SEEKNAL_VERSION):
            uv = _uv()
            if uv is None:
                print("uv is required on the SSH host to build the Seeknal runtime "
                      "(curl -LsSf https://astral.sh/uv/install.sh | sh)", file=sys.stderr)
                return 3
            (root / ".complete").unlink(missing_ok=True)
            shutil.copyfile(LOCK, root / "requirements.lock.txt")
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()),
                   "UV_CACHE_DIR": str(root / "uv-cache")}
            if not python.is_file():
                subprocess.run([uv, "venv", "--python", PYTHON_VERSION, str(root / "venv")],
                               check=True, env=env, stdout=sys.stderr)
            subprocess.run([uv, "pip", "sync", "--python", str(python), "--require-hashes",
                            str(root / "requirements.lock.txt")], check=True, env=env, stdout=sys.stderr)
            if _seeknal_version(python) != SEEKNAL_VERSION:
                print(f"remote runtime does not provide Seeknal {SEEKNAL_VERSION}", file=sys.stderr)
                return 4
            (root / ".complete").write_text(SEEKNAL_VERSION + "\n")
    # Keep the lexical venv path: resolving the symlink would leave the venv.
    print(json.dumps({"python": str(python), "seeknal_version": SEEKNAL_VERSION}))
    return 0


READY_MARKER = "SEEKNAL_STUDIO_READY "
HEARTBEAT_TIMEOUT = 45.0


def _ready(port: int, token: str) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/activity", headers={TOKEN_HEADER: token},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    # The backend's SIGTERM handler cancels jobs and closes REPL/agent workers.
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _read_token() -> str:
    data = b""
    while not data.endswith(b"\n") and len(data) < 256:
        chunk = os.read(0, 1)
        if not chunk:
            break
        data += chunk
    return data.decode(errors="replace").strip()


BUSY_MARKER = "SEEKNAL_STUDIO_BUSY "


def _lock_holder(path: Path) -> dict:
    try:
        holder = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return {}
    return {key: holder[key] for key in ("client", "started_at") if isinstance(holder.get(key), str)}


def _project_lock(project: str, deadline: float):
    """Hold one backend per project; a stale session exits within the heartbeat timeout.

    The holder records where it was opened from, so a second laptop is told who has
    the project instead of only that it is busy."""

    locks = CACHE / "locks"
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(os.path.realpath(os.path.expanduser(project)).encode()).hexdigest()[:24]
    path = locks / f"{name}.lock"
    # Append mode: opening must not wipe the current holder's record.
    guard = open(path, "a+")
    announced = False
    while True:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            guard.seek(0)
            guard.truncate()
            guard.write(json.dumps({
                "client": (os.environ.get("SSH_CLIENT") or "").split(" ")[0] or "this host",
                "started_at": time.strftime("%Y-%m-%d %H:%M %Z"),
            }))
            guard.flush()
            return guard, {}
        except BlockingIOError:
            if not announced:
                print(BUSY_MARKER + json.dumps(_lock_holder(path)), flush=True)
                announced = True
            if time.monotonic() > deadline:
                guard.close()
                return None, _lock_holder(path)
            time.sleep(0.5)


def serve(project: str, runtime_python: str) -> int:
    token = _read_token()
    if len(token) < 32:
        print("missing session token", file=sys.stderr)
        return 2
    guard, holder = _project_lock(project, time.monotonic() + HEARTBEAT_TIMEOUT + 15)
    if guard is None:
        print("another Studio backend still holds this remote project", holder, file=sys.stderr)
        return 5
    logs = CACHE / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    log_path = logs / f"studio-{stamp}.log"
    port_file = logs / f".port-{stamp}"
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"}}
    env.update({
        "SEEKNAL_STUDIO_PORT": "0",
        "SEEKNAL_STUDIO_PORT_FILE": str(port_file),
        "SEEKNAL_STUDIO_PROJECT": project,
        "SEEKNAL_RUNTIME_PYTHON": runtime_python,
        "SEEKNAL_STUDIO_ACCESS_TOKEN": token,
        "PYTHONUNBUFFERED": "1",
    })
    with open(log_path, "a") as log:
        process = subprocess.Popen(
            [runtime_python, str(HERE / "server.py")], cwd=HERE, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    closed = threading.Event()

    def watch_stdin() -> None:
        # The local session sends a heartbeat line every few seconds. EOF or
        # silence (a half-open connection after sleep or a network change)
        # ends the session so the backend never outlives its client.
        try:
            while True:
                readable, _, _ = select.select([0], [], [], HEARTBEAT_TIMEOUT)
                if not readable or not os.read(0, 4096):
                    return
        finally:
            closed.set()

    def terminate(_signum, _frame) -> None:
        closed.set()

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, terminate)
    threading.Thread(target=watch_stdin, daemon=True).start()
    try:
        deadline = time.monotonic() + 60
        port = None
        while port is None or not _ready(port, token):
            if process.poll() is not None or closed.is_set() or time.monotonic() > deadline:
                print(f"Studio backend failed to start; see {log_path}", file=sys.stderr)
                return 1
            if port is None and port_file.is_file():
                try:
                    port = int(port_file.read_text().strip())
                except ValueError:
                    port = None
            time.sleep(0.2)
        print(READY_MARKER + json.dumps({"port": port, "log": str(log_path), "pid": process.pid}), flush=True)
        while not closed.is_set() and process.poll() is None:
            closed.wait(0.5)
        return 0 if closed.is_set() else 1
    finally:
        _stop(process)
        port_file.unlink(missing_ok=True)
        guard.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ensure-runtime")
    served = commands.add_parser("serve")
    served.add_argument("--project", required=True)
    served.add_argument("--runtime-python", required=True)
    args = parser.parse_args()
    if args.command == "ensure-runtime":
        return ensure_runtime()
    return serve(args.project, args.runtime_python)


if __name__ == "__main__":
    raise SystemExit(main())
