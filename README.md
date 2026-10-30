# Seeknal Studio

Seeknal Studio is a local desktop workbench for building and running Seeknal pipelines. It links the project tree, pipeline graph, YAML/Python editor, materialized-output preview, run logs, and a project-bound AI agent in one window.

The included projects use synthetic local CSV data. Studio does not connect to production data by default.

## Start the desktop app

Studio currently expects:

- the Studio-managed Seeknal 2.14.0 runtime at `.runtime/venv`;
- the source-built Webframe binary at `../webframe-evaluation/bin/webframe`;
- `uv` plus a Python 3.11 or newer interpreter to create the managed runtime; and
- for the AI pilot, a Sumopod key in `~/sumopod-ai-api-key` with owner-only permissions.

Create or resynchronize the runtime from the hash-locked dependency set, then verify its identity:

```sh
PYTHON_311="$(uv python find 3.11)"
python3 runtime_setup.py --python "$PYTHON_311"
python3 runtime_setup.py --check
```

`runtime_setup.py` can start under the macOS Python 3.9 bootstrap interpreter, but the selected SDK interpreter must satisfy Seeknal's Python 3.11 requirement. The default resolver uses `.runtime/runtime-config.json`, requires the published Seeknal distribution to be exactly 2.14.0, and fails closed if the runtime is missing, malformed, or resolves outside the managed environment. It never silently falls back to the adjacent editable Signal checkout.

The legacy `SEEKNAL_SIGNAL` setting is now an explicit override, not the default. `SEEKNAL_RUNTIME_PYTHON`, `SEEKNAL_SIGNAL`, or `SEEKNAL_STUDIO_RUNTIME_CONFIG` may select another runtime only when that interpreter independently proves Seeknal 2.14.0. See [docs/runtime-2.14.md](docs/runtime-2.14.md) for setup, provenance, override, test, and local app-bundle boundaries.

Protect the existing key file:

```sh
chmod 600 ~/sumopod-ai-api-key
```

To open the verified synthetic pilot, double-click **Open Seeknal Pilot.command**, or run:

```sh
python3 open_pilot.py
```

This fixed launcher accepts no project argument. It verifies `pilot/workspace/` against the source-only template and clears exactly `ATLAS_API_URL` and `ATLAS_PORTAL_URL` from the pilot child. Those unrelated operator settings cannot make the standalone synthetic fixture fail closed. The native launcher receipt confirms the exact model/project binding, a successful three-node run, a fresh three-row preview, and no recorded credential value.

For the ordinary commerce demo, run:

```sh
python3 launch.py
```

`pilot/template/` is the repeatable source-only fixture. Each live acceptance run recreates `pilot/workspace/` from that template, then asks the real model to build the transform. The top-level `pilot/` directory preserves the initial completed run as historical evidence; do not use it as the clean rerun input.

To open another trusted Seeknal project:

```sh
python3 launch.py --project /absolute/path/to/seeknal-project
```

The normal launcher preserves the project's governance environment, including Atlas variables; only the fixed synthetic-pilot launcher removes those two variables. Use `--browser` to open the same interface in the system browser. Set `SEEKNAL_WEBFRAME` only when the binary lives elsewhere. Use the runtime overrides described above only to select another verified Seeknal 2.14.0 environment.

Both launchers select an available loopback port, start the Studio service, wait for readiness, and open Webframe. Closing the window stops the owned service and agent process. On macOS, service logs are stored under `~/Library/Logs/Seeknal Studio/`.

### Local access token

Each app launch creates a random browser secret and hands it to the local service (or SSH gateway) through a private environment variable. A loopback request is served only when it carries the port-scoped `seeknal_studio_<port>` cookie (HttpOnly, SameSite=Strict) or the secret in the `X-Seeknal-Studio-Token` header; anything else gets `403 Open Seeknal Studio from the app`, so other users and processes on this machine cannot drive Studio. The browser obtains the cookie once from `http://127.0.0.1:<port>/?studio_session=<secret>`, which redirects to `/`; the query is never accepted on `/api` paths. Webframe receives the secret through its environment rather than its command line, and `--browser` opens the one-time link. If cookies are blocked, the window tries the bootstrap once per tab and then shows the message.

