#!/usr/bin/env python3
"""Seeknal Studio loopback service."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import threading
import time
import uuid
import hashlib
import difflib
import hmac
import re
import tempfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401 - route tests serve Handler with it
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from local_access import SECRET_ENV, BrowserAccess, NoStoreMixin, http_server
from project_service import ProjectService
from environment_service import (
    EnvironmentBusyError,
    EnvironmentConflictError,
    EnvironmentService,
)
from repl_service import ReplService
from agent_worker import _safe_drafts_root
from runtime_resolver import RuntimeResolutionError, resolve_runtime

ROOT = Path(__file__).resolve().parent
PROJECT = Path(os.environ.get("SEEKNAL_STUDIO_PROJECT", ROOT / "demo")).resolve()
try:
    RUNTIME = resolve_runtime()
    RUNTIME_ERROR = None
except RuntimeResolutionError as error:
    # Keep the module importable for route/unit tests and surface the verified
    # dependency error from main() rather than silently selecting another SDK.
    RUNTIME = None
    RUNTIME_ERROR = str(error)
PYTHON = RUNTIME.python if RUNTIME else ROOT / ".runtime" / "venv" / "bin" / "python"
SEEKNAL = RUNTIME.seeknal if RUNTIME else ROOT / ".runtime" / "venv" / "bin" / "seeknal"
SDK_ROOT = RUNTIME.sdk_root if RUNTIME else None
BRIDGE = ROOT / "engine_bridge.py"
PORT = int(os.environ.get("SEEKNAL_STUDIO_PORT", "18766"))
RUNTIME_ID = uuid.uuid4().hex
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
# Remote (SSH) backends require a per-session token so other users on the SSH
# host cannot drive this loopback service. Pop it so child processes never
# inherit it.
ACCESS_TOKEN = os.environ.pop("SEEKNAL_STUDIO_ACCESS_TOKEN", None)
# Local launches also require the launcher's per-launch browser secret so other
# users and processes on this machine cannot drive Studio.
BROWSER_ACCESS = BrowserAccess(os.environ.pop(SECRET_ENV, None))
NOTICE = os.environ.pop("SEEKNAL_STUDIO_NOTICE", None)
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
STATIC = {"index.html", "styles.css", "app.js"}
MAX_BODY = 300_000

COMMANDS = {
    "run": ["run", "--full"],
    "validate": ["run", "--dry-run"],
    "plan": ["run", "--show-plan"],
    "lineage": ["lineage", "--path", ".", "--output", "target/lineage.html", "--no-open"],
}
DRAFT_ACTIONS = {"validate-draft", "apply-draft"}
DRAFT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,127}")
DRAFT_DESCRIPTION_LIMIT = 2_000
DRAFT_DEPS_LIMIT = 1_000
DRAFT_OPTIONS_LOCK = threading.Lock()
DRAFT_OPTIONS_CACHE: dict | None = None
WORK_ACCEPTANCE_LOCK = threading.RLock()
# One lint at a time; a request that cannot start within DIAGNOSTICS_WAIT
# seconds reports "unavailable" instead of queueing behind others.
DIAGNOSTICS_LOCK = threading.Lock()
DIAGNOSTICS_WAIT = 1.0
DIAGNOSTICS_TIMEOUT = 5


class EditorConflictError(RuntimeError):
    """The editor's base content no longer matches the project file."""


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seeknal_version() -> str | None:
    return RUNTIME.dist_version if RUNTIME else None


def draft_details(relative: object) -> dict | None:
    path = safe_file(relative)
    if path is None or not isinstance(relative, str) or not relative.startswith(".seeknal/drafts/"):
        return None
    resolved = bridge("draft-details", relative)
    kind, name = resolved["kind"], resolved["name"]
    if not re.fullmatch(r"[a-z_]+", kind) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("draft has unsupported kind or name")
    destination = PROJECT / resolved["destination"]
    resolved_destination = destination.resolve()
    if PROJECT not in resolved_destination.parents or destination.is_symlink():
        raise ValueError("draft destination escapes project")
    return {
        "path": relative, "file": path, "kind": kind, "name": name,
        "destination": destination, "content_hash": file_hash(path),
    }


