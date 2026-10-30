# Plan: live editor diagnostics (LSP phase 1)

Status: proposed, awaiting approval. Nothing here is implemented yet.

Goal: while editing a pipeline file, Studio underlines and lists
1. syntax errors (node YAML, the SQL inside it, Python node files),
2. unknown table / node names,
3. unknown column names.

Completion, hover types and go-to-definition come later (M4).

## 1. Architecture

An LSP-shaped HTTP endpoint, `POST /api/diagnostics`, not a full LSP server yet.

- The editor is a `<textarea>` over a mirrored highlight layer (`index.html:149-151`) and the
  frontend has no build step, so there is no LSP client to talk to. A pygls server would also
  need websocket support that neither `http.server` nor the SSH gateway proxy has
  (`remote_gateway.py:106-119`).
- Response fields follow LSP (`range`, `severity` 1-4, `code`, `source`), so a pygls adapter
  can wrap the same checker later without rework.
- Checks live in a new pure module `studio_lint.py`, run in the managed Seeknal venv through a
  new `engine_bridge.py diagnostics` action (JSON on stdin, like `extract_editor_sql`,
  `server.py:508-533`).
- SSH workspaces work unchanged: the gateway already forwards every `/api/*` path
  (`remote_gateway.py:130-148`), so linting runs on the host that owns the files.
- Linting never calls `DAGBuilder.build()`, because that imports user `.py` pipelines
  (`seeknal/pipeline/discoverer.py`). The linter builds its own index from
  `yaml.safe_load` of `seeknal/**/*.yml` (size-capped, no symlinks), cached by file
  mtime and size. Python node columns come from the server's cached `/api/graph` result.
- Import cost measured in the runtime: yaml + sqlglot ~0.11 s. A per-request subprocess is
  acceptable; if p95 exceeds ~300 ms, move to a persistent worker like `repl_service`.

## 2. Where each diagnostic's answer comes from

### Syntax
- YAML: `yaml.safe_load` `problem_mark` for line/column. Seeknal's
  `validate_yaml_syntax` raises `typer.Exit`, so it is reimplemented, not called.
- Node shape: kind and required fields from Seeknal's `validators.SCHEMAS` and
  `DAGBuilder.KIND_MAP`; name must match the file (`engine_bridge.py:292`). Unknown keys
  are warnings.
- SQL: `sqlglot.parse(read="duckdb")` gives line/column. An error is reported only if
  DuckDB's own parser (`extract_statements`, parses without executing) also rejects it;
  otherwise a "could not analyse" warning, so grammar differences never produce false errors.
- `{{ }}` templates and `ref('…')` are replaced by same-length placeholders before parsing so
  positions stay exact.
- Python node files: `ast.parse` only.
- Positions inside YAML block scalars come from `yaml.compose` `start_mark` plus the block's
  line and indent offset.

### Table / node names
- Every `inputs[].ref` must exist in the project index.
- Each table in SQL must be a CTE, `kind.name` / `kind_name`, `input_N` within range,
  `__THIS__`, or `ref('x')` with x in `inputs` (the executor otherwise fails with
  "Unknown ref", `transform_executor.py:548,590-603`).
- A node that exists but is not in `inputs` is a warning. "Did you mean" suggestions use
  `difflib`.

### Column names (first available source wins)
1. Declared `columns:` in the upstream node's YAML.
2. Inferred from the upstream transform's SQL with sqlglot `qualify`, walked in DAG order
   and memoised; `SELECT *` expands only when the upstream schema is known.
3. Schema of the last run's output, read from the file footer of
   `target/intermediate/{kind}_{name}.parquet`. Errors from this source are warnings,
   labelled "from last run".
4. Otherwise unknown: column checks are skipped for that table with one info note.

Pipelines are never executed for linting, and the REPL's DuckDB session is not used.
To avoid false positives, only qualified `t.col` references, or unqualified names found in
no table in scope and no select alias, are flagged.

## 3. API

Request: `{path, content, version}` (body limit 300 kB, as today).

Response:
```json
{"path": "...", "version": 7, "content_sha256": "...", "index_rev": "...", "elapsed_ms": 42,
 "diagnostics": [{"range": {"start": {"line": 4, "character": 9}, "end": {"line": 4, "character": 14}},
   "severity": 1, "code": "unknown-column", "source": "seeknal-studio",
   "message": "Column 'amout' not found in source.orders",
   "data": {"suggestions": ["amount"], "schema_origin": "declared"}}],
 "skipped": [{"code": "columns", "reason": "schema of transform.x unknown"}]}
```

- Client: request 350 ms after the last keystroke, and immediately when a file opens or
  the graph reloads. Abort the previous request. Ignore responses whose `version` or `path`
  no longer match the buffer.
- Server: one lint at a time, 5 s timeout. On timeout it returns empty diagnostics with an
  `unavailable` reason instead of an error. It does not take the execution lock.

## 4. Editor UI

- Wavy underlines: an extra pass in `renderSyntax` (`app.js:730-750`) wraps ranges in
  `diag-error` / `diag-warning` spans; the highlight layer shares the textarea's font
  metrics, so marks line up.
- Gutter: a severity dot on affected line numbers.
- Message on hover (computed from pointer position, since the textarea sits on top), and in a
  status strip whenever the cursor is inside a diagnostic, so it also works from the keyboard.
- Problems panel: lists the current buffer's diagnostics; clicking one moves the cursor there.
  (Today "Problems" counts failed jobs; that count stays, the new list is added.)
- Diagnostics are advisory: they never block Save or Dry run.

## 5. Milestones

| Milestone | Scope | Done when |
|---|---|---|
| M1 Syntax | endpoint, YAML / node-shape / SQL / Python syntax, underlines, Problems list | a `SELEC` typo inside a `|` block is marked at the exact line and column; p95 < 300 ms on `demo/`; a `.py` node that writes a marker file on import never runs; `target/` is untouched |
| M2 Tables | unknown `inputs` refs, unknown SQL tables, `ref()` not in inputs, suggestions | no false positives on CTEs, aliases, `input_N`, `__THIS__` (reusing the cases in `test_sql_draft_routes.py`) |
| M3 Columns | schema index, SQL inference, last-run fallback | typos in qualified and unqualified columns flagged with suggestions; no flags on select aliases, `EXCLUDE`/`REPLACE`, struct access; stale last-run schemas only warn |
| M4 Later | completion of tables/columns, hover types, go-to-definition, then a pygls adapter if a proper editor component is adopted | separate plan |

Tests: `tests/test_diagnostics.py` runs `studio_lint` on temporary fixture projects
(including must-not-flag cases); a route test; a gateway-forwarding test; Node checks of the
pure frontend helpers (decoration ranges, stale-response handling). Each milestone is also
verified in the running app on the local and SSH workspaces.

## 6. Risks and open questions

- sqlglot and DuckDB grammars differ; handled by confirming syntax errors with DuckDB.
- DuckDB extensions (alias reuse in WHERE/GROUP BY, `COLUMNS(*)`, lambdas) can cause false
  column errors; handled by the conservative flagging rule above.
- A remote host running an older Studio returns 404 for the new route; the UI then hides
  diagnostics silently.
- Open: which table-name forms Seeknal officially supports (bare `kind_name`, `__THIS__`,
  upstream nodes not listed in `inputs`); whether diagnostics should ever block Dry run;
  whether skipping column checks for Python nodes without a cached graph is acceptable.