The launcher binds the loopback port once and hands that listening socket to every service incarnation, so the port is never free during a workspace switch or reconnect; requests made during a restart wait in the socket backlog. The same secret is reused for the whole launch. The launcher's readiness probe sends no credential: the service answers a random challenge with an HMAC proof that it holds the secret.

The secret and link are never printed or logged. Developer tooling reads the link from `access-url` (mode 0600) in the launcher's private 0700 directory, whose path the launcher prints as `Access link file: …`; the file is removed when the launcher exits. A service started directly with `server.py` and no `SEEKNAL_STUDIO_UI_SECRET` keeps the earlier unauthenticated loopback behaviour for development.

Limitations:

- The app window (Webframe) is the protected path. With `--browser`, the browser sends the Studio cookie to every `127.0.0.1` port, because cookies ignore ports, so a page another local user serves on another loopback port can capture and replay it. Use `--browser` only on a single-user machine. On Linux, `xdg-open` also briefly exposes the one-time link in its command line (macOS passes it to `osascript` on stdin).
- Webframe exposes the secret as the `STUDIO_SESSION` page global to any top-level page it shows, for example a link or file dropped onto the window.

## Owned AI agent

Studio starts its own agent worker. You do not need to start a separate Seeknal gateway or export a gateway URL.

The current pilot is pinned to:

- provider protocol: OpenAI-compatible;
- endpoint: `https://ai.sumopod.com/v1`;
- model: `deepseek-v4.1-flash:netra`; and
- credential source: `~/sumopod-ai-api-key`, read only by the backend.

Before enabling the prompt box, Studio verifies the exact model, checks function calling, authenticates the worker handshake, matches the worker to the canonical selected project and its current fingerprint, and confirms a draft-only tool surface. The credential value never enters browser state, evidence files, or the app bundle.

The agent can inspect pipeline definitions and create files under `.seeknal/drafts`. It cannot validate, apply, run, execute Python or SQL, publish, or ingest. Those actions remain explicit Studio controls.

The controlled workflow is:

1. Ask the agent to inspect the selected project and create a draft.
2. Open **Review drafts** and inspect the real destination diff.
3. Select **Validate draft** to run Seeknal validation.
4. Select **Apply changes** to apply the exact reviewed and validated content.
5. Select **Run pipeline** explicitly.
6. Inspect the materialized output and lineage.

Source and destination hashes bind review approval to the applied content. A changed draft or destination requires a fresh review. Applying a draft never starts a pipeline run automatically.

See [docs/agent-pilot.md](docs/agent-pilot.md) for architecture, API behavior, security boundaries, and pilot reproduction.

## Pipeline workbench

Open **Graph**, **YAML**, **Logs**, **SQL REPL**, and **Configuration** from the activity rail. Open tools appear as closable tabs in editor groups. Split the workspace to keep two tools side by side, and move tabs between the left and right groups. Select a graph node to inspect its dependencies, then open its definition in YAML. Studio supports:

- YAML and Python pipeline editing with dirty-buffer guards;
- SQL help inside transform YAML without saving the active editor buffer;
- explorer-based draft creation and contextual **Dry run → Apply draft** or **Dry run → Save changes** actions;
- a project-bound Seeknal SQL REPL with tables, schemas, session history, and output provenance;
- managed workspace switching through the title bar, **File → Saved Workspaces**, and **Configuration → Workspaces**;
- movable lineage nodes with a saved per-project layout, search, fit, and auto layout;
- a resizable YAML workspace with focus mode;
- plan, validation, full/scoped execution, lineage generation, logs, and cancellation;
- data exploration and resizable results in the real Seeknal SQL REPL;
- saved named workspaces with editable project paths and a quick title-bar switcher;
- masked project environment editing through Configuration; and
- stale-output tracking after edits, failed runs, cancellation, and restart.

Studio exposes only allowlisted project actions. It does not provide an arbitrary interactive shell. Run and REPL history currently cover the active Studio session.

See [docs/ide-workflow.md](docs/ide-workflow.md) for the editor-to-draft flow, SQL REPL, and project switching.

## Open and switch projects

Start Studio with `python3 launch.py`. Use **Configuration → Workspaces → Add workspace**, **File → Add Workspace…**, or `⌘O`/`Ctrl+O` to save a project name and directory. On macOS, **Browse…** opens the native folder picker. Saving a registration leaves the current project open. Choose the saved name from the title-bar selector, **File → Saved Workspaces**, or its **Open** button to switch.

