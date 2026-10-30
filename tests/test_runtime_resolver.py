from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import runtime_resolver
from runtime_resolver import (
    RuntimeIdentity,
    RuntimeResolutionError,
    resolve_runtime,
    runtime_identity,
)


ROOT = Path(__file__).resolve().parents[1]
MANAGED_PYTHON = ROOT / ".runtime" / "venv" / "bin" / "python"


def identity(python: Path, version: str = "2.14.0") -> RuntimeIdentity:
    prefix = python.parent.parent
    module = prefix / "lib/python3.11/site-packages/seeknal/__init__.py"
    return RuntimeIdentity(
        python=python,
        seeknal=prefix / "bin/seeknal",
        sdk_root=module.parent,
        module_file=module,
        version=version,
        dist_version=version,
        prefix=prefix,
    )


class RuntimeResolverTests(unittest.TestCase):
    def test_real_managed_runtime_identity_is_published_distribution(self) -> None:
        self.assertTrue(MANAGED_PYTHON.is_file(), "run runtime_setup.py first")
        actual = runtime_identity(MANAGED_PYTHON)
        self.assertEqual("2.14.0", actual.version)
        self.assertEqual("2.14.0", actual.dist_version)
        self.assertEqual("seeknal", actual.sdk_root.name)
        self.assertEqual("__init__.py", actual.module_file.name)
        self.assertIn("site-packages", actual.module_file.parts)
        self.assertNotIn("/project/mta/signal/src/", str(actual.module_file))

    def test_default_config_resolves_managed_runtime(self) -> None:
        actual = resolve_runtime(environ={})
        self.assertEqual(MANAGED_PYTHON, actual.python)
        self.assertEqual("managed-pypi", actual.provenance)
        self.assertEqual("2.14.0", actual.dist_version)

    def test_explicit_python_override_has_precedence(self) -> None:
        override = Path("/opt/studio-runtime/bin/python")
        with mock.patch.object(
            runtime_resolver, "runtime_identity", return_value=identity(override)
        ) as identify:
            actual = resolve_runtime(
                environ={
                    "SEEKNAL_RUNTIME_PYTHON": str(override),
                    "SEEKNAL_SIGNAL": "/ignored/signal",
                }
            )
        identify.assert_called_once_with(override)
        self.assertEqual("override:SEEKNAL_RUNTIME_PYTHON", actual.provenance)

    def test_legacy_signal_override_is_explicit_and_reports_actual_mismatch(self) -> None:
        python = Path("/workspace/signal/.venv/bin/python")
        with mock.patch.object(
            runtime_resolver,
            "runtime_identity",
            return_value=identity(python, version="2.12.0"),
        ):
            with self.assertRaisesRegex(
                RuntimeResolutionError,
                r"expected 2\.14\.0, got 2\.12\.0.*override:SEEKNAL_SIGNAL",
            ):
                resolve_runtime(environ={"SEEKNAL_SIGNAL": "/workspace/signal"})

    def test_explicit_config_must_use_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "runtime.json"
            config.write_text(json.dumps({"python": "venv/bin/python"}))
            with self.assertRaisesRegex(RuntimeResolutionError, "absolute path"):
                resolve_runtime(config_path=config, environ={})

    def test_bundled_config_confines_python_to_declared_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            config = temporary / "runtime-config.json"
            config.write_text(
                json.dumps(
                    {
                        "python": str(
                            temporary / "managed/venv/../outside/bin/python"
                        ),
                        "runtime_root": str(temporary / "managed"),
                        "provenance": "managed-pypi",
                    }
                )
            )
            with mock.patch.object(runtime_resolver, "DEFAULT_CONFIG", config):
                with self.assertRaisesRegex(RuntimeResolutionError, "escapes"):
                    resolve_runtime(environ={})

    def test_config_env_is_an_explicit_user_selected_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "runtime.json"
            python = Path("/external/managed/venv/bin/python")
            config.write_text(
                json.dumps({"python": str(python), "provenance": "test-config"})
            )
            with mock.patch.object(
                runtime_resolver, "runtime_identity", return_value=identity(python)
            ):
                actual = resolve_runtime(
                    environ={"SEEKNAL_STUDIO_RUNTIME_CONFIG": str(config)}
                )
        self.assertEqual("test-config", actual.provenance)
        self.assertEqual(python, actual.python)


if __name__ == "__main__":
    unittest.main()
