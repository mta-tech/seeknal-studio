#!/usr/bin/env python3
"""Supervise one project-bound backend and desktop window at a time."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from typing import NamedTuple, Optional, Tuple, Union

from local_access import LISTEN_FD_ENV, READY_PATH, SECRET_ENV, WINDOW_ENV, bootstrap_url, readiness_proof
from remote_workspace import RemoteSession, RemoteTarget
from runtime_resolver import resolve_runtime

ROOT = Path(__file__).resolve().parent
PRIVATE_ENV = {
    "SEEKNAL_STUDIO_BASELINE_RECEIPT", "SEEKNAL_STUDIO_BASELINE_NONCE",
    "SEEKNAL_STUDIO_CONTROL_FILE", "SEEKNAL_STUDIO_CONTROL_NONCE", SECRET_ENV, LISTEN_FD_ENV,
}
GOVERNANCE_URLS = ("ATLAS_API_URL", "ATLAS_PORTAL_URL")
WINDOW_ENVIRONMENT_KEYS = {
    "HOME", "PATH", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TMPDIR",
    "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
    "USER", "LOGNAME", "SHELL", "TERM", "__CF_USER_TEXT_ENCODING",
}
RECONNECT_WINDOW_SECONDS = 300
PILOT_AUTHORITY_ARTIFACTS = (
    "seeknal_project.yml", "data/orders.csv", "data/customers.csv",
    "seeknal/sources/orders.yml", "seeknal/sources/customers.yml",
    "SEEKNAL_ASK.md", "EXPECTED_OUTPUT.json",
)
PILOT_DEFINITIONS = {
    "sources/customers.yml", "sources/orders.yml", "transforms/customer_value_pilot.yml",
}


class PilotAuthority(NamedTuple):
    project: Path
    sha256: dict[str, str]
    definitions: frozenset[str]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


# No proxies and no redirects: the probe only ever talks to the launcher's socket.
_PROBE = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def wait_ready(process: subprocess.Popen, url: str, secret: str, timeout: float = 30) -> None:
    """Wait until the service proves it holds ``secret``; the probe never sends it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Studio service exited ({process.returncode}); see the service log")
        challenge = secrets.token_hex(16)
        try:
            with _PROBE.open(f"{url}{READY_PATH}?challenge={challenge}", timeout=5) as response:
                proof = json.load(response).get("proof")
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            time.sleep(0.15)
            continue
        if isinstance(proof, str) and hmac.compare_digest(proof, readiness_proof(secret, challenge)):
            return
        raise RuntimeError("The service on the Studio port could not prove it belongs to this launch")
    raise RuntimeError(f"Studio service did not become ready within {timeout:g} seconds")


def stop(process: Optional[subprocess.Popen]) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def consume_baseline(source: dict[str, str]) -> Tuple[dict[str, str], Optional[PilotAuthority]]:
    from project_service import validate_project

    environment = {key: value for key, value in source.items() if key not in PRIVATE_ENV}
    receipt_name = source.get("SEEKNAL_STUDIO_BASELINE_RECEIPT")
    expected_nonce = source.get("SEEKNAL_STUDIO_BASELINE_NONCE")
    if not receipt_name and not expected_nonce:
        return environment, None
    if not receipt_name or not expected_nonce:
        raise RuntimeError("incomplete private pilot baseline receipt")
    receipt = Path(receipt_name)
    try:
        if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_mode & 0o077:
            raise RuntimeError("private pilot baseline receipt is unsafe")
        value = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("nonce") != expected_nonce:
            raise RuntimeError("private pilot baseline receipt is invalid")
        variables = value.get("governance")
        if not isinstance(variables, dict) or set(variables) != set(GOVERNANCE_URLS):
            raise RuntimeError("private pilot baseline receipt schema is invalid")
        for name in GOVERNANCE_URLS:
            record = variables[name]
            if not isinstance(record, dict) or set(record) != {"present", "value"}:
                raise RuntimeError("private pilot baseline receipt schema is invalid")
            if record["present"] is True and isinstance(record["value"], str):
                environment[name] = record["value"]
            elif record["present"] is False and record["value"] is None:
                environment.pop(name, None)
            else:
                raise RuntimeError("private pilot baseline receipt schema is invalid")
        proof = value.get("pilot_authority")
        if not isinstance(proof, dict) or set(proof) != {"sha256", "definitions"}:
            raise RuntimeError("private pilot authority proof is invalid")
        hashes = proof["sha256"]
        definitions = proof["definitions"]
        if (
            not isinstance(hashes, dict)
            or set(hashes) != set(PILOT_AUTHORITY_ARTIFACTS)
            or not all(isinstance(item, str) and len(item) == 64 for item in hashes.values())
            or not isinstance(definitions, list)
            or set(definitions) != PILOT_DEFINITIONS
        ):
            raise RuntimeError("private pilot authority proof is invalid")
        return environment, PilotAuthority(
            validate_project(value.get("pilot_project")),
            dict(hashes),
            frozenset(definitions),
        )
    finally:
        receipt.unlink(missing_ok=True)