Studio accepts only a directory containing `seeknal_project.yml`. Cancelling the picker, choosing an invalid directory, declining the dirty-buffer warning, or hitting an active job, agent turn, or REPL query leaves the current project unchanged. A standalone `python3 server.py` process cannot switch projects; the endpoint returns HTTP 409 because no managed launcher is available.

After the launcher accepts a project, it stops the current window and project-bound backend, then starts a new backend, agent, and REPL for the selected canonical directory. No editor, agent, or REPL context carries across projects.

The fixed synthetic pilot remains isolated only while its verified authority files match. Switching from that pilot to a normal project restores the launcher's original Atlas governance environment. Project switching does not weaken production governance.

### Remote workspaces over SSH

In the workspace form, set **Location** to **SSH remote host**, enter the host as `user@host` or an `~/.ssh/config` alias (optional port), and the remote project directory (`/…` or `~/…`). Saving checks the host over SSH and stores the canonical remote path. Opening it works like VS Code Remote-SSH: the whole project-bound backend (pipeline jobs, graph, previews, SQL REPL, agent) runs on the host, next to the data.

- Authentication uses your existing SSH keys and agent with `BatchMode=yes`. Studio never prompts for or stores passwords.
- First open uploads the Studio backend to `~/.cache/seeknal-studio/server/<content-hash>/` and builds the pinned runtime (`requirements-runtime.lock.txt`, Seeknal 2.14.0, Python 3.11 via `uv`) under `~/.cache/seeknal-studio/runtimes/<lock-hash>/`. This took about 30 s on FK-Legion. Later opens take about 2 s. The host needs `python3` and `uv`.
- The remote backend binds an OS-assigned loopback port on the host and accepts only requests carrying a random per-session token. The token is sent over the SSH channel, never on a command line. On this Mac, the SSH forward ends in a Unix socket inside the launcher's private 0700 directory. A local gateway serves the UI, the saved-workspace list, and switching, so you can always switch back to a local project.
- The session sends a heartbeat every 10 s. If the host stops hearing it for 45 s, for example after the laptop sleeps or changes networks, it stops the backend and its workers. Only one backend at a time can hold a remote project.
- Closing the window, quitting, or switching away stops the remote backend. After a lost connection the launcher reconnects at most once per 5 minutes. If opening or reconnecting fails, it reopens the last local project and shows the error.
- Agent provider credentials are read on the host (for example `~/sumopod-ai-api-key` there). Nothing is copied from this Mac.
- Remote workspaces are never used as the startup project, so launching Studio never depends on the network.

Live acceptance: `python tests/remote_ssh_e2e.py` (defaults to `fitra@100.73.79.64` and `~/project/seeknal-studio-remote-demo`; receipt in `evidence/remote-ssh/receipt.json`).

## Draft SQL from transform YAML

Select a transform `.yml` or `.yaml` file. The editor’s **Tools** menu provides SQL assistance and transfer actions:

- **Draft SQL with AI** sends the current editor buffer, selected transform, declared graph inputs, and observed output columns to the verified project-bound model. The request requires the same transform kind and name, preserves non-SQL metadata, and may create only a reviewable draft.
- **Send SQL to REPL** parses the transform SQL, rewrites only references declared in `inputs` (including `input_N` aliases), and fills the REPL editor. It does not execute the SQL.

Open drafts from the explorer’s **Drafts** section. Editing a draft offers **Dry run**; after a successful check, **Apply draft** publishes that exact version to the project. The editor then follows the active file.

Editing an active pipeline YAML offers **Dry run changes**. It validates a temporary snapshot without changing the project file. A successful check offers **Save changes**. Editing again clears that approval and requires another dry run. Failed or cancelled checks preserve the buffer and never save the active file. Apply and Save changes never start a pipeline run.

The clean editor shows its saved state without disabled Draft/Dry run/Apply buttons. Ordinary project configuration files retain direct Save behavior. `⌘S`/`Ctrl+S` follows the same contextual action as the footer.

## SQL REPL

Open the **SQL REPL** tab and select **Start REPL**. Studio starts a persistent, read-only Seeknal REPL bound to the selected project and its profile/environment context. It loads non-provider values from the project's `.env`; provider API keys and tokens are removed from the REPL process.

