# Seeknal Studio handoff to Claude

User explicitly requested ownership handoff to the existing Claude in the left Herdr pane. The completed implementation is in `/Users/fitrakacamarga/project/mta/research/seeknal-studio`; the parent shell is `/Users/fitrakacamarga/project/mta/research`.

## Immediate instruction

Take over this workspace and retain the existing Claude session/cwd/model. Read this handoff, DESIGN.md, README.md, and the latest receipt. Acknowledge understanding and readiness for the user's next request. The latest requested feature is complete; there is no unfinished feature assignment or active native goal to resume. Avoid starting duplicate servers or changing code just to acknowledge the handoff. Follow applicable AGENTS.md instructions and preserve others' work.

## Product and completed work

Seeknal Studio is a local desktop IDE: vanilla HTML/CSS/JavaScript, stdlib Python HTTP service, and a native Webframe shell. It executes real Seeknal 2.14.0, not mocked pipelines. Source is not in a Git repository (verified at handoff); no commit/PR exists to claim.

- Graph: movable/persistent per-project lineage nodes, search, Fit, Auto layout, Open YAML.
- Editor groups: dynamic closable tabs, independent left/right panes, drag/move/split/merge, accessible resizable dividers, focus modes. Tool DOMs are singletons; do not clone IDs, workers, or buffers.
- Explorer: separate Pipeline files and Drafts; real `seeknal draft` through New draft. Internal `.editor-*` validation snapshots remain hidden.
- Contextual editor lifecycle: active YAML edit -> Dry run changes -> Save changes; new draft edit -> Dry run -> Apply draft -> Saved / Active. No disabled Draft/Dry run/Apply strip in a clean editor. Non-pipeline project files retain ordinary Save. Cmd/Ctrl+S follows the contextual action.
- Validation is tied to exact content and destination hashes. Active files stay untouched until explicit save/apply; edits invalidate approval. Newer edits during apply are preserved and rebased onto the destination. Conflicts, cancellation, and failure never inherit approval.
- Important SDK fix: native Seeknal dry-run can return exit 0 after a transform BinderError. Studio captures the SDK's structured preview result and blocks approval when preview execution failed. `native_exit_code=0` remains recorded, while Studio reports failure. Missing/arbitrary structured evidence fails closed; source schema-only and recognized definition-only checks are explicit.
- Logs: dedicated page; historical selection never replaces active job/cancel authority.
- SQL REPL: real persistent project-bound Seeknal REPL, table/schema discovery, read-only SQL, bounded results, cancellation, history, output provenance/staleness/reload. Queries accept trailing semicolons. Inspect/query/reload submissions have synchronous reservations.
- Configuration: project/runtime identity, provider verification, masked field-level project `.env` editing. Existing values never return to the browser; atomic 0600 writes, optimistic hashes, protected runtime/provider keys, and busy-work guards.
- Latest feature: saved named workspaces = name + one canonical Seeknal project directory. Configuration supports add, edit name/path, native Browse, Use current project, and Forget (metadata only). Title bar Quick Open and File -> Saved Workspaces switch through stable IDs. File -> Add Workspace / Cmd/Ctrl+O opens registration. Saving metadata never auto-switches. Invalid/moved saved paths remain available for repair.
- Workspace state preserves legacy last/recent and arbitrary config fields; atomic private JSON + thread locks/flock prevent lost writes. Stale ID updates cannot resurrect forgotten registrations. One accepted switch per runtime spans legacy and workspace routes. CRUD/open are mutually exclusive in the UI; dirty editor/config forms use an in-app confirmation and one-shot unload authorization. Managed switch restarts backend/agent/REPL and clears old project context.

## Main source and APIs

- `app.js`, `index.html`, `styles.css`, `DESIGN.md`: UI and state machines.
- `server.py`: loopback API, JobStore, CLI execution, exact snapshot validation/apply, agent/REPL/project integration.
- `engine_bridge.py`: actual SDK graph/preview, canonical target resolution, structured dry-run capture, SQL AST input mapping, hash-bound apply.
- `project_service.py`: workspace registry, canonical project validation, native folder picker, private launcher switch channel.
- `environment_service.py`: safe environment metadata/updates.
- `repl_service.py`, `repl_worker.py`: real SDK REPL process lifecycle and queries.
- `agent_runtime.py`, `agent_worker.py`: owned project-bound draft-only model runtime.
- `runtime_resolver.py`, `runtime_setup.py`, runtime requirement locks: managed SDK.
- `launch.py`, `build_app.py`: managed process/window lifecycle and unsigned local app bundle.

