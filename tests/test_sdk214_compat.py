from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_server(project: Path):
    previous = os.environ.get("SEEKNAL_STUDIO_PROJECT")
    os.environ["SEEKNAL_STUDIO_PROJECT"] = str(project)
    try:
        spec = importlib.util.spec_from_file_location("studio_sdk214_server", ROOT / "server.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = previous


def wait_job(store, job_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job timed out: {store.get(job_id)}")


def wait_repl(service, value: dict, timeout: float = 60) -> dict:
    query_id = value["query_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = service.get(query_id)
        if result and result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.05)
    raise AssertionError(f"REPL query timed out: {service.get(query_id)}")


class SDK214CompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from runtime_resolver import resolve_runtime

        cls.runtime = resolve_runtime()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = Path(cls.temporary.name) / "project"
        shutil.copytree(ROOT / "demo", cls.project)
        cls.server = load_server(cls.project)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.AGENT.close()
        cls.server.REPL.close()
        cls.server.PROJECTS.close()
        cls.temporary.cleanup()

    def test_runtime_identity_is_managed_214_and_ignores_dirty_pythonpath(self) -> None:
        from runtime_resolver import resolve_runtime, runtime_identity

        dirty = ROOT.parent.parent / "signal" / "src"
        resolved = resolve_runtime(environ={"PYTHONPATH": str(dirty)})
        identity = runtime_identity(resolved.python)
        self.assertEqual("2.14.0", resolved.dist_version)
        self.assertEqual("2.14.0", identity.dist_version)
        self.assertEqual(resolved.module_file, identity.module_file)
        self.assertIn("seeknal-studio/.runtime/venv", str(resolved.module_file))
        polluted = {**os.environ, "PYTHONPATH": str(dirty), "PYTHONHOME": "/tmp/invalid"}
        with mock.patch.dict(os.environ, polluted, clear=True):
            child = self.server.clean_environment()
        self.assertNotIn("PYTHONPATH", child)
        self.assertNotIn("PYTHONHOME", child)

    def test_real_draft_edit_dry_run_hash_apply_run_and_preview(self) -> None:
        store = self.server.JobStore()
        yaml_request = self.server.validate_draft_request({
            "node_type": "transform",
            "name": "sdk214_compat",
            "description": "SDK 2.14 compatibility",
            "python": False,
            "deps": "",
        })
        yaml_job = wait_job(store, store.create("draft", draft_request=yaml_request)["id"])
        self.assertEqual("succeeded", yaml_job["status"], yaml_job["logs"])
        draft_path = self.project / yaml_job["draft_path"]
        draft_path.write_text(
            "kind: transform\n"
            "name: sdk214_compat\n"
            "description: SDK 2.14 compatibility\n"
            "transform: |\n"
            "  SELECT customer_id, lifetime_value FROM input_0\n"
            "inputs:\n"
            "  - ref: transform.customer_ltv\n"
        )
        content_hash = hashlib.sha256(draft_path.read_bytes()).hexdigest()
        validation = wait_job(store, store.create(
            "validate-draft", path=yaml_job["draft_path"], content_hash=content_hash,
        )["id"])
        self.assertEqual("succeeded", validation["status"], validation["logs"])
        self.assertIn("customer_id", validation["logs"])
        applied = wait_job(store, store.create(
            "apply-draft", path=yaml_job["draft_path"], content_hash=content_hash,
            destination_hash=None, confirmed=True,
        )["id"])
        self.assertEqual("succeeded", applied["status"], applied["logs"])
        self.assertFalse(draft_path.exists())
        self.assertTrue((self.project / "seeknal/transforms/sdk214_compat.yml").is_file())
        run = wait_job(store, store.create("run")["id"], timeout=120)
        self.assertEqual("succeeded", run["status"], run["logs"])
        preview = self.server.bridge("preview", "transform.sdk214_compat")
        self.assertEqual(3, preview["row_count"])

        python_request = self.server.validate_draft_request({
            "node_type": "transform", "name": "sdk214_python", "description": None,
            "python": True, "deps": "pandas",
        })
        python_job = wait_job(store, store.create("draft", draft_request=python_request)["id"])
        self.assertEqual("succeeded", python_job["status"], python_job["logs"])
        python_draft = self.project / python_job["draft_path"]
        self.assertIn("# /// script", python_draft.read_text())

    def test_real_persistent_repl_tables_schema_and_query(self) -> None:
        from repl_service import ReplService

        service = ReplService(
            self.project,
            self.runtime.python,
            ROOT / "repl_worker.py",
            lambda: {"HOME": os.environ.get("HOME", "")},
            threading.Lock(),
            sdk_version=self.runtime.dist_version,
            sdk_module_file=self.runtime.module_file,
        )
        try:
            service.start()
            deadline = time.monotonic() + 60
            status = service.status()
            while time.monotonic() < deadline:
                status = service.status()
                if status["state"] not in {"starting", "stopped"}:
                    break
                time.sleep(0.05)
            self.assertEqual("ready", status["state"], status)
            self.assertEqual("2.14.0", status["sdk_version"])
            self.assertEqual(str(self.runtime.module_file), status["sdk_module_file"])
            tables = wait_repl(service, service.execute(".tables"))
            schema = wait_repl(service, service.execute(".schema sdk214_compat"))
            query = wait_repl(service, service.execute(
                "SELECT count(*) AS rows FROM sdk214_compat"
            ))
            self.assertEqual("succeeded", tables["status"], tables)
            self.assertIn("sdk214_compat", {row[0] for row in tables["rows"]})
            self.assertEqual("succeeded", schema["status"], schema)
            self.assertIn("customer_id", {row[0] for row in schema["rows"]})
            self.assertEqual([[3]], query["rows"])
            self.assertEqual(tables["generation"], query["generation"])
        finally:
            service.close()

    def test_actual_agent_schema_terminal_event_and_runtime_cancel(self) -> None:
        from agent_runtime import AgentRuntime
        from agent_worker import _safe_toolset, verify_effective_tools
        from pydantic_ai import AgentRunResultEvent
        from pydantic_ai.models.test import TestModel
        from seeknal.ask.agents.agent import create_agent

        config = {
            "provider": "openai", "model": "deepseek-v4.1-flash:netra",
            "base_url": "https://ai.sumopod.com/v1", "sdk_root": str(self.runtime.sdk_root),
        }
        tools = verify_effective_tools(self.project, config, "test-only-key")
        self.assertEqual(sorted(tools), sorted({
            "read_pipeline", "search_pipelines", "search_project_files", "draft_node", "write_draft", "ask_user",
        }))

        async def collect_events() -> list[object]:
            agent, deps, _history, _cost = create_agent(
                self.project, provider="openai", model="deepseek-v4.1-flash:netra",
                api_key="test-only-key", include_web=False, environment="gateway",
            )
            model = TestModel(call_tools=[], custom_output_text="sdk 2.14 event verified")
            events = []
            with agent.override(model=model, toolsets=[_safe_toolset()]):
                async with agent.run_stream_events(
                    "Acknowledge readiness.", deps=deps, capabilities=[]
                ) as stream:
                    async for event in stream:
                        events.append(event)
            return events

        events = asyncio.run(collect_events())
        self.assertTrue(any(isinstance(event, AgentRunResultEvent) for event in events))

        runtime = AgentRuntime(
            self.project, self.runtime.python, ROOT / "agent_worker.py",
            sdk_root=self.runtime.sdk_root, sdk_version=self.runtime.dist_version,
            sdk_module_file=self.runtime.module_file,
        )
        runtime._turns[("session", "turn")] = {
            "session_id": "session", "turn_id": "turn", "status": "running",
            "events": [], "answer": None, "error": None,
        }
        runtime._latest_turn["session"] = "turn"
        cancelled = runtime.cancel("session", "turn")
        assert cancelled is not None
        self.assertEqual("cancelled", cancelled["status"])

    def test_project_change_requires_new_worker_handshake_and_server_rebind(self) -> None:
        from agent_runtime import AgentRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            shutil.copytree(ROOT / "demo", project)
            key = root / "key"
            key.write_text("test-key")
            key.chmod(0o600)
            worker = root / "identity_worker.py"
            worker.write_text(
                "import hashlib,hmac,json,sys\n"
                "from pathlib import Path\n"
                "def fingerprint(project):\n"
                " d=hashlib.sha256(); paths=[project/'seeknal_project.yml']; "
                "paths.extend((project/'seeknal').glob('**/*')); "
                "paths.extend((project/'data').glob('**/*'))\n"
                " for p in sorted(p for p in paths if p.is_file() and not p.is_symlink()):\n"
                "  d.update(p.relative_to(project).as_posix().encode()); d.update(p.read_bytes())\n"
                " return d.hexdigest()\n"
                "init=json.loads(sys.stdin.readline()); project=Path(init['project_path']).resolve(); "
                "fp=fingerprint(project); nonce=init['nonce']; token=init['token']; config=init['config']\n"
                "proof=hmac.new(token.encode(),f'{nonce}\\n{project}\\n{fp}'.encode(),hashlib.sha256).hexdigest()\n"
                "print(json.dumps({'type':'hello','nonce':nonce,'project_path':str(project),"
                "'project_fingerprint':fp,'proof':proof,'provider':config['provider'],"
                "'model':config['model'],'model_verified':True,'tool_calling_verified':True,"
                "'capabilities':{'draft_only':True,'tools':['read_pipeline','search_pipelines',"
                "'search_project_files','draft_node','write_draft','ask_user']}}),flush=True)\n"
                "for line in sys.stdin:\n"
                " request=json.loads(line); print(json.dumps({'type':'response',"
                "'request_id':request.get('request_id'),'ok':True,'result':{'pong':True}}),flush=True)\n"
            )
            runtime = AgentRuntime(
                project, self.runtime.python, worker, signal_root=root,
                config_path=root / "agent.json",
            )
            runtime._config["api_key_file"] = str(key)
            try:
                first = runtime.status()
                self.assertTrue(first["ready"], first)
                first_generation = runtime._generation
                first_nonce = first["identity_nonce"]

                project_file = project / "seeknal_project.yml"
                project_file.write_text(project_file.read_text() + "\n# changed once\n")
                stale = runtime.peek_status()
                self.assertFalse(stale["project_verified"])
                self.assertFalse(stale["ready"])
                refreshed = runtime.status()
                self.assertTrue(refreshed["ready"], refreshed)
                self.assertGreater(runtime._generation, first_generation)
                self.assertNotEqual(first_nonce, refreshed["identity_nonce"])

                service = self.server.AgentService()
                service.runtime = runtime
                service._cache_status({**refreshed, "state": "ready"})
                second_generation = runtime._generation
                second_nonce = refreshed["identity_nonce"]
                project_file.write_text(project_file.read_text() + "# changed twice\n")
                rebinding = service.status()
                self.assertEqual("configuring", rebinding["state"])
                self.assertFalse(rebinding["ready"])
                deadline = time.monotonic() + 10
                rebound = rebinding
                while time.monotonic() < deadline:
                    rebound = service.status()
                    if rebound.get("ready"):
                        break
                    time.sleep(0.02)
                self.assertTrue(rebound["ready"], rebound)
                self.assertTrue(rebound["project_verified"], rebound)
                self.assertGreater(runtime._generation, second_generation)
                self.assertNotEqual(second_nonce, rebound["identity_nonce"])
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
