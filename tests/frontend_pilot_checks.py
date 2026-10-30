from pathlib import Path
import json
import re
import shutil
import subprocess
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

    def test_polling_retries_transient_failures_with_bounded_backoff(self):
        poll_body = re.search(r"async function pollAgentSession\(.*?\n}\n", self.app, re.DOTALL)
        self.assertIsNotNone(poll_body)
        body = poll_body.group(0)
        self.assertIn("failures < AGENT_POLL_RETRIES && isTransientPollError(error)", body)
        self.assertIn("failures + 1", body)
        self.assertIn("Math.min(500 * 2 ** failures, 8000)", body)
        self.assertIn("state.agent.pollTimer = setTimeout", body)
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        helper = re.search(r"const AGENT_POLL_RETRIES = \d+;\nconst AGENT_POLL_TIMEOUT_MS = \d+;\nfunction isTransientPollError\(error\) \{.*?\n}\n", self.app, re.DOTALL)
        self.assertIsNotNone(helper)
        script = helper.group(0) + """
const http = status => Object.assign(new Error("x"), { status });
console.log(JSON.stringify([
  isTransientPollError(new TypeError("Failed to fetch")),
  isTransientPollError(http(502)), isTransientPollError(http(503)), isTransientPollError(http(429)),
  isTransientPollError(http(404)), isTransientPollError(http(409)),
  isTransientPollError(new Error("Agent turn identity mismatch")),
]));
"""
        result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual([True, True, True, True, False, False, False], json.loads(result.stdout))

        # Drive the real pollAgentSession against a scripted backend.
        harness = helper.group(0) + body + """
const run = async responses => {
  const log = { delays: [], finished: null, errors: [], statuses: [], signals: [] };
  globalThis.state = { agent: { request: 1, cancelled: false, cancelPending: false, busy: true, activeTurnId: "t", eventSeq: -1 } };
  globalThis.setTimeout = (fn, delay) => { log.delays.push(delay); Promise.resolve().then(fn); return 0; };
  globalThis.api = async (_path, options) => { log.signals.push(options?.signal instanceof AbortSignal); let next = responses.shift(); if (typeof next === "function") next = next(); if (next instanceof Error) throw next; return next; };
  globalThis.requireAgentTurn = session => session;
  globalThis.takeNewAgentEvents = () => [];
  globalThis.appendAgentEvents = () => {};
  globalThis.isAgentTerminal = status => ["succeeded", "failed", "cancelled"].includes(status);
  globalThis.renderAgentTurnStatus = (_turn, text) => log.statuses.push(text);
  globalThis.finishAgentSession = async session => { log.finished = session.status; };
  globalThis.appendAgentMessage = (_who, text) => log.errors.push(text);
  globalThis.setStatus = () => {};
  globalThis.renderAgentStatus = () => {};
  await pollAgentSession(1, "s", "t");
  for (let i = 0; i < 50; i++) await new Promise(resolve => setImmediate(resolve));
  return { ...log, busy: state.agent.busy };
};
const http = status => Object.assign(new Error(`HTTP ${status}`), { status });
(async () => {
  const flaky = await run([new TypeError("Failed to fetch"), http(502), { status: "running" }, http(503), { status: "succeeded" }]);
  const down = await run(Array.from({ length: 10 }, () => http(503)));
  const gone = await run([http(404)]);
  const timedOut = await run([Object.assign(new Error("timed out"), { name: "TimeoutError" }), { status: "succeeded" }]);
  const cancelling = await run([() => { state.agent.cancelPending = true; return http(502); }]);
  console.log(JSON.stringify({ flaky, down, gone, timedOut, cancelling }));
})();
"""
        result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30, check=True)
        outcome = json.loads(result.stdout)
        # Failures back off 500, 1000; a good poll resets the count; then success.
        self.assertEqual([500, 1000, 500, 500], outcome["flaky"]["delays"])
        self.assertEqual("succeeded", outcome["flaky"]["finished"])
        self.assertEqual([], outcome["flaky"]["errors"])
        self.assertIn("Connection lost · retrying…", outcome["flaky"]["statuses"])
        # A backend that stays down is given up on after six bounded retries.
        self.assertEqual([500, 1000, 2000, 4000, 8000, 8000], outcome["down"]["delays"])
        self.assertEqual(["HTTP 503"], outcome["down"]["errors"])
        self.assertFalse(outcome["down"]["busy"])
        # A non-transient error fails immediately.
        self.assertEqual([], outcome["gone"]["delays"])
        self.assertEqual(["HTTP 404"], outcome["gone"]["errors"])
        # Giving up leaves a final status instead of a stale "retrying…".
        self.assertEqual("Connection lost", outcome["down"]["statuses"][-1])
        self.assertEqual("Connection lost", outcome["gone"]["statuses"][-1])
        # Every poll carries a timeout, and hitting it counts as transient.
        self.assertTrue(all(outcome["flaky"]["signals"]))
        self.assertEqual([500], outcome["timedOut"]["delays"])
        self.assertEqual("succeeded", outcome["timedOut"]["finished"])
        # A failure that lands while a cancel is pending leaves the turn to the cancel.
        self.assertEqual([], outcome["cancelling"]["delays"])
        self.assertEqual([], outcome["cancelling"]["errors"])
        self.assertTrue(outcome["cancelling"]["busy"])

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
