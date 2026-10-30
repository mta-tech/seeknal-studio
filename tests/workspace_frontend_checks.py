from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


def function_body(source: str, name: str) -> str:
    match = re.search(rf"(?:async )?function {re.escape(name)}\([^)]*\) \{{", source)
    if not match:
        raise AssertionError(f"missing function {name}")
    start = match.start()
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated function {name}")


class WorkspaceFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app.js").read_text()
        cls.html = (ROOT / "index.html").read_text()
        cls.css = (ROOT / "styles.css").read_text()

    def test_startup_routes_to_graph_and_grid_items_have_explicit_columns(self):
        initialize = function_body(self.app, "initialize")
        self.assertIn("renderEditorGroups()", initialize)
        self.assertNotIn('setMainView("graph")', initialize)
        self.assertIn('class="studio-main"', self.html)
        self.assertIn('data-split="false"', self.html)
        self.assertIn('left: { tabs: ["graph"], active: "graph" }', self.app)
        self.assertIn('right: { tabs: [], active: null }', self.app)
        for selector, column in (
            (".activity-rail", "1"),
            (".explorer", "2"),
            (".vertical-resizer", "3"),
            (".studio-main", "4"),
            (".agent-panel", "5"),
        ):
            self.assertIn(f".workspace>{selector}{{grid-column:{column}}}", self.css)

    def test_historical_log_selection_never_replaces_execution_authority(self):
        initialize = function_body(self.app, "initialize")
        render_jobs = function_body(self.app, "renderJobs")
        select_log = function_body(self.app, "selectLogJob")
        self.assertIn('job.status === "running"', initialize)
        self.assertIn('job.status === "queued"', initialize)
        self.assertIn("const authoritativeJob = state.activeJob", render_jobs)
        self.assertIn("const job = state.jobs.find", render_jobs)
        self.assertIn("const displayRunning = job", render_jobs)
        self.assertIn("const running = authoritativeJob", render_jobs)
        self.assertIn("state.selectedLogJobId = id", select_log)
        self.assertNotIn("state.activeJob =", select_log)
        self.assertIn("Cancel active job ${authoritativeJob.id}", render_jobs)

    def test_busy_environment_conflict_preserves_typed_values(self):
        save_environment = function_body(self.app, "saveEnvironment")
        self.assertIn('error_code === "environment_conflict"', save_environment)
        self.assertIn("await loadEnvironment()", save_environment)
        self.assertIn("Busy · values preserved", save_environment)
        busy_branch = save_environment.split('error_code === "environment_conflict"', 1)[1].split("finally", 1)[0]
        self.assertEqual(busy_branch.count("await loadEnvironment()"), 1)
        self.assertIn("entered values remain in this form", busy_branch)

    def test_resizers_support_pointer_keyboard_and_accessible_values(self):
        self.assertIn('id="workspace-resizer"', self.html)
        self.assertIn('id="repl-resizer"', self.html)
        self.assertEqual(self.html.count('role="separator"'), 3)
        self.assertGreaterEqual(self.html.count('aria-valuenow='), 3)
        self.assertIn('["ArrowLeft", "ArrowRight"]', self.app)
        self.assertIn('["ArrowUp", "ArrowDown"]', self.app)
        self.assertGreaterEqual(self.app.count('setAttribute("aria-valuenow"'), 4)

    def test_graph_drag_preserves_grab_offset_and_clamps_full_card(self):
        render_graph = function_body(self.app, "renderGraph")
        fit_graph = function_body(self.app, "fitGraph")
        self.assertIn("const grabX = event.clientX - (buttonRect.left + buttonRect.width / 2)", render_graph)
        self.assertIn("const grabY = event.clientY - (buttonRect.top + buttonRect.height / 2)", render_graph)
        self.assertIn("moveEvent.clientX - grabX - rect.left", render_graph)
        self.assertIn("moveEvent.clientY - grabY - rect.top", render_graph)
        self.assertIn("button.offsetWidth / 2 / rect.width", render_graph)
        self.assertIn("button.offsetHeight / 2 / rect.height", render_graph)
        self.assertIn("saveGraphPositions()", render_graph)
        self.assertIn("card.offsetWidth / 2", fit_graph)
        self.assertIn("card.offsetHeight / 2", fit_graph)

    def test_escape_resets_focus_visual_and_accessibility_state(self):
        bind_events = function_body(self.app, "bindEvents")
        self.assertIn('classList.remove("editor-focused")', bind_events)
        self.assertIn('$("#editor-focus").textContent = "Focus"', bind_events)
        self.assertIn('$("#editor-focus").setAttribute("aria-pressed", "false")', bind_events)
        self.assertIn('classList.remove("results-focused")', bind_events)
        self.assertIn('$("#repl-results-focus").textContent = "Focus results"', bind_events)
        self.assertIn('$("#repl-results-focus").setAttribute("aria-pressed", "false")', bind_events)

    def test_repl_table_inspection_is_separate_from_user_query_results(self):
        inspect_tables = function_body(self.app, "inspectReplTables")
        poll_inspection = function_body(self.app, "pollReplInspection")
        run_query = function_body(self.app, "runReplQuery")
        self.assertIn('body: { sql: ".tables" }', inspect_tables)
        self.assertIn("state.repl.inspectPending = true", inspect_tables)
        self.assertLess(inspect_tables.index("state.repl.inspectPending = true"), inspect_tables.index('await api("/api/repl/query"'))
        self.assertIn("state.repl.inspectId", inspect_tables)
        self.assertIn("state.repl.queryId", inspect_tables)
        self.assertIn("renderReplTables(names)", poll_inspection)
        self.assertNotIn("renderReplResult", poll_inspection)
        self.assertNotIn("state.repl.history", poll_inspection)
        self.assertIn("state.repl.inspectId", run_query)
        self.assertIn("state.repl.queryPending = true", run_query)
        self.assertLess(run_query.index("state.repl.queryPending = true"), run_query.index('await api("/api/repl/query"'))
        self.assertIn("state.repl.inspectPending", run_query)
        self.assertIn('payload.columns.indexOf("table")', poll_inspection)
        self.assertIn('title="Open SQL REPL data exploration"', self.html)

    def test_repl_acceptance_reservation_blocks_duplicate_and_user_query(self):
        inspect_tables = function_body(self.app, "inspectReplTables")
        run_query = function_body(self.app, "runReplQuery")
        harness = f"""
const assert = require('assert');
const document = {{ createElement: () => ({{}}) }};
const elements = new Map();
const $ = selector => {{
  if (!elements.has(selector)) elements.set(selector, {{ value: 'SELECT 1', disabled: false, replaceChildren() {{}} }});
  return elements.get(selector);
}};
const state = {{ repl: {{ inspectPending:false, queryPending:false, inspectId:null, queryId:null, tablesLoaded:false, history:[] }} }};
let accepts = 0, resolveAccept;
const api = () => {{ accepts += 1; return new Promise(resolve => {{ resolveAccept = resolve; }}); }};
const renderReplHistory = () => {{}};
const renderReplError = error => {{ throw new Error(error); }};
const refreshReplStatus = async () => {{}};
const pollReplInspection = async id => {{ assert.equal(id, 'inspect-1'); state.repl.inspectId = null; }};
const pollReplQuery = () => {{ throw new Error('user query overtook inspection'); }};
{inspect_tables}
{run_query}
(async () => {{
  const first = inspectReplTables();
  const duplicate = inspectReplTables();
  const user = runReplQuery();
  await Promise.resolve();
  assert.equal(accepts, 1);
  assert.equal(state.repl.inspectPending, true);
  assert.equal(state.repl.queryPending, false);
  resolveAccept({{query_id:'inspect-1'}});
  await Promise.all([first, duplicate, user]);
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        result = subprocess.run(["node"], input=harness, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repl_reload_reservation_blocks_duplicate_mutation(self):
        reload_repl = function_body(self.app, "reloadRepl")
        harness = f"""
