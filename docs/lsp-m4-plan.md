# Plan: editor intelligence (LSP M4)

Status: approved 2026-10-31, after one independent review round. M4.1 in progress.
Builds on M1–M3 (`docs/lsp-diagnostics-plan.md`).

Goal: in a pipeline YAML file, Studio
1. shows what a name is on hover (node, table, column, with its type),
2. jumps to where a name is defined,
3. offers completions for table names, `ref('…')` targets, `inputs` refs and columns.

Python node files keep syntax-only diagnostics. YAML key hover and completion, history
navigation, the REPL editor and an LSP server are later work (section 8).

## 1. Starting point (measured 2026-10-31)

- `/api/diagnostics` starts a new `engine_bridge.py diagnostics` process per request
  (`server.py:574-615`), serialised by `DIAGNOSTICS_LOCK` with a 1 s wait
  (`server.py:81-82,593`). On `demo/` that takes p50 182 ms, p95 214 ms
  (`tests/test_diagnostics.py:732`). The same `lint()` in a warm process takes p50 32 ms,
  p95 38 ms; the rest is interpreter start and importing yaml, sqlglot and duckdb (~240 ms).
- `ProjectIndex` (`studio_lint.py:135`) is rebuilt on every lint. It keeps node ids, kind,
  declared column names, SQL and inputs, but no file path, line or type, although YAML
  `columns:` carries `dtype` (`demo/seeknal/sources/orders.yml:6`).
- `SchemaResolver.schema` (`studio_lint.py:992`) prefers the last-run schema over declared
  columns when both exist. `demo/target/cache/source/orders.parquet` exists, so in the
  running demo `orders` columns come from the last run.
- `parquet_columns` (`studio_lint.py:1074`) reads only names from `parquet_schema()`.
  DuckDB 2.0's `parquet_schema` also returns `duckdb_type` (`VARCHAR`, `BIGINT`, `DATE`
  for `orders.parquet`); `logical_type` is NULL there.
- `Schema` and `Source` (`studio_lint.py:951-975`) carry names only. For an unqualified
  column, `column_sources` (`studio_lint.py:1339`) returns every table in scope, and
  `check_columns` (`studio_lint.py:1272`) only tests whether any of them has the name; no
  owner is chosen.
- The span parser (`parse_sql`, `studio_lint.py:640-666`) records spans for tables and
  columns only: not CTE names, alias declarations, the qualifier in `o.amount`, or `AS`
  projections. `SqlLocator.range` (`studio_lint.py:581`) falls back to the `transform:`
  key range when the SQL does not map 1:1 to the buffer (folded or quoted scalars).
  `sql_origin` (`studio_lint.py:548`) maps only `|` blocks and single-line plain scalars.
- The editor is a `<textarea>` over a highlight layer. `editorMetrics` (`app.js:1010`) and
  `pointerPosition` (`app.js:251`) map pointer to position with a fixed character width.
  The textarea's keydown handler turns Tab into two spaces (`app.js:3081`). The document
  handler (`app.js:3228-3239`) binds Cmd/Ctrl+S, O, K (global search), `\` and Esc (which
  also leaves focus mode and closes the agent panel).
- `openFile(path, {guard})` (`app.js:795`) asks before leaving an unsaved buffer. There is
  one `#code-editor`, so the YAML view is open in one editor group at a time.
- Already handled for any new `POST /api/*` route: the loopback check and the per-launch
  access token (`server.py:1557-1561`), and forwarding over SSH (`remote_gateway.py:158-163`).

## 2. Decisions

**A. Hover and go-to-definition come from the lint reply.**
The lint pass returns `symbols` for the buffer, and the client looks up the cursor or
pointer position locally, with no extra request. A reply is used only while its `version`
equals the buffer's. While a lint is pending, hover shows diagnostics only, and
go-to-definition asks for an immediate lint and acts on the reply if the cursor has not moved.

The work in `studio_lint.py` this needs (all M4.1):
- `Schema`/`Source` carry, per column, name, type, origin and definition, and
  `scope_output`, `expand_star`, `renamed_output` and the UNION merge pass them through.
- The span parser also records CTE names, alias declarations, qualifiers and `AS`
  projections.
- Owner rules for a column:
  - qualified: the qualifier's table;
  - unqualified: the one table in scope that has it;
  - `USING` / `NATURAL` join columns: the left table, with the right one also listed;
  - a reused select alias: its projection.
  Otherwise the symbol is "ambiguous" or "unresolved", with candidates listed and no
  definition.