Workspace API: GET `/api/workspaces`; POST `/api/workspaces` `{name,path,id?}`; POST `/api/workspaces/remove` `{id}`; POST `/api/workspaces/open` `{id}`. Existing `/api/projects` and native browse remain. Editor preparation: POST `/api/editor/prepare` `{path,content,original_content}` -> snapshot/diff; actual jobs use `/api/jobs` actions `validate-draft` / `apply-draft` with hashes and explicit confirmation.

## Runtime, model, and boundaries

Studio-owned `.runtime/venv` is pinned to public Seeknal 2.14.0. Preserve the lexical venv Python path; resolving its symlink loses the venv. Clear inherited Python shadow variables when running checks. Original `/Users/fitrakacamarga/project/mta/signal` is a dirty SDK checkout, not the managed runtime; preserve it and do not upgrade/edit it as part of Studio work.

Provider remains Sumopod OpenAI-compatible, model `deepseek-v4.1-flash:netra`, base `https://ai.sumopod.com/v1`. Backend key file is `~/sumopod-ai-api-key`. Never print or copy its contents or put credentials into the browser. The model has exactly five inspection/draft tools; it cannot apply, run pipelines, execute SQL, or invoke a shell. User-driven REPL is separate.

Preserve project authority and Atlas governance environment on normal project switches. Test launchers may isolate Atlas variables only for the named synthetic fixtures; this is not production/governance validation. Global user workspace registrations and original project data were not changed by the acceptance fixtures.

## Fresh evidence and live state

Latest acceptance: `evidence/workspaces/receipt.json` and README. Source hashes still match that receipt at handoff (all 13 files, no drift).

- 163 tests passed under managed runtime; 53 frontend/runtime checks passed.
- Ruff, Python compilation, JS syntax, and focused Pyright passed.
- Independent code/spec/security review: APPROVE, no remaining findings.
- Actual Computer Use added two registrations, rejected an invalid path, renamed a workspace, forgot/re-added it without deleting files, cancelled a dirty switch retaining edits, confirmed a dirty switch without a second browser unload prompt, and completed A -> B -> A.
- Real SQL order counts were 6 -> 2 -> 6; every restart had a distinct runtime ID, and agent/REPL bindings followed the selected path. Saved entries survived both switches.
- Browser UI `http://127.0.0.1:18862` was checked live at handoff: Commerce project at `evidence/workspaces/commerce`.
- Latest packaged native service `http://127.0.0.1:18863` was checked live at handoff: Analytics project at `evidence/workspaces/analytics` (changed from the original receipt's initial Commerce state, presumably by the user). Respect that current selection.
- Both report Seeknal 2.14.0. The private demo registry is `evidence/workspaces/projects.json`; it contains Commerce demo and Analytics lab. Each service has its own current binding; don't infer current workspace from the global last field.
- Bundle: `dist/Seeknal Studio.app`, 13 matching source files + managed runtime config. Prior bundles are preserved under `dist-backups/`.
- Native Webframe was launched and packaged HTTP/assets verified. Computer Use could not attach to its standalone native binary; visual interaction/screenshots were verified in Chrome. Do not claim native visual automation.
- Older demo services/windows may remain from prior stages. Do not indiscriminately kill/restart them or overwrite user buffers. Recheck ports/process identity before lifecycle actions.

Earlier receipts: `evidence/computer-use-prompt/` (actual DeepSeek prompt -> draft -> dry-run -> apply -> successful three-node run), `evidence/workspace-pages/`, `evidence/editor-groups/`, `evidence/editor-lifecycle/`, and `evidence/seeknal-2.14/`. Each receipt applies to its recorded source/fixture; don't convert historical checks into fresh verification.

## Verification commands

Run from Studio. The full tests below use only synthetic fixtures; normal app launch must preserve the user's environment.

```sh
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ -u ATLAS_API_URL -u ATLAS_PORTAL_URL .runtime/venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV -u __PYVENV_LAUNCHER__ .runtime/venv/bin/python -m unittest discover -s tests -p '*checks.py' -q
node --check app.js
```

The system Python has missing SDK/provider dependencies and gave misleading failures; use the managed venv. Full suite takes roughly 100 seconds. Some pre-existing dynamic-import typing diagnostics occur when broadening Pyright to legacy tests; focused changed production/tests passed. No need to rerun the whole suite solely to accept this handoff.

## Remaining limitations / next work

No currently requested implementation remains. Distribution is unsigned and local: the app points at an external managed venv, not a portable installer. Native attachment, signing/notarization, portable distribution, LSP, and fuller per-workspace session restoration are potential future work, not delivered claims or newly authorized assignments. Source currently has no Git history; establish a repository/commit strategy only if requested. Await the user's next direction after acknowledging ownership.
