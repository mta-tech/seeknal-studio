from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CliDraftFrontendChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app.js").read_text()
        cls.html = (ROOT / "index.html").read_text()
        cls.css = (ROOT / "styles.css").read_text()

    def test_primary_draft_and_file_menu_open_real_cli_dialog(self):
        self.assertIn('id="new-draft-dialog"', self.html)
        self.assertIn('id="menu-new-node"', self.html)
        self.assertIn('$("#draft-editor").addEventListener("click", openNewDraftDialog)', self.app)
        self.assertIn('$("#menu-new-node").addEventListener("click", openNewDraftDialog)', self.app)
        self.assertIn('api("/api/drafts/options")', self.app)
        self.assertIn('startJob("draft", null, extra)', self.app)

    def test_cli_fields_match_backend_contract_and_never_force(self):
        for field in (
            "new-draft-type",
            "new-draft-name",
            "new-draft-description",
            "new-draft-python",
            "new-draft-deps",
        ):
            self.assertIn(f'id="{field}"', self.html)
        submit = re.search(r"async function submitCliDraft\(\).*?\n}", self.app, re.DOTALL)
        self.assertIsNotNone(submit)
        body = submit.group(0)
        for key in ("node_type", "name", "description", "python", "deps"):
            self.assertIn(key, body)
        command = re.search(r"function updateNewDraftForm\(\).*?\n}", self.app, re.DOTALL)
        self.assertIsNotNone(command)
        self.assertIn('.textContent = command.join(" ")', command.group(0))
        self.assertNotIn("--force", self.app)

    def test_generated_draft_opens_directly_in_guided_editor(self):
        self.assertIn('if (state.dirty)', self.app)
        self.assertIn('Save or discard the modified editor buffer before creating a CLI draft', self.app)
        self.assertIn('job.action === "draft"', self.app)
        self.assertIn('await selectDraft(draft, { groupId: context.groupId })', self.app)
        self.assertIn('state.draftJobContexts.set(job.id', self.app)
        self.assertIn('startJob("validate-draft"', self.app)
        self.assertIn('startJob("apply-draft"', self.app)

    def test_managed_draft_edits_prepare_with_optimistic_gate(self):
        self.assertIn('api("/api/editor/prepare"', self.app)
        self.assertIn('original_content: state.originalContent', self.app)
        self.assertIn('flow.kind === "draft" && state.dirty', self.app)
        self.assertIn('state.originalContent = buffer', self.app)
        self.assertIn('state.draftValidationJobId = null', self.app)

    def test_malformed_drafts_are_visible_but_cannot_run_or_apply(self):
        self.assertIn('draftErrors: []', self.app)
        self.assertIn('Array.isArray(payload.errors)', self.app)
        self.assertIn('function selectDraftError(error)', self.app)
        self.assertIn('error.error || "Seeknal could not resolve this draft."', self.app)
        self.assertIn('$("#validate-draft").disabled = true', self.app)
        self.assertIn('$("#apply-draft").disabled = true', self.app)
        self.assertIn('.draft-file.draft-error', self.css)

    def test_turn_cards_are_matched_by_exact_dataset_identity(self):
        self.assertIn('.find(item => item.dataset.turnId === turnId)', self.app)

    def test_agent_turns_dedupe_events_and_render_one_answer(self):
        self.assertIn("seenEvents: new Set()", self.app)
        self.assertIn("completedTurns: new Set()", self.app)
        self.assertIn("function takeNewAgentEvents", self.app)
        self.assertIn("if (state.agent.completedTurns.has(turnId)) return", self.app)
        finish = re.search(r"async function finishAgentSession\(session\).*?\n}", self.app, re.DOTALL)
        self.assertIsNotNone(finish)
        self.assertIn("renderAgentAnswer(turnId, session.answer)", finish.group(0))
        self.assertNotIn('appendAgentMessage("agent", session.answer)', finish.group(0))
        self.assertNotIn("ready for your review. Validation", finish.group(0))

    def test_tool_output_is_compact_expandable_and_text_only(self):
        events = re.search(r"function appendAgentEvents\(events.*?\n}", self.app, re.DOTALL)
        self.assertIsNotNone(events)
        body = events.group(0)
        self.assertIn('document.createElement("details")', body)
        self.assertIn("raw.textContent = text", body)
        self.assertNotIn("innerHTML", body)
        self.assertIn(".tool-details pre", self.css)
        self.assertIn("max-height:220px", self.css)

    def test_agent_panel_owns_vertical_scroll_without_feed_x_overflow(self):
        self.assertIn("overflow-x:hidden;overflow-y:auto", self.css)
        self.assertIn("overflow-wrap:anywhere", self.css)
        self.assertIn("flex:0 0 auto", self.css)
        self.assertIn("@media(max-width:420px)", self.css)
        self.assertIn("width:100vw!important", self.css)


if __name__ == "__main__":
    unittest.main()