const assert = require('assert');
const elements = new Map();
const $ = selector => {{ if (!elements.has(selector)) elements.set(selector, {{disabled:false, innerHTML:'', textContent:''}}); return elements.get(selector); }};
const state = {{ repl: {{reloadPending:false,queryPending:false,inspectPending:false,queryId:null,inspectId:null,result:{{}},stale:true,tablesLoaded:true}} }};
let reloads = 0, inspections = 0, resolveReload;
const api = path => {{ assert.equal(path, '/api/repl/reload'); reloads += 1; return new Promise(resolve => {{resolveReload=resolve;}}); }};
const renderReplStatus = () => {{}};
const inspectReplTables = async () => {{ inspections += 1; }};
const showError = error => {{ throw error; }};
const refreshReplStatus = async () => {{}};
{reload_repl}
(async () => {{
  const first = reloadRepl(); const duplicate = reloadRepl();
  await Promise.resolve();
  assert.equal(reloads, 1); assert.equal(state.repl.reloadPending, true);
  resolveReload({{state:'ready'}}); await Promise.all([first, duplicate]);
  assert.equal(inspections, 1); assert.equal(state.repl.reloadPending, false);
}})().catch(error => {{console.error(error);process.exit(1);}});
"""
        result = subprocess.run(["node"], input=harness, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_saved_workspace_surfaces_are_first_class_and_accessible(self):
        for marker in (
            'id="workspace-menu-trigger"',
            'aria-haspopup="menu"',
            'id="workspace-menu-list"',
            'id="manage-workspaces"',
            'class="configuration-card workspaces-card"',
            'id="workspace-list"',
            'id="workspace-form"',
            'id="workspace-use-current"',
            'id="workspace-browse"',
        ):
            self.assertIn(marker, self.html)
        self.assertNotIn("local workspace", self.html)
        self.assertIn("Remove from Studio list; files remain", self.app)
        self.assertIn('event.key === "Escape"', self.app)
        self.assertIn('!event.target.closest(".workspace-switcher")', self.app)

    def test_workspace_contract_uses_backend_ids_and_managed_restart(self):
        load = function_body(self.app, "loadWorkspaces")
        save = function_body(self.app, "saveWorkspace")
        remove = function_body(self.app, "requestForgetWorkspace")
        open_workspace = function_body(self.app, "admitWorkspaceOpen")
        restart = function_body(self.app, "awaitProjectRestart")
        self.assertIn('api("/api/workspaces")', load)
        self.assertIn('api("/api/workspaces", { method: "POST", body })', save)
        self.assertIn('api("/api/workspaces/remove", { method: "POST", body: { id: workspace.id } })', remove)
        self.assertIn('api("/api/workspaces/open", { method: "POST", body: { id: admitted.id } })', open_workspace)
        self.assertNotIn("body: { path:", open_workspace)
        self.assertIn('api("/api/workspaces")', restart)
        self.assertIn("awaitProjectRestart(target.path, 0, target.id)", open_workspace)

    def test_current_unavailable_and_standalone_status_runtime(self):
        normalize = function_body(self.app, "normalizeWorkspaces")
        is_current = function_body(self.app, "workspaceIsCurrent")
        open_reason = function_body(self.app, "workspaceOpenReason")
        harness = f"""
