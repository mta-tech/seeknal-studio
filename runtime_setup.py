#!/usr/bin/env python3
"""Create and verify Studio's isolated, reproducible Seeknal SDK runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from runtime_resolver import (
    DEFAULT_CONFIG,
    ROOT,
    RUNTIME_DIR,
    SUPPORTED_SEEKNAL_VERSION,
    resolve_runtime,
)


VENV = RUNTIME_DIR / "venv"
LOCK = ROOT / "requirements-runtime.lock.txt"


def _uv() -> Path:
    configured = os.environ.get("UV")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path.home() / ".local" / "bin" / "uv")
    discovered = shutil.which("uv")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("uv is required to build the managed Seeknal runtime")


def _run(command: list[str], *, env: dict[str, str]) -> None:
    subprocess.run(command, check=True, cwd=ROOT, env=env)


def ensure_runtime(source_python: Path) -> dict[str, str]:
    if not source_python.is_absolute() or not source_python.is_file():
        raise RuntimeError(f"source Python must be an existing absolute path: {source_python}")
    uv = _uv()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "UV_CACHE_DIR": str(RUNTIME_DIR / "uv-cache"),
    }
    managed_python = VENV / "bin" / "python"
    if not managed_python.is_file():
        _run(
            [str(uv), "venv", "--python", str(source_python), str(VENV)],
            env=env,
        )
    if not LOCK.is_file():
        raise RuntimeError(f"managed runtime lockfile is missing: {LOCK}")
    _run(
        [
            str(uv), "pip", "sync", "--python", str(managed_python),
            "--require-hashes", str(LOCK),
        ],
        env=env,
    )
    config = {
        "schema_version": 1,
        "python": str(managed_python.absolute()),
        "runtime_root": str(RUNTIME_DIR.absolute()),
        "expected_seeknal_version": SUPPORTED_SEEKNAL_VERSION,
        "provenance": "managed-pypi",
    }
    DEFAULT_CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    runtime = resolve_runtime(environ={})
    return {
        "python": str(runtime.python),
        "seeknal": str(runtime.seeknal),
        "sdk_root": str(runtime.sdk_root),
        "module_file": str(runtime.module_file),
        "version": runtime.version,
        "dist_version": runtime.dist_version,
        "provenance": runtime.provenance,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, default=Path(sys.executable).absolute())
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        runtime = resolve_runtime(environ={})
        payload = {
            "python": str(runtime.python), "seeknal": str(runtime.seeknal),
            "sdk_root": str(runtime.sdk_root), "module_file": str(runtime.module_file),
            "version": runtime.version, "dist_version": runtime.dist_version,
            "provenance": runtime.provenance,
        }
    else:
        payload = ensure_runtime(args.python.absolute())
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
