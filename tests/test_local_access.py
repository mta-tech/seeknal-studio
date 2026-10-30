from __future__ import annotations

import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_access import (
    DENIED, DENIED_ROOT, QUERY, SECRET_ENV, TOKEN_HEADER, BrowserAccess, cookie_name, cookie_value,
    readiness_proof, request_cookie,
)


ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-ui-secret-0123456789abcdefghijklmnopqrstuv"


def load_server():
    spec = importlib.util.spec_from_file_location("studio_local_access_server", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class BrowserAccessUnitTests(unittest.TestCase):
    def test_no_secret_admits_everything(self):
        access = BrowserAccess(None)
        self.assertTrue(access.allowed({}, 1234))
        self.assertIsNone(access.bootstrap(f"/?{QUERY}=anything", 1234))

    def test_cookie_is_port_scoped(self):
        access = BrowserAccess(SECRET)
        value = cookie_value(SECRET, 1234)
        self.assertNotEqual(value, cookie_value(SECRET, 1235))
        self.assertNotIn(SECRET, value)
        self.assertTrue(access.allowed({"Cookie": f"other=x; {cookie_name(1234)}={value}"}, 1234))
        self.assertFalse(access.allowed({"Cookie": f"{cookie_name(1235)}={value}"}, 1234))
        self.assertFalse(access.allowed({"Cookie": f"{cookie_name(1234)}={cookie_value(SECRET, 1235)}"}, 1234))

    def test_malformed_neighbour_cookie_does_not_hide_ours(self):
        header = f'bad="unterminated; {cookie_name(9)}={cookie_value(SECRET, 9)}'
        self.assertEqual(cookie_value(SECRET, 9), request_cookie(header, cookie_name(9)))

    def test_readiness_proof_is_keyed_apart_from_the_cookie(self):
        self.assertNotEqual(readiness_proof(SECRET, "ab" * 16), readiness_proof("other", "ab" * 16))
        self.assertNotEqual(cookie_value(SECRET, 1234), readiness_proof(SECRET, "seeknal-studio-ui:1234"))

    def test_refusal_page_bootstraps_at_most_once_per_tab(self):
        # Blocked cookies must not turn the bootstrap into a redirect loop.
        script = DENIED_ROOT.decode()
        self.assertIn('!sessionStorage.getItem("studio_bootstrap")', script)
        self.assertLess(script.index('sessionStorage.setItem("studio_bootstrap"'), script.index("location.replace"))
        self.assertIn("catch", script)

    def test_bootstrap_only_on_root(self):
        access = BrowserAccess(SECRET)
        self.assertTrue(access.bootstrap(f"/?{QUERY}={SECRET}", 1))
        self.assertFalse(access.bootstrap(f"/?{QUERY}=wrong", 1))
        self.assertFalse(access.bootstrap(f"/?{QUERY}={SECRET}&{QUERY}={SECRET}", 1))
        self.assertIsNone(access.bootstrap(f"/api/state?{QUERY}={SECRET}", 1))
        self.assertIsNone(access.bootstrap(f"/index.html?{QUERY}={SECRET}", 1))
        self.assertIsNone(access.bootstrap("/", 1))


class ServerBrowserAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.project = Path(cls.tempdir.name) / "demo"
        shutil.copytree(ROOT / "demo", cls.project, ignore=shutil.ignore_patterns("target", ".seeknal"))
        with patch.dict(os.environ, {
            "SEEKNAL_STUDIO_PROJECT": str(cls.project),
            "SEEKNAL_STUDIO_PROJECTS_FILE": str(Path(cls.tempdir.name) / "projects.json"),
            SECRET_ENV: SECRET,
        }):
            cls.server = load_server()
            cls.inherited = os.environ.get(SECRET_ENV)
        cls.httpd = cls.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server.PORT = cls.port
        cls.server.HOSTS = {f"127.0.0.1:{cls.port}"}
        cls.server.ORIGINS = {f"http://127.0.0.1:{cls.port}"}
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server.PROJECTS.close()
        cls.tempdir.cleanup()

    def request(self, path, *, method="GET", headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def cookie(self, port=None):
        port = self.port if port is None else port
        return {"Cookie": f"{cookie_name(port)}={cookie_value(SECRET, port)}"}

    def test_secret_is_removed_from_the_environment(self):
        self.assertIsNone(self.inherited)

    def test_bootstrap_sets_port_scoped_cookie_and_redirects(self):
        status, headers, body = self.request(f"/?{QUERY}={SECRET}")
        self.assertEqual(303, status)
        self.assertEqual("/", headers["Location"])
        self.assertEqual(b"", body)
        cookie = headers["Set-Cookie"]
        self.assertTrue(cookie.startswith(f"seeknal_studio_{self.port}={cookie_value(SECRET, self.port)};"))
        attributes = {part.strip() for part in cookie.split(";")[1:]}
        self.assertEqual({"HttpOnly", "SameSite=Strict", "Path=/"}, attributes)
        self.assertNotIn(SECRET, cookie)
        status, _, page = self.request("/", headers=self.cookie())
        self.assertEqual(200, status)
        self.assertIn(b"<html", page.lower())

    def test_cookie_admits_static_api_and_post(self):
        for path in ("/", "/index.html", "/app.js", "/styles.css", "/api/state", "/api/projects"):
            with self.subTest(path=path):
                self.assertEqual(200, self.request(path, headers=self.cookie())[0])

    def test_responses_are_never_cached(self):
        # A cached "/" would let a new Webframe window skip its bootstrap.
        for path, headers in (("/", self.cookie()), ("/app.js", self.cookie()), ("/api/state", self.cookie()),
                              ("/", {}), (f"/?{QUERY}={SECRET}", {}), ("/missing.js", self.cookie())):
            with self.subTest(path=path):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                try:
                    connection.request("GET", path, headers=headers)
                    response = connection.getresponse()
                    response.read()
                    values = [value for key, value in response.getheaders() if key.lower() == "cache-control"]
                finally:
                    connection.close()
                self.assertEqual(["no-store"], values)
        status, _, _ = self.request(
            "/api/unknown", method="POST", body=b"{}",
            headers={**self.cookie(), "Content-Type": "application/json"},
        )
        self.assertEqual(404, status)

    def test_missing_or_wrong_secret_is_refused_everywhere(self):
        attempts = {
            "missing": {},
            "wrong cookie": {"Cookie": f"{cookie_name(self.port)}={cookie_value('wrong', self.port)}"},
            "wrong header": {TOKEN_HEADER: "wrong"},
        }
        for label, headers in attempts.items():
            for path in ("/", "/index.html", "/app.js", "/api/state", "/api/file?path=seeknal_project.yml"):
                with self.subTest(label=label, path=path):
                    status, _, body = self.request(path, headers=headers)
                    self.assertEqual(403, status)
                    self.assertIn(b"Open Seeknal Studio from the app", body)
            with self.subTest(label=label, method="POST"):
                status, response_headers, body = self.request(
                    "/api/jobs", method="POST", body=b'{"action":"run"}',
                    headers={**headers, "Content-Type": "application/json"},
                )
                self.assertEqual((403, DENIED), (status, body))
                self.assertTrue(response_headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(403, self.request("/app.js", method="HEAD")[0])
        self.assertEqual([], self.server.JOBS.list())

    def test_wrong_bootstrap_secret_is_refused_without_cookie(self):
        status, headers, body = self.request(f"/?{QUERY}=wrong")
        self.assertEqual((403, DENIED), (status, body))
        self.assertNotIn("Set-Cookie", headers)

    def test_header_token_is_accepted(self):
        status, _, _ = self.request("/api/state", headers={TOKEN_HEADER: SECRET})
        self.assertEqual(200, status)

    def test_readiness_challenge_needs_no_credentials_and_reveals_only_a_proof(self):
        challenge = "0f" * 16
        status, headers, body = self.request(f"/api/ready?challenge={challenge}")
        self.assertEqual(200, status)
        self.assertEqual({"proof": readiness_proof(SECRET, challenge)}, json.loads(body))
        self.assertNotIn(SECRET.encode(), body)
        self.assertNotIn("Set-Cookie", headers)
        for bad in ("", "?challenge=xyz", "?challenge=" + "A" * 32):
            with self.subTest(bad=bad):
                self.assertEqual(400, self.request("/api/ready" + bad)[0])
        self.assertEqual(403, self.request("/api/ready?challenge=" + challenge, method="POST", body=b"{}")[0])

    def test_cookie_named_for_another_port_is_rejected(self):
        other = self.port + 1
        stolen_value = {"Cookie": f"{cookie_name(other)}={cookie_value(SECRET, self.port)}"}
        self.assertEqual(403, self.request("/api/state", headers=stolen_value)[0])
        self.assertEqual(403, self.request("/api/state", headers=self.cookie(other))[0])

    def test_bootstrap_query_is_ignored_on_api_and_static_paths(self):
        for path in (f"/api/state?{QUERY}={SECRET}", f"/app.js?{QUERY}={SECRET}"):
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(403, status)
                self.assertNotIn("Set-Cookie", headers)

    def test_refusal_page_for_root_bootstraps_webframe_from_page_global(self):
        status, headers, body = self.request("/")
        self.assertEqual(403, status)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b"STUDIO_SESSION", body)
        self.assertNotIn(SECRET.encode(), body)

    def test_origin_check_still_applies_with_valid_cookie(self):
        status, _, _ = self.request("/api/state", headers={**self.cookie(), "Origin": "http://evil.invalid"})
        self.assertEqual(403, status)

    def test_without_ui_secret_behaviour_is_unchanged(self):
        with patch.object(self.server, "BROWSER_ACCESS", BrowserAccess(None)):
            self.assertEqual(200, self.request("/api/state")[0])
            self.assertEqual(200, self.request("/app.js")[0])
            self.assertEqual(200, self.request(f"/?{QUERY}={SECRET}")[0])
            self.assertEqual(404, self.request("/api/ready?challenge=" + "0f" * 16)[0])


if __name__ == "__main__":
    unittest.main()
