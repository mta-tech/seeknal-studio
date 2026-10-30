# Owned agent pilot

This document describes the local Seeknal Studio agent pilot. It uses the real Seeknal agent and engine against an isolated synthetic project. It does not authorize production data access or deployment.

## Runtime flow

```text
Seeknal Studio launcher
  -> loopback Studio service
     -> authenticated owned agent worker
        -> exact Sumopod model and function-call readiness checks
        -> real Seeknal create_agent(...) with a draft-only toolset
     -> Studio review and job APIs
        -> validate -> explicit apply -> explicit run -> inspect output
```

The Studio service owns the worker process and closes it on cancellation or shutdown. The worker runs in the selected Seeknal environment and uses the same canonical project as the editor and job system.

## Provider configuration

The pilot accepts one exact configuration:

| Setting | Value |
| --- | --- |
| Provider adapter | OpenAI-compatible |
| Base URL | `https://ai.sumopod.com/v1` |
| Model | `deepseek-v4.1-flash:netra` |
| API key file | `~/sumopod-ai-api-key` |

The key file must be a regular, non-symlink file with mode `0600`. The worker reads its value directly. The browser receives provider/model/readiness metadata, never the key.

Studio persists non-secret agent settings under the user's application-support directory with owner-only permissions. The macOS bundle contains the default key-file path string, but never copies the key or the user configuration file.

## Readiness and identity

Studio keeps the prompt composer disabled until every check succeeds:

1. the exact model appears in Sumopod's authenticated model list;
2. the model returns the mandatory function call used by the readiness probe;
3. the worker returns the launch nonce, canonical project path, and project-content fingerprint;
4. the worker proof matches the supervisor's HMAC calculation; and
5. the effective model request exposes only the expected draft-safe tools.

The project fingerprint covers the project configuration, `seeknal/` definitions, and local `data/` inputs. If those inputs change, the existing worker identity stops being ready and must be re-established.

## Agent authority

The model receives these tools:

- `read_pipeline`
- `search_pipelines`
- `search_project_files`
- `draft_node`
- `write_draft`

Draft writes are constrained to regular YAML/Python files inside the selected project's `.seeknal/drafts` directory. The runtime rejects symlink escapes and oversized or invalid draft paths.

The agent does not receive apply, run, execution, shell, publish, ingest, web, or arbitrary project-write tools. Prompt instructions reinforce this boundary, but the enforced tool allowlist is the authority.

Studio owns validation, application, and execution:

- validation runs as an allowlisted Seeknal job;
- apply requires explicit confirmation after successful validation;
- source and destination hashes must still match the reviewed diff; and
- a pipeline run requires a separate explicit action.

## HTTP API

