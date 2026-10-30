#!/usr/bin/env python3
"""Open the completed synthetic Seeknal Studio pilot safely."""

from __future__ import annotations

import os
import json
import hashlib
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
WORKSPACE_PATH = ROOT / "pilot" / "workspace"
TEMPLATE_PATH = ROOT / "pilot" / "template"
BUNDLE_EXECUTABLE = ROOT / "dist" / "Seeknal Studio.app" / "Contents" / "MacOS" / "Seeknal Studio"
PILOT_ENVIRONMENT_EXCLUSIONS = ("ATLAS_API_URL", "ATLAS_PORTAL_URL")
AUTHORITY_ARTIFACTS = (
    "seeknal_project.yml",
    "data/orders.csv",
    "data/customers.csv",
    "seeknal/sources/orders.yml",
    "seeknal/sources/customers.yml",
    "SEEKNAL_ASK.md",
    "EXPECTED_OUTPUT.json",
)
EXPECTED_PIPELINE_DEFINITIONS = {
    "sources/customers.yml",
    "sources/orders.yml",
    "transforms/customer_value_pilot.yml",
}


def _reject_symlinks(root: Path, label: str) -> None:
    if root.is_symlink():
        raise RuntimeError(f"{label} path is a symlink")
    for directory, names, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in (*names, *files):
            if (base / name).is_symlink():
                raise RuntimeError(f"{label} contains a symlink: {(base / name).relative_to(root)}")


def validate_workspace() -> Path:
    _reject_symlinks(WORKSPACE_PATH, "pilot workspace")
    _reject_symlinks(TEMPLATE_PATH, "pilot template")
    if not WORKSPACE_PATH.is_dir() or not TEMPLATE_PATH.is_dir():
        raise RuntimeError("pilot workspace or source-only template is missing")
    workspace = WORKSPACE_PATH.resolve()
    template = TEMPLATE_PATH.resolve()
    expected_root = (ROOT / "pilot").resolve()
    if expected_root not in workspace.parents or expected_root not in template.parents:
        raise RuntimeError("pilot workspace path is unsafe")
    for relative in AUTHORITY_ARTIFACTS:
        source = template / relative
        actual = workspace / relative
        if not source.is_file() or not actual.is_file():
            raise RuntimeError(f"pilot workspace is incomplete or unsafe: {relative}")
        if source.read_bytes() != actual.read_bytes():
            raise RuntimeError(f"pilot authority artifact changed: {relative}")
    definitions = {
        path.relative_to(workspace / "seeknal").as_posix()
        for path in (workspace / "seeknal").rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml", ".py"}
    }
    if definitions != EXPECTED_PIPELINE_DEFINITIONS:
        raise RuntimeError("pilot pipeline definition set changed")
    return workspace


def pilot_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if source is None else source)
    for name in PILOT_ENVIRONMENT_EXCLUSIONS:
        environment.pop(name, None)
    return environment


def write_baseline_receipt(directory: Path, workspace: Path, source: dict[str, str]) -> tuple[Path, str]:
    nonce = secrets.token_hex(32)
    receipt = directory / "baseline.json"
    value = {
        "nonce": nonce,
        "pilot_project": str(workspace),
        "governance": {
            name: {"present": name in source, "value": source.get(name)}
            for name in PILOT_ENVIRONMENT_EXCLUSIONS
        },
        "pilot_authority": {
            "sha256": {
                relative: hashlib.sha256((workspace / relative).read_bytes()).hexdigest()
                for relative in AUTHORITY_ARTIFACTS
            },
            "definitions": sorted(EXPECTED_PIPELINE_DEFINITIONS),
        },
    }
    descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return receipt, nonce


def main() -> int:
    if len(sys.argv) != 1:
        print("Seeknal Pilot does not accept project or runtime arguments.", file=sys.stderr)
        return 2
    workspace = validate_workspace()
    if not BUNDLE_EXECUTABLE.is_file():
        print("Seeknal Studio.app is missing; rebuild it with python3 build_app.py.", file=sys.stderr)
        return 1
    print(f"Opening verified synthetic pilot: {workspace}", flush=True)
    try:
        baseline = os.environ.copy()
        with tempfile.TemporaryDirectory(prefix="seeknal-pilot-") as temporary_name:
            temporary = Path(temporary_name)
            os.chmod(temporary, 0o700)
            receipt, nonce = write_baseline_receipt(temporary, workspace, baseline)
            environment = pilot_environment(baseline)
            environment["SEEKNAL_STUDIO_BASELINE_RECEIPT"] = str(receipt)
            environment["SEEKNAL_STUDIO_BASELINE_NONCE"] = nonce
            result = subprocess.run(
                [str(BUNDLE_EXECUTABLE), "--project", str(workspace)],
                cwd=ROOT,
                env=environment,
                check=False,
            )
    except OSError as error:
        print(f"Could not launch Seeknal Pilot: {error}", file=sys.stderr)
        return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
