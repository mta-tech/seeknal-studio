# IDE workflow

This guide covers the verified Seeknal Studio editor workflow: draft SQL inside transform YAML, validate and apply changes explicitly, inspect data in the project-bound SQL REPL, and switch project directories through the managed desktop launcher.

The earlier 14-check [IDE workflow receipt](../evidence/ide-workflow/receipt.json) covers its frozen source, focused checks, and packaged native A-to-B project switching. A later [Computer Use prompt-builder receipt](../evidence/computer-use-prompt/README.md) verifies a real model draft, dry run, apply, and three-node execution on Seeknal 2.14.0. Each receipt applies to its recorded source and fixture.

## Editor groups

The activity rail opens tools. The tab strip shows only tools that are open. Each editor group has its own selected tab. Use the group split control to create a right-hand group, move tabs between groups, and resize the divider to choose their widths. Tabs also move by drag and drop. `⌘\` / `Ctrl+\` opens the right group; arrow keys, Home, and End navigate tabs inside a group. Moving a tool tab preserves its editor buffer, query text, and running session. Closing a REPL tab keeps its query and session for reopening; a modified YAML tab stays open until saved. A tool has one live view and moves between groups; this does not create another REPL session or an independent duplicate YAML buffer.

## Workspace tools

- **Graph** provides full-page lineage. Drag a node or use arrow keys while it is focused. Positions persist in this browser per project. **Fit** keeps cards inside the canvas, **Auto layout** restores generated positions, and **Open YAML** opens the selected definition.
- **YAML** provides a dedicated editor. Drag the explorer divider, or focus it and use left/right arrows, to adjust space. **Focus** expands the editor across the workspace; Escape restores the panels. Draft, dry run, apply, and save retain their separate actions.
- **Logs** provides a job list and full command output. Inspecting an older job leaves cancellation attached to the actual active job. History covers this Studio process.
- **SQL REPL** provides real project tables, SQL, query history, and results. Drag the query/results divider or use its up/down arrows. **Focus results** expands the table; Escape restores SQL.
- **Configuration** provides project/runtime identity, agent verification, and project `.env` editing. Existing values are masked and never returned to the browser. Blank value fields preserve existing entries; removal is explicit. Saves check the loaded file hash and use owner-only permissions. Active work blocks changes, and an idle REPL reloads after saving. Studio, Python, and model-provider control variables cannot be overridden here. Unsupported multiline environment files require external editing.

The embedded pipeline output preview has been removed. Explore materialized data through SQL REPL.

## Start a managed Studio session

From the Studio directory, run:

```sh
python3 launch.py
```

The managed launcher owns one project-bound backend and window at a time. It opens the last valid project, or the included demo when no valid recent project exists.

To select a project at launch:

```sh
python3 launch.py --project /absolute/path/to/seeknal-project
```

The directory must contain a regular `seeknal_project.yml` file.

## Saved workspaces

A workspace is a saved name and one Seeknal project directory. Open **Configuration → Workspaces** to add a workspace, name it, and enter its directory path. The directory must already contain `seeknal_project.yml`; saving a workspace does not create a project or switch the running session.

Use the title-bar workspace selector for quick switching, or select **Open workspace** in Configuration. Studio restarts its project-bound backend, agent, and REPL on the selected directory. Unsaved editor and configuration inputs are guarded; active work must finish before switching.

Workspace names and paths persist separately from recent project history. Rename or edit a saved path in Configuration. A moved or unavailable project remains listed so its path can be repaired. Removing a workspace forgets its Studio registration and leaves all project files intact.

## Open another project directory

Use **Configuration → Workspaces → Add workspace**, **File → Add Workspace…**, or `⌘O` / `Ctrl+O` to register a project.

1. Enter a workspace name and project directory, or use **Browse…**. **Use current project** fills the currently open directory.
2. Select **Save workspace**. This saves the registration without switching.
3. Choose the saved workspace in the title-bar selector or **File → Saved Workspaces**, or select its **Open** button in Configuration.
4. Wait for the managed launcher to restart Studio on that project.

Folder-picker selection has not been verified through Computer Use; entering a canonical path is the verified fallback.

Invalid directories, cancelled picker/confirmation dialogs, and active project work leave the current project unchanged. A standalone `python3 server.py` session can manage registrations but cannot switch project runtimes.

After a switch, the old backend, agent, and REPL stop. A new project-bound runtime starts on the selected canonical directory. Editor buffers, queries, results, and agent conversations do not cross project boundaries.

Named workspaces persist separately from the eight-path recent-project history retained for launcher compatibility. Unavailable named workspaces remain visible for path repair. Forgetting a registration leaves its project directory intact.

### Pilot-to-normal governance boundary

The fixed synthetic pilot removes only `ATLAS_API_URL` and `ATLAS_PORTAL_URL`, and only while its verified pilot authority still matches. The launcher retains the original environment baseline. Switching to any normal project restores that baseline, including the original Atlas governance variables.

This behavior isolates the synthetic fixture. It does not relax production governance or authorize production access.

## Draft SQL with AI

The current owned agent configuration remains:

| Setting | Value |
| --- | --- |
| Provider protocol | OpenAI-compatible Sumopod |
| Model | `deepseek-v4.1-flash:netra` |
| Key file | `~/sumopod-ai-api-key` |

The backend reads the key file. The browser receives readiness and identity metadata, never the key value.

To request SQL help:

1. Open a transform YAML file.
2. Select **Tools → Draft SQL with AI**.
3. Review the displayed file, transform, input/schema, and buffer context.
4. Describe the SQL change.
5. Select **Send and create draft**.

Studio sends the real model:

- the full current editor buffer, including unsaved edits;
- the active file path and selected transform identity;
- declared graph inputs; and
- observed output columns when available.

The request requires exactly one reviewable transform draft with the same kind and name. It instructs the model to preserve every non-SQL metadata field and change only the requested SQL. The agent remains draft-only: it cannot save the source, validate, apply, run the pipeline, or execute SQL.

The agent-context limit is 256 KB. Editor preparation supports buffers up to 300 KB.

## Check and publish editor changes

Draft creation belongs in the explorer: select **New draft** under **Drafts**, or open an existing draft there. The clean active editor has no disabled Draft/Dry run/Apply action strip.

For a draft:

1. Edit its YAML.
2. Select **Dry run** in the editor footer. Studio prepares the draft buffer and runs the real Seeknal check; a separate Save step is unnecessary.
3. After that exact version passes, select **Apply draft**.
4. Studio opens the active destination and shows **Saved · Active**. The draft leaves the Drafts section.

For an active pipeline file:

1. Edit its YAML.
2. Select **Dry run changes**. Studio validates an internal snapshot; the active file remains unchanged.
3. After the check passes, select **Save changes**.
4. The footer clears and the editor returns to **Saved · Active**.

Editing again invalidates approval. Failure or cancellation keeps the buffer and requires another check. Save/apply checks both the prepared content hash and the destination hash. If you type newer edits while save/apply is running, Studio rebases those edits onto the saved destination and preserves them as unsaved changes.

`⌘S` / `Ctrl+S` follows the same contextual action. Non-pipeline project files retain ordinary direct Save. Publishing an edit never starts a pipeline run.

Seeknal 2.14 can return exit zero when transform preview execution failed. Studio records the native exit code but rejects that validation through the SDK’s structured preview outcome. Source schema-only fallback is a definition check and does not imply that data rows were previewed.

## Send transform SQL to the REPL

To inspect or adjust a transform query without executing it:

1. Open a transform YAML file.
2. Select **Tools → Send SQL to REPL**.
3. Review the populated SQL REPL editor.
4. Select **Run** or press `⌘Enter`/`Ctrl+Enter` only when you want to execute the query.

Studio parses the YAML and SQL before filling the REPL editor. It requires `kind: transform` and a transform name matching the active filename. The SQL AST rewrite changes only declared input references:

- `source.customers` becomes the registered REPL table name `source_customers` when that exact ref appears under `inputs`.
- `input_0` maps to the first declared input, `input_1` to the second, and so on.
- unrelated table references remain unchanged.

This action fills the REPL editor. It does not run the query, save the YAML, create a draft, or modify pipeline outputs.

## Use the project-bound SQL REPL

1. Open the **SQL REPL** tab.
2. Select **Start REPL**.
3. Enter SQL or a supported REPL command.
4. Select **Run** or press `⌘Enter`/`Ctrl+Enter`.

Studio starts the real persistent Seeknal REPL in the selected project context. It uses the selected project profile/environment context and loads non-provider variables from the project's regular, non-symlink `.env` file. Provider credentials—including Sumopod and other model API keys or tokens—are removed from the REPL environment.

The REPL is a read-only data-inspection surface. It does not expose a shell or pipeline execution control.

Supported commands:

| Command | Result |
| --- | --- |
| `.tables` | List registered tables. |
| `.schema <table>` | Describe one safely named table. |
| `.help` | Show supported commands. |
| `.reload` | Recreate the REPL and register current project outputs. |
| `.quit` | Close the current REPL worker. |

Query results are limited to 500 visible rows. Studio keeps query text, results, duration, REPL generation, session identity, run ID, and output signature for the current Studio session.

### Reload after pipeline output changes

The REPL binds to the output signature present when its worker starts. When a pipeline run changes `run_state.json`, Studio:

- marks earlier results as historical;
- shows **Reload required**;
- refuses a new query against the stale binding; and
- requires **Reload outputs** or `.reload` before querying the new outputs.

Reloading restarts only the REPL worker for the current project. Switching projects restarts the whole project-bound Studio runtime.

## Delivery boundary

The current deliverable remains a local development build. It does not include:

- a signed or notarized macOS application;
- a portable managed Python/Seeknal runtime;
- Windows or Linux installers;
- automatic updates; or
- an LSP, semantic refactoring engine, or code-intelligence server.

The Sumopod model and backend-private key-file contract are unchanged. The new workflow does not broaden agent authority or production data access.

## Verification evidence

Current workflow evidence:

- [IDE workflow receipt](../evidence/ide-workflow/receipt.json): 14 passed checks covering the real model draft, hash-bound workflow, explicit materialization, real persistent REPL, provenance reload, A-to-B project rebind, cleanup, and unchanged canonical pilot fixtures.
- [Manual dirty-buffer receipt](../evidence/ide-workflow/manual-dirty-regression.json) and [native screenshot](../evidence/ide-workflow/manual-dirty-apply.png): draft and dry run preserve unsaved content, and Apply alone writes the reviewed snapshot. The architecture review separately confirms that newer typing is not discarded.
- [Native package receipt](../evidence/ide-workflow/native-package.json): actual packaged project A-to-B typed-path switch, fresh runtime and agent identity, **Ready · verified**, old-process cleanup, and the explicit native-picker observation gap.
- [Bundle manifest](../evidence/ide-workflow/bundle-manifest.json): all 11 packaged source hashes match the frozen source.
- [Architecture review](../evidence/ide-workflow/architecture-review.md): **CLEAR** for the frozen source and rebuilt development app.
- [Final unit tests](../evidence/ide-workflow/final-unit-tests.txt): 99 passed.
- [Final frontend checks](../evidence/ide-workflow/final-frontend-tests.txt): 5 passed.

Independent review concluded **APPROVE** and architecture review concluded **CLEAR**. At documentation close, the final aggregate was rerunning after a test-fixture-only race where an old PID file was read before a valid positive PID was published. The corrected fixture waits for a nonempty positive PID; production source and packaged hashes were unchanged, and no product blocker was identified.

Native folder-picker launch is verified, but native-dialog selection is not. The typed canonical path fallback in the actual packaged app is verified.
