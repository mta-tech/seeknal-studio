from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendPilotContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = (ROOT / "app.js").read_text()
        cls.html = (ROOT / "index.html").read_text()

    def test_exact_pilot_model_and_backend_only_key_reference(self):
        self.assertIn('model: "deepseek-v4.1-flash:netra"', self.app)
        self.assertIn('value="~/sumopod-ai-api-key"', self.html)
        self.assertNotRegex(self.app + self.html, r"(?i)(api[_-]?key\s*[:=]\s*['\"][^~])")

    def test_async_agent_turn_contract_is_used(self):
        self.assertIn('/api/agent/sessions/${encodeURIComponent(sessionId)}', self.app)
        self.assertIn('status === "succeeded"', self.app)
        self.assertIn('status === "cancelled"', self.app)
        self.assertIn('normalizeAgentEvents(session.events)', self.app)
        self.assertIn('Number.isFinite(Number(event.seq))', self.app)

    def test_readiness_requires_verified_project_and_draft_only_policy(self):
        self.assertIn("state.agent.project_verified", self.app)
        self.assertIn("state.agent.project_path === state.project.project_path", self.app)
        self.assertIn("state.agent.capabilities?.draft_only === true", self.app)
        self.assertIn("apply/run denied", self.app)

    def test_agent_cannot_silently_apply_or_run(self):
        ask_body = re.search(r"async function askAgent\(question\).*?\n}\n", self.app, re.DOTALL)
        self.assertIsNotNone(ask_body)
        self.assertNotIn("apply-draft", ask_body.group(0))
        self.assertNotIn('startJob("run")', ask_body.group(0))
        self.assertIn("Review drafts", self.html)
        self.assertIn("Apply changes", self.html)
        self.assertIn("Run pipeline", self.html)

    def test_cancellation_requires_backend_confirmation_and_recovers_polling(self):
        cancel_body = re.search(r"async function cancelAgent\(\).*?\n}\n", self.app, re.DOTALL)
        self.assertIsNotNone(cancel_body)
        body = cancel_body.group(0)
        confirmation = body.index('session?.status === "cancelled"')
        cancelled_assignment = body.index("state.agent.cancelled = true")
        self.assertGreater(cancelled_assignment, confirmation)
        self.assertIn("Cancellation was not confirmed; monitoring the active turn", body)
        self.assertIn("Cancellation failed; agent state remains active or unknown", body)
        self.assertGreaterEqual(body.count("await pollAgentSession(request, sessionId, turnId)"), 2)

    def test_immediate_terminal_responses_are_not_reported_as_success(self):
        self.assertIn('session.status === "succeeded"', self.app)
        self.assertIn('session.status === "cancelled"', self.app)
        self.assertIn('appendAgentMessage("agent", session.error || "Agent turn failed."', self.app)
        self.assertIn('setStatus("Agent request failed or acceptance is unknown", "error")', self.app)
        self.assertIn("state.agent.busy = false", self.app)
        self.assertIn("await finishAgentSession(terminalPayload)", self.app)
        self.assertIn('payload.answer && !payload.status ? { ...payload, status: "succeeded" }', self.app)

    def test_folder_names_are_rendered_as_text(self):
        render_files = re.search(r"function renderFiles\(\).*?\n}\n", self.app, re.DOTALL)
        self.assertIsNotNone(render_files)
        body = render_files.group(0)
        self.assertIn("folderLabel.textContent = folder", body)
        self.assertNotIn("${folder}", body)

    def test_async_backend_bootstrap_is_observed_without_duplicate_configuration(self):
        initialize = re.search(r"async function initialize\(\).*?\n}\n", self.app, re.DOTALL)
        self.assertIsNotNone(initialize)
        self.assertIn("startAgentReadinessWatch()", initialize.group(0))
        self.assertNotIn("await configureAgent()", initialize.group(0))
        self.assertIn('api("/api/agent/status")', self.app)
        self.assertIn("generation !== state.agent.readinessGeneration", self.app)
        self.assertIn('state.agent.state === "failed"', self.app)
        self.assertIn('"not_started", "configuring"', self.app)

    def test_each_prompt_requires_a_distinct_correlated_turn(self):
        self.assertIn("crypto.randomUUID()", self.app)
        self.assertIn("crypto.getRandomValues", self.app)
        self.assertIn("state.agent.activeTurnId = turnId", self.app)
        self.assertIn("session.turn_id !== turnId", self.app)
        self.assertIn("turn_id: turnId", self.app)
        self.assertIn("?turn_id=${encodeURIComponent(turnId)}", self.app)
        self.assertIn("const sessionId = state.agent.sessionId", self.app)
        self.assertIn("const turnId = createAgentTurnId()", self.app)
        self.assertIn("requireAgentTurn(", self.app)
        self.assertIn("state.agent.activeTurnId = null", self.app)


if __name__ == "__main__":
    unittest.main()