The REPL supports SQL plus:

- `.tables`
- `.schema <table>`
- `.help`
- `.reload`
- `.quit`

Queries and results remain available in the current browser/Studio session. Each result records the pipeline output provenance used by that REPL generation. When pipeline outputs change, Studio marks earlier results historical and requires **Reload outputs** before another query can run against the new outputs.

## Development

Start the service on its default port:

```sh
.runtime/venv/bin/python server.py
```

Then open `http://127.0.0.1:18766`. Set `SEEKNAL_STUDIO_PORT` to change the port and `SEEKNAL_STUDIO_PROJECT` to select a project.

Legacy external-gateway compatibility is opt-in:

```sh
SEEKNAL_STUDIO_AGENT_MODE=external \
SEEKNAL_GATEWAY_URL=http://127.0.0.1:8000 \
python3 launch.py
```

External gateway health does not attest project identity. Studio labels that mode as unverified and requires acknowledgement before sending a prompt.

## Current IDE workflow verification

The final current-source Seeknal 2.14 gate passed. The deterministic end-to-end receipt records 7/7 checks: managed PyPI identity; replay of the preserved real-turn event shape without another provider request; real YAML and Python CLI scaffolds; a three-row dry-run preview; exact apply; explicit pipeline run; persistent REPL query; a fresh verified worker nonce after Apply; isolated project switching; and unchanged canonical fixtures. The separate native package receipt passes 2/2 checks: all 12 packaged sources plus `.runtime/runtime-config.json` match the frozen source, the packaged backend launches on managed Seeknal 2.14.0, and closing the launcher stops it. See the [2.14 end-to-end receipt](evidence/seeknal-2.14/e2e-receipt.json), [package receipt](evidence/seeknal-2.14/package-receipt.json), and [13-entry bundle manifest](evidence/seeknal-2.14/bundle-manifest.json).

The preserved real-agent observation records one actual DeepSeek turn on managed Seeknal 2.14.0 before the final rebind delta. Final deterministic replay verified that observed event shape and the answer/status deduplication without issuing another user turn; it does not claim a fresh full provider turn after the final source hash. The frozen runtime uses `pydantic-ai-slim==1.107.9` and `pydantic-deep==0.3.31`. [Independent review](evidence/seeknal-2.14/code-review.md) approved the upgrade after 12 focused runtime/compatibility tests and the broader 69-test scoped set; managed-runtime Pyright reported zero errors and warnings, Ruff and compilation passed, and all 152 locked distributions were compatible.

The adjacent dirty Signal checkout and its editable Seeknal 2.12.0 environment were not upgraded or modified. Studio's Pydantic AI 1.107 streaming/native-tool compatibility, managed-interpreter binding, and SDK identity checks live in Studio code rather than changes to Seeknal package source. See [the 2.14 runtime guide](docs/runtime-2.14.md) and [runtime receipt](evidence/seeknal-2.14/runtime-receipt.json).

The current frozen source passed a 14-check end-to-end workflow covering the real Sumopod draft, dirty-buffer review, hash-bound dry run and apply, explicit pipeline run, persistent REPL, SQL-to-REPL provenance, output reload, project switching, cleanup, and unchanged canonical pilot fixtures. See the [IDE workflow receipt](evidence/ide-workflow/receipt.json).

The manual dirty-buffer regression separately proves that draft creation and dry run do not write the source, while explicit Apply writes the exact reviewed snapshot. The architecture review additionally confirms that newer editor changes are preserved instead of discarded. See the [manual dirty-buffer receipt](evidence/ide-workflow/manual-dirty-regression.json), [native observation](evidence/ide-workflow/manual-dirty-apply.png), and [architecture review](evidence/ide-workflow/architecture-review.md).

The rebuilt development app matched all 11 packaged source files to the frozen source. In the actual packaged app, a typed canonical path switched project A to project B on the same port, reaped the old project processes, and reached **Ready · verified** with fresh project identity. See the [bundle manifest](evidence/ide-workflow/bundle-manifest.json) and [native package receipt](evidence/ide-workflow/native-package.json).

