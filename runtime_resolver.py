"""Resolve and verify the isolated Seeknal SDK runtime used by Studio."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Mapping, Optional, Tuple, Union


ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / ".runtime"
DEFAULT_CONFIG = RUNTIME_DIR / "runtime-config.json"
SUPPORTED_SEEKNAL_VERSION = "2.14.0"


class RuntimeResolutionError(RuntimeError):
    """Raised when Studio cannot prove a supported Seeknal runtime identity."""


@dataclass(frozen=True)
class RuntimeIdentity:
    python: Path
    seeknal: Path
    sdk_root: Path
    module_file: Path
    version: str
    dist_version: str
    prefix: Path


@dataclass(frozen=True)
class RuntimeInfo:
    python: Path
    seeknal: Path
    sdk_root: Path
    module_file: Path
    version: str
    dist_version: str
    provenance: str


_IDENTITY_SCRIPT = r"""
import importlib.metadata as metadata
import importlib.util
import json
from pathlib import Path
import sys

dist_version = metadata.version("seeknal")
spec = importlib.util.find_spec("seeknal")
if spec is None or spec.origin is None:
    raise RuntimeError("the seeknal distribution has no importable seeknal module")
module_file = Path(spec.origin).resolve(strict=True)
prefix = Path(sys.prefix).resolve(strict=True)
try:
    module_file.relative_to(prefix)
except ValueError as exc:
    raise RuntimeError(
        f"seeknal module is outside the selected interpreter prefix: {module_file}"
    ) from exc
seeknal = prefix / "bin" / "seeknal"
if not seeknal.is_file():
    raise RuntimeError(f"seeknal CLI is missing from the selected runtime: {seeknal}")
print(json.dumps({
    "python": sys.executable,
    "seeknal": str(seeknal),
    "sdk_root": str(module_file.parent),
    "module_file": str(module_file),
    "version": dist_version,
    "dist_version": dist_version,
    "prefix": str(prefix),
}))
"""


def _absolute_path(value: Union[str, os.PathLike], *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeResolutionError(f"{label} must be an absolute path: {path}")
    # Collapse ``..`` without resolving the managed venv's intentional Python
    # symlink to its base interpreter outside the venv.
    return Path(os.path.abspath(path))


def runtime_identity(python: Union[Path, str]) -> RuntimeIdentity:
    """Read identity through *python* without importing the Seeknal SDK here."""

    executable = _absolute_path(python, label="runtime python")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeResolutionError(f"runtime python is not executable: {executable}")
    try:
        completed = subprocess.run(
            [str(executable), "-I", "-c", _IDENTITY_SCRIPT],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        payload = json.loads(completed.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise RuntimeResolutionError(
            f"cannot identify Seeknal runtime {executable}: {detail.strip()}"
        ) from exc

    try:
        return RuntimeIdentity(
            python=_absolute_path(payload["python"], label="identified python"),
            seeknal=_absolute_path(payload["seeknal"], label="identified seeknal CLI"),
            sdk_root=_absolute_path(payload["sdk_root"], label="identified SDK root"),
            module_file=_absolute_path(payload["module_file"], label="identified module"),
            version=str(payload["version"]),
            dist_version=str(payload["dist_version"]),
            prefix=_absolute_path(payload["prefix"], label="identified prefix"),
        )
    except (KeyError, TypeError, RuntimeResolutionError) as exc:
        raise RuntimeResolutionError(
            f"runtime identity from {executable} is malformed"
        ) from exc


def _config_python(config_path: Path) -> Tuple[Path, str, Optional[Path]]:
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        python = _absolute_path(payload["python"], label="configured runtime python")
        runtime_root_value = payload.get("runtime_root")
        runtime_root = (
            _absolute_path(runtime_root_value, label="configured runtime root")
            if runtime_root_value is not None
            else None
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeResolutionError(f"invalid runtime config {config_path}: {exc}") from exc
    return python, str(payload.get("provenance") or f"config:{config_path}"), runtime_root


def resolve_runtime(
    config_path: Optional[Union[Path, str]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> RuntimeInfo:
    """Resolve Studio's selected runtime and require the supported SDK version.

    Overrides are intentionally explicit. Studio never falls back to the editable
    Signal checkout when its managed runtime is missing or invalid.
    """

    env = os.environ if environ is None else environ
    if env.get("SEEKNAL_RUNTIME_PYTHON"):
        python = _absolute_path(
            env["SEEKNAL_RUNTIME_PYTHON"], label="SEEKNAL_RUNTIME_PYTHON"
        )
        provenance = "override:SEEKNAL_RUNTIME_PYTHON"
    elif env.get("SEEKNAL_SIGNAL"):
        signal_root = _absolute_path(env["SEEKNAL_SIGNAL"], label="SEEKNAL_SIGNAL")
        python = signal_root / ".venv" / "bin" / "python"
        provenance = "override:SEEKNAL_SIGNAL"
    else:
        selected_config = config_path or env.get("SEEKNAL_STUDIO_RUNTIME_CONFIG")
        config = _absolute_path(
            selected_config or DEFAULT_CONFIG,
            label="runtime config",
        )
        python, provenance, runtime_root = _config_python(config)
        if config == DEFAULT_CONFIG:
            if runtime_root is None:
                raise RuntimeResolutionError(
                    f"bundled runtime config must declare runtime_root: {config}"
                )
            managed_venv = runtime_root / "venv"
            try:
                python.relative_to(managed_venv)
            except ValueError as exc:
                raise RuntimeResolutionError(
                    f"bundled runtime python escapes {managed_venv}: {python}"
                ) from exc

    identity = runtime_identity(python)
    if identity.dist_version != SUPPORTED_SEEKNAL_VERSION:
        raise RuntimeResolutionError(
            "unsupported Seeknal runtime: "
            f"expected {SUPPORTED_SEEKNAL_VERSION}, got {identity.dist_version} "
            f"from {identity.module_file} ({provenance})"
        )
    return RuntimeInfo(
        python=identity.python,
        seeknal=identity.seeknal,
        sdk_root=identity.sdk_root,
        module_file=identity.module_file,
        version=identity.version,
        dist_version=identity.dist_version,
        provenance=provenance,
    )
