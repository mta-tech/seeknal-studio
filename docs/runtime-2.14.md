# Seeknal 2.14 managed runtime

Seeknal Studio uses a Studio-owned, isolated runtime for the published Seeknal 2.14.0 distribution selected as the latest release for this upgrade. This is a local development runtime, not a global upgrade of the adjacent Signal repository.

## Create or resynchronize the runtime

Install `uv`, make a Python 3.11 or newer interpreter available, then run from the Studio root:

```sh
PYTHON_311="$(uv python find 3.11)"
python3 runtime_setup.py --python "$PYTHON_311"
python3 runtime_setup.py --check
```

The bootstrap script itself supports the macOS Python 3.9 interpreter. The interpreter passed with `--python` must satisfy Seeknal's Python 3.11 requirement. Do not invoke `.runtime/runtime_setup.py`; the script lives at the Studio root.

`runtime_setup.py` creates `.runtime/venv`, synchronizes `requirements-runtime.lock.txt` with required hashes, and writes `.runtime/runtime-config.json`. The current lock resolves 152 distributions. The `--check` command is read-only and prints the selected interpreter, CLI, module origin, version, distribution version, and provenance.

## Runtime resolution

The default resolver reads `.runtime/runtime-config.json` and accepts only Seeknal 2.14.0. It verifies the installed distribution and importable module through the selected interpreter, requires the module and CLI to come from that interpreter's environment, and rejects a managed Python path that escapes the configured runtime root.

Studio subprocesses remove inherited `PYTHONPATH`, `PYTHONHOME`, `VIRTUAL_ENV`, and `__PYVENV_LAUNCHER__` values before starting runtime workers. This prevents an editable source checkout from shadowing the managed wheel. A missing or invalid managed runtime is an error; Studio does not fall back to the editable Seeknal 2.12.0 checkout.

The following settings are explicit overrides:

| Setting | Selection |
| --- | --- |
| `SEEKNAL_RUNTIME_PYTHON` | Absolute path to a runtime Python interpreter |
| `SEEKNAL_STUDIO_RUNTIME_CONFIG` | Absolute path to another runtime configuration |
| `SEEKNAL_SIGNAL` | Legacy checkout override; selects `<checkout>/.venv/bin/python` |

Every override must independently resolve the published Seeknal 2.14.0 distribution. Seeknal 2.12.0 and other versions are rejected. `SEEKNAL_SIGNAL` remains available for deliberate compatibility work, but it is no longer runtime discovery or a default.

## Verified provenance

The runtime receipt records:

| Property | Verified value |
| --- | --- |
| Seeknal distribution and CLI | `2.14.0` |
| Wheel | `seeknal-2.14.0-py3-none-any.whl` |
| Wheel SHA-256 | `a39b786ea8b310b13386030d72a3f0993e9047caa06a44eb3091a530ed870e1c` |
| Managed Python | `.runtime/venv/bin/python` (Python 3.11.4 in the recorded runtime) |
| Dependency set | 152 hash-locked distributions |
| Pydantic AI | `pydantic-ai-slim==1.107.9` |
| Pydantic Deep | `pydantic-deep==0.3.31` |
| System site packages | disabled |
| Provenance | `managed-pypi` |

See [`evidence/seeknal-2.14/runtime-receipt.json`](../evidence/seeknal-2.14/runtime-receipt.json) for the wheel and runtime identity and [`evidence/seeknal-2.14/runtime-freeze.txt`](../evidence/seeknal-2.14/runtime-freeze.txt) for the installed distribution snapshot.

The existing dirty Signal checkout and its editable Seeknal 2.12.0 virtual environment were preserved. The upgrade does not claim that repository or its virtual environment was upgraded. Compatibility changes remain in Studio: adapters handle Pydantic AI 1.107 streaming and native tool events, subprocesses bind to the lexical managed-venv interpreter, and readiness reports the actual SDK distribution and module origin. The installed Seeknal package source remains the published wheel baseline.

## Verify locally

Run compatibility checks through the managed interpreter and remove inherited Python path state:

```sh
python3 runtime_setup.py --check
.runtime/venv/bin/seeknal --version
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ .runtime/venv/bin/python -m unittest discover -s tests -p 'test_runtime_resolver.py' -v
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ .runtime/venv/bin/python -m unittest discover -s tests -p 'test_sdk214_compat.py' -v
```

The runtime-specific commands above pass 7 resolver tests and 5 SDK compatibility tests. The [independent final review](../evidence/seeknal-2.14/code-review.md) also records the broader 69-test scoped set, managed-runtime Pyright with zero errors and warnings, Ruff and compilation passing, and all 152 locked distributions compatible. The checks cover managed identity under a polluted `PYTHONPATH`, the real CLI draft/dry-run/apply/run path, the persistent REPL, agent event/tool compatibility, cancellation, and project rebind behavior.

The final deterministic browser acceptance passes 7/7 checks. It verifies the managed PyPI SDK identity, real YAML and Python CLI scaffolds, a three-row dry-run preview, exact apply, explicit run, persistent REPL results, a fresh worker nonce after the Apply fingerprint change, project-switch isolation, and unchanged canonical fixtures. See [`evidence/seeknal-2.14/e2e-receipt.json`](../evidence/seeknal-2.14/e2e-receipt.json).

One real DeepSeek turn was observed on managed Seeknal 2.14.0 before the final rebind delta and preserved in [`real-agent-turn-observation.json`](../evidence/seeknal-2.14/real-agent-turn-observation.json). The final run replayed that observed event shape to verify one answer, one terminal status, and stable scrolling without making another provider request. This proves deterministic UI handling of the real event shape; it does not claim a fresh full model turn after the final source hash.

## Local macOS bundle boundary

`build_app.py` copies the runtime configuration into the unsigned development app, but it does not copy `.runtime/venv`. The configuration points to the existing external managed runtime on the build machine. The resulting app is local-development packaging, not a portable installer.

Final package QA passes 2/2 checks. The bundle manifest matches 12 packaged source files and `.runtime/runtime-config.json` to the frozen source, the native app launches the managed 2.14.0 backend, and closing the launcher stops it. See [`package-receipt.json`](../evidence/seeknal-2.14/package-receipt.json) and the [13-entry bundle manifest](../evidence/seeknal-2.14/bundle-manifest.json).

Build only after the runtime check succeeds and `dist/Seeknal Studio.app` does not exist; the builder refuses to overwrite an existing bundle:

```sh
python3 runtime_setup.py --check
python3 build_app.py
open 'dist/Seeknal Studio.app'
```

Moving the app to another machine without provisioning a matching runtime will not produce a supported installation.

If the managed configuration is absent, the builder reports the correct root-level bootstrap command: `python3 runtime_setup.py --python <Python-3.11-or-newer>`.