def pilot_authority_matches(authority: PilotAuthority) -> bool:
    project = authority.project
    try:
        if project.is_symlink() or not project.is_dir():
            return False
        for relative in PILOT_AUTHORITY_ARTIFACTS:
            path = project / relative
            if path.is_symlink() or not path.is_file():
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != authority.sha256[relative]:
                return False
        definitions = set()
        for path in (project / "seeknal").rglob("*"):
            if path.is_symlink():
                return False
            if path.is_file() and path.suffix in {".yml", ".yaml", ".py"}:
                definitions.add(path.relative_to(project / "seeknal").as_posix())
        return definitions == set(authority.definitions) == PILOT_DEFINITIONS
    except (OSError, KeyError):
        return False


def environment_for_project(
    baseline: dict[str, str], project: Path, authority: Optional[PilotAuthority],
) -> dict[str, str]:
    environment = dict(baseline)
    if authority is not None and project == authority.project and pilot_authority_matches(authority):
        for name in GOVERNANCE_URLS:
            environment.pop(name, None)
    return environment


def window_environment(source: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in source.items()
        if key in WINDOW_ENVIRONMENT_KEYS or key.startswith("LC_")
    }


def read_switch(path: Path, nonce: str) -> Optional[Union[Path, RemoteTarget]]:
    from project_service import validate_project

    if not path.exists():
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise RuntimeError("project switch request is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("nonce") != nonce:
            raise RuntimeError("project switch request is invalid")
        if "remote" in value:
            return RemoteTarget.from_json(value["remote"])
        return validate_project(value.get("project"))
    finally:
        path.unlink(missing_ok=True)


def write_access_link(private: Path, url: str) -> Path:
    """Keep the one-time browser link out of logs; developer tooling reads this file."""
    path = private / "access-url"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(url + "\n")
    return path


def remembered_project() -> Optional[Path]:
    from project_service import default_config_path, validate_project

    config = default_config_path()
    try:
        if config.is_symlink() or not config.is_file() or config.stat().st_mode & 0o077:
            return None
        value = json.loads(config.read_text(encoding="utf-8"))
        return validate_project(value.get("last"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def main() -> int:
    try:
        runtime = resolve_runtime()
    except RuntimeError as error:
        print(f"Seeknal SDK 2.14.0 runtime is unavailable: {error}", file=sys.stderr)
        return 2
    if sys.version_info < (3, 10):
        os.execv(str(runtime.python), [str(runtime.python), str(Path(__file__).resolve()), *sys.argv[1:]])

    from project_service import new_control_nonce, validate_project

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, help="Explicit local project; defaults to the last project or included demo")
    parser.add_argument("--port", type=int, default=0, help="Loopback port, 0 selects an available port")
    parser.add_argument("--browser", action="store_true", help="Use the system browser instead of Webframe")
    parser.add_argument("--webframe", type=Path, help="Path to a Webframe executable")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        baseline, pilot_authority = consume_baseline(os.environ.copy())
        project = validate_project(str(args.project)) if args.project else (remembered_project() or validate_project(str(ROOT / "demo")))
        runtime = resolve_runtime(environ=baseline)
    except (OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))
    binary = args.webframe or Path(baseline.get("SEEKNAL_WEBFRAME", ROOT / "bin" / "webframe"))
    if not binary.is_file():
        binary = ROOT.parent / "webframe-evaluation" / "bin" / "webframe"
    if not args.browser and not binary.is_file():
        parser.error("Webframe missing. Set SEEKNAL_WEBFRAME or launch with --browser")
    try:
        # Owned for the whole launch and handed to every service incarnation,
        # so no other local user can take the port during a restart.
        listener = socket.create_server(("127.0.0.1", args.port))
    except OSError as error:
        parser.error(f"cannot listen on 127.0.0.1:{args.port}: {error}")
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    log_dir = Path.home() / "Library" / "Logs" / "Seeknal Studio" if sys.platform == "darwin" else Path.home() / ".local" / "state" / "seeknal-studio"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"studio-{port}.log"
    server = window = None
    session: Optional[RemoteSession] = None
    browser_opened = False
    notice: Optional[str] = None
    target: Union[Path, RemoteTarget] = project
    reconnected_at: dict[str, float] = {}

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    try:
        with tempfile.TemporaryDirectory(prefix="seeknal-studio-") as private_name, log_path.open("a") as log:
            private = Path(private_name)
            os.chmod(private, 0o700)
            control = private / "switch.json"
            nonce = new_control_nonce()
            # One browser secret per app launch: workspace switches and
            # reconnects restart the service on the same port with the same
            # secret, so the open page's cookie keeps working.
            ui_secret = secrets.token_urlsafe(32)
            access_link = write_access_link(private, bootstrap_url(ui_secret, port))
            while True:
                if isinstance(target, RemoteTarget):
                    print(f"Opening remote workspace {target.location}…", flush=True)
                    session = RemoteSession(target, log=log, socket_dir=private)
                    try:
                        session.start()
                    except (OSError, RuntimeError) as error:
                        session = None
                        notice = f"Could not open remote workspace {target.name or target.location}: {error}"
                        print(notice, file=sys.stderr, flush=True)
                        target = project
                        continue
                    env = dict(baseline)
                    env.update(session.gateway_environment())
                    entrypoint = ROOT / "remote_gateway.py"
                    label = f"ssh {target.location}"
                else:
                    project = target
                    env = environment_for_project(baseline, project, pilot_authority)
                    if (
                        pilot_authority is not None
                        and project == pilot_authority.project
                        and not pilot_authority_matches(pilot_authority)
                    ):
                        print(
                            "Verified pilot authority changed; preserving the normal governance environment.",
                            file=sys.stderr,
                            flush=True,
                        )
                    env.update({
                        "SEEKNAL_STUDIO_PROJECT": str(project),
                        "SEEKNAL_RUNTIME_PYTHON": str(runtime.python),
                    })
                    entrypoint = ROOT / "server.py"
                    label = str(project)
                env.update({
                    "SEEKNAL_STUDIO_PORT": str(port),
                    "SEEKNAL_STUDIO_CONTROL_FILE": str(control), "SEEKNAL_STUDIO_CONTROL_NONCE": nonce,
                    SECRET_ENV: ui_secret, LISTEN_FD_ENV: str(listener.fileno()),
                })
                if notice:
                    env["SEEKNAL_STUDIO_NOTICE"] = notice
                notice = None
                # The previous incarnation has exited (stop() waits), so only
                # one service accepts on the shared socket at a time.
                server = subprocess.Popen(
                    [str(runtime.python), str(entrypoint)],
                    cwd=ROOT, env=env, stdout=log, stderr=log, pass_fds=(listener.fileno(),),
                )
                try:
                    wait_ready(server, url, ui_secret, timeout=60 if session is not None else 30)
                except (OSError, RuntimeError) as error:
                    if session is None:
                        raise
                    stop(server)
                    server = None
                    session.stop()
                    session = None
                    notice = f"Could not open remote workspace {target.name or target.location}: {error}"
                    print(notice, file=sys.stderr, flush=True)
                    target = project
                    continue
                print(
                    f"Seeknal Studio: {url}\nProject: {label}\nService log: {log_path}\nAccess link file: {access_link}",
                    flush=True,
                )
                if args.browser:
                    if not browser_opened:
                        print(
                            "Note: --browser isolates Studio less on shared machines; the app window is the protected path.",
                            file=sys.stderr, flush=True,
                        )
                        webbrowser.open(bootstrap_url(ui_secret, port))
                        browser_opened = True
                else:
                    # Never put the secret on the command line: other users can
                    # read it there. Webframe exposes WF_* as page globals.
                    window_env = window_environment(baseline)
                    window_env[WINDOW_ENV] = ui_secret
                    window = subprocess.Popen([str(binary.resolve()), "-w", "1584x992", url], cwd=ROOT, env=window_env, stdout=log, stderr=log)
                selected = None
                while selected is None:
                    if session is not None and (server.poll() is not None or not session.alive()):
                        # Reconnect at most once per window; a repeated loss
                        # (crash loop, unreachable host) falls back to the last
                        # local project with a visible notice.
                        location = session.target.location
                        now = time.monotonic()
                        if now - reconnected_at.get(location, -RECONNECT_WINDOW_SECONDS) >= RECONNECT_WINDOW_SECONDS:
                            reconnected_at[location] = now
                            print(f"Lost SSH session to {location}; reconnecting…", file=sys.stderr, flush=True)
                            selected = session.target
                        else:
                            notice = f"Lost connection to remote workspace {session.target.name or session.target.location}; reopened the last local project"
                            print(notice, file=sys.stderr, flush=True)
                            selected = project
                        break
                    if server.poll() is not None:
                        raise RuntimeError(f"Studio service exited ({server.returncode}); see the service log")
                    if window is not None and window.poll() is not None:
                        return 0
                    selected = read_switch(control, nonce)
                    if selected is None:
                        time.sleep(0.1)
                stop(window)
                window = None
                stop(server)
                server = None
                if session is not None:
                    session.stop()
                    session = None
                target = selected
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"{exc}\nService log: {log_path}", file=sys.stderr)
        return 1
    finally:
        stop(window)
        stop(server)
        if session is not None:
            session.stop()
        listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