- A symbol is emitted only when its range is exact; `SqlLocator`'s key-range fallback
  produces no symbol. Symbol ranges go through `utf16_range` like diagnostics.

**B. Completion is a separate, position-specific request.**
`POST /api/completion` with `{path, content, version, position}`. Steps:
1. Convert the UTF-16 position to a code-point offset. SQL completion applies only when
   the cursor is inside a `|` block (at or right of the block indent, or on a blank line
   inside it) or a single-line plain scalar, the same forms `sql_origin` maps. Otherwise
   only the `inputs` ref context applies.
2. Decide the context from the tokens before the cursor, with a small lexer that tolerates
   an unclosed quote: `FROM` / `JOIN` / `,` in a FROM list → table; inside `ref('` → node;
   `ident.` → that table's columns; other SQL → columns; `ref:` under `inputs` → node.
   No parse is needed for table and node contexts.
3. For column contexts only, insert `__studio_cursor__` at the cursor and run the scope
   analysis. The DuckDB syntax confirmation that gates `check_sql`
   (`sql_syntax_error`, `studio_lint.py:729-732`) is skipped on this path. If sqlglot still cannot parse, the
   fallback offers the columns of every input, marked "not resolved".

The client asks once at the start of a word and filters locally while the word is typed.
It asks again only on a new `.`, a new clause keyword or a new word.

**C. A persistent lint worker replaces the per-request process (M4.3).**
- One `engine_bridge.py lint-worker` per open project, in the managed venv, with
  `clean_environment()`, `cwd` at the project, no shell, no extra file descriptors
  (`close_fds`), and JSON lines over stdin and stdout. It runs only `studio_lint`, so no
  project code is imported.
- It is not started in a new session, and it exits when stdin closes. So it ends with the
  server even when the launcher SIGKILLs the server (`launch.py:87-92`).
- The server owns a `LintClient` that replaces `DIAGNOSTICS_LOCK`: one request in flight
  at a time, plus a queue where completion goes ahead of lint and a lint superseded by a
  newer version of the same path is dropped before it is sent.
- It never takes `JOBS.execution_lock` or the project lock, so completion keeps working
  while a pipeline runs.
- A request over 5 s kills the worker and the next request starts a new one. The worker
  is also stopped after 10 idle minutes, which `ReplService` does not do, so this is new.
- Each reply carries the worker's `ru_maxrss`, normalised (bytes on macOS, KB on Linux).
  Past 512 MB the worker is replaced after the current reply.
- The index is cached in the worker. Each request recomputes the signature (path,
  mtime_ns, size) from a fresh `os.walk` of `seeknal/` with the same discovery filter as
  `ProjectIndex.__init__` (`studio_lint.py:145-157`), and rebuilds the index when it
  differs, so added and deleted node files are noticed.
- If the worker cannot start, diagnostics fall back to the per-request process and
  completion replies `unavailable`.

**D. Types and origins are per column, and only where known.**
- Declared: `columns.<name>.dtype`, as written. When a name is declared, the hover shows
  the declared type and the definition goes to the declared key, even if a last-run schema
  exists; the last-run type is added as "last run: BIGINT" when it differs.
- Last run: `parquet_schema().duckdb_type`.
- Inferred: sqlglot `annotate_types` on the upstream query, without `qualify` (which
  raises for the whole statement if one column is unresolved). Owners come from rule A: on a
  copy of the tree, each owned unqualified column gets its owner's qualifier before
  annotation, since sqlglot types only qualified columns. Each column's type is built with `DataType.build` inside try/except, so one odd `dtype`
  (`float64`) only loses that column's type. Masked `ref()` tables get schema entries.
  Types sqlglot reports as UNKNOWN are not shown.
- Diagnostics severities from M3 do not change.

**E. Definitions point only at project files.**
Targets are project-relative paths from the index, which skips symlinks and stays inside
`seeknal/`. The client opens them through `/api/file`, which validates the path again.
Drafts and `target/` are never targets.

Cross-file targets (node `name:` line, declared column key, upstream select expression)
are computed only for nodes the buffer references, at most 50 per lint. Each such file is
composed with `yaml.compose` inside its own try/except and within `INDEX_FILE_LIMIT`, so a
bad upstream file loses only its targets, never the current file's diagnostics.

## 3. What is resolved