The macOS **Browse…** control launched the native folder picker, but the separate OS dialog was not addressable through Computer Use. Native-picker selection is therefore unverified; the typed-path fallback is verified. Independent review concluded **APPROVE** and architecture review concluded **CLEAR**. The focused frozen-source records show [99 unit tests](evidence/ide-workflow/final-unit-tests.txt) and [5 frontend checks](evidence/ide-workflow/final-frontend-tests.txt) passing. At documentation close, the final aggregate was rerunning after a test-fixture-only positive-PID publication race fix; no product defect or packaged-source change was identified.

The earlier real-agent synthetic pilot evidence remains valid historical evidence for its own frozen source and fixture. The current IDE workflow receipt is the authority for the newer editor, project-switch, and persistent-REPL claims.

## Historical pilot verification

Run the offline regression suite and static checks:

```sh
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ .runtime/venv/bin/python -m unittest discover -s tests -v
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ .runtime/venv/bin/python tests/frontend_pilot_checks.py
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ .runtime/venv/bin/python -m unittest discover -s tests -p 'pilot_fixture_test.py' -v
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ .runtime/venv/bin/python -m py_compile server.py engine_bridge.py agent_runtime.py agent_worker.py launch.py build_app.py tests/pilot_e2e.py
node --check app.js
```

For the earlier frozen pilot source, the recorded verification passed 75 checks: 63 discovered regression tests, 9 frontend contract checks, and 3 source-only fixture checks. The receipts cover turn correlation, project identity mismatch, missing/failed provider readiness, draft-only tool policy, draft-path and symlink escape rejection, worker/descendant cancellation, API redaction, file/job serialization, hash-bound draft application, stale-output behavior, fixed-workspace validation, selective Atlas isolation, launcher ownership, and bundle boundaries.

The real provider-backed acceptance command is:

```sh
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ .runtime/venv/bin/python tests/pilot_e2e.py
```

That command makes a real, billable Sumopod request. It recreates and mutates only `pilot/workspace/` from the source-only template and requires the configured key file.

The frozen-source browser acceptance run succeeded with `deepseek-v4.1-flash:netra`. Its client UUID matched the runtime `turn_id`; it used only draft-safe tools, stopped before apply/run, displayed and validated the real diff, applied the exact reviewed SHA through an explicit action, ran the pipeline explicitly, and matched three output rows and lineage to the independent expected result. The receipt is [evidence/pilot/e2e-real.json](evidence/pilot/e2e-real.json); the earlier completed run remains in [evidence/pilot/e2e-real-first-success.json](evidence/pilot/e2e-real-first-success.json).

A metamorphic check then added one synthetic paid order, observed the expected output change, restored the source, and recovered the original output. Native Webframe also loaded the verified project, real three-node graph, and three-row preview. See [evidence/pilot/e2e-metamorphic.json](evidence/pilot/e2e-metamorphic.json) and [evidence/pilot/e2e-native.json](evidence/pilot/e2e-native.json).

These receipts verify the pilot and its input sensitivity. The asynchronous frontend regression records **Checking** → **Ready · verified**, and the native run independently confirms the real owned runtime ready against the completed project. The fixed launcher then repeated the real native run successfully while isolating only the two Atlas variables. Independent architecture review is **CLEAR**. The tracked goal still awaits the root workflow's final state audit and completion transition; these execution receipts do not perform that transition.

## macOS development bundle

Build the unsigned local bundle only when the destination does not already exist:

```sh
python3 build_app.py
open 'dist/Seeknal Studio.app'
```

The rebuilt bundle includes Studio, Webframe, the demo, the owned-agent code, and a copy of the local managed-runtime configuration. It does not embed `.runtime/venv`; the copied configuration still points to the external managed 2.14.0 runtime on this development machine, so the bundle is not portable. Final native QA matched all 12 packaged sources and the runtime configuration to the frozen source, launched the managed 2.14.0 backend, exposed the native draft types, and proved that closing the launcher stopped the backend. See [evidence/seeknal-2.14/package-receipt.json](evidence/seeknal-2.14/package-receipt.json). The bundle does not include credentials or user agent configuration. Signing, notarization, a portable managed runtime, automatic updates, and Windows/Linux installers remain future release work.

The editor is intentionally lightweight and does not yet provide an LSP or refactoring engine. The current deliverable is a local pilot, not a production deployment.
