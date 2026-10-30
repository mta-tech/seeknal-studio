from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import Request, urlopen
import socket
import subprocess
import sys

import launch
from local_access import TOKEN_HEADER, WINDOW_ENV, link_secret, read_access_link


class ProjectSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pilot = self.project("pilot")
        self.seed_pilot_authority()
        self.ordinary = self.project("ordinary")

    def tearDown(self):
        self.temporary.cleanup()

    def project(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        (path / "seeknal_project.yml").write_text(f"name: {name}\n")
        return path.resolve()

    def seed_pilot_authority(self):
        content = {
            "data/orders.csv": "order_id\n1\n",
            "data/customers.csv": "customer_id\n1\n",
            "seeknal/sources/orders.yml": "kind: source\nname: orders\n",
            "seeknal/sources/customers.yml": "kind: source\nname: customers\n",
            "SEEKNAL_ASK.md": "# Ask\n",
            "EXPECTED_OUTPUT.json": "{}\n",
            "seeknal/transforms/customer_value_pilot.yml": "kind: transform\nname: customer_value_pilot\ntransform: SELECT 1\n",
        }
        for relative, value in content.items():
            path = self.pilot / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)

    def authority_proof(self):
        return {
            "sha256": {
                relative: hashlib.sha256((self.pilot / relative).read_bytes()).hexdigest()
                for relative in launch.PILOT_AUTHORITY_ARTIFACTS
            },
            "definitions": sorted(launch.PILOT_DEFINITIONS),
        }

    def test_private_baseline_restores_parent_governance_after_pilot_switch(self):
        receipt = self.root / "baseline.json"
        receipt.write_text(json.dumps({
            "nonce": "nonce",
            "pilot_project": str(self.pilot),
            "governance": {
                "ATLAS_API_URL": {"present": True, "value": "https://fake-api.invalid"},
                "ATLAS_PORTAL_URL": {"present": True, "value": "https://fake-portal.invalid"},
            },
            "pilot_authority": self.authority_proof(),
        }))
        os.chmod(receipt, 0o600)
        baseline, pilot = launch.consume_baseline({
            "PATH": "/usr/bin",
            "SEEKNAL_STUDIO_BASELINE_RECEIPT": str(receipt),
            "SEEKNAL_STUDIO_BASELINE_NONCE": "nonce",
        })
        self.assertFalse(receipt.exists())
        self.assertNotIn("SEEKNAL_STUDIO_BASELINE_NONCE", baseline)
        pilot_environment = launch.environment_for_project(baseline, self.pilot, pilot)
        self.assertNotIn("ATLAS_API_URL", pilot_environment)
        self.assertNotIn("ATLAS_PORTAL_URL", pilot_environment)
        ordinary_environment = launch.environment_for_project(baseline, self.ordinary, None)
        self.assertEqual("https://fake-api.invalid", ordinary_environment["ATLAS_API_URL"])
        self.assertEqual("https://fake-portal.invalid", ordinary_environment["ATLAS_PORTAL_URL"])

    def test_changed_authority_after_leaving_pilot_never_strips_governance(self):
        authority = launch.PilotAuthority(
            self.pilot,
            self.authority_proof()["sha256"],
            frozenset(launch.PILOT_DEFINITIONS),
        )
        baseline = {"ATLAS_API_URL": "api", "ATLAS_PORTAL_URL": "portal"}
        self.assertNotIn("ATLAS_API_URL", launch.environment_for_project(baseline, self.pilot, authority))
        (self.pilot / "data/orders.csv").write_text("order_id\nchanged\n")
        self.assertEqual(baseline, launch.environment_for_project(baseline, self.pilot, authority))

    def test_extra_definition_after_leaving_pilot_never_strips_governance(self):
        authority = launch.PilotAuthority(
            self.pilot,
            self.authority_proof()["sha256"],
            frozenset(launch.PILOT_DEFINITIONS),
        )
        baseline = {"ATLAS_API_URL": "api", "ATLAS_PORTAL_URL": "portal"}
        extra = self.pilot / "seeknal" / "sources" / "extra.yml"
        extra.write_text("kind: source\nname: extra\n")
        self.assertEqual(baseline, launch.environment_for_project(baseline, self.pilot, authority))

    def test_transform_sql_can_change_without_changing_pilot_authority(self):
        authority = launch.PilotAuthority(
            self.pilot,
            self.authority_proof()["sha256"],
            frozenset(launch.PILOT_DEFINITIONS),
        )
        transform = self.pilot / "seeknal" / "transforms" / "customer_value_pilot.yml"
        transform.write_text(transform.read_text().replace("SELECT 1", "SELECT 2"))
        baseline = {"ATLAS_API_URL": "api", "ATLAS_PORTAL_URL": "portal"}
        result = launch.environment_for_project(baseline, self.pilot, authority)
        self.assertNotIn("ATLAS_API_URL", result)
        self.assertNotIn("ATLAS_PORTAL_URL", result)

    def test_switch_control_requires_matching_nonce_and_is_consumed(self):
        control = self.root / "switch.json"
        control.write_text(json.dumps({"nonce": "correct", "project": str(self.ordinary)}))
        os.chmod(control, 0o600)
        self.assertEqual(self.ordinary, launch.read_switch(control, "correct"))
        self.assertFalse(control.exists())
        control.write_text(json.dumps({"nonce": "wrong", "project": str(self.ordinary)}))
        os.chmod(control, 0o600)
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            launch.read_switch(control, "correct")
        self.assertFalse(control.exists())

    def test_ordinary_project_environment_preserves_governance_values(self):
        baseline = {
            "ATLAS_API_URL": "https://fake-api.invalid",
            "ATLAS_PORTAL_URL": "https://fake-portal.invalid",
        }
        result = launch.environment_for_project(baseline, self.ordinary, None)
        self.assertEqual(baseline, result)

    def test_launcher_restarts_backend_and_window_for_new_project(self):
        webframe = self.root / "fake-webframe"
        windows = self.root / "windows"
        windows.mkdir()
        webframe.write_text(
            "#!/usr/bin/env python3\nimport json, os, sys, time\n"
            f"path = os.path.join({str(windows)!r}, str(os.getpid()))\n"
            f"open(path + '.tmp', 'w').write(json.dumps({{'argv': sys.argv[1:], 'session': os.environ.get({WINDOW_ENV!r})}}))\n"
            "os.rename(path + '.tmp', path + '.json')\n"
            "time.sleep(60)\n"
        )
        webframe.chmod(0o755)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = os.environ.copy()
        environment["SEEKNAL_STUDIO_PROJECTS_FILE"] = str(self.root / "launcher-projects.json")
        process = subprocess.Popen([
            sys.executable, str(Path(launch.__file__)),
            "--project", str(self.pilot), "--port", str(port),
            "--webframe", str(webframe),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)

        access: dict[str, str] = {}

        def request(path: str, payload: dict | None = None):
            data = json.dumps(payload).encode() if payload is not None else None
            headers = {"Origin": f"http://127.0.0.1:{port}", **access}
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = Request(
                f"http://127.0.0.1:{port}{path}", data=data, headers=headers,
                method="POST" if data is not None else "GET",
            )
            with urlopen(req, timeout=2) as response:
                return response.status, json.load(response)

        output = ""
        try:
            for line in iter(process.stdout.readline, ""):
                output += line
                link = read_access_link(output)
                if link:
                    access[TOKEN_HEADER] = link_secret(link)
                    break
            self.assertTrue(access, output)
            with self.assertRaises(URLError):
                access_header = access.pop(TOKEN_HEADER)
                try:
                    request("/api/state")
                finally:
                    access[TOKEN_HEADER] = access_header
            deadline = time.monotonic() + 20
            initial = None
            while time.monotonic() < deadline:
                try:
                    _, initial = request("/api/state")
                    break
                except URLError:
                    time.sleep(0.1)
            self.assertIsNotNone(initial)
            self.assertEqual(str(self.pilot), initial["project_path"])
            while time.monotonic() < deadline and not list(windows.glob("*.json")):
                time.sleep(0.1)
            status, accepted = request("/api/projects/open", {"path": str(self.ordinary)})
            self.assertEqual(202, status)
            self.assertTrue(accepted["switching"])
            switched = None
            while time.monotonic() < deadline:
                try:
                    _, candidate = request("/api/state")
                    if candidate["project_path"] == str(self.ordinary):
                        switched = candidate
                        break
                except (URLError, ConnectionError):
                    # The old backend may reset a request while it stops.
                    pass
                # The launcher keeps the port bound across the restart.
                with socket.socket() as squatter, self.assertRaises(OSError):
                    squatter.bind(("127.0.0.1", port))
                time.sleep(0.1)
            self.assertIsNotNone(switched)
            self.assertNotEqual(initial["runtime_id"], switched["runtime_id"])
            window_deadline = time.monotonic() + 10
            while time.monotonic() < window_deadline and len(list(windows.glob("*.json"))) < 2:
                time.sleep(0.1)
            opened = [json.loads(path.read_text()) for path in windows.glob("*.json")]
            secret = access[TOKEN_HEADER]
            # Both windows got the same launch secret privately, never on argv.
            self.assertEqual([secret, secret], [item["session"] for item in opened])
            self.assertFalse(any(secret in " ".join(item["argv"]) for item in opened))
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                output += process.stdout.read()
                process.stdout.close()
            if process.stderr is not None:
                output += process.stderr.read()
                process.stderr.close()
        self.assertEqual(0, process.returncode)
        self.assertNotIn(access[TOKEN_HEADER], output)


if __name__ == "__main__":
    unittest.main()
