from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("open_pilot", ROOT / "open_pilot.py")
assert SPEC and SPEC.loader
open_pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(open_pilot)


class PilotLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.template = self.root / "pilot" / "template"
        self.workspace = self.root / "pilot" / "workspace"
        shutil.copytree(ROOT / "pilot" / "template", self.template)
        shutil.copytree(self.template, self.workspace)
        transform = self.workspace / "seeknal" / "transforms" / "customer_value_pilot.yml"
        transform.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "pilot" / "workspace" / "seeknal" / "transforms" / transform.name, transform)
        self.paths = patch.multiple(
            open_pilot,
            ROOT=self.root,
            WORKSPACE_PATH=self.workspace,
            TEMPLATE_PATH=self.template,
        )
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temporary.cleanup()

    def test_workspace_is_fixed_to_completed_synthetic_pilot(self):
        self.assertEqual(self.workspace, open_pilot.WORKSPACE_PATH)
        self.assertEqual(self.workspace.resolve(), open_pilot.validate_workspace())

    def test_workspace_symlink_is_rejected_before_resolution(self):
        shutil.rmtree(self.workspace)
        self.workspace.symlink_to(self.template, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "workspace path is a symlink"):
            open_pilot.validate_workspace()

    def test_descendant_symlink_is_rejected_before_resolution(self):
        (self.workspace / "data" / "linked.csv").symlink_to(self.root / "outside.csv")
        with self.assertRaisesRegex(RuntimeError, "workspace contains a symlink"):
            open_pilot.validate_workspace()

    def test_changed_csv_with_same_row_count_is_rejected(self):
        orders = self.workspace / "data" / "orders.csv"
        orders.write_text(orders.read_text().replace("125000", "126000", 1))
        with self.assertRaisesRegex(RuntimeError, "authority artifact changed: data/orders.csv"):
            open_pilot.validate_workspace()

    def test_substring_compatible_source_yaml_change_is_rejected(self):
        source = self.workspace / "seeknal" / "sources" / "orders.yml"
        source.write_text(source.read_text().replace("Synthetic commerce orders", "Synthetic changed orders"))
        with self.assertRaisesRegex(RuntimeError, "authority artifact changed: seeknal/sources/orders.yml"):
            open_pilot.validate_workspace()

    def test_extra_source_definition_is_rejected(self):
        (self.workspace / "seeknal" / "sources" / "extra.yml").write_text("kind: source\nname: extra\n")
        with self.assertRaisesRegex(RuntimeError, "pipeline definition set changed"):
            open_pilot.validate_workspace()

    def test_extra_non_source_definition_is_rejected(self):
        extra = self.workspace / "seeknal" / "exposures" / "extra.py"
        extra.parent.mkdir()
        extra.write_text("# unexpected pipeline definition\n")
        with self.assertRaisesRegex(RuntimeError, "pipeline definition set changed"):
            open_pilot.validate_workspace()

    def test_environment_removes_only_unrelated_atlas_urls(self):
        source = {
            "ATLAS_API_URL": "https://unrelated.example",
            "ATLAS_PORTAL_URL": "https://unrelated.example/portal",
            "OPENAI_API_KEY": "preserved-placeholder",
            "PATH": "/usr/bin",
        }
        result = open_pilot.pilot_environment(source)
        self.assertEqual({"OPENAI_API_KEY": "preserved-placeholder", "PATH": "/usr/bin"}, result)

    def test_private_receipt_contains_only_governance_baseline_and_fixed_authority_proof(self):
        receipt, nonce = open_pilot.write_baseline_receipt(
            self.root, self.workspace.resolve(),
            {"ATLAS_API_URL": "fake-api", "ATLAS_PORTAL_URL": "fake-portal", "SECRET": "excluded"},
        )
        value = json.loads(receipt.read_text())
        self.assertEqual(nonce, value["nonce"])
        self.assertNotIn("SECRET", receipt.read_text())
        self.assertEqual(set(open_pilot.AUTHORITY_ARTIFACTS), set(value["pilot_authority"]["sha256"]))
        self.assertEqual(open_pilot.EXPECTED_PIPELINE_DEFINITIONS, set(value["pilot_authority"]["definitions"]))
        self.assertEqual(0, receipt.stat().st_mode & 0o077)

    def test_main_launches_only_fixed_workspace_without_reading_key(self):
        completed = subprocess.CompletedProcess([], 0)
        with (
            patch.object(sys, "argv", ["open_pilot.py"]),
            patch.object(open_pilot, "validate_workspace", return_value=self.workspace.resolve()) as validate,
            patch.object(Path, "is_file", return_value=True),
            patch.object(open_pilot.subprocess, "run", return_value=completed) as run,
            patch.dict(os.environ, {"ATLAS_API_URL": "x", "ATLAS_PORTAL_URL": "y"}, clear=False),
        ):
            self.assertEqual(0, open_pilot.main())
        validate.assert_called_once_with()
        command = run.call_args.args[0]
        self.assertEqual([str(open_pilot.BUNDLE_EXECUTABLE), "--project", str(self.workspace.resolve())], command)
        self.assertNotIn("ATLAS_API_URL", run.call_args.kwargs["env"])
        self.assertNotIn("ATLAS_PORTAL_URL", run.call_args.kwargs["env"])

    def test_main_rejects_all_operator_arguments(self):
        with patch.object(sys, "argv", ["open_pilot.py", "--project", "/tmp/other"]):
            self.assertEqual(2, open_pilot.main())


if __name__ == "__main__":
    unittest.main()