| Under the cursor | Hover shows | Definition goes to |
|---|---|---|
| `inputs[].ref` value, `ref('x')`, `kind.name` / `kind_name` table | kind, name, file, column count, schema origin | the node's `name:` line (YAML) or decorator line (Python) |
| `input_N`, `__THIS__`, mapping-form input name | the node it stands for | the matching `inputs` entry in this buffer |
| CTE name or table alias | what it stands for | its declaration in this buffer |
| Column | name, type, owner, origin ("declared", "from SQL", "from last run"), or candidates when ambiguous | declared key; else the upstream select expression; else the CTE / subquery select item in this buffer; none for last-run-only or ambiguous columns |

## 4. API

The `/api/diagnostics` reply gains `targets` and `symbols`. Targets are shared, so repeated
references do not repeat paths:
```json
"targets": [
  {"kind": "column", "name": "amount", "type": "integer", "last_run_type": "BIGINT",
   "owner": "source.orders", "origin": "declared",
   "path": "seeknal/sources/orders.yml",
   "range": {"start": {"line": 10, "character": 2}, "end": {"line": 10, "character": 8}}},
  {"kind": "column", "name": "customer_id", "origin": "ambiguous",
   "candidates": ["source.orders", "source.customers"]}
],
"symbols": [[9, 11, 9, 17, 0]]
```
Each symbol is `[startLine, startChar, endLine, endChar, targetIndex]`. An ambiguous or
unresolved target has `origin` `"ambiguous"` or `"unresolved"`, `candidates`, and no
`path` or `range`. At most 2,000
symbols and 500 targets; strings are clipped like diagnostic messages. When capped, a
`skipped` entry says so.

`POST /api/completion` reply:
```json
{"path": "...", "version": 12, "context": "column", "isIncomplete": false,
 "replace": {"start": {"line": 9, "character": 11}, "end": {"line": 9, "character": 13}},
 "items": [{"label": "amount", "kind": "column", "detail": "integer · source.orders",
            "insertText": "amount", "sortText": "0amount"}]}
```
Kinds: `node`, `input`, `cte`, `alias`, `column`. Table contexts list inputs, `input_N`,
`__THIS__` and CTEs first, then other project nodes marked "not in inputs". At most 200
items. The same body limit and path checks as diagnostics apply. No project code or SQL is
run; the placeholder only changes the text that is parsed.

## 5. Editor UI

- **Hover**: the existing tooltip (`#diagnostic-tooltip`) shows symbol info, below any
  diagnostic at the same position, after the pointer rests 300 ms.
  - From the keyboard, Shift+F12 shows the same at the caret and writes it once to
    the polite live region.
  - The status strip shows the symbol under the caret when there is no diagnostic, but
    does not announce it, so caret moves stay quiet.
- **Go to definition**: F12 or Cmd/Ctrl+click.
  - Same file: move the caret and scroll, as `revealDiagnostic` does.
  - Other file: `openFile(path)` with the unsaved-changes guard, then place the caret.
  - Back to the position before the last jump: Ctrl+- on macOS, Alt+Left elsewhere
    (Ctrl+- zooms out in Chromium on Windows and Linux). One step only; a history stack
    is later work.
  - With no target, the status line says why ("ambiguous: orders or customers",
    "known from last run only").
- **Completion popup**: a listbox below the caret, placed with `editorMetrics`.
  - Up/Down move, Enter or Tab accept (Tab still inserts two spaces when the popup is
    closed), Esc closes.
  - Esc and the accept keys call `stopPropagation` while the popup is open, so the global
    Esc handler does not also leave focus mode.
  - It opens after `.`, `ref('`, `FROM `, `JOIN `, and after 2 identifier characters
    inside SQL.
  - Ctrl+Space opens it anywhere. macOS often binds Ctrl+Space to switching input
    sources, so Cmd/Ctrl+I does the same.
  - Accepting uses `setRangeText` plus an `input` event, so undo, dirty state and lint
    scheduling behave as for typing.
- **Accessibility**: the textarea keeps its textbox role. It gets `aria-autocomplete="list"`,
  `aria-controls` and `aria-activedescendant` pointing at the selected option; the popup
  is a `role="listbox"` of `role="option"` items. `aria-expanded` is not used, since a
  textbox does not support it. Whether screen readers announce the popup without
  `role="combobox"` is checked with VoiceOver in M4.4.
- Everything stays advisory. A 404 from an older remote Studio hides the feature until the
  workspace reloads, as diagnostics already do.

## 6. Milestones