const assert = require('assert');
const state = {{ project: {{project_path:'/a'}}, workspaces: {{editingId:null,forgetId:null,formSnapshot:null}} }};
{normalize}
{is_current}
{open_reason}
state.workspaces = normalizeWorkspaces({{
  current: {{id:'a',name:'Commerce',path:'/a',available:true,reason:null,can_switch:true}},
  entries: [], managed:true, can_switch:true
}});
assert.equal(workspaceIsCurrent({{id:'a',path:'/different'}}), true);
assert.equal(workspaceOpenReason({{id:'b',path:'/missing',available:false,reason:'Directory moved',can_switch:false}}), 'Directory moved');
state.workspaces.managed = false; state.workspaces.can_switch = false;
assert.equal(workspaceOpenReason({{id:'b',path:'/b',available:true,reason:null,can_switch:false}}), 'Open requires a managed Studio launcher');
"""
        result = subprocess.run(["node"], input=harness, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_switch_guard_runtime_blocks_busy_and_preserves_dirty_inputs_on_cancel(self):
        unsaved_workspace = function_body(self.app, "hasUnsavedWorkspaceForm")
        block_reason = function_body(self.app, "workspaceSwitchBlockReason")
        discard_warnings = function_body(self.app, "workspaceDiscardWarnings")
        harness = f"""
const assert = require('assert');
const values = {{
  '#workspace-form': {{hidden:false}}, '#workspace-id': {{value:'w'}},
  '#workspace-name-input': {{value:'Renamed'}}, '#workspace-path-input': {{value:'/a'}},
}};
const $ = selector => values[selector];
const state = {{dirty:false, environment:{{dirty:false}}, workspaces:{{formSnapshot:{{id:'w',name:'Original',path:'/a'}}}}}};
let active = true;
const hasActiveWork = () => active;
const hasUnsavedEnvironmentInputs = () => false;
{unsaved_workspace}
{block_reason}
{discard_warnings}
assert.match(workspaceSwitchBlockReason(), /Finish or cancel/);
active = false;
assert.deepEqual(workspaceDiscardWarnings(), ['The workspace name or project path form has unsaved changes.']);
assert.equal(values['#workspace-name-input'].value, 'Renamed');
state.workspaces.formSnapshot.name = 'Renamed'; state.dirty = true;
assert.deepEqual(workspaceDiscardWarnings(), ['The modified YAML editor buffer has not been saved.']);
"""
        result = subprocess.run(["node"], input=harness, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_open_workspace_runtime_posts_captured_registry_id_only(self):
        open_workspace = function_body(self.app, "admitWorkspaceOpen")
        harness = f"""
const assert = require('assert');
let request = null, restarted = null;
const state = {{workspaces:{{openPending:null,savePending:false,forgetPending:null,unloadAuthorized:false}}}};
const renderWorkspaces = () => {{}};
const setWorkspaceFormDisabled = () => {{}};
const workspaceStatus = () => {{}};
const setStatus = () => {{}};
const loadWorkspaces = async () => {{}};
const api = async (path, options) => {{ request = {{path, options}}; return {{workspace:{{id:'server-id',name:'B',path:'/canonical/b'}}}}; }};
const awaitProjectRestart = path => {{ restarted = path; }};
const setTimeout = callback => callback();
const workspaceOperationPending = () => Boolean(state.workspaces.openPending || state.workspaces.savePending || state.workspaces.forgetPending);
{open_workspace}
(async () => {{
  await admitWorkspaceOpen({{id:'captured-id',name:'B',path:'/typed/b',available:true,can_switch:true}});
  assert.equal(request.path, '/api/workspaces/open');
  assert.deepEqual(request.options.body, {{id:'captured-id'}});
  assert.equal(restarted, '/canonical/b');
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        result = subprocess.run(["node"], input=harness, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
