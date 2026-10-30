from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import queue
import stat
import sys
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from agent_runtime import AgentRuntime, DEFAULT_CONFIG
from agent_worker import SAFE_TOOL_NAMES, _safe_drafts_root, write_draft
from runtime_resolver import resolve_runtime

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = resolve_runtime()


def _ensure_runtime_imports() -> None:
    sdk_root = str(RUNTIME.sdk_root)
    if sdk_root not in sys.path:
        sys.path.insert(0, sdk_root)


FAKE_WORKER = r'''
import hashlib,hmac,json,os,pathlib,signal,subprocess,sys,time
init=json.loads(sys.stdin.readline())
config=init['config']
if config.get('hello_delay'): time.sleep(config['hello_delay'])
project=init['project_path']; nonce=init['nonce']; token=init['token']
fingerprint='__FINGERPRINT__'
if config.get('identity_mismatch'):
 project=project+'/wrong'
proof=hmac.new(token.encode(),f"{nonce}\n{project}\n{fingerprint}".encode(),hashlib.sha256).hexdigest()
if config.get('fail_init'):
 print(json.dumps({'type':'fatal','error':'provider readiness failed'}),flush=True); raise SystemExit(1)
print(json.dumps({'type':'hello','nonce':nonce,'project_path':project,'project_fingerprint':fingerprint,'proof':proof,'provider':'openai','model':'deepseek-v4.1-flash:netra','model_verified':True,'tool_calling_verified':True,'capabilities':{'draft_only':True,'tools':['read_pipeline','search_pipelines','search_project_files','draft_node','write_draft','ask_user']}}),flush=True)
for line in sys.stdin:
 request=json.loads(line)
 if request['operation']=='ask':
  if request['question']=='block':
   if config.get('descendant_pid_file'):
    child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
    pathlib.Path(config['descendant_pid_file']).write_text(str(child.pid))
   time.sleep(60)
  if request['question']=='confirm':
   print(json.dumps({'type':'event','request_id':request['request_id'],'event':{'type':'ask_user','data':{'question_id':'a'*32,'question':'Which node type?','options':[{'label':'transform','description':'SQL','recommended':True},{'label':'source','description':'','recommended':False}],'multi_select':False}}}),flush=True)
   answer=json.loads(sys.stdin.readline())
   print(json.dumps({'type':'response','request_id':request['request_id'],'ok':True,'result':{'answer':'chose '+answer['answer']+' for '+answer['question_id'][:4]}}),flush=True)
   continue
  if request['question']=='expire':
   print(json.dumps({'type':'event','request_id':request['request_id'],'event':{'type':'ask_user','data':{'question_id':'c'*32,'question':'Still there?','options':[{'label':'yes','description':'','recommended':False}],'multi_select':False}}}),flush=True)
   time.sleep(0.5)
   print(json.dumps({'type':'event','request_id':request['request_id'],'event':{'type':'ask_user_timeout','data':{'question_id':'c'*32}}}),flush=True)
   time.sleep(0.5)
   print(json.dumps({'type':'response','request_id':request['request_id'],'ok':True,'result':{'answer':'no answer'}}),flush=True)
   continue
  print(json.dumps({'type':'event','request_id':request['request_id'],'event':{'type':'tool_call','data':{'name':'draft_node'}}}),flush=True)
  print(json.dumps({'type':'response','request_id':request['request_id'],'ok':True,'result':{'answer':'draft ready','events':[{'seq':1,'type':'tool_call','data':{'name':'draft_node'}}]}}),flush=True)
'''


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        (self.project / "seeknal").mkdir(parents=True)
        (self.project / "data").mkdir()
        (self.project / "seeknal_project.yml").write_text("name: pilot\n")
        self.key = self.root / "key"
        self.key.write_text("not-a-real-secret")
        os.chmod(self.key, stat.S_IRUSR | stat.S_IWUSR)
        self.config = self.root / "agent.json"
        from agent_runtime import _fingerprint
        worker_text = FAKE_WORKER.replace("__FINGERPRINT__", _fingerprint(self.project))
        self.worker = self.root / "worker.py"
        self.worker.write_text(worker_text)
        self.runtime = AgentRuntime(
            self.project, Path(sys.executable), self.worker,
            signal_root=self.root, config_path=self.config,
        )
        self.runtime._config.update({**DEFAULT_CONFIG, "api_key_file": str(self.key), "signal_root": str(self.root)})

    def _reload_runtime(self) -> None:
        self.runtime.close()
        self.runtime = AgentRuntime(
            self.project, Path(sys.executable), self.worker,
            signal_root=self.root, config_path=self.config,
        )

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_identity_and_async_turn(self):
        initial = self.runtime.peek_status()
        self.assertTrue(initial["configured"])
        self.assertFalse(initial["ready"])
        status = self.runtime.status()
        self.assertTrue(status["ready"])
        self.assertTrue(status["project_verified"])
        self.assertTrue(status["model_verified"])
        self.assertTrue(status["tool_calling_verified"])
        turn = self.runtime.ask("make a draft", "pilot-1", timeout=5)
        self.assertEqual("succeeded", turn["status"])
        self.assertEqual("draft ready", turn["answer"])
        self.assertEqual(1, len([event for event in turn["events"] if event["type"] == "tool_call"]))
        second = self.runtime.ask("another draft", "pilot-1", timeout=5)
        self.assertEqual(1, len([event for event in second["events"] if event["type"] == "tool_call"]))

    def test_turn_id_retry_is_idempotent_and_exactly_recoverable(self):
        first = self.runtime.start("make a draft", "stable-session", turn_id="turn-retry-1")
        retry = self.runtime.start("ignored duplicate body", "stable-session", turn_id="turn-retry-1")
        self.assertEqual(first["turn_id"], retry["turn_id"])
        deadline = time.monotonic() + 3
        recovered = None
        while time.monotonic() < deadline:
            recovered = self.runtime.get("stable-session", "turn-retry-1")
            if recovered and recovered["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual("succeeded", recovered["status"])
        terminal_retry = self.runtime.start(
            "still ignored", "stable-session", turn_id="turn-retry-1"
        )
        self.assertEqual(recovered, terminal_retry)
        self.assertEqual(1, len([
            event for event in recovered["events"] if event["type"] == "tool_call"
        ]))

    def test_failed_second_turn_never_replays_previous_answer(self):
        first = self.runtime.ask(
            "first draft", "stable-session", turn_id="turn-success", timeout=5
        )
        self.assertEqual("succeeded", first["status"])
        self.key.unlink()
        with self.assertRaisesRegex(RuntimeError, "key file is missing"):
            self.runtime.start("second draft", "stable-session", turn_id="turn-failed")
        failed = self.runtime.get("stable-session", "turn-failed")
        self.assertEqual("failed", failed["status"])
        self.assertIsNone(failed["answer"])
        self.assertNotEqual(first["answer"], failed["answer"])
        self.assertEqual("turn-failed", self.runtime.get("stable-session")["turn_id"])

    def test_turn_id_validation(self):
        with self.assertRaisesRegex(ValueError, "invalid turn_id"):
            self.runtime.start("draft", "stable-session", turn_id="bad/turn")

    def test_identity_mismatch_fails_closed(self):
        self.runtime._config["identity_mismatch"] = True
        status = self.runtime.status()
        self.assertFalse(status["ready"])
        self.assertIn("identity verification failed", status["error"])

    def test_failed_provider_readiness_fails_closed(self):
        self.runtime._config["fail_init"] = True
        status = self.runtime.status()
        self.assertFalse(status["ready"])
        self.assertIn("provider readiness failed", status["error"])

    def test_cancel_reaps_worker(self):
        self.runtime.start("block", "pilot-cancel")
        deadline = time.monotonic() + 2
        while self.runtime._process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        process = self.runtime._process
        turn = self.runtime.cancel("pilot-cancel")
        self.assertEqual("cancelled", turn["status"])
        self.assertIsNotNone(process.poll())

    def test_cancel_reaps_descendant_process_group(self):
        pid_file = self.root / "descendant.pid"
        self.runtime._config["descendant_pid_file"] = str(pid_file)
        self.runtime.start("block", "pilot-descendant")
        deadline = time.monotonic() + 3
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(pid_file.is_file())
        descendant = int(pid_file.read_text())
        self.runtime.cancel("pilot-descendant")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["ps", "-p", str(descendant), "-o", "stat="], capture_output=True, text=True
            )
            if result.returncode != 0 or not result.stdout.strip() or "Z" in result.stdout:
                break
            time.sleep(0.05)
        else:
            self.fail("agent descendant survived cancellation")

    def test_cancelled_queued_turn_never_runs_after_lock_release(self):
        gate = threading.Lock()
        gate.acquire()
        self.runtime.execution_lock = gate
        with mock.patch("threading.excepthook") as thread_error:
            self.runtime.start("block", "pilot-queued")
            turn = self.runtime.cancel("pilot-queued")
            gate.release()
            deadline = time.monotonic() + 2
            while self.runtime.get("pilot-queued")["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            thread_error.assert_not_called()
        self.assertEqual("cancelled", turn["status"])
        self.assertEqual("cancelled", self.runtime.get("pilot-queued")["status"])

    def test_old_reader_eof_cannot_poison_new_worker_generation(self):
        release_old_reader = threading.Event()

        class DelayedOldStdout:
            def __iter__(self):
                release_old_reader.wait(timeout=2)
                yield json.dumps({
                    "type": "event",
                    "request_id": "new-request",
                    "event": {"type": "tool_call", "data": {"name": "draft_node"}},
                }) + "\n"

        old_process = SimpleNamespace(stdout=DelayedOldStdout())
        old_waiter: queue.Queue = queue.Queue()
        new_waiter: queue.Queue = queue.Queue()
        with self.runtime._lock:
            self.runtime._pending["old-request"] = (1, old_waiter)
            self.runtime._pending["new-request"] = (2, new_waiter)
            self.runtime._request_sessions["new-request"] = (2, "new-session", "new-turn")
            self.runtime._turns[("new-session", "new-turn")] = {
                "session_id": "new-session", "turn_id": "new-turn", "status": "running", "events": [],
                "answer": None, "error": None,
            }
            self.runtime._latest_turn["new-session"] = "new-turn"
        reader = threading.Thread(
            target=self.runtime._read_stdout,
            args=(old_process, 1, "__hello__:1"),
        )
        reader.start()
        release_old_reader.set()
        reader.join(timeout=2)
        self.assertFalse(reader.is_alive())
        self.assertEqual("agent worker exited", old_waiter.get_nowait()["error"])
        self.assertTrue(new_waiter.empty())
        self.assertEqual([], self.runtime._turns[("new-session", "new-turn")]["events"])

    def test_event_released_after_cancel_barrier_is_not_appended(self):
        release_event = threading.Event()

        class DelayedEventStdout:
            def __iter__(self):
                release_event.wait(timeout=2)
                yield json.dumps({
                    "type": "event", "request_id": "request-1",
                    "event": {"type": "tool_call", "data": {"name": "draft_node"}},
                }) + "\n"

        process = SimpleNamespace(stdout=DelayedEventStdout())
        waiter: queue.Queue = queue.Queue()
        with self.runtime._lock:
            self.runtime._pending["request-1"] = (1, waiter)
            self.runtime._request_sessions["request-1"] = (1, "session-1", "turn-1")
            self.runtime._turns[("session-1", "turn-1")] = {
                "session_id": "session-1", "turn_id": "turn-1", "status": "running", "events": [],
                "answer": None, "error": None,
            }
            self.runtime._latest_turn["session-1"] = "turn-1"
        reader = threading.Thread(
            target=self.runtime._read_stdout,
            args=(process, 1, "__hello__:1"),
        )
        reader.start()
        with self.runtime._lock:
            self.runtime._turns[("session-1", "turn-1")]["status"] = "cancelled"
        release_event.set()
        reader.join(timeout=2)
        self.assertEqual([], self.runtime._turns[("session-1", "turn-1")]["events"])

    def test_concurrent_first_status_shares_one_verified_worker(self):
        self.runtime._config["hello_delay"] = 0.2
        barrier = threading.Barrier(3)
        results = []

        def check_status():
            barrier.wait()
            results.append(self.runtime.status())

        threads = [threading.Thread(target=check_status) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(2, len(results))
        self.assertTrue(all(result["ready"] for result in results))
        self.assertEqual(1, self.runtime._generation)
        self.assertEqual(results[0]["identity_nonce"], results[1]["identity_nonce"])

    def test_config_requires_exact_provider_model_and_private_key(self):
        with self.assertRaisesRegex(ValueError, "model must"):
            self.runtime.configure({"model": "fallback-model"})
        os.chmod(self.key, 0o644)
        with self.assertRaisesRegex(ValueError, "owner-only"):
            self.runtime.configure({"api_key_file": str(self.key)})

    def test_config_rejects_symlink_without_touching_destination(self):
        outside = self.root / "outside-config"
        outside.write_text("keep")
        self.config.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "configuration path is unsafe"):
            self.runtime.configure({})
        self.assertEqual("keep", outside.read_text())

    def test_corrupt_saved_config_fails_closed_then_explicit_configure_recovers(self):
        self.config.write_text("{not-json")
        os.chmod(self.config, 0o600)
        self._reload_runtime()
        status = self.runtime.status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["ready"])
        self.assertIn("Expecting property name", status["error"])
        recovered = self.runtime.configure({"api_key_file": str(self.key)})
        self.assertTrue(recovered["configured"])
        self.assertTrue(recovered["ready"])

    def test_unsafe_saved_config_mode_fails_closed(self):
        self.config.write_text(json.dumps({**DEFAULT_CONFIG, "api_key_file": str(self.key)}))
        os.chmod(self.config, 0o644)
        self._reload_runtime()
        status = self.runtime.status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["ready"])
        self.assertIn("permissions must be owner-only", status["error"])

    def test_unknown_saved_config_schema_fails_closed(self):
        self.config.write_text(json.dumps({
            **DEFAULT_CONFIG, "api_key_file": str(self.key), "unexpected": "unsafe",
        }))
        os.chmod(self.config, 0o600)
        self._reload_runtime()
        status = self.runtime.status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["ready"])
        self.assertIn("schema is invalid", status["error"])

    def test_missing_key_is_not_configured_or_ready(self):
        self.runtime._config["api_key_file"] = str(self.root / "missing")
        status = self.runtime.status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["ready"])

    def test_key_removed_after_readiness_stops_worker(self):
        self.assertTrue(self.runtime.status()["ready"])
        process = self.runtime._process
        self.key.unlink()
        status = self.runtime.status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["ready"])
        self.assertIsNotNone(process.poll())

    def test_peek_status_immediately_downgrades_project_change_and_dead_worker(self):
        self.assertTrue(self.runtime.status()["ready"])
        (self.project / "seeknal/new.yml").write_text("kind: transform\nname: new\n")
        changed = self.runtime.peek_status()
        self.assertFalse(changed["ready"])
        self.assertFalse(changed["project_verified"])
        self.runtime._process.terminate()
        self.runtime._process.wait(timeout=2)
        dead = self.runtime.peek_status()
        self.assertFalse(dead["ready"])
        self.assertFalse(dead["healthy"])

    def test_ask_user_question_waits_for_answer_then_resumes(self):
        started = self.runtime.start("confirm", "pilot-ask", "turn-ask")
        self.assertEqual("running", started["status"])
        deadline = time.monotonic() + 5
        turn = started
        while not turn.get("pending_question") and time.monotonic() < deadline:
            time.sleep(0.05)
            turn = self.runtime.get("pilot-ask", "turn-ask")
        pending = turn["pending_question"]
        self.assertEqual("Which node type?", pending["question"])
        self.assertEqual(["transform", "source"], [option["label"] for option in pending["options"]])
        with self.assertRaisesRegex(ValueError, "no longer waiting"):
            self.runtime.answer("pilot-ask", "turn-ask", "b" * 32, "source")
        with self.assertRaisesRegex(ValueError, "1-2000"):
            self.runtime.answer("pilot-ask", "turn-ask", "a" * 32, "   ")
        answered = self.runtime.answer("pilot-ask", "turn-ask", "a" * 32, "transform")
        self.assertIsNone(answered["pending_question"])
        self.assertEqual("user_answer", answered["events"][-1]["type"])
        with self.assertRaisesRegex(ValueError, "no longer waiting"):
            self.runtime.answer("pilot-ask", "turn-ask", "a" * 32, "source")
        while turn["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            turn = self.runtime.get("pilot-ask", "turn-ask")
        self.assertEqual("succeeded", turn["status"])
        self.assertEqual("chose transform for aaaa", turn["answer"])

    def test_waiting_for_an_answer_does_not_use_the_turn_time_limit(self):
        with mock.patch("agent_runtime.TURN_ACTIVE_SECONDS", 1.5):
            self.runtime.start("confirm", "pilot-wait", "turn-wait")
            time.sleep(3)
            turn = self.runtime.get("pilot-wait", "turn-wait")
            self.assertEqual("running", turn["status"])
            self.runtime.answer("pilot-wait", "turn-wait", "a" * 32, "source")
            deadline = time.monotonic() + 5
            while turn["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.05)
                turn = self.runtime.get("pilot-wait", "turn-wait")
        self.assertEqual("succeeded", turn["status"])

    def _wait_for(self, session_id, turn_id, predicate):
        deadline = time.monotonic() + 5
        turn = self.runtime.get(session_id, turn_id)
        while not predicate(turn) and time.monotonic() < deadline:
            time.sleep(0.05)
            turn = self.runtime.get(session_id, turn_id)
        return turn

    def test_ask_user_timeout_event_clears_pending_question(self):
        self.runtime.start("expire", "pilot-expire", "turn-expire")
        turn = self._wait_for("pilot-expire", "turn-expire", lambda t: t.get("pending_question"))
        self.assertEqual("c" * 32, turn["pending_question"]["question_id"])
        turn = self._wait_for("pilot-expire", "turn-expire", lambda t: not t.get("pending_question"))
        self.assertIn("ask_user_timeout", [event["type"] for event in turn["events"]])
        turn = self._wait_for("pilot-expire", "turn-expire", lambda t: t["status"] != "running")
        self.assertEqual("succeeded", turn["status"])

    def test_cancel_while_waiting_clears_question_and_rejects_late_answer(self):
        self.runtime.start("confirm", "pilot-cancel", "turn-cancel")
        self._wait_for("pilot-cancel", "turn-cancel", lambda t: t.get("pending_question"))
        cancelled = self.runtime.cancel("pilot-cancel", "turn-cancel")
        self.assertEqual("cancelled", cancelled["status"])
        self.assertIsNone(cancelled["pending_question"])
        with self.assertRaisesRegex(ValueError, "no longer waiting"):
            self.runtime.answer("pilot-cancel", "turn-cancel", "a" * 32, "transform")

    def test_active_time_limit_fails_turn_and_stops_worker(self):
        self.runtime.status()
        process = self.runtime._process
        with mock.patch("agent_runtime.TURN_ACTIVE_SECONDS", 1):
            self.runtime.start("block", "pilot-slow", "turn-slow")
            turn = self._wait_for("pilot-slow", "turn-slow", lambda t: t["status"] != "running")
        self.assertEqual("failed", turn["status"])
        self.assertIn("timed out", turn["error"])
        self.assertIsNone(self.runtime._process)
        self.assertIsNotNone(process.poll())

    def test_worker_routes_stdin_answers_and_releases_waiters_on_eof(self):
        import agent_worker
        self.addCleanup(agent_worker._INPUT_CLOSED.clear)
        read_fd, write_fd = os.pipe()
        reader, writer = os.fdopen(read_fd, "r"), os.fdopen(write_fd, "w")
        self.addCleanup(reader.close)
        emitted, requests = [], queue.Queue()

        async def scenario():
            with mock.patch.object(agent_worker, "_emit", emitted.append), mock.patch("sys.stdin", reader):
                threading.Thread(target=agent_worker._read_requests, args=(requests,), daemon=True).start()
                first = asyncio.create_task(agent_worker.ask_user("One?", [{"label": "a"}]))
                while not emitted:
                    await asyncio.sleep(0.01)
                question_id = emitted[-1]["event"]["data"]["question_id"]
                writer.write(json.dumps({"operation": "ping", "request_id": "r1"}) + "\n")
                writer.write(json.dumps({"operation": "answer", "question_id": question_id, "answer": "a"}) + "\n")
                writer.flush()
                answer = await first
                second = asyncio.create_task(agent_worker.ask_user("Two?", [{"label": "b"}]))
                while len(emitted) < 2:
                    await asyncio.sleep(0.01)
                writer.close()
                with self.assertRaisesRegex(RuntimeError, "closed the session"):
                    await asyncio.wait_for(second, 5)
                return answer

        self.assertEqual("a", asyncio.run(scenario()))
        self.assertIn('"ping"', requests.get(timeout=2))
        self.assertIsNone(requests.get(timeout=2))
        self.assertEqual({}, agent_worker._ANSWER_WAITERS)

    def test_worker_ask_user_refuses_labels_it_would_have_to_shorten(self):
        import agent_worker
        with mock.patch.object(agent_worker, "_emit") as emit:
            reply = asyncio.run(agent_worker.ask_user("?", [{"label": "x" * 121}]))
        self.assertIn("120 characters", reply)
        with mock.patch.object(agent_worker, "_emit") as emit_many:
            many = asyncio.run(agent_worker.ask_user("?", [{"label": str(index)} for index in range(7)]))
        self.assertIn("at most 6", many)
        emit.assert_not_called()
        emit_many.assert_not_called()

    def test_worker_ask_user_emits_question_and_returns_delivered_answer(self):
        import agent_worker
        emitted = []

        async def scenario():
            with mock.patch.object(agent_worker, "_emit", emitted.append):
                task = asyncio.create_task(agent_worker.ask_user("Pick one", [
                    {"label": "keep", "description": "x" * 900, "recommended": "true"},
                    {"label": "", "description": "dropped"}, "not-an-option",
                    {"label": "replace", "multi_select": "true"},
                ]))
                while not emitted:
                    await asyncio.sleep(0.01)
                data = emitted[0]["event"]["data"]
                agent_worker._deliver_answer({"question_id": "f" * 32, "answer": "ignored"})
                agent_worker._deliver_answer({"question_id": data["question_id"], "answer": "keep"})
                agent_worker._deliver_answer({"question_id": data["question_id"], "answer": "second"})
                return data, await task

        data, answer = asyncio.run(scenario())
        self.assertEqual("keep", answer)
        self.assertEqual(["keep", "replace"], [option["label"] for option in data["options"]])
        self.assertEqual(500, len(data["options"][0]["description"]))
        self.assertTrue(data["options"][0]["recommended"])
        self.assertTrue(data["multi_select"])
        self.assertEqual({}, agent_worker._ANSWER_WAITERS)
        self.assertEqual("No options provided.", asyncio.run(agent_worker.ask_user("?", [])))

    def test_tool_contract_is_draft_only(self):
        self.assertEqual(
            {"read_pipeline", "search_pipelines", "search_project_files", "draft_node", "write_draft", "ask_user"},
            set(SAFE_TOOL_NAMES),
        )
        forbidden = {"apply_draft", "run_pipeline", "execute_python", "execute_uv_script", "write_project_file"}
        self.assertFalse(forbidden.intersection(SAFE_TOOL_NAMES))

    def test_real_seeknal_agent_effective_schema_is_exact_allowlist(self):
        script = """
import asyncio
import json
from pathlib import Path
from agent_worker import verify_effective_tools
project=Path(__import__('sys').argv[1])
config={'sdk_root':__import__('sys').argv[2],'expected_seeknal_version':__import__('sys').argv[3]}
print(json.dumps(verify_effective_tools(project,config,'test-only-key')))
"""
        environment = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        result = subprocess.run(
            [str(RUNTIME.python), "-c", script, str(self.project), str(RUNTIME.sdk_root), RUNTIME.dist_version],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(0, result.returncode, result.stderr[-1000:])
        self.assertEqual(sorted(SAFE_TOOL_NAMES), json.loads(result.stdout.strip().splitlines()[-1]))

    def test_draft_directory_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        internal = self.project / ".seeknal"
        internal.mkdir()
        (internal / "drafts").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            _safe_drafts_root(self.project)

    def test_write_draft_handler_writes_only_verified_draft_path(self):
        _ensure_runtime_imports()
        from seeknal.ask.agents.tools._context import set_tool_context

        discovery = SimpleNamespace(refresh=lambda: None)
        set_tool_context(SimpleNamespace(
            project_path=self.project,
            fs_lock=threading.Lock(),
            artifact_discovery=discovery,
        ))
        drafts = _safe_drafts_root(self.project)
        trap = self.root / "temp-trap"
        trap.write_text("unchanged")
        (drafts / "draft_transform_customer_value.yml.studio-agent.tmp").symlink_to(trap)
        result = write_draft("draft_transform_customer_value.yml", "kind: transform\nname: customer_value\n")
        self.assertIn("Saved draft", result)
        self.assertTrue((self.project / ".seeknal/drafts/draft_transform_customer_value.yml").is_file())
        self.assertEqual("unchanged", trap.read_text())
        rejected = write_draft("../../outside.yml", "secret")
        self.assertIn("Invalid draft filename", rejected)
        self.assertFalse((self.root / "outside.yml").exists())

    def test_write_draft_rejects_symlink_target_without_touching_destination(self):
        _ensure_runtime_imports()
        from seeknal.ask.agents.tools._context import set_tool_context

        discovery = SimpleNamespace(refresh=lambda: None)
        set_tool_context(SimpleNamespace(
            project_path=self.project,
            fs_lock=threading.Lock(),
            artifact_discovery=discovery,
        ))
        drafts = _safe_drafts_root(self.project)
        outside = self.root / "outside.yml"
        outside.write_text("keep")
        (drafts / "draft_transform_escape.yml").symlink_to(outside)
        result = write_draft("draft_transform_escape.yml", "replace")
        self.assertIn("unsafe", result)
        self.assertEqual("keep", outside.read_text())

    def test_draft_node_ignores_project_template_override(self):
        templates = self.project / "seeknal" / "templates"
        templates.mkdir(parents=True)
        (templates / "transform.yml.j2").write_text("MALICIOUS_PROJECT_TEMPLATE")
        script = """
import asyncio,json,threading
from pathlib import Path
from types import SimpleNamespace
from agent_worker import draft_node
from seeknal.ask.agents.tools._context import set_tool_context
project=Path(__import__('sys').argv[1])
set_tool_context(SimpleNamespace(project_path=project,fs_lock=threading.Lock(),artifact_discovery=SimpleNamespace(refresh=lambda:None)))
print(json.dumps({'result':asyncio.run(draft_node('transform','safe_template'))}))
"""
        environment = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        result = subprocess.run(
            [str(RUNTIME.python), "-c", script, str(self.project)],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr[-1000:])
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        draft = self.project / ".seeknal/drafts/draft_transform_safe_template.yml"
        self.assertIn("Created draft", payload["result"])
        self.assertTrue(draft.is_file())
        self.assertNotIn("MALICIOUS_PROJECT_TEMPLATE", draft.read_text())


if __name__ == "__main__":
    unittest.main()