def create_editor_draft(relative: object, content: object) -> dict:
    target = safe_file(relative)
    if (
        target is None
        or not isinstance(relative, str)
        or not relative.startswith("seeknal/")
        or target.suffix not in {".yml", ".yaml"}
        or not isinstance(content, str)
    ):
        raise ValueError("an active Seeknal YAML file is required")
    if len(content.encode("utf-8")) > MAX_BODY:
        raise ValueError("draft content exceeds 300 KB")
    drafts = _safe_drafts_root(PROJECT)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".editor-", suffix=target.suffix, dir=drafts)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_relative = temporary.relative_to(PROJECT).as_posix()
        details = draft_details(temporary_relative)
        if details is None or details["destination"].resolve() != target:
            raise ValueError("draft kind and name must resolve to the edited file")
        filename = f"draft_{details['kind']}_{details['name']}_{uuid.uuid4().hex[:8]}{target.suffix}"
        destination = drafts / filename
        os.link(temporary, destination)
        temporary.unlink()
        draft_relative = destination.relative_to(PROJECT).as_posix()
        return {
            "draft": {
                "path": draft_relative,
                "name": details["name"],
                "kind": details["kind"],
                "content_hash": file_hash(destination),
            },
            "destination": relative,
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def resolve_editor_details(relative: str, content: str) -> dict:
    try:
        result = subprocess.run(
            [str(PYTHON), str(BRIDGE), "editor-details", str(PROJECT), relative],
            cwd=PROJECT,
            env=clean_environment(),
            input=content,
            capture_output=True,
            text=True,
            timeout=20,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Seeknal content validation timed out") from error
    if result.returncode:
        message = (result.stderr or result.stdout).strip().splitlines()[-1]
        message = re.sub(r"^[\w.]+(?:Error|Exception):\s*", "", message)
        if "while parsing" in message.lower() or "while scanning" in message.lower():
            message = "YAML syntax is invalid; check indentation, quoting, and mapping structure"
        raise ValueError(f"Editor content is invalid: {message}")
    details = json.loads(result.stdout)
    kind, name, destination = details.get("kind"), details.get("name"), details.get("destination")
    if (
        not isinstance(kind, str)
        or not re.fullmatch(r"[a-z_]+", kind)
        or not isinstance(name, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
        or not isinstance(destination, str)
    ):
        raise ValueError("Editor content has an unsupported kind or name")
    return details


def atomic_write_text(target: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".studio.tmp", dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def draft_diff(details: dict, destination_content: str | None = None) -> dict:
    destination = details["destination"]
    if destination_content is None:
        before = destination.read_text(encoding="utf-8").splitlines() if destination.is_file() else []
        destination_hash = file_hash(destination) if destination.is_file() else None
    else:
        before = destination_content.splitlines()
        destination_hash = hashlib.sha256(destination_content.encode("utf-8")).hexdigest()
    text = details["file"].read_text(encoding="utf-8")
    relative = details["path"]
    return {
        "path": relative,
        "destination": destination.relative_to(PROJECT).as_posix(),
        "diff": "\n".join(difflib.unified_diff(
            before, text.splitlines(),
            fromfile=destination.relative_to(PROJECT).as_posix(),
            tofile=relative,
            lineterm="",
        )),
        "content_hash": details["content_hash"],
        "destination_hash": destination_hash,
    }


def prepare_editor_snapshot(relative: object, content: object, original_content: object) -> dict:
    if not all(isinstance(value, str) for value in (relative, content, original_content)):
        raise ValueError("path, content, and original_content are required")
    assert isinstance(relative, str) and isinstance(content, str) and isinstance(original_content, str)
    if len(content.encode("utf-8")) > MAX_BODY or len(original_content.encode("utf-8")) > MAX_BODY:
        raise ValueError("editor content exceeds 300 KB")
    source = safe_file(relative)
    if source is None or source.suffix not in {".yml", ".yaml", ".py"}:
        raise ValueError("path is not an editable Seeknal pipeline file")
    managed = relative.startswith(".seeknal/drafts/")
    active = relative.startswith("seeknal/")
    if not managed and not active:
        raise ValueError("path is not an editable Seeknal pipeline file")
    current_content = source.read_text(encoding="utf-8")
    if current_content != original_content:
        raise EditorConflictError("File changed on disk; reload it before preparing these edits")
    if active and content == original_content:
        return {"unchanged": True, "mode": "active", "source_path": relative}

    resolved = resolve_editor_details(relative, content)
    resolved_destination = resolved["destination"]
    if active and resolved_destination != relative:
        raise ValueError("kind or name changes would rename this pipeline file; create a new draft instead")

    if managed:
        existing = draft_details(relative)
        if existing is None:
            raise ValueError("managed draft is unavailable")
        existing_destination = existing["destination"].relative_to(PROJECT).as_posix()
        if resolved_destination != existing_destination:
            raise ValueError("kind or name changes would retarget this managed draft")
        if content != original_content:
            if source.read_text(encoding="utf-8") != original_content:
                raise EditorConflictError("Draft changed on disk; reload it before preparing these edits")
            atomic_write_text(source, content)
            JOBS.validated_drafts = {
                item for item in JOBS.validated_drafts if item[0] != relative
            }
        snapshot_path = relative
        mode = "draft"
    else:
        drafts_root = _safe_drafts_root(PROJECT)
        snapshot_name = (
            f".editor-{resolved['kind']}-{resolved['name']}-{uuid.uuid4().hex[:8]}{source.suffix}"
        )
        snapshot_file = drafts_root / snapshot_name
        try:
            with snapshot_file.open("x", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            snapshot_file.unlink(missing_ok=True)
            raise
        snapshot_path = snapshot_file.relative_to(PROJECT).as_posix()
        mode = "active"

    if active and source.read_text(encoding="utf-8") != original_content:
        (PROJECT / snapshot_path).unlink(missing_ok=True)
        raise EditorConflictError("File changed on disk while edits were being prepared; reload it")

    details = draft_details(snapshot_path)
    if details is None:
        if active:
            (PROJECT / snapshot_path).unlink(missing_ok=True)
        raise RuntimeError("prepared draft could not be reviewed")
    destination = details["destination"].relative_to(PROJECT).as_posix()
    if destination != resolved_destination:
        if active:
            details["file"].unlink(missing_ok=True)
        raise RuntimeError("prepared draft target changed during review")
    diff = draft_diff(details, original_content if active else None)
    return {
        "unchanged": content == original_content,
        "snapshot": {
            "path": snapshot_path,
            "content_hash": details["content_hash"],
            "destination": destination,
            "destination_hash": diff["destination_hash"],
            "source_path": relative,
            "mode": mode,
        },
        "diff": diff,
    }


def project_files() -> list[dict[str, str]]:
    files = []
    candidates = list((PROJECT / "seeknal").glob("**/*"))
    candidates.extend((PROJECT / ".seeknal" / "drafts").glob("**/*"))
    for path in sorted(candidates):
        if path.suffix not in {".yml", ".yaml", ".py"}:
            continue
        if path.parent == PROJECT / ".seeknal" / "drafts" and path.name.startswith(".editor-"):
            continue
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(PROJECT).as_posix()
            kind = path.parent.name.removesuffix("s")
            if path.parent.name == "feature_groups":
                kind = "feature_group"
            files.append({"path": relative, "name": path.stem, "kind": kind})
    project_file = PROJECT / "seeknal_project.yml"
    if project_file.is_file():
        files.append({"path": "seeknal_project.yml", "name": "seeknal_project", "kind": "project"})
    return files


def project_fingerprint() -> str:
    digest = hashlib.sha256()
    paths = [PROJECT / "seeknal_project.yml"]
    paths.extend((PROJECT / "seeknal").glob("**/*"))
    paths.extend((PROJECT / "data").glob("**/*"))
    for path in sorted(path for path in paths if path.is_file() and not path.is_symlink()):
        digest.update(path.relative_to(PROJECT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def output_signature() -> str:
    """Hash Seeknal's run state files, which every run rewrites, including runs
    started by someone else (another Studio, a terminal, a scheduler)."""
    digest = hashlib.sha256()
    target = PROJECT / "target"
    states = [target / "run_state.json", *target.glob("environments/*/run_state.json")]
    for path in sorted(path for path in states if path.is_file() and not path.is_symlink()):
        digest.update(path.relative_to(PROJECT).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def freshness_fingerprint(sources: str | None = None) -> str:
    # Outputs are fresh only for the sources of the last successful Studio run
    # and only while nobody has run anything since.
    return f"{sources or project_fingerprint()}:{output_signature()}"


STUDIO_STATE = PROJECT / "target" / "studio_state.json"


def save_successful_fingerprint(value: str) -> None:
    STUDIO_STATE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STUDIO_STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"project_fingerprint": value}))
    os.replace(temporary, STUDIO_STATE)


def safe_file(relative: object) -> Path | None:
    if not isinstance(relative, str) or relative.startswith("/") or "\0" in relative:
        return None
    candidate = PROJECT / relative
    if candidate.is_symlink():
        return None
    resolved = candidate.resolve()
    if PROJECT not in resolved.parents:
        return None
    allowed = {item["path"] for item in project_files()}
    internal_snapshot = (
        candidate.parent == PROJECT / ".seeknal" / "drafts"
        and candidate.name.startswith(".editor-")
        and candidate.suffix in {".yml", ".yaml", ".py"}
        and candidate.is_file()
    )
    return resolved if relative in allowed or internal_snapshot else None


def clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        environment.pop(name, None)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def bridge(action: str, *arguments: str) -> dict:
    try:
        result = subprocess.run(
            [str(PYTHON), str(BRIDGE), action, str(PROJECT), *arguments],
            cwd=PROJECT,
            env=clean_environment(),
            capture_output=True,
            text=True,
            timeout=20,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Seeknal bridge timed out during {action}") from error
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-4000:])
    return json.loads(result.stdout)


def draft_options() -> dict:
    global DRAFT_OPTIONS_CACHE
    with DRAFT_OPTIONS_LOCK:
        if DRAFT_OPTIONS_CACHE is None:
            result = bridge("draft-options")
            entries = result.get("node_types") if isinstance(result, dict) else None
            if not isinstance(entries, list) or not entries:
                raise RuntimeError("Seeknal returned no draft node types")
            options = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise RuntimeError("Seeknal returned invalid draft metadata")
                value = entry.get("value")
                normalized = entry.get("normalized")
                python_supported = entry.get("python_supported")
                if (
                    not isinstance(value, str)
                    or re.fullmatch(r"[a-z][a-z_-]*", value) is None
                    or not isinstance(normalized, str)
                    or re.fullmatch(r"[a-z][a-z_]*", normalized) is None
                    or not isinstance(python_supported, bool)
                ):
                    raise RuntimeError("Seeknal returned invalid draft metadata")
                options.append({
                    "value": value,
                    "normalized": normalized,
                    "python_supported": python_supported,
                })
            excluded_entries = result.get("excluded_node_types", [])
            if not isinstance(excluded_entries, list):
                raise RuntimeError("Seeknal returned invalid excluded draft metadata")
            excluded = []
            for entry in excluded_entries:
                if not isinstance(entry, dict):
                    raise RuntimeError("Seeknal returned invalid excluded draft metadata")
                value = entry.get("value")
                normalized = entry.get("normalized")
                python_supported = entry.get("python_supported")
                reason = entry.get("reason")
                if (
                    not isinstance(value, str)
                    or re.fullmatch(r"[a-z][a-z_-]*", value) is None
                    or not isinstance(normalized, str)
                    or re.fullmatch(r"[a-z][a-z_]*", normalized) is None
                    or not isinstance(python_supported, bool)
                    or not isinstance(reason, str)
                    or not reason
                ):
                    raise RuntimeError("Seeknal returned invalid excluded draft metadata")
                excluded.append({
                    "value": value,
                    "normalized": normalized,
                    "python_supported": python_supported,
                    "reason": reason,
                })
            DRAFT_OPTIONS_CACHE = {
                "node_types": options,
                "excluded_node_types": excluded,
            }
        return {
            key: [dict(entry) for entry in entries]
            for key, entries in DRAFT_OPTIONS_CACHE.items()
        }


def validate_draft_request(data: dict) -> dict:
    metadata = draft_options()
    # Each kind is listed once, but older callers may still send the
    # underscore spelling (feature_group); resolve it to the listed value.
    # An alias is honoured only if it names exactly one listed kind and is
    # never the spelling of an excluded kind.
    by_value = {entry["value"]: entry for entry in metadata["node_types"]}
    excluded = {entry["value"] for entry in metadata["excluded_node_types"]}
    aliases: dict[str, list[dict]] = {}
    for entry in metadata["node_types"]:
        for alias in {entry["normalized"], entry["normalized"].replace("_", "-")} - by_value.keys() - excluded:
            aliases.setdefault(alias, []).append(entry)
    by_value.update({alias: entries[0] for alias, entries in aliases.items() if len(entries) == 1})
    node_type = data.get("node_type")
    name = data.get("name")
    description = data.get("description")
    python = data.get("python", False)
    deps = data.get("deps", "")
    if not isinstance(node_type, str) or node_type not in by_value:
        raise ValueError("invalid draft node_type")
    if not isinstance(name, str) or DRAFT_NAME.fullmatch(name) is None:
        raise ValueError("draft name must be a strict identifier up to 128 characters")
    if description is not None and (
        not isinstance(description, str)
        or len(description) > DRAFT_DESCRIPTION_LIMIT
        or any(character in description for character in "\0\r\n")
    ):
        raise ValueError("draft description is invalid or too long")
    if not isinstance(python, bool):
        raise ValueError("draft python must be a boolean")
    if not isinstance(deps, str) or len(deps) > DRAFT_DEPS_LIMIT or any(
        character in deps for character in "\0\r\n"
    ):
        raise ValueError("draft deps are invalid or too long")
    option = by_value[node_type]
    node_type = option["value"]
    if python and not option["python_supported"]:
        raise ValueError(f"Python drafts are not supported for {node_type}")
    if deps and not python:
        raise ValueError("draft deps require python=true")
    return {
        "node_type": node_type,
        "normalized_type": option["normalized"],
        "name": name,
        "description": description,
        "python": python,
        "deps": deps,
    }


def extract_editor_sql(relative: object, content: object) -> dict:
    target = safe_file(relative)
    if (
        target is None
        or not isinstance(relative, str)
        or not isinstance(content, str)
        or target.suffix not in {".yml", ".yaml"}
        or len(content.encode("utf-8")) > MAX_BODY
    ):
        raise ValueError("an active Seeknal YAML file is required")
    result = subprocess.run(
        [str(PYTHON), str(BRIDGE), "extract-sql", str(PROJECT), relative],
        cwd=PROJECT,
        env=clean_environment(),
        input=content,
        capture_output=True,
        text=True,
        timeout=20,
        shell=False,
    )
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip()[-1000:])
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get("sql"), str):
        raise ValueError("transform SQL could not be extracted")
    return value


def editor_diagnostics(data: dict) -> dict:
    """Advisory lint of an editor buffer. Lint problems never fail the request:
    they are reported as an ``unavailable`` reason with no diagnostics."""
    relative = data.get("path")
    content = data.get("content")
    version = data.get("version")
    target = safe_file(relative)
    if target is None or not isinstance(content, str) or target.suffix not in {".yml", ".yaml", ".py"}:
        raise ValueError("a Seeknal project file is required")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("version must be an integer")
    if len(content.encode("utf-8")) > MAX_BODY:
        raise ValueError("editor content exceeds 300 KB")
    started = time.monotonic()
    response = {
        "path": relative, "version": version,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "index_rev": None, "diagnostics": [], "skipped": [],
    }
    if not DIAGNOSTICS_LOCK.acquire(timeout=DIAGNOSTICS_WAIT):
        return {**response, "unavailable": "another lint is still running", "elapsed_ms": 0}
    try:
        result = subprocess.run(
            [str(PYTHON), str(BRIDGE), "diagnostics", str(PROJECT)],
            cwd=PROJECT,
            env=clean_environment(),
            input=json.dumps({"path": relative, "content": content}),
            capture_output=True,
            text=True,
            timeout=DIAGNOSTICS_TIMEOUT,
            shell=False,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
        if isinstance(value, dict) and isinstance(value.get("diagnostics"), list):
            response.update(
                diagnostics=value["diagnostics"],
                skipped=value.get("skipped") if isinstance(value.get("skipped"), list) else [],
                index_rev=value.get("index_rev"),
            )
        else:
            detail = (result.stderr or "").strip().splitlines()[-1:] or ["no output"]
            response["unavailable"] = f"lint failed: {detail[0][:200]}"
    except subprocess.TimeoutExpired:
        response["unavailable"] = f"lint timed out after {DIAGNOSTICS_TIMEOUT} s"
    except (OSError, ValueError) as error:
        response["unavailable"] = f"lint failed: {str(error)[:200]}"
    finally:
        DIAGNOSTICS_LOCK.release()
    response["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return response


class JobStore:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.execution_lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.processes: dict[str, subprocess.Popen] = {}
        self.validated_drafts: set[tuple[str, str]] = set()
        self.last_successful_fingerprint: str | None = None
        if STUDIO_STATE.is_file() and not STUDIO_STATE.is_symlink():
            try:
                saved = json.loads(STUDIO_STATE.read_text()).get("project_fingerprint")
                if saved == freshness_fingerprint():
                    self.last_successful_fingerprint = saved
            except (json.JSONDecodeError, OSError):
                pass

    def create(
        self, action: str, node: str | None = None, *, path: str | None = None,
        content_hash: str | None = None, destination_hash: str | None = None,
        confirmed: bool = False, draft_request: dict | None = None,
    ) -> dict:
        with WORK_ACCEPTANCE_LOCK:
            job_id = uuid.uuid4().hex[:12]
            job = {
                "id": job_id, "action": action, "node": node, "path": path,
                "content_hash": content_hash, "destination_hash": destination_hash,
                "confirmed": confirmed, "status": "queued",
                "draft_request": dict(draft_request) if draft_request is not None else None,
                "logs": "", "exit_code": None, "created_at": time.time(),
                "started_at": None, "finished_at": None,
            }
            with self.lock:
                if (
                    action == "validate-draft"
                    and isinstance(path, str)
                    and isinstance(content_hash, str)
                ):
                    self.validated_drafts.discard((path, content_hash))
                self.jobs[job_id] = job
            threading.Thread(target=self._execute, args=(job_id,), daemon=True).start()
            return dict(job)

    def _execute(self, job_id: str) -> None:
        with self.execution_lock:
            with self.lock:
                job = self.jobs[job_id]
                if job["status"] == "cancelled":
                    return
                job.update(status="running", started_at=time.time())
                action, node, path = job["action"], job["node"], job["path"]
                content_hash = job["content_hash"]
                destination_hash = job["destination_hash"]
                confirmed = job["confirmed"]
                draft_request = job["draft_request"]
                was_validated = (path, content_hash) in self.validated_drafts
                if action == "run":
                    self.last_successful_fingerprint = None
            start_fingerprint = project_fingerprint()
            if action == "run":
                STUDIO_STATE.unlink(missing_ok=True)
            details: dict | None = None
            if action in DRAFT_ACTIONS:
                details = draft_details(path)
                if details is None or details["content_hash"] != content_hash:
                    self._fail(job_id, "Draft changed after review")
                    return
                current_destination_hash = file_hash(details["destination"]) if details["destination"].is_file() else None
                if action == "apply-draft" and current_destination_hash != destination_hash:
                    self._fail(job_id, "Destination changed after review")
                    return
                if action == "apply-draft" and (confirmed is not True or not was_validated):
                    self._fail(job_id, "Explicit confirmation and validation are required")
                    return
            root_draft: Path | None = None
            managed_draft: Path | None = None
            managed_draft_created = False
            structured_validation = False
            if action == "draft":
                if not isinstance(draft_request, dict):
                    self._fail(job_id, "Draft request is missing")
                    return
                suffix = ".py" if draft_request["python"] else ".yml"
                filename = f"draft_{draft_request['normalized_type']}_{draft_request['name']}{suffix}"
                root_draft = PROJECT / filename
                try:
                    drafts_root = _safe_drafts_root(PROJECT)
                except (OSError, ValueError) as error:
                    self._fail(job_id, str(error))
                    return
                managed_draft = drafts_root / filename
                if root_draft.exists() or root_draft.is_symlink():
                    self._fail(job_id, f"Draft file already exists: {filename}")
                    return
                if managed_draft.exists() or managed_draft.is_symlink():
                    self._fail(job_id, f"Managed draft already exists: .seeknal/drafts/{filename}")
                    return
                command = [str(SEEKNAL), "draft", draft_request["node_type"], draft_request["name"]]
                if draft_request["description"] is not None:
                    command.append(f"--description={draft_request['description']}")
                if draft_request["python"]:
                    command.append("--python")
                if draft_request["deps"]:
                    command.append(f"--deps={draft_request['deps']}")
            elif action == "validate-draft":
                assert details is not None
                if details["file"].suffix in {".yml", ".yaml"}:
                    structured_validation = True
                    command = [
                        str(PYTHON), str(BRIDGE), "validate-draft", str(PROJECT), path, job_id,
                    ]
                else:
                    command = [str(SEEKNAL), "dry-run", path, "--limit", "10"]
            elif action == "apply-draft":
                command = [
                    str(PYTHON), str(BRIDGE), "apply-draft", str(PROJECT), path,
                    content_hash, destination_hash if destination_hash is not None else "-",
                ]
            else:
                command = [str(SEEKNAL), *COMMANDS[action]]
            if action == "run" and node:
                command.extend(["--nodes", node])
            process: subprocess.Popen[str] | None = None
            try:
                process = subprocess.Popen(
                    command, cwd=PROJECT, env=clean_environment(), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, shell=False, start_new_session=True,
                )
                with self.lock:
                    self.processes[job_id] = process
                    cancelled_before_start = self.jobs[job_id]["status"] == "cancelled"
                if cancelled_before_start:
                    self._terminate(process)

                def collect_output() -> None:
                    assert process.stdout is not None
                    for line in process.stdout:
                        with self.lock:
                            current = self.jobs[job_id]["logs"] + line
                            self.jobs[job_id]["logs"] = current[-100_000:]

                reader = threading.Thread(target=collect_output, daemon=True)
                reader.start()
                process.wait(timeout=120)
                reader.join(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()
                validation_error: str | None = None
                validation_result: dict | None = None
                if structured_validation:
                    marker = f"__SEEKNAL_STUDIO_VALIDATION_{job_id}__="
                    with self.lock:
                        log_lines = self.jobs[job_id]["logs"].splitlines()
                    marker_lines = [line for line in log_lines if line.startswith(marker)]
                    with self.lock:
                        self.jobs[job_id]["logs"] = "\n".join(
                            line for line in log_lines if not line.startswith(marker)
                        )
                        if log_lines:
                            self.jobs[job_id]["logs"] += "\n"
                    if process.returncode == 0:
                        if len(marker_lines) != 1:
                            validation_error = "The SDK returned no trustworthy validation outcome"
                        else:
                            try:
                                parsed_validation = json.loads(marker_lines[0][len(marker):])
                            except json.JSONDecodeError:
                                parsed_validation = None
                            if not isinstance(parsed_validation, dict):
                                validation_error = "The SDK returned a malformed validation outcome"
                            else:
                                validation_result = parsed_validation
                            if (
                                validation_result is not None
                                and validation_result.get("validation_passed") is not True
                            ):
                                reason = validation_result.get("validation_error")
                                validation_error = (
                                    reason if isinstance(reason, str) and reason
                                    else "The SDK could not execute this pipeline preview"
                                )
                    if validation_error is not None:
                        with self.lock:
                            self.jobs[job_id]["logs"] += (
                                f"[ERR] Studio validation failed: {validation_error}\n"
                            )
                if action == "draft" and not cancelled_before_start:
                    assert root_draft is not None and managed_draft is not None
                    with self.lock:
                        cancelled_draft = self.jobs[job_id]["status"] == "cancelled"
                    if process.returncode == 0 and not cancelled_draft:
                        if (
                            not root_draft.is_file()
                            or root_draft.is_symlink()
                            or root_draft.resolve().parent != PROJECT
                        ):
                            raise RuntimeError("Seeknal did not create the expected regular draft file")
                        if managed_draft.exists() or managed_draft.is_symlink():
                            raise FileExistsError(
                                f"Managed draft already exists: {managed_draft.relative_to(PROJECT)}"
                            )
                        os.link(root_draft, managed_draft)
                        managed_draft_created = True
                        root_draft.unlink()
                        digest = file_hash(managed_draft)
                        with self.lock:
                            self.jobs[job_id].update(
                                draft_path=managed_draft.relative_to(PROJECT).as_posix(),
                                content_hash=digest,
                                node_type=draft_request["node_type"],
                                name=draft_request["name"],
                                python=draft_request["python"],
                            )
                    else:
                        self._note_retained_root_draft(job_id, root_draft)
                end_fingerprint = project_fingerprint()
                current_draft = draft_details(path) if action == "validate-draft" else None
                effective_returncode = 1 if validation_error is not None else process.returncode
                with self.lock:
                    cancelled_after_exit = self.jobs[job_id]["status"] == "cancelled"
                full_run_success = (
                    action == "run" and node is None and process.returncode == 0
                    and start_fingerprint == end_fingerprint and not cancelled_after_exit
                )
                with self.lock:
                    cancelled = self.jobs[job_id]["status"] == "cancelled"
                    if (
                        cancelled
                        and action == "draft"
                        and managed_draft_created
                        and managed_draft is not None
                        and managed_draft.is_file()
                        and not managed_draft.is_symlink()
                    ):
                        managed_draft.unlink()
                        self.jobs[job_id].pop("draft_path", None)
                        self.jobs[job_id]["content_hash"] = None
                    self.jobs[job_id].update(
                        exit_code=effective_returncode,
                        status="cancelled" if cancelled else ("succeeded" if effective_returncode == 0 else "failed"),
                        finished_at=time.time(),
                    )
                    if action == "validate-draft":
                        self.jobs[job_id]["native_exit_code"] = process.returncode
                        if validation_result is not None:
                            preview_status = validation_result.get("preview_status")
                            validation_message = validation_result.get("validation_message")
                            if isinstance(preview_status, str):
                                self.jobs[job_id]["preview_status"] = preview_status
                            if isinstance(validation_message, str) and validation_message:
                                self.jobs[job_id]["validation_message"] = validation_message
                        if validation_error is not None:
                            self.jobs[job_id]["validation_error"] = validation_error
                    if full_run_success and not cancelled:
                        successful = freshness_fingerprint(end_fingerprint)
                        save_successful_fingerprint(successful)
                        self.last_successful_fingerprint = successful
                    elif action == "run":
                        self.last_successful_fingerprint = None
                    if action == "validate-draft" and effective_returncode == 0 and not cancelled:
                        if current_draft is not None and current_draft["content_hash"] == content_hash:
                            self.validated_drafts.add((path, content_hash))
                        else:
                            self.jobs[job_id].update(status="failed", logs=self.jobs[job_id]["logs"] + "\nDraft changed during validation", exit_code=1)
                    if action == "apply-draft" and process.returncode == 0 and not cancelled:
                        self.validated_drafts.discard((path, job["content_hash"]))
                        self.last_successful_fingerprint = None
            except subprocess.TimeoutExpired:
                if process is not None:
                    self._terminate(process)
                self._note_retained_root_draft(job_id, root_draft)
                with self.lock:
                    self.jobs[job_id].update(exit_code=124, status="failed", finished_at=time.time())
            except Exception as error:
                if (
                    managed_draft_created
                    and managed_draft is not None
                    and managed_draft.is_file()
                    and not managed_draft.is_symlink()
                ):
                    managed_draft.unlink()
                with self.lock:
                    logs = self.jobs[job_id]["logs"]
                    separator = "" if not logs or logs.endswith("\n") else "\n"
                    self.jobs[job_id].update(
                        logs=f"{logs}{separator}{error}\n"[-100_000:],
                        exit_code=1, status="failed", finished_at=time.time(),
                    )
                self._note_retained_root_draft(job_id, root_draft)
            finally:
                if process is not None and process.stdout is not None:
                    process.stdout.close()
                with self.lock:
                    self.processes.pop(job_id, None)

    def _note_retained_root_draft(self, job_id: str, root_draft: Path | None) -> None:
        if root_draft is None or not (root_draft.exists() or root_draft.is_symlink()):
            return
        message = f"Unresolved root draft retained for manual review: {root_draft.name}"
        with self.lock:
            logs = self.jobs[job_id]["logs"]
            separator = "" if not logs or logs.endswith("\n") else "\n"
            self.jobs[job_id]["logs"] = f"{logs}{separator}{message}\n"[-100_000:]

    def _fail(self, job_id: str, message: str) -> None:
        with self.lock:
            self.jobs[job_id].update(
                status="failed", logs=message, exit_code=1, finished_at=time.time()
            )

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
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
        # Reaping the direct child is not proof that its descendants stopped.
        # Always finish the owned process group, including TERM-resistant
        # grandchildren whose parent exited promptly.
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.poll() is None:
            process.wait(timeout=2)

    def cancel(self, job_id: str) -> dict | None:
        process = None
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return None
            if job["status"] in {"queued", "running"}:
                job["status"] = "cancelled"
                job["finished_at"] = time.time()
                if job["action"] == "run":
                    self.last_successful_fingerprint = None
                    STUDIO_STATE.unlink(missing_ok=True)
                process = self.processes.get(job_id)
            result = dict(job)
        if process:
            self._terminate(process)
        return result

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(item) for item in sorted(self.jobs.values(), key=lambda item: item["created_at"], reverse=True)]

    def busy(self) -> bool:
        with self.lock:
            return any(job["status"] in {"queued", "running"} for job in self.jobs.values())

    def cancel_all(self) -> None:
        with self.lock:
            job_ids = [job_id for job_id, job in self.jobs.items() if job["status"] in {"queued", "running"}]
        for job_id in job_ids:
            self.cancel(job_id)


JOBS = JobStore()


DEFAULT_AGENT_CONFIG = {
    "provider": "openai",
    "model": "deepseek-v4.1-flash:netra",
    "base_url": "https://ai.sumopod.com/v1",
    "api_key_file": "~/sumopod-ai-api-key",
}


def safe_agent_error(error: Exception) -> str:
    """Keep agent diagnostics useful without reflecting credentials."""
    message = str(error).replace("\n", " ").strip()[-1000:]
    message = re.sub(
        r"(?i)(api[_ -]?key|authorization|bearer)(\s*[:=]\s*)\S+",
        r"\1\2[redacted]", message,
    )
    message = re.sub(
        r"([?&](?:api_?key|token|access_token)=)[^&\s]+",
        r"\1[redacted]", message, flags=re.I,
    )
    return message or error.__class__.__name__


def valid_turn_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


class AgentService:
    """Use a verified, owned agent unless legacy external mode is explicit."""

    def __init__(self) -> None:
        self.mode = (
            "external"
            if os.environ.get("SEEKNAL_STUDIO_AGENT_MODE", "owned").lower() == "external"
            else "owned"
        )
        self.runtime = None
        self.start_error: str | None = None
        self.start_lock = threading.Lock()
        self.status_lock = threading.Lock()
        self.active_calls = 0
        self.cached_status = {
            "mode": self.mode, "configured": False, "healthy": False,
            "ready": False, "project_verified": False,
            "provider": DEFAULT_AGENT_CONFIG["provider"],
            "model": DEFAULT_AGENT_CONFIG["model"],
            "state": "not_started",
        }

    def _cache_status(self, status: dict) -> dict:
        sanitized = {"mode": self.mode, **status}
        with self.status_lock:
            self.cached_status = sanitized
        return dict(sanitized)

    @staticmethod
    def _create_runtime():
        from agent_runtime import AgentRuntime

        return AgentRuntime(
            PROJECT,
            PYTHON,
            ROOT / "agent_worker.py",
            sdk_root=SDK_ROOT,
            sdk_version=RUNTIME.dist_version if RUNTIME else None,
            sdk_module_file=RUNTIME.module_file if RUNTIME else None,
            execution_lock=JOBS.execution_lock,
        )

    def start_default(self) -> dict:
        if self.mode == "external":
            return self.status()
        with self.start_lock:
            try:
                self._cache_status({
                    "configured": True,
                    "healthy": False,
                    "ready": False,
                    "project_verified": False,
                    "provider": DEFAULT_AGENT_CONFIG["provider"],
                    "model": DEFAULT_AGENT_CONFIG["model"],
                    "state": "configuring",
                    "message": "Verifying provider and project identity",
                })
                if self.runtime is None:
                    # AgentRuntime loads its private persisted configuration and
                    # falls back to the same built-in pilot defaults itself.
                    # Do not rewrite persisted settings on every app launch.
                    self.runtime = self._create_runtime()
                status = self.runtime.status()
                if not status.get("ready") or not status.get("project_verified"):
                    raise RuntimeError(status.get("error") or "Owned agent readiness failed")
                self.start_error = None
                self._cache_status({**status, "state": "ready"})
            except Exception as error:
                self.start_error = safe_agent_error(error)
                if self.runtime is not None:
                    self.runtime.close()
                self.runtime = None
                self._cache_status({
                    "configured": False, "healthy": False, "ready": False,
                    "project_verified": False, "provider": DEFAULT_AGENT_CONFIG["provider"],
                    "model": DEFAULT_AGENT_CONFIG["model"], "state": "failed",
                    "error": self.start_error,
                })
        return self.status()

    def status(self) -> dict:
        if self.mode == "external":
            if not os.environ.get("SEEKNAL_GATEWAY_URL"):
                return {
                    "mode": "external", "configured": False, "healthy": False,
                    "ready": False, "project_verified": False,
                    "error": "External gateway URL is not configured",
                }
            try:
                gateway_request("/health", timeout=3)
                return {
                    "mode": "external", "configured": True, "healthy": True,
                    "ready": True, "project_verified": False,
                    "error": "External gateway project identity is unverified",
                }
            except RuntimeError as error:
                return {
                    "mode": "external", "configured": True, "healthy": False,
                    "ready": False, "project_verified": False,
                    "error": safe_agent_error(error),
                }
        if self.runtime is None:
            with self.status_lock:
                return dict(self.cached_status)
        # Status first revokes stale readiness from a nonblocking snapshot. A
        # changed project then schedules one serialized authenticated rebind.
        peek_status = getattr(self.runtime, "peek_status", None)
        if callable(peek_status):
            live = peek_status()
            if not isinstance(live, dict):
                raise RuntimeError("Owned agent returned an invalid status")
            with self.status_lock:
                previous_state = self.cached_status.get("state")
            if live.get("ready"):
                live.update(state="ready")
                live.pop("message", None)
            elif (
                live.get("healthy")
                and not live.get("project_verified")
                and previous_state != "configuring"
                and not live.get("error")
            ):
                live.update(
                    state="configuring",
                    message="Re-verifying changed project identity",
                )
                self._cache_status(live)
                threading.Thread(
                    target=self.start_default,
                    name="seeknal-agent-project-rebind",
                    daemon=True,
                ).start()
                return dict(live)
            elif previous_state == "configuring" and not live.get("error"):
                live.update(
                    state="configuring",
                    message="Verifying provider and project identity",
                )
            else:
                live.update(state="failed" if live.get("error") else "not_ready")
            return self._cache_status(live)
        with self.status_lock:
            return dict(self.cached_status)

    def configure(self, payload: dict) -> dict:
        if self.mode != "owned":
            raise ValueError("Owned agent configuration is unavailable in external mode")
        runtime_payload = dict(payload)
        if "key_path" in runtime_payload:
            runtime_payload["api_key_file"] = runtime_payload.pop("key_path")
        # Configuration and background bootstrap share one transition lock so
        # neither can overwrite the other's runtime or readiness snapshot.
        with self.start_lock:
            if self.runtime is None:
                # Apply a submitted key before any default readiness check.
                self.runtime = self._create_runtime()
            runtime = self.runtime
            try:
                result = runtime.configure(runtime_payload)
                if not result.get("ready") or not result.get("project_verified"):
                    raise RuntimeError(
                        result.get("error") or "Owned agent configuration did not verify readiness"
                    )
                self.start_error = None
                return self._cache_status({**result, "state": "ready"})
            except Exception as error:
                self.start_error = safe_agent_error(error)
                runtime.close()
                if self.runtime is runtime:
                    self.runtime = None
                self._cache_status({
                    "configured": False, "healthy": False, "ready": False,
                    "project_verified": False, "provider": DEFAULT_AGENT_CONFIG["provider"],
                    "model": DEFAULT_AGENT_CONFIG["model"], "state": "failed",
                    "error": self.start_error,
                })
                raise

    def start(self, question: str, session_id: str, turn_id: str) -> dict:
        with self.status_lock:
            self.active_calls += 1
        try:
            if self.mode == "external":
                response = gateway_request(
                    "/ask", {
                        "question": question, "session_id": session_id,
                        "turn_id": turn_id, "attachments": [],
                    },
                    timeout=60,
                )
                response.setdefault("turn_id", turn_id)
                return response
            if self.runtime is None:
                self.start_default()
            if self.runtime is None:
                raise RuntimeError(self.start_error or "Owned agent could not start")
            status = self.status()
            if not status.get("ready") or not status.get("project_verified"):
                raise RuntimeError(status.get("error") or "Owned agent readiness check failed")
            response = self.runtime.start(question, session_id, turn_id=turn_id)
            if response.get("turn_id") != turn_id:
                raise RuntimeError("Owned agent returned a mismatched turn_id")
            return response
        finally:
            with self.status_lock:
                self.active_calls -= 1

    def busy(self) -> bool:
        with self.status_lock:
            if self.active_calls:
                return True
        runtime = self.runtime
        if runtime is None:
            return False
        lock = getattr(runtime, "_lock", None)
        turns = getattr(runtime, "_turns", None)
        if lock is None or not isinstance(turns, dict):
            return False
        with lock:
            return any(turn.get("status") == "running" for turn in turns.values())

    def get(self, session_id: str, turn_id: str | None = None) -> dict:
        if self.mode != "owned" or self.runtime is None:
            raise ValueError("Owned agent session not found")
        session = self.runtime.get(session_id, turn_id=turn_id)
        if session is None:
            raise ValueError("Owned agent session not found")
        if turn_id is not None and session.get("turn_id") != turn_id:
            raise RuntimeError("Owned agent returned a mismatched turn_id")
        return session

    def answer(self, session_id: str, turn_id: str, question_id: str, answer: str) -> dict:
        if self.mode != "owned" or self.runtime is None:
            raise ValueError("Owned agent session not found")
        session = self.runtime.answer(session_id, turn_id, question_id, answer)
        if session.get("turn_id") != turn_id:
            raise RuntimeError("Owned agent returned a mismatched turn_id")
        return session

    def cancel(self, session_id: str, turn_id: str | None = None) -> dict:
        if self.mode == "external":
            response = gateway_request(
                f"/cancel/{session_id}",
                {**({"turn_id": turn_id} if turn_id is not None else {})},
                timeout=10,
            )
            if turn_id is not None:
                response.setdefault("turn_id", turn_id)
            return response
        if self.runtime is None:
            raise ValueError("Owned agent session not found")
        session = self.runtime.cancel(session_id, turn_id=turn_id)
        if session is None:
            raise ValueError("Owned agent session not found")
        if turn_id is not None and session.get("turn_id") != turn_id:
            raise RuntimeError("Owned agent returned a mismatched turn_id")
        return session

    def close(self) -> None:
        if self.runtime is not None:
            self.runtime.close()
            self.runtime = None


AGENT = AgentService()

REPL = ReplService(
    PROJECT,
    PYTHON,
    ROOT / "repl_worker.py",
    clean_environment,
    JOBS.execution_lock,
    sdk_version=RUNTIME.dist_version if RUNTIME else None,
    sdk_module_file=RUNTIME.module_file if RUNTIME else None,
)
REPL_QUERY_IDS: set[str] = set()
REPL_QUERY_LOCK = threading.Lock()


def active_work() -> list[str]:
    active = []
    if JOBS.busy():
        active.append("pipeline job")
    if AGENT.busy():
        active.append("agent turn")
    if REPL.status().get("state") == "starting":
        active.append("REPL startup")
    with REPL_QUERY_LOCK:
        query_ids = list(REPL_QUERY_IDS)
    for query_id in query_ids:
        query = REPL.get(query_id)
        if query is not None and query.get("status") in {"queued", "starting", "running"}:
            if "REPL query" not in active:
                active.append("REPL query")
        else:
            with REPL_QUERY_LOCK:
                REPL_QUERY_IDS.discard(query_id)
    return active


def environment_runtime() -> dict[str, object]:
    repl = REPL.status()
    return {
        "seeknal_version": seeknal_version(),
        "python": str(PYTHON),
        "repl_state": repl.get("state"),
    }


ENVIRONMENT = EnvironmentService(
    PROJECT,
    busy=active_work,
    runtime=environment_runtime,
)


control_value = os.environ.get("SEEKNAL_STUDIO_CONTROL_FILE")
PROJECTS = ProjectService(
    PROJECT,
    busy=active_work,
    control_path=Path(control_value) if control_value else None,
    control_nonce=os.environ.get("SEEKNAL_STUDIO_CONTROL_NONCE"),
    notice=NOTICE,
)


def gateway_request(path: str, payload: dict | None = None, timeout: int = 30) -> dict:
    base = os.environ.get("SEEKNAL_GATEWAY_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("Seeknal gateway is not configured")
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    token = os.environ.get("SEEKNAL_GATEWAY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"gateway HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"gateway unavailable: {error.reason}") from error


class Handler(NoStoreMixin, SimpleHTTPRequestHandler):
    server_version = "SeeknalStudio/0.1"

    def trusted(self) -> bool:
        if ACCESS_TOKEN is not None and not hmac.compare_digest(
            self.headers.get("X-Seeknal-Studio-Token", ""), ACCESS_TOKEN
        ):
            return False
        return self.headers.get("Host") in HOSTS and self.headers.get("Origin") in {None, *ORIGINS}

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def payload(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 0 or length > MAX_BODY:
            raise ValueError("request too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    def do_HEAD(self) -> None:
        if not self.trusted():
            self.send_json({"error": "loopback origin required"}, 403)
            return
        if BROWSER_ACCESS.admit(self, PORT):
            super().do_HEAD()

    def do_GET(self) -> None:
        if not self.trusted():
            self.send_json({"error": "loopback origin required"}, 403)
            return
        if not BROWSER_ACCESS.admit(self, PORT, bootstrap=True):
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/state":
                agent = AGENT.status()
                self.send_json({
                    "project": PROJECT.name, "project_path": str(PROJECT),
                    "runtime_id": RUNTIME_ID, "seeknal_version": seeknal_version(),
                    "files": project_files(), "notice": NOTICE,
                    "gateway": {
                        "configured": bool(agent.get("configured")),
                        "project_verified": bool(agent.get("project_verified")),
                        "mode": agent.get("mode"),
                    },
                })
            elif parsed.path == "/api/activity":
                self.send_json({"active": active_work()})
            elif parsed.path == "/api/file":
                values = parse_qs(parsed.query)
                target = safe_file(values.get("path", [None])[0])
                if target is None:
                    self.send_json({"error": "file not found"}, 404)
                else:
                    # One read: the hash must describe exactly the content served,
                    # or a teammate's save in between would be overwritten later.
                    raw = target.read_bytes()
                    self.send_json({
                        "path": values.get("path", [None])[0],
                        "content": raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"),
                        "content_hash": hashlib.sha256(raw).hexdigest(),
                    })
            elif parsed.path == "/api/graph":
                result = bridge("graph")
                fresh = JOBS.last_successful_fingerprint == freshness_fingerprint()
                result["fresh"] = fresh
                if not fresh:
                    for node in result["nodes"]:
                        if node["status"] in {"success", "cached"}:
                            node["status"] = "stale"
                self.send_json(result)
            elif parsed.path == "/api/preview":
                node = parse_qs(parsed.query).get("node", [""])[0]
                result = bridge("preview", node)
                fresh = JOBS.last_successful_fingerprint == freshness_fingerprint()
                result.update(fresh=fresh, freshness="current" if fresh else "stale")
                if fresh:
                    self.send_json(result)
                else:
                    self.send_json(
                        {**result, "rows": [], "error": "Output is stale; run the pipeline after the latest edit"},
                        409,
                    )
            elif parsed.path == "/api/diff":
                relative = parse_qs(parsed.query).get("path", [None])[0]
                if not isinstance(relative, str):
                    raise ValueError("draft path is required")
                details = draft_details(relative)
                if details is None:
                    self.send_json({"error": "draft not found"}, 404)
                else:
                    self.send_json(draft_diff(details))
            elif parsed.path == "/api/drafts":
                drafts = []
                errors = []
                for item in project_files():
                    if item["path"].startswith(".seeknal/drafts/"):
                        try:
                            details = draft_details(item["path"])
                        except (OSError, RuntimeError, ValueError) as error:
                            errors.append({"path": item["path"], "error": safe_agent_error(error)})
                            continue
                        if details is None:
                            continue
                        drafts.append({**item, "content_hash": details["content_hash"]})
                self.send_json({"drafts": drafts, "errors": errors})
            elif parsed.path == "/api/drafts/options":
                self.send_json(draft_options())
            elif parsed.path == "/api/projects":
                self.send_json(PROJECTS.status())
            elif parsed.path == "/api/workspaces":
                self.send_json(PROJECTS.workspaces_status())
            elif parsed.path == "/api/environment":
                self.send_json(ENVIRONMENT.status())
            elif parsed.path == "/api/repl/status":
                self.send_json(REPL.status())
            elif parsed.path == "/api/repl/query":
                query_id = parse_qs(parsed.query).get("id", [None])[0]
                if not isinstance(query_id, str):
                    raise ValueError("query id is required")
                else:
                    query = REPL.get(query_id)
                    self.send_json(query) if query else self.send_json({"error": "query not found"}, 404)
            elif parsed.path == "/api/jobs":
                self.send_json({"jobs": JOBS.list()})
            elif parsed.path.startswith("/api/jobs/"):
                job = JOBS.get(parsed.path.rsplit("/", 1)[-1])
                self.send_json(job) if job else self.send_json({"error": "job not found"}, 404)
            elif parsed.path == "/api/agent/status":
                self.send_json(AGENT.status())
            elif parsed.path.startswith("/api/agent/sessions/"):
                session_id = parsed.path.rsplit("/", 1)[-1]
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
                    raise ValueError("invalid session_id")
                turn_id = parse_qs(parsed.query).get("turn_id", [None])[0]
                if turn_id is not None and not valid_turn_id(turn_id):
                    raise ValueError("invalid turn_id")
                self.send_json(AGENT.get(session_id, turn_id))
            else:
                if parsed.path == "/":
                    self.path = "/index.html"
                super().do_GET()
        except (RuntimeError, FileNotFoundError, ValueError) as error:
            self.send_json({"error": str(error)}, 400)

    def do_POST(self) -> None:
        if not self.trusted():
            self.send_json({"error": "loopback origin required"}, 403)
            return
        if not BROWSER_ACCESS.admit(self, PORT):
            return
        try:
            data = self.payload()
            if self.path == "/api/file":
                target = safe_file(data.get("path"))
                content = data.get("content")
                base_hash = data.get("base_hash")
                if target is None or not isinstance(content, str):
                    raise ValueError("invalid editable file")
                if not isinstance(base_hash, str) or re.fullmatch(r"[0-9a-f]{64}", base_hash) is None:
                    raise ValueError("base_hash of the file as it was opened is required")
                if not JOBS.execution_lock.acquire(blocking=False):
                    busy = "An agent turn is running (it may be waiting for your answer)" if AGENT.busy() \
                        else "A pipeline job is running"
                    self.send_json({"error": f"{busy}; save after it finishes"}, 409)
                    return
                try:
                    # Someone else (another Studio, a terminal, git) may have
                    # changed the file since it was opened: never overwrite that.
                    if file_hash(target) != base_hash:
                        raise EditorConflictError(
                            "File changed on disk since you opened it, so your edits were not saved. "
                            "They are still in the editor: copy them, close and reopen the file, then reapply them."
                        )
                    # A per-save temp name keeps concurrent backends from
                    # interleaving writes; the file keeps its permissions.
                    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.studio.tmp")
                    try:
                        with temporary.open("x", encoding="utf-8") as handle:
                            handle.write(content)
                        os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
                    saved_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                finally:
                    JOBS.execution_lock.release()
                fresh = JOBS.last_successful_fingerprint == freshness_fingerprint()
                self.send_json({
                    "saved": True, "path": data["path"], "bytes": len(content.encode()),
                    "content_hash": saved_hash, "preview_fresh": fresh,
                })
            elif self.path == "/api/editor/prepare":
                if not JOBS.execution_lock.acquire(blocking=False):
                    self.send_json({"error": "Active work must finish before preparing editor changes"}, 409)
                    return
                try:
                    result = prepare_editor_snapshot(
                        data.get("path"), data.get("content"), data.get("original_content"),
                    )
                finally:
                    JOBS.execution_lock.release()
                status = 201 if result.get("snapshot", {}).get("mode") == "active" else 200
                self.send_json(result, status)
            elif self.path == "/api/drafts/from-editor":
                if not JOBS.execution_lock.acquire(blocking=False):
                    self.send_json({"error": "Active work must finish before creating a draft"}, 409)
                    return
                try:
                    result = create_editor_draft(data.get("path"), data.get("content"))
                finally:
                    JOBS.execution_lock.release()
                self.send_json(result, 201)
            elif self.path == "/api/projects/browse":
                self.send_json(PROJECTS.browse())
            elif self.path == "/api/projects/open":
                try:
                    with WORK_ACCEPTANCE_LOCK:
                        project = PROJECTS.prepare_switch(data.get("path"))
                        PROJECTS.queue_switch(project)
                except RuntimeError as error:
                    self.send_json({"error": str(error)}, 409)
                    return
                self.send_json({"switching": True, "project": {"path": str(project), "name": project.name}}, 202)
            elif self.path == "/api/workspaces":
                probed = PROJECTS.probe_workspace_request(data.get("host"), data.get("path"), data.get("ssh_port"))
                with WORK_ACCEPTANCE_LOCK:
                    workspace = PROJECTS.save_workspace(
                        data.get("name"), data.get("path"), data.get("id"),
                        data.get("host"), data.get("ssh_port"), probed,
                    )
                self.send_json({"workspace": workspace})
            elif self.path == "/api/workspaces/remove":
                with WORK_ACCEPTANCE_LOCK:
                    result = PROJECTS.remove_workspace(data.get("id"))
                self.send_json(result)
            elif self.path == "/api/workspaces/open":
                try:
                    probed = PROJECTS.probe_workspace(data.get("id"))
                    with WORK_ACCEPTANCE_LOCK:
                        project, workspace = PROJECTS.prepare_workspace_switch(data.get("id"), probed)
                        PROJECTS.queue_switch(project)
                except RuntimeError as error:
                    self.send_json({"error": str(error)}, 409)
                    return
                self.send_json({"switching": True, "workspace": workspace}, 202)
            elif self.path == "/api/environment":
                reloaded: dict[str, object] | None = None
                try:
                    with WORK_ACCEPTANCE_LOCK:
                        repl_was_ready = REPL.status().get("ready") is True
                        result = ENVIRONMENT.update(data)
                        if repl_was_ready:
                            reloaded = REPL.reload()
                except EnvironmentBusyError as error:
                    self.send_json({
                        "error": safe_agent_error(error),
                        "error_code": "environment_busy",
                    }, 409)
                    return
                except EnvironmentConflictError as error:
                    self.send_json({
                        "error": safe_agent_error(error),
                        "error_code": "environment_conflict",
                    }, 409)
                    return
                if repl_was_ready and reloaded is not None:
                    result["runtime"] = environment_runtime()
                    result["reload"] = {
                        "required": False,
                        "state": reloaded.get("state"),
                        "message": "The running REPL is reloading with the saved project environment.",
                    }
                else:
                    result["reload"] = {
                        "required": False,
                        "message": "Saved values apply when the REPL or next Seeknal CLI process starts.",
                    }
                self.send_json(result)
            elif self.path == "/api/repl/start":
                with WORK_ACCEPTANCE_LOCK:
                    result = REPL.start()
                self.send_json(result, 202)
            elif self.path == "/api/repl/query":
                sql = data.get("sql")
                if not isinstance(sql, str):
                    raise ValueError("sql is required")
                else:
                    with WORK_ACCEPTANCE_LOCK:
                        query = REPL.execute(sql)
                        with REPL_QUERY_LOCK:
                            REPL_QUERY_IDS.add(query["query_id"])
                    self.send_json(query, 202)
            elif self.path == "/api/diagnostics":
                self.send_json(editor_diagnostics(data))
            elif self.path == "/api/repl/extract":
                self.send_json(extract_editor_sql(data.get("path"), data.get("content")))
            elif self.path == "/api/repl/reload":
                with WORK_ACCEPTANCE_LOCK:
                    result = REPL.reload()
                self.send_json(result, 202)
            elif self.path.startswith("/api/repl/query/") and self.path.endswith("/cancel"):
                query_id = self.path.split("/")[-2]
                query = REPL.cancel(query_id)
                self.send_json(query) if query else self.send_json({"error": "query not found"}, 404)
            elif self.path == "/api/repl/cancel":
                query_id = data.get("id") or data.get("query_id")
                if not isinstance(query_id, str):
                    raise ValueError("query id is required")
                query = REPL.cancel(query_id)
                self.send_json(query) if query else self.send_json({"error": "query not found"}, 404)
            elif self.path == "/api/jobs":
                action, node = data.get("action"), data.get("node")
                if not isinstance(action, str):
                    raise ValueError("invalid job request")
                if action == "draft":
                    self.send_json(JOBS.create(
                        action, draft_request=validate_draft_request(data)
                    ), 202)
                    return
                valid_node = node is None or (
                    isinstance(node, str) and re.fullmatch(r"[a-z_]+\.[A-Za-z0-9_-]+", node)
                )
                if action in DRAFT_ACTIONS:
                    details = draft_details(data.get("path"))
                    expected_hash = data.get("content_hash")
                    if details is None or expected_hash != details["content_hash"]:
                        raise ValueError("draft changed; review the latest diff")
                    if action == "apply-draft":
                        if data.get("confirmed") is not True:
                            raise ValueError("apply requires confirmed=true")
                        if (details["path"], expected_hash) not in JOBS.validated_drafts:
                            raise ValueError("validate this exact draft before apply")
                        current_destination_hash = file_hash(details["destination"]) if details["destination"].is_file() else None
                        if data.get("destination_hash") != current_destination_hash:
                            raise ValueError("destination changed; review the latest diff")
                    self.send_json(JOBS.create(
                        action, path=details["path"], content_hash=expected_hash,
                        destination_hash=data.get("destination_hash"),
                        confirmed=data.get("confirmed") is True,
                    ), 202)
                    return
                if action not in COMMANDS or not valid_node:
                    raise ValueError("invalid job request")
                self.send_json(JOBS.create(action, node), 202)
            elif self.path.startswith("/api/jobs/") and self.path.endswith("/cancel"):
                job_id = self.path.split("/")[-2]
                job = JOBS.cancel(job_id)
                self.send_json(job) if job else self.send_json({"error": "job not found"}, 404)
            elif self.path == "/api/agent/ask":
                question = data.get("question")
                session_id = data.get("session_id")
                turn_id = data.get("turn_id")
                if (
                    not isinstance(question, str)
                    or not question.strip()
                    or not isinstance(session_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)
                    or not valid_turn_id(turn_id)
                ):
                    raise ValueError("question, session_id, and UUID turn_id are required")
                assert isinstance(turn_id, str)
                with WORK_ACCEPTANCE_LOCK:
                    response = AGENT.start(question.strip(), session_id, turn_id)
                if response.get("turn_id") != turn_id:
                    raise RuntimeError("Agent response turn_id does not match the request")
                self.send_json(response, 202 if AGENT.mode == "owned" else 200)
            elif self.path == "/api/agent/answer":
                session_id = data.get("session_id")
                turn_id = data.get("turn_id")
                if (
                    not isinstance(session_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)
                    or not valid_turn_id(turn_id)
                ):
                    raise ValueError("session_id and UUID turn_id are required")
                assert isinstance(turn_id, str)
                self.send_json(AGENT.answer(session_id, turn_id, data.get("question_id"), data.get("answer")))
            elif self.path == "/api/agent/config":
                with WORK_ACCEPTANCE_LOCK:
                    result = AGENT.configure(data)
                self.send_json(result)
            elif self.path == "/api/agent/cancel":
                session_id = data.get("session_id")
                turn_id = data.get("turn_id")
                if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
                    raise ValueError("session_id is required")
                if AGENT.mode == "owned" and not valid_turn_id(turn_id):
                    raise ValueError("UUID turn_id is required")
                if turn_id is not None and not valid_turn_id(turn_id):
                    raise ValueError("invalid turn_id")
                self.send_json(AGENT.cancel(session_id, turn_id))
            else:
                self.send_json({"error": "unknown endpoint"}, 404)
        except EditorConflictError as error:
            self.send_json({"error": safe_agent_error(error), "error_code": "editor_conflict"}, 409)
        except (json.JSONDecodeError, ValueError, RuntimeError) as error:
            self.send_json({"error": safe_agent_error(error)}, 400)

    def translate_path(self, path: str) -> str:
        relative = urlparse(path).path.lstrip("/") or "index.html"
        return str(ROOT / relative) if relative in STATIC else str(ROOT / "__not_found__")

    def log_message(self, format: str, *args: object) -> None:
        pass


def bind_ephemeral_port(port: int) -> None:
    global PORT, HOSTS, ORIGINS
    PORT = port
    HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
    port_file = os.environ.pop("SEEKNAL_STUDIO_PORT_FILE", None)
    if port_file:
        descriptor = os.open(port_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(f"{PORT}\n")


def main() -> None:
    if RUNTIME is None:
        raise SystemExit(f"Seeknal SDK 2.14.0 runtime is unavailable: {RUNTIME_ERROR}")
    if not SEEKNAL.is_file() or not PYTHON.is_file():
        raise SystemExit("Resolved Seeknal SDK 2.14.0 runtime is incomplete")
    if not PROJECT.is_dir():
        raise SystemExit(f"Project does not exist: {PROJECT}")
    server = http_server(PORT, Handler)
    if PORT == 0:
        # Remote (SSH) backends bind an OS-assigned port so no other user can
        # race for a pre-selected one, then report it through a private file.
        bind_ephemeral_port(server.server_address[1])

    def shutdown(_signum: int, _frame: object) -> None:
        JOBS.cancel_all()
        AGENT.close()
        REPL.close()
        PROJECTS.close()
        # Interrupt provider readiness as well as serve_forever. The finally
        # block below performs idempotent cleanup for either lifecycle phase.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    agent_bootstrap = threading.Thread(
        target=AGENT.start_default, name="seeknal-agent-bootstrap", daemon=True,
    )
    try:
        agent_bootstrap.start()
        print(f"Seeknal Studio: http://127.0.0.1:{PORT}", flush=True)
        server.serve_forever()
    finally:
        JOBS.cancel_all()
        AGENT.close()
        REPL.close()
        PROJECTS.close()
        if agent_bootstrap.is_alive():
            agent_bootstrap.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
