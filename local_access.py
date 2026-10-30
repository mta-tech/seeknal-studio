"""Per-launch browser access for Studio's loopback services.

Loopback Host/Origin checks stop web pages, not other users or processes on the
same machine.  The launcher therefore hands each local service a per-launch
secret.  The browser opens ``/?studio_session=<secret>`` once; the service
answers with a port-scoped HttpOnly cookie and redirects to ``/``.  Every later
request must carry that cookie or the secret in the token header.

Webframe receives the secret as the ``STUDIO_SESSION`` page global (from its
private environment) rather than in its world-readable command line; the
refusal page for ``/`` uses it to take the same bootstrap route.

The launcher owns the listening socket for the whole launch and hands it to
each service incarnation, so the port is never free for another local user to
take between restarts; its readiness probe carries no credential and instead
asks the service to prove that it holds the secret.
"""

from __future__ import annotations

import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
from typing import Mapping, Optional
from urllib.parse import parse_qs, urlparse

SECRET_ENV = "SEEKNAL_STUDIO_UI_SECRET"
QUERY = "studio_session"
TOKEN_HEADER = "X-Seeknal-Studio-Token"
WINDOW_ENV = "WF_STUDIO_SESSION"
LISTEN_FD_ENV = "SEEKNAL_STUDIO_LISTEN_FD"
READY_PATH = "/api/ready"
DENIED = b"Open Seeknal Studio from the app\n"
# Same-origin navigation, so the SameSite=Strict cookie set by the bootstrap
# redirect is sent with the following request for ``/``. One attempt per tab:
# when cookies are blocked the page stays on the message instead of looping.
DENIED_ROOT = (
    b'<!doctype html><meta charset="utf-8"><title>Seeknal Studio</title>'
    b"<p>Open Seeknal Studio from the app</p>"
    b'<script>try { if (typeof STUDIO_SESSION === "string" && !sessionStorage.getItem("studio_bootstrap")) {'
    b' sessionStorage.setItem("studio_bootstrap", "1");'
    b' location.replace("/?' + QUERY.encode() + b'=" + encodeURIComponent(STUDIO_SESSION)); } } catch (error) {}</script>\n'
)
_CHALLENGE = re.compile(r"[0-9a-f]{32,128}")


def cookie_name(port: int) -> str:
    # Cookies are not isolated by port on 127.0.0.1; two Studio instances must
    # not overwrite each other's session.
    return f"seeknal_studio_{port}"


def cookie_value(secret: str, port: int) -> str:
    return hmac.new(secret.encode(), f"seeknal-studio-ui:{port}".encode(), hashlib.sha256).hexdigest()


def readiness_proof(secret: str, challenge: str) -> str:
    # A separate key keeps proofs for caller-chosen challenges unrelated to the cookie.
    key = hmac.new(secret.encode(), b"seeknal-studio-ready", hashlib.sha256).digest()
    return hmac.new(key, challenge.encode(), hashlib.sha256).hexdigest()


def http_server(port: int, handler: type) -> ThreadingHTTPServer:
    """Serve on the launcher's listening socket when handed one, else bind 127.0.0.1:port."""
    descriptor = os.environ.pop(LISTEN_FD_ENV, None)
    if descriptor is None:
        return ThreadingHTTPServer(("127.0.0.1", port), handler)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler, bind_and_activate=False)
    server.socket.close()
    server.socket = socket.socket(fileno=int(descriptor))
    server.socket.set_inheritable(False)
    server.server_address = server.socket.getsockname()
    server.server_name, server.server_port = server.server_address[:2]
    return server


def bootstrap_url(secret: str, port: int) -> str:
    return f"http://127.0.0.1:{port}/?{QUERY}={secret}"


def read_access_link(launcher_output: str) -> Optional[str]:
    """Developer tooling: the one-time link from the private file launch.py names.

    launch.py never prints the link; it writes it to ``access-url`` (0600) in its
    private state directory and prints ``Access link file: <path>``.
    """
    found = re.findall(r"^Access link file: (.+)$", launcher_output, re.M)
    if not found:
        return None
    path = Path(found[-1].strip())
    return path.read_text(encoding="utf-8").strip() if path.is_file() else None


def link_secret(link: str) -> str:
    return parse_qs(urlparse(link).query)[QUERY][0]


def request_cookie(header: Optional[str], name: str) -> Optional[str]:
    # Parsed by hand: SimpleCookie gives up on the whole header when another
    # local app on 127.0.0.1 left a malformed cookie.
    for part in (header or "").split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value
    return None


class NoStoreMixin:
    """Mark responses ``no-store`` unless the handler chose its own cache policy.

    WebKit otherwise reuses a cached ``/`` in a new Webframe window, skipping
    the access check that bootstraps the window's cookie.
    """

    _cache_control = False

    def send_header(self, keyword: str, value: str) -> None:
        if keyword.lower() == "cache-control":
            self._cache_control = True
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        if not self._cache_control:
            super().send_header("Cache-Control", "no-store")
        self._cache_control = False
        super().end_headers()


class BrowserAccess:
    """Check one service's per-launch secret; ``None`` keeps the service open."""

    def __init__(self, secret: Optional[str]) -> None:
        self.secret = secret or None

    def allowed(self, headers: Mapping[str, str], port: int) -> bool:
        if self.secret is None:
            return True
        token = headers.get(TOKEN_HEADER)
        if token is not None and hmac.compare_digest(token.encode(), self.secret.encode()):
            return True
        cookie = request_cookie(headers.get("Cookie"), cookie_name(port))
        return cookie is not None and hmac.compare_digest(cookie.encode(), cookie_value(self.secret, port).encode())

    def bootstrap(self, path: str, port: int) -> Optional[bool]:
        """Return None when ``path`` is no bootstrap, else whether its secret is valid.

        Only ``GET /`` bootstraps; API paths never accept the query secret.
        """
        if self.secret is None:
            return None
        parsed = urlparse(path)
        if parsed.path != "/":
            return None
        values = parse_qs(parsed.query).get(QUERY)
        if values is None:
            return None
        return len(values) == 1 and hmac.compare_digest(values[0].encode(), self.secret.encode())

    def set_cookie(self, port: int) -> str:
        assert self.secret is not None
        return f"{cookie_name(port)}={cookie_value(self.secret, port)}; HttpOnly; SameSite=Strict; Path=/"

    def admit(self, handler: BaseHTTPRequestHandler, port: int, *, bootstrap: bool = False) -> bool:
        """Answer a bootstrap, readiness challenge or refusal; True lets the request proceed."""
        if bootstrap and self.secret is not None and urlparse(handler.path).path == READY_PATH:
            challenge = parse_qs(urlparse(handler.path).query).get("challenge", [""])[0]
            valid = _CHALLENGE.fullmatch(challenge) is not None
            body = json.dumps({"proof": readiness_proof(self.secret, challenge)} if valid else {"error": "invalid challenge"}).encode()
            handler.send_response(200 if valid else 400)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(body)
            return False
        accepted = self.bootstrap(handler.path, port) if bootstrap else None
        if accepted:
            handler.send_response(303)
            handler.send_header("Location", "/")
            handler.send_header("Set-Cookie", self.set_cookie(port))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return False
        if accepted is None and self.allowed(handler.headers, port):
            return True
        root = bootstrap and handler.path == "/"
        body = DENIED_ROOT if root else DENIED
        handler.send_response(403)
        handler.send_header("Content-Type", "text/html; charset=utf-8" if root else "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(body)
        return False
