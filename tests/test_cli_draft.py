from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def load_server():
    spec = importlib.util.spec_from_file_location("studio_cli_draft_server", ROOT / "server.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class CliDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = Path(cls.temporary.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project)
        cls.previous_project = os.environ.get("SEEKNAL_STUDIO_PROJECT")
        os.environ["SEEKNAL_STUDIO_PROJECT"] = str(cls.project)
        cls.server = load_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.AGENT.close()
        cls.server.REPL.close()
        cls.server.PROJECTS.close()
        if cls.previous_project is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = cls.previous_project
        cls.temporary.cleanup()

    def wait(self, store, job_id: str) -> dict:
        deadline = time.time() + 30
        while time.time() < deadline:
            job = store.get(job_id)
            if job["status"] not in {"queued", "running"}:
                return job
            time.sleep(0.05)
        self.fail("draft job did not finish")

    def fake_seeknal(self, filename: str, *, exit_code: int, sleep: bool = False) -> Path:
        executable = Path(self.temporary.name) / filename
        executable.write_text(
            f"#!{sys.executable}\n"
            "import pathlib, sys, time\n"
            "target = pathlib.Path.cwd() / f'draft_source_{sys.argv[3]}.yml'\n"
            "target.write_bytes(b'foreign bytes after preflight')\n"
            + ("time.sleep(20)\n" if sleep else "")
            + f"raise SystemExit({exit_code})\n"
        )
        executable.chmod(0o700)
        self.addCleanup(executable.unlink, missing_ok=True)
        return executable

    def draft(self, **overrides) -> tuple[dict, Path]:
        payload = {
            "node_type": "source",
            "name": "cli_source_test",
            "description": "CLI source test",
            "python": False,
            "deps": "",
            **overrides,
        }
        request = self.server.validate_draft_request(payload)
        store = self.server.JobStore()
        job = self.wait(store, store.create("draft", draft_request=request)["id"])
        path = self.project / job.get("draft_path", "missing")
        self.addCleanup(path.unlink, missing_ok=True)
        return job, path

    def test_options_come_from_installed_seeknal_metadata(self):
        metadata = self.server.draft_options()
        options = metadata["node_types"]
        by_value = {item["value"]: item for item in options}
        self.assertEqual("feature_group", by_value["feature-group"]["normalized"])
        self.assertTrue(by_value["source"]["python_supported"])
        self.assertFalse(by_value["semantic-model"]["python_supported"])
        normalized = [item["normalized"] for item in options + metadata["excluded_node_types"]]
        self.assertEqual(len(normalized), len(set(normalized)), "aliases must be listed once")
        self.assertNotIn("feature_group", by_value)
        excluded = {item["value"]: item for item in metadata["excluded_node_types"]}
        self.assertIn("common-source", excluded)
        self.assertIn("managed target", excluded["common-source"]["reason"])

    def test_underscore_spellings_resolve_to_the_listed_value(self):
        for sent, expected in (
            ("feature_group", "feature-group"),
            ("semantic_model", "semantic-model"),
            ("second_order_aggregation", "second-order-aggregation"),
            ("feature-group", "feature-group"),
        ):
            with self.subTest(sent=sent):
                request = self.server.validate_draft_request({"node_type": sent, "name": "safe"})
                self.assertEqual(expected, request["node_type"])
        for rejected in ("common_source", "common-source", "feature group", "FEATURE_GROUP"):
            with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                self.server.validate_draft_request({"node_type": rejected, "name": "safe"})

    def test_ambiguous_or_excluded_aliases_are_not_resolved(self):
        metadata = {
            "node_types": [
                {"value": "alpha-beta", "normalized": "alpha_beta", "python_supported": False},
                {"value": "alpha_beta_x", "normalized": "alpha_beta", "python_supported": False},
                {"value": "gamma", "normalized": "gamma_delta", "python_supported": False},
            ],
            "excluded_node_types": [
                {"value": "gamma-delta", "normalized": "other", "python_supported": False, "reason": "x"},
            ],
        }
        with patch.object(self.server, "draft_options", return_value=metadata):
            for listed in ("alpha-beta", "alpha_beta_x", "gamma"):
                with self.subTest(listed=listed):
                    request = self.server.validate_draft_request({"node_type": listed, "name": "safe"})
                    self.assertEqual(listed, request["node_type"])
            # alpha_beta names two kinds; gamma-delta is an excluded kind's spelling.
            for rejected in ("alpha_beta", "gamma-delta"):
                with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                    self.server.validate_draft_request({"node_type": rejected, "name": "safe"})
            request = self.server.validate_draft_request({"node_type": "gamma_delta", "name": "safe"})
            self.assertEqual("gamma", request["node_type"])

    def test_successful_options_are_cached_but_failures_retry(self):
        metadata = {
            "node_types": [{
                "value": "source", "normalized": "source", "python_supported": True,
            }],
            "excluded_node_types": [],
        }
        setattr(self.server, "DRAFT_OPTIONS_CACHE", None)
        self.addCleanup(setattr, self.server, "DRAFT_OPTIONS_CACHE", None)
        with patch.object(self.server, "bridge", return_value=metadata) as bridge:
            first = self.server.draft_options()
            first["node_types"][0]["value"] = "mutated"
            second = self.server.draft_options()
        bridge.assert_called_once_with("draft-options")
        self.assertEqual("source", second["node_types"][0]["value"])

        setattr(self.server, "DRAFT_OPTIONS_CACHE", None)
        with patch.object(
            self.server, "bridge", side_effect=[RuntimeError("temporary"), metadata]
        ) as retrying_bridge:
            with self.assertRaisesRegex(RuntimeError, "temporary"):
                self.server.draft_options()
            self.assertEqual(metadata, self.server.draft_options())
        self.assertEqual(2, retrying_bridge.call_count)

    def test_every_advertised_yaml_kind_resolves_to_managed_review_target(self):
        unique = {}
        for option in self.server.draft_options()["node_types"]:
            unique.setdefault(option["normalized"], option["value"])
        for index, (normalized, node_type) in enumerate(unique.items()):
            with self.subTest(node_type=node_type):
                name = f"resolve_{index}_{normalized}"
                job, _draft = self.draft(
                    node_type=node_type,
                    name=name,
                    description=f"Resolver coverage for {normalized}",
                )
                self.assertEqual("succeeded", job["status"], job["logs"])
                details = self.server.draft_details(job["draft_path"])
                self.assertIsNotNone(details)
                assert details is not None
                self.assertEqual(normalized, details["kind"])
                self.assertEqual(name, details["name"])

    def test_real_cli_creates_yaml_source_and_transform_managed_drafts(self):
        source_job, source = self.draft()
        transform_job, transform = self.draft(
            node_type="transform", name="cli_transform_test", description="CLI transform test"
        )
        self.assertEqual("succeeded", source_job["status"], source_job["logs"])
        self.assertEqual("succeeded", transform_job["status"], transform_job["logs"])
        self.assertEqual(".seeknal/drafts/draft_source_cli_source_test.yml", source_job["draft_path"])
        self.assertEqual(".seeknal/drafts/draft_transform_cli_transform_test.yml", transform_job["draft_path"])
        self.assertIn("kind: source", source.read_text())
        self.assertIn("kind: transform", transform.read_text())
        self.assertEqual(hashlib.sha256(transform.read_bytes()).hexdigest(), transform_job["content_hash"])
        self.assertFalse((self.project / "draft_source_cli_source_test.yml").exists())
        self.assertFalse((self.project / "seeknal/sources/cli_source_test.yml").exists())

    def test_real_cli_creates_supported_python_draft_with_deps(self):
        job, draft = self.draft(
            node_type="transform",
            name="cli_python_test",
            description="Python scaffold",
            python=True,
            deps="pandas,requests",
        )
        self.assertEqual("succeeded", job["status"], job["logs"])
        self.assertTrue(job["draft_path"].endswith("draft_transform_cli_python_test.py"))
        content = draft.read_text()
        self.assertIn("# /// script", content)
        self.assertIn('"pandas"', content)
        self.assertIn('"requests"', content)

    def test_project_template_is_honored_by_user_invoked_cli(self):
        templates = self.project / "seeknal" / "templates"
        templates.mkdir(exist_ok=True)
        template = templates / "source.yml.j2"
        template.write_text(
            "# PROJECT_TEMPLATE_MARKER\nkind: source\nname: {{ name }}\n"
            "description: {{ description }}\nsource:\n  type: file\n  path: data/example.csv\n"
        )
        self.addCleanup(template.unlink, missing_ok=True)
        job, draft = self.draft(name="custom_template_test")
        self.assertEqual("succeeded", job["status"], job["logs"])
        self.assertIn("PROJECT_TEMPLATE_MARKER", draft.read_text())

    def test_root_and_managed_collisions_fail_without_launch_or_overwrite(self):
        cases = [
            self.project / "draft_source_collision_root.yml",
            self.project / ".seeknal" / "drafts" / "draft_source_collision_managed.yml",
        ]
        for existing in cases:
            with self.subTest(existing=existing.name):
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_text("preserve me")
                self.addCleanup(existing.unlink, missing_ok=True)
                name = "collision_root" if "root" in existing.name else "collision_managed"
                request = self.server.validate_draft_request({
                    "node_type": "source", "name": name, "python": False, "deps": ""
                })
                store = self.server.JobStore()
                with patch.object(self.server.subprocess, "Popen") as popen:
                    job = self.wait(store, store.create("draft", draft_request=request)["id"])
                self.assertEqual("failed", job["status"])
                popen.assert_not_called()
                self.assertEqual("preserve me", existing.read_text())

    def test_failure_preserves_root_file_created_after_preflight(self):
        name = "foreign_failure"
        root = self.project / f"draft_source_{name}.yml"
        self.addCleanup(root.unlink, missing_ok=True)
        request = self.server.validate_draft_request({
            "node_type": "source", "name": name, "python": False, "deps": "",
        })
        store = self.server.JobStore()
        original_seeknal = self.server.SEEKNAL
        setattr(
            self.server, "SEEKNAL",
            self.fake_seeknal("fake-seeknal-failure", exit_code=1),
        )
        try:
            job = self.wait(store, store.create("draft", draft_request=request)["id"])
        finally:
            setattr(self.server, "SEEKNAL", original_seeknal)
        self.assertEqual("failed", job["status"])
        self.assertEqual(b"foreign bytes after preflight", root.read_bytes())
        self.assertIn(root.name, job["logs"])

    def test_timeout_preserves_root_file_created_after_preflight(self):
        name = "foreign_timeout"
        root = self.project / f"draft_source_{name}.yml"
        self.addCleanup(root.unlink, missing_ok=True)
        request = self.server.validate_draft_request({
            "node_type": "source", "name": name, "python": False, "deps": "",
        })

        class TimedOutProcess:
            def __init__(self):
                root.write_bytes(b"foreign bytes after preflight")
                self.stdout = io.StringIO("")
                self.pid = 12345
                self.returncode = None

            def wait(self, timeout: float = 120):
                raise subprocess.TimeoutExpired(cmd="seeknal draft", timeout=timeout)

        store = self.server.JobStore()
        with (
            patch.object(
                self.server.subprocess, "Popen", side_effect=lambda *args, **kwargs: TimedOutProcess()
            ),
            patch.object(store, "_terminate"),
        ):
            job = self.wait(store, store.create("draft", draft_request=request)["id"])
        self.assertEqual("failed", job["status"])
        self.assertEqual(124, job["exit_code"], job["logs"])
        self.assertEqual(b"foreign bytes after preflight", root.read_bytes())
        self.assertIn(root.name, job["logs"])

    def test_cancel_preserves_root_file_created_after_preflight(self):
        name = "foreign_cancel"
        root = self.project / f"draft_source_{name}.yml"
        self.addCleanup(root.unlink, missing_ok=True)
        request = self.server.validate_draft_request({
            "node_type": "source", "name": name, "python": False, "deps": "",
        })
        store = self.server.JobStore()
        original_seeknal = self.server.SEEKNAL
        setattr(
            self.server, "SEEKNAL",
            self.fake_seeknal("fake-seeknal-cancel", exit_code=1, sleep=True),
        )
        try:
            job = store.create("draft", draft_request=request)
            deadline = time.time() + 5
            while time.time() < deadline and not root.exists():
                time.sleep(0.02)
            self.assertTrue(root.exists())
            store.cancel(job["id"])
            deadline = time.time() + 5
            while time.time() < deadline and store.get(job["id"])["exit_code"] is None:
                time.sleep(0.02)
            finished = store.get(job["id"])
        finally:
            setattr(self.server, "SEEKNAL", original_seeknal)
        self.assertEqual("cancelled", finished["status"])
        self.assertEqual(b"foreign bytes after preflight", root.read_bytes())
        self.assertIn(root.name, finished["logs"])

    def test_invalid_kind_name_and_types_are_rejected_before_execution(self):
        invalid = [
            {"node_type": "--help", "name": "safe"},
            {"node_type": "source", "name": "--force"},
            {"node_type": "source", "name": "bad/name"},
            {"node_type": "semantic-model", "name": "safe", "python": True},
            {"node_type": "source", "name": "safe", "python": False, "deps": "--python"},
        ]
        original_popen = self.server.subprocess.Popen
        with patch.object(self.server.subprocess, "Popen", wraps=original_popen) as popen:
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    self.server.validate_draft_request(payload)
        draft_commands = [
            call.args[0] for call in popen.call_args_list
            if len(call.args[0]) > 1 and call.args[0][1] == "draft"
        ]
        self.assertEqual([], draft_commands)

    def test_description_and_deps_are_bound_as_values_not_flags(self):
        job, draft = self.draft(
            node_type="source",
            name="bound_options_test",
            description="--python",
            python=True,
            deps="--force",
        )
        self.assertEqual("succeeded", job["status"], job["logs"])
        content = draft.read_text()
        self.assertIn("--python", content)
        self.assertIn('"--force"', content)


class CliDraftHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.project = Path(cls.temporary.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project)
        cls.previous_project = os.environ.get("SEEKNAL_STUDIO_PROJECT")
        os.environ["SEEKNAL_STUDIO_PROJECT"] = str(cls.project)
        cls.server = load_server()
        cls.httpd = cls.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        setattr(cls.server, "HOSTS", {f"127.0.0.1:{cls.port}"})
        setattr(cls.server, "ORIGINS", {f"http://127.0.0.1:{cls.port}"})
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server.AGENT.close()
        cls.server.REPL.close()
        cls.server.PROJECTS.close()
        if cls.previous_project is None:
            os.environ.pop("SEEKNAL_STUDIO_PROJECT", None)
        else:
            os.environ["SEEKNAL_STUDIO_PROJECT"] = cls.previous_project
        cls.temporary.cleanup()

    def request(self, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            headers={"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_options_and_draft_job_routes(self):
        status, options = self.request("/api/drafts/options")
        self.assertEqual(200, status)
        self.assertTrue(any(item["value"] == "transform" for item in options["node_types"]))
        status, job = self.request("/api/jobs", {
            "action": "draft", "node_type": "transform", "name": "http_draft_test",
            "description": "HTTP draft", "python": False, "deps": "",
        })
        self.assertEqual(202, status)
        deadline = time.time() + 30
        while time.time() < deadline:
            _, job = self.request(f"/api/jobs/{job['id']}")
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        self.assertEqual("succeeded", job["status"], job["logs"])
        self.assertEqual(".seeknal/drafts/draft_transform_http_draft_test.yml", job["draft_path"])
        (self.project / job["draft_path"]).unlink()

    def test_options_timeout_returns_json_error_and_is_not_cached(self):
        setattr(self.server, "DRAFT_OPTIONS_CACHE", None)
        self.addCleanup(setattr, self.server, "DRAFT_OPTIONS_CACHE", None)
        timeout = subprocess.TimeoutExpired(
            cmd=["secret-command"], timeout=20, output="secret-output"
        )
        with patch.object(self.server.subprocess, "run", side_effect=timeout):
            status, payload = self.request("/api/drafts/options")
        self.assertEqual(400, status)
        self.assertIn("Seeknal bridge timed out during draft-options", payload["error"])
        self.assertNotIn("secret", payload["error"])
        self.assertIsNone(self.server.DRAFT_OPTIONS_CACHE)

    def test_malformed_draft_is_reported_without_hiding_valid_drafts(self):
        drafts = self.project / ".seeknal" / "drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        valid = drafts / "draft_transform_listing_valid.yml"
        invalid = drafts / "draft_common_source_listing_invalid.yml"
        valid.write_text("kind: transform\nname: listing_valid\ntransform: SELECT 1\n")
        invalid.write_text("kind: common-source\nname: listing_invalid\n")
        self.addCleanup(valid.unlink, missing_ok=True)
        self.addCleanup(invalid.unlink, missing_ok=True)
        status, payload = self.request("/api/drafts")
        self.assertEqual(200, status)
        self.assertIn(valid.relative_to(self.project).as_posix(), {
            item["path"] for item in payload["drafts"]
        })
        self.assertEqual([invalid.relative_to(self.project).as_posix()], [
            item["path"] for item in payload["errors"]
        ])


if __name__ == "__main__":
    unittest.main()