The browser uses these loopback endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/agent/status` | Read provider, model, readiness, verified project identity, and capabilities. |
| `POST /api/agent/config` | Validate backend-only provider settings and restart the owned worker. |
| `POST /api/agent/ask` | Start an asynchronous turn; returns HTTP 202 for owned mode. |
| `GET /api/agent/sessions/<session_id>` | Poll status, ordered tool events, answer, or sanitized error. |
| `POST /api/agent/cancel` | Cancel the active turn and reap the worker. |

The frontend polls events by sequence number, refreshes the project and draft list after completion, and keeps review/apply/run as separate controls.

## Isolated acceptance projects

The pilot separates repeatable input from generated evidence:

- `pilot/template/` is the source-only fixture used for every fresh live run.
- `pilot/workspace/` is recreated from the template, then contains the actual model-produced transform and real outputs after a successful run.
- top-level `pilot/` preserves the initial completed run as historical evidence.

The template contains two source definitions and synthetic CSV files:

- 6 orders;
- 3 customers; and
- an independently calculated expected result in `pilot/EXPECTED_OUTPUT.json`.

It intentionally starts without a transform or output. `tests/pilot_e2e.py` replaces `pilot/workspace/` with a fresh copy of this template before the real request. The acceptance prompt asks the model to create one `customer_value_pilot` transform draft that joins the two sources, filters paid orders, and aggregates customer lifetime value and order count.

## Open the verified pilot

Double-click `Open Seeknal Pilot.command`, or run:

```sh
python3 open_pilot.py
```

The helper accepts no project/runtime arguments and always opens the verified `pilot/workspace/`. It rejects changed authority files, unexpected pipeline definitions, symlinks, and an incomplete template/workspace. For this fixed synthetic CSV fixture only, it removes `ATLAS_API_URL` and `ATLAS_PORTAL_URL` from the child process. The ordinary `launch.py` path preserves those governance variables for normal projects.

`evidence/pilot/fixed-launcher-native.json` records a positive native launch: exact model and project verification, three nodes/two edges, 3 customer rows, 6 order rows, three fresh output rows, and exit-zero pipeline execution. No credential value was recorded.

The acceptance sequence verifies:

1. exact provider/model, tool calling, and canonical project identity;
2. browser-submitted real model turn;
3. only draft-safe tools were called;
4. the agent created one draft and did not apply or run it;
5. the browser displayed the real destination diff;
6. Seeknal validated the exact draft;
7. explicit apply moved the reviewed draft;
8. explicit full run produced a Parquet output;
9. output rows matched the independently calculated expected result; and
10. lineage included the applied transform.

Run the repeatable acceptance from the Studio root:

```sh
python3 tests/pilot_e2e.py
```

This command calls the real Sumopod API and may incur usage charges. It does not read the key into the test process or record the key in evidence.

The final frozen-source run completed successfully. `evidence/pilot/e2e-real.json` records the current completed workspace; `evidence/pilot/e2e-real-first-success.json` preserves the earlier successful run. The final event record shows project discovery, pipeline reads, draft creation, and draft writes from `deepseek-v4.1-flash:netra`, followed by separate browser actions for validation, apply, run, output inspection, and lineage verification. The client-generated UUID exactly matched the runtime `turn_id`, and the reviewed draft SHA exactly matched the applied target SHA.

Additional completed checks:

- `python3 tests/pilot_metamorphic.py` changed one synthetic source row, observed the expected aggregate change, restored the source, and recovered the baseline output.
- `python3 tests/pilot_native.py` loaded the completed workspace through native Webframe and verified the model/project binding, three-node graph, fresh three-row preview, and validation job.

Those checks use `pilot/workspace/`; they do not make another model request. The launcher-hardened source passes 63 discovered tests, 9 frontend contract checks, and 3 fixture checks: 75 checks in total. Ten of the 63 discovered tests cover fixed workspace selection, argument rejection, authority-file changes, extra pipeline definitions, symlink rejection, and selective Atlas-variable removal. The browser readiness regression records **Checking** → **Ready · verified** without exposing credential data; the native receipt independently proves the actual owned runtime ready on the selected project.

`evidence/pilot/e2e-package.json` records the rebuilt application check. Its manifest matches all eight packaged source files to the frozen source, the exact model and project are verified, the graph and preview load, and closing the native window stops the owned backend. Independent architecture review is **CLEAR** in `evidence/pilot/architect-review.md`. The pilot is verified; the tracked goal still awaits the root workflow's final state audit and completion transition.

## Test boundaries

Offline verification:

```sh
python3 -m unittest discover -s tests -v
python3 tests/frontend_pilot_checks.py
python3 -m unittest discover -s tests -p 'pilot_fixture_test.py' -v
python3 -m py_compile server.py engine_bridge.py agent_runtime.py agent_worker.py launch.py build_app.py tests/pilot_e2e.py
node --check app.js
```

The offline suite uses fake workers and temporary project copies for failure, identity, cancellation, API, and packaging tests. Those tests prove control behavior; they do not prove that the selected external model can complete the pipeline task.

Provider readiness proves exact model availability and function calling. It does not by itself prove the full draft-to-output workflow; the successful `tests/pilot_e2e.py` receipt supplies that separate evidence.

## Compatibility and limits

Set `SEEKNAL_STUDIO_AGENT_MODE=external` to use the legacy gateway proxy deliberately. External gateway health cannot prove which project it controls, so Studio treats that identity as unverified.

Agent and run history currently lasts only for the active Studio service session. The pilot does not provide production-data integration, a signed or self-contained installer, durable cross-restart sessions/history, token-by-token UI streaming, an LSP, or production deployment. The owned runtime requires network access, an owner-only readable key file, exact-model availability, and a successful function-call probe before it becomes ready. It targets one exact Sumopod model so acceptance evidence cannot silently drift to another provider or model.