| Milestone | Scope | Done when |
|---|---|---|
| M4.1 Hover | per-column type/origin/definition through scopes, new spans, owner rules, `targets`/`symbols`, tooltip, keyboard hover | in `demo/` with `target/`, hovering `amount` in a transform shows `integer · source.orders (declared) · last run: BIGINT`; with `target/` removed, `integer · source.orders (declared)`; a `USING` column shows its left table; an ambiguous column shows candidates and no definition; no symbol info from an older reply; lint p95 on `demo/` rises by at most 20 ms |
| M4.2 Go to definition | in-buffer and cross-file targets, one-step back, guard | F12 on a ref opens the node file at its `name:` line; on a declared column, at its key; on an upstream SQL column, at its select expression; a dirty buffer asks first; same results on the SSH workspace; a deeply nested upstream YAML file loses only its targets |
| M4.3 Lint worker | `lint-worker`, `LintClient` queue, index cache, idle stop, RSS limit, fallback | warm lint p95 < 60 ms on `demo/` and < 300 ms on a generated 2,000-file project; a crashed or hung worker is replaced and the next request succeeds; completion is answered while a pipeline run holds the execution lock; the marker-file test still proves no project code is imported; no worker process remains after a workspace switch, server exit, or SIGKILL of the server |
| M4.4 Completion | endpoint, context lexer, placeholder analysis, popup | `o.` lists the columns of `o`'s table; `ref('` (unclosed) lists nodes; `JOIN ` lists inputs and CTEs; a CTE defined below the cursor is offered; nothing SQL-related is offered left of the block indent; p95 < 100 ms request to popup locally; accepting is undone with one Cmd/Ctrl+Z; Esc closes the popup without leaving focus mode |

M4.1 and M4.2 run on today's per-request process, so they can ship before the worker.
M4.4 depends on M4.3.

Tests:
- `tests/test_diagnostics.py` gains symbol cases:
  - each table form, CTEs and aliases;
  - qualified, unqualified, `USING` and ambiguous columns;
  - nested scopes and UNION;
  - types from each origin, including a bad `dtype`;
  - inexact ranges producing no symbol;
  - non-BMP characters;
  - caps;
  - a fixture with `target/` present.
- A new `tests/test_completion.py` covers each context, the unclosed `ref('`, positions
  outside the block indent, the placeholder, the parse-failure fallback and limits.
- `tests/test_lint_worker.py` covers start, crash, timeout, idle stop, RSS replacement,
  stdin-EOF exit, queue priority, dropping superseded lints, the execution-lock case, and
  that adding or deleting a node file invalidates the cached index.
- Node checks cover the pure frontend helpers: symbol lookup, popup filtering, replace
  ranges and key handling.
- Each milestone is also verified in the running app, locally and over SSH, with the
  headless browser.

## 7. Risks

- **Position drift after edits.** Hover and definition use only a reply for the current
  version, and completion is computed for the exact buffer.
- **Completion lag over SSH.** One round trip per request. If p95 over Tailscale exceeds
  250 ms, reuse the last reply's candidates for the same context and refresh in the
  background.
- **Type inference cost.** `annotate_types` runs per upstream query; it gets its own share
  of the 20 ms budget and is skipped (types left unknown) past 10 ms per lint.
- **Wrong types.** Only declared, last-run and sqlglot-annotated types are shown, each with
  its origin.
- **Textarea limits.** Popup placement assumes a fixed-width font and no wrapping, as hover
  already does.
- **An older remote host** returns 404 for `/api/completion` and no `symbols`; the features
  hide themselves.

## 8. Later

- YAML key hover and completion (`kind` values, node fields). Needs field descriptions,
  which the copied `SCHEMAS` (`studio_lint.py:38`) does not have.
- A back/forward history stack; rename; find references; formatting; quick fixes for
  "did you mean".
- Python node files beyond syntax.
- REPL editor completion, from the REPL's live DuckDB catalog rather than the linter.
- **LSP server (pygls):** not built in M4. The reply shapes follow LSP, so an adapter is a
  thin wrapper over the worker. It is worth building only if Studio adopts a real editor
  component (CodeMirror 6 or Monaco, which needs a vendored bundle or a build step) or if
  people want these checks in VS Code.

## 9. Decisions on the open questions

1. Completion opens automatically while typing, as planned in section 5 (approved).
2. The worker stops after 10 idle minutes, accepting a ~250 ms cold start on the next
   request (approved).
