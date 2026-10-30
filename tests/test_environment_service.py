from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from dotenv import dotenv_values

from environment_service import (
    EnvironmentBusyError,
    EnvironmentConflictError,
    EnvironmentService,
)


class EnvironmentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.project = Path(self.directory.name) / "commerce"
        self.project.mkdir()
        (self.project / "seeknal_project.yml").write_text("name: commerce\n", encoding="utf-8")
        self.active: list[str] = []
        self.service = EnvironmentService(
            self.project,
            busy=lambda: list(self.active),
            runtime=lambda: {
                "seeknal_version": "2.14.0",
                "python": "/managed/python",
                "repl_state": "stopped",
            },
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_status_lists_keys_without_exposing_values(self) -> None:
        secret = "postgresql://reader:do-not-return@db/warehouse"
        env = self.project / ".env"
        env.write_text(
            "# project connection\n"
            f"WAREHOUSE_DSN={secret}\n"
            "EMPTY=\n"
            "OPENAI_API_KEY=also-do-not-return\n",
            encoding="utf-8",
        )

        status = self.service.status()
        serialized = json.dumps(status)

        self.assertNotIn(secret, serialized)
        self.assertNotIn("also-do-not-return", serialized)
        self.assertEqual(status["environment"]["path"], str(env.resolve()))
        entries = {entry["key"]: entry for entry in status["environment"]["entries"]}
        self.assertTrue(entries["WAREHOUSE_DSN"]["has_value"])
        self.assertFalse(entries["EMPTY"]["has_value"])
        self.assertTrue(entries["WAREHOUSE_DSN"]["editable"])
        self.assertFalse(entries["OPENAI_API_KEY"]["editable"])
        self.assertEqual(entries["OPENAI_API_KEY"]["protected_reason"], "model provider variable")

    def test_update_is_atomic_owner_only_and_preserves_unrelated_lines(self) -> None:
        env = self.project / ".env"
        env.write_text(
            "# keep this comment\n"
            "ATLAS_API_URL=https://old.example.test\n"
            "WAREHOUSE_DSN=old\n",
            encoding="utf-8",
        )
        env.chmod(0o644)
        before = self.service.status()

        result = self.service.update({
            "updates": {
                "WAREHOUSE_DSN": "postgresql://reader:p'a\\ss@db/warehouse?sslmode=require",
                "LAKEKEEPER_WAREHOUSE": "sandbox",
            },
            "remove": ["ATLAS_API_URL"],
            "content_hash": before["environment"]["content_hash"],
        })

        text = env.read_text(encoding="utf-8")
        values = dotenv_values(env, interpolate=False)
        self.assertIn("# keep this comment", text)
        self.assertNotIn("ATLAS_API_URL", text)
        self.assertEqual(values["WAREHOUSE_DSN"], "postgresql://reader:p'a\\ss@db/warehouse?sslmode=require")
        self.assertEqual(values["LAKEKEEPER_WAREHOUSE"], "sandbox")
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertTrue(result["saved"])
        self.assertEqual(
            result["environment"]["content_hash"],
            hashlib.sha256(env.read_bytes()).hexdigest(),
        )
        self.assertNotIn("p'a", json.dumps(result))
        self.assertEqual(list(self.project.glob(".env.studio-*")), [])

    def test_duplicate_assignments_are_replaced_once(self) -> None:
        env = self.project / ".env"
        env.write_text("VALUE=one\nexport VALUE = two\nKEEP=yes\n", encoding="utf-8")
        before = self.service.status()

        self.service.update({
            "updates": {"VALUE": "three"},
            "remove": [],
            "content_hash": before["environment"]["content_hash"],
        })

        self.assertEqual(env.read_text(encoding="utf-8").count("VALUE="), 1)
        self.assertEqual(dotenv_values(env, interpolate=False)["VALUE"], "three")

    def test_optimistic_hash_and_active_work_block_changes(self) -> None:
        initial = self.service.status()["environment"]["content_hash"]
        (self.project / ".env").write_text("EXTERNAL=change\n", encoding="utf-8")
        with self.assertRaisesRegex(EnvironmentConflictError, "changed"):
            self.service.update({"updates": {"WAREHOUSE": "x"}, "remove": [], "content_hash": initial})
        self.assertEqual((self.project / ".env").read_text(), "EXTERNAL=change\n")

        current = self.service.status()["environment"]["content_hash"]
        self.active[:] = ["pipeline job", "REPL query"]
        with self.assertRaisesRegex(EnvironmentBusyError, "pipeline job, REPL query"):
            self.service.update({"updates": {"WAREHOUSE": "x"}, "remove": [], "content_hash": current})
        self.assertEqual((self.project / ".env").read_text(), "EXTERNAL=change\n")

    def test_rejects_invalid_protected_or_ambiguous_updates_without_echoing_values(self) -> None:
        content_hash = self.service.status()["environment"]["content_hash"]
        cases = (
            ({"BAD-KEY": "secret"}, "invalid environment key"),
            ({"SEEKNAL_STUDIO_PORT": "9999"}, "Studio control variable"),
            ({"OPENAI_API_KEY": "secret"}, "model provider variable"),
            ({"PYTHONPATH": "/tmp/trap"}, "Python runtime variable"),
            ({"EMPTY": ""}, "use remove"),
            ({"EXPANSION": "before-${HOME}-after"}, "cannot contain"),
            ({"MULTILINE": "first\nsecond"}, "single line"),
        )
        for updates, message in cases:
            with self.subTest(updates=list(updates)):
                with self.assertRaisesRegex(ValueError, message) as raised:
                    self.service.update({"updates": updates, "remove": [], "content_hash": content_hash})
                submitted_value = next(iter(updates.values()))
                if submitted_value:
                    self.assertNotIn(submitted_value, str(raised.exception))

    def test_explicit_remove_can_clean_up_a_protected_existing_key(self) -> None:
        env = self.project / ".env"
        env.write_text("OPENAI_API_KEY=old-secret\nWAREHOUSE=keep\n", encoding="utf-8")
        content_hash = self.service.status()["environment"]["content_hash"]

        self.service.update({
            "updates": {},
            "remove": ["OPENAI_API_KEY"],
            "content_hash": content_hash,
        })

        self.assertEqual(env.read_text(encoding="utf-8"), "WAREHOUSE=keep\n")

    def test_symlink_environment_is_rejected(self) -> None:
        outside = Path(self.directory.name) / "outside.env"
        outside.write_text("SECRET=outside\n", encoding="utf-8")
        (self.project / ".env").symlink_to(outside)

        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            self.service.status()
        self.assertEqual(outside.read_text(encoding="utf-8"), "SECRET=outside\n")

    def test_multiline_existing_value_is_rejected_without_leaving_secret_fragments(self) -> None:
        env = self.project / ".env"
        original = (
            'MULTILINE="first secret line\n'
            'second secret line"\n'
            "WAREHOUSE=keep\n"
        )
        env.write_text(original, encoding="utf-8")
        content_hash = self.service.status()["environment"]["content_hash"]

        for payload in (
            {"updates": {"MULTILINE": "replacement"}, "remove": []},
            {"updates": {}, "remove": ["MULTILINE"]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RuntimeError, "multiline quoted value"):
                    self.service.update({**payload, "content_hash": content_hash})
                self.assertEqual(env.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
