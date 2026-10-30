from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class IdeFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app.js").read_text()
        cls.html = (ROOT / "index.html").read_text()
        cls.css = (ROOT / "styles.css").read_text()

    def test_editor_guided_dry_run_and_apply_are_real_actions(self):
        for label in ("Draft SQL with AI", "Dry run changes", "Save validated changes", "New draft"):
            self.assertIn(label, self.html)
        self.assertIn('api("/api/editor/prepare"', self.app)
        self.assertIn("original_content: state.originalContent", self.app)
        self.assertIn('startJob("validate-draft"', self.app)
        self.assertIn('startJob("apply-draft"', self.app)
        self.assertIn("content_hash: approval.contentHash", self.app)
        self.assertIn("confirmed: true", self.app)
        self.assertIn('action !== "validate-draft" && !exactSnapshotApply', self.app)
        self.assertIn("local.snapshotAuthorized === true", self.app)
        self.assertIn("pendingDraftApply", self.app)
        self.assertIn("bufferAtApply", self.app)
        self.assertIn("matchesApplyContext(applied, state.selectedFile", self.app)
        self.assertIn("newer editor edits were preserved", self.app)

    def test_manual_buffer_draft_is_not_an_editor_tool(self):
        self.assertNotIn('id="draft-from-editor"', self.html)
        self.assertNotIn('$("#draft-from-editor")', self.app)
        self.assertIn("async function createDraftFromEditor()", self.app)
        self.assertIn("createDraftFromEditor,", self.app)

    def test_pipeline_explorer_excludes_managed_drafts(self):
        render_files = re.search(r"function renderFiles\(\).*?\n}", self.app, re.DOTALL)
        self.assertIsNotNone(render_files)
        self.assertIn("if (isDraftPath(file.path)) continue", render_files.group(0))
        self.assertIn('id="explorer-drafts"', self.html)

    def test_ai_sql_context_is_full_current_transform_buffer_and_draft_only(self):
        body = re.search(r"async function sendSqlDraftRequest\(\).*?\n}\n", self.app, re.DOTALL)
        self.assertIsNotNone(body)
        text = body.group(0)
        self.assertIn("context.content", text)
        self.assertIn("selected_transform", text)
        self.assertIn("observed_output_columns", text)
        self.assertIn("Preserve every non-SQL metadata field", text)
        self.assertIn("Never save the source file, apply a draft, validate, run, or execute SQL", text)
        self.assertNotIn('startJob("run")', text)
        self.assertNotIn("saveCurrentFile", text)
        self.assertRegex(self.app, r"/\\\.ya\?ml\$/i")

    def test_project_menu_uses_saved_workspace_protocol_and_dirty_guard(self):
        self.assertIn('role="menubar"', self.html)
        self.assertIn("Add Workspace…", self.html)
        self.assertIn('api("/api/workspaces")', self.app)
        self.assertIn('api("/api/projects/browse"', self.app)
        self.assertIn('api("/api/workspaces/open"', self.app)
        self.assertIn("state.dirty && !window.confirm", self.app)
        self.assertIn("hasActiveWork()", self.app)
        self.assertIn("hasUnsavedEnvironmentInputs()", self.app)
        self.assertIn("hasUnsavedWorkspaceForm()", self.app)
        self.assertIn("awaitProjectRestart", self.app)
        self.assertIn("window.location.reload()", self.app)
        self.assertIn("event.key.toLowerCase() === \"o\"", self.app)
        self.assertNotIn('type="file"', self.html)

    def test_repl_is_a_project_bound_user_driven_surface(self):
        self.assertIn('id="repl-workbench"', self.html)
        self.assertIn('api("/api/repl/status")', self.app)
        self.assertIn('api("/api/repl/start"', self.app)
        self.assertIn('api("/api/repl/query"', self.app)
        self.assertIn("/api/repl/query?id=${encodeURIComponent(id)}", self.app)
        self.assertIn("/api/repl/query/${encodeURIComponent(id)}/cancel", self.app)
        self.assertIn('api("/api/repl/reload"', self.app)
        self.assertIn('api("/api/repl/extract"', self.app)
        self.assertIn("payload.repl_sql || payload.sql", self.app)
        self.assertIn("payload.rewritten_refs", self.app)
        self.assertIn("AST-mapped", self.app)
        self.assertIn("SQL copied to REPL editor; it has not been executed", self.app)
        self.assertIn("event.key === \"Enter\"", self.app)
        self.assertIn("markReplStale", self.app)
        self.assertIn("payload.needs_reload === true", self.app)
        self.assertIn('payload.error_code === "reload_required"', self.app)
        self.assertIn("payload.historical || payload.current === false", self.app)

    def test_menu_keyboard_and_focus_accessibility_hooks_exist(self):
        self.assertIn('role="menu"', self.html)
        self.assertIn('aria-expanded="false"', self.html)
        self.assertIn("if (event.key === \"Escape\")", self.app)
        self.assertIn('.focus()', self.app)
        self.assertIn("button:focus-visible", self.css)


if __name__ == "__main__":
    unittest.main()
