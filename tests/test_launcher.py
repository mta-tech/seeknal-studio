import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from local_access import LISTEN_FD_ENV, readiness_proof

spec = importlib.util.spec_from_file_location('studio_launch', Path(__file__).resolve().parents[1] / 'launch.py')
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


class LauncherTests(unittest.TestCase):
    def test_webframe_environment_is_minimal_and_excludes_governance_and_provider_values(self):
        result = launch.window_environment({
            "HOME": "/tmp/home", "PATH": "/usr/bin", "LANG": "en_US.UTF-8",
            "LC_MESSAGES": "en_US.UTF-8", "DISPLAY": ":1",
            "ATLAS_API_URL": "https://fake-atlas.invalid",
            "OPENAI_API_KEY": "fake-key", "SEEKNAL_STUDIO_CONTROL_NONCE": "private",
            "WF_TOKEN": "webframe-global", "SEEKNAL_GATEWAY_TOKEN": "gateway",
        })
        self.assertEqual({
            "HOME": "/tmp/home", "PATH": "/usr/bin", "LANG": "en_US.UTF-8",
            "LC_MESSAGES": "en_US.UTF-8", "DISPLAY": ":1",
        }, result)

    def test_shutdown_terminates_owned_process(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            launch.stop(process)
            self.assertIsNotNone(process.poll())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_readiness_rejects_exited_backend(self):
        process = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(7)'])
        process.wait()
        with self.assertRaisesRegex(RuntimeError, 'exited \\(7\\)'):
            launch.wait_ready(process, 'http://127.0.0.1:1', SECRET, timeout=.2)


SECRET = 'launcher-secret-0123456789abcdefghijklmnopq'


class Recorder(BaseHTTPRequestHandler):
    """A local service that records raw requests and answers per ``mode``."""

    def do_GET(self):
        raw = self.requestline + '\r\n' + str(self.headers)
        self.server.seen.append(raw)
        mode = self.server.mode
        if mode == 'redirect':
            self.send_response(302)
            self.send_header('Location', self.server.redirect_to)
            self.end_headers()
            return
        challenge = parse_qs(urlparse(self.path).query).get('challenge', [''])[0]
        proof = readiness_proof(SECRET, challenge) if mode == 'honest' else 'f' * 64
        body = json.dumps({'proof': proof}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
        del format, args


class ReadinessProbeTests(unittest.TestCase):
    def serve(self, mode, redirect_to=None):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Recorder)
        server.mode, server.seen, server.redirect_to = mode, [], redirect_to
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f'http://127.0.0.1:{server.server_address[1]}'

    def running(self):
        process = unittest.mock.Mock()
        process.poll.return_value = None
        return process

    def assert_no_secret(self, *servers):
        for server in servers:
            for raw in server.seen:
                self.assertNotIn(SECRET, raw)
                self.assertNotIn('Cookie', raw)
                self.assertNotIn('X-Seeknal-Studio-Token', raw)

    def test_probe_accepts_proof_without_sending_the_secret(self):
        server, url = self.serve('honest')
        launch.wait_ready(self.running(), url, SECRET, timeout=5)
        self.assertTrue(server.seen[0].startswith('GET /api/ready?challenge='))
        self.assert_no_secret(server)

    def test_squatter_without_the_secret_is_rejected_and_learns_nothing(self):
        server, url = self.serve('squatter')
        with self.assertRaisesRegex(RuntimeError, 'could not prove'):
            launch.wait_ready(self.running(), url, SECRET, timeout=5)
        self.assert_no_secret(server)

    def test_probe_follows_no_redirect_and_no_proxy(self):
        elsewhere, elsewhere_url = self.serve('honest')
        redirecting, url = self.serve('redirect', redirect_to=elsewhere_url + '/api/ready')
        with patch.dict(os.environ, {'http_proxy': elsewhere_url, 'HTTP_PROXY': elsewhere_url, 'no_proxy': ''}):
            with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
                launch.wait_ready(self.running(), url, SECRET, timeout=.5)
        self.assertTrue(redirecting.seen)
        self.assertEqual([], elsewhere.seen)
        self.assert_no_secret(redirecting)


class LauncherSecretTests(unittest.TestCase):
    """Drive main() across a workspace switch with every process mocked."""

    def run_launcher(self, *flags, switches=1):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        webframe = home / 'webframe'
        webframe.write_text('')
        calls = []
        links = []
        events = []
        listeners = []
        switch_results = [launch.ROOT / 'demo'] * switches

        def popen(command, **kwargs):
            env = kwargs['env']
            calls.append((command, env, kwargs))
            process = unittest.mock.Mock()
            is_server = command[-1].endswith('server.py')
            events.append(('start', 'server' if is_server else 'window', process))
            if is_server:
                link = Path(env['SEEKNAL_STUDIO_CONTROL_FILE']).parent / 'access-url'
                links.append((link.read_text(), link.stat().st_mode & 0o777))
                # The handed-over descriptor is the launcher's live listening socket.
                descriptor = int(env[LISTEN_FD_ENV])
                with socket.socket(fileno=os.dup(descriptor)) as shared:
                    name = shared.getsockname()
                with socket.create_connection(name, timeout=2):
                    listening = True  # the kernel queues it in the shared backlog
                listeners.append((descriptor, kwargs['pass_fds'], name, listening))
            # The last window closes, which ends the launcher normally.
            is_last_window = command[0] == str(webframe.resolve()) and not switch_results
            process.poll.return_value = 0 if is_last_window else None
            return process

        def read_switch(_path, _nonce):
            if switch_results:
                return switch_results.pop()
            if '--browser' in flags:
                raise KeyboardInterrupt
            return None

        runtime = unittest.mock.Mock(python=Path(sys.executable))
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(('ATLAS_', 'SEEKNAL_STUDIO_'))
        }
        environment.update(HOME=str(home), SEEKNAL_STUDIO_UI_SECRET='inherited-must-not-be-used')
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(sys, 'argv', ['launch.py', '--project', str(launch.ROOT / 'demo'), '--port', '0',
                                           '--webframe', str(webframe), *flags]), \
                patch.object(launch, 'resolve_runtime', return_value=runtime), \
                patch.object(launch.subprocess, 'Popen', side_effect=popen), \
                patch.object(launch, 'wait_ready') as ready, \
                patch.object(launch, 'read_switch', side_effect=read_switch), \
                patch.object(launch, 'stop', side_effect=lambda process: events.append(('stop', None, process))), \
                patch.object(launch.signal, 'signal'), \
                patch.object(launch.time, 'sleep'), \
                patch.object(launch.webbrowser, 'open') as browser, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = launch.main()
        port = int(calls[0][1]['SEEKNAL_STUDIO_PORT'])
        logs = ''.join(path.read_text() for path in home.rglob('studio-*.log'))
        return {
            'result': result, 'calls': calls, 'links': links, 'ready': ready, 'browser': browser, 'port': port,
            'events': events, 'listeners': listeners, 'output': stdout.getvalue() + stderr.getvalue() + logs,
        }

    def test_same_secret_survives_backend_restart_and_never_reaches_output(self):
        run = self.run_launcher()
        self.assertEqual(0, run['result'])
        port, output = run['port'], run['output']
        servers = [env for command, env, _ in run['calls'] if command[-1].endswith('server.py')]
        windows = [(command, env) for command, env, _ in run['calls'] if not command[-1].endswith('server.py')]
        self.assertEqual((2, 2), (len(servers), len(windows)))
        secrets = {env['SEEKNAL_STUDIO_UI_SECRET'] for env in servers}
        self.assertEqual(1, len(secrets))
        secret = secrets.pop()
        self.assertGreaterEqual(len(secret), 40)
        self.assertNotEqual('inherited-must-not-be-used', secret)
        self.assertEqual([secret, secret], [call.args[2] for call in run['ready'].call_args_list])
        for command, env in windows:
            self.assertEqual(['-w', '1584x992', f'http://127.0.0.1:{port}'], command[1:])
            self.assertNotIn(secret, ' '.join(command))
            self.assertEqual(secret, env['WF_STUDIO_SESSION'])
            self.assertNotIn('SEEKNAL_STUDIO_UI_SECRET', env)
            self.assertNotIn(LISTEN_FD_ENV, env)
        link = f'http://127.0.0.1:{port}/?studio_session={secret}\n'
        self.assertEqual([(link, 0o600)] * 2, run['links'])
        self.assertIn('Access link file: ', output)
        self.assertIn(f'Seeknal Studio: http://127.0.0.1:{port}\n', output)
        self.assertNotIn(secret, output)

    def test_one_listening_socket_is_handed_to_every_incarnation(self):
        run = self.run_launcher()
        (first, first_fds, first_name, first_listening), (second, second_fds, second_name, second_listening) = run['listeners']
        self.assertEqual(first, second)
        self.assertEqual(((first,), (first,)), (first_fds, second_fds))
        self.assertEqual((('127.0.0.1', run['port']),) * 2, (first_name, second_name))
        self.assertTrue(first_listening and second_listening)
        # Released only when the launcher exits.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', run['port']))

    def test_previous_incarnation_stops_before_the_next_starts(self):
        events = self.run_launcher()['events']
        first_server = next(process for kind, role, process in events if kind == 'start' and role == 'server')
        starts = [index for index, (kind, role, _) in enumerate(events) if kind == 'start' and role == 'server']
        stopped = next(index for index, (kind, _, process) in enumerate(events) if kind == 'stop' and process is first_server)
        self.assertLess(starts[0], stopped)
        self.assertLess(stopped, starts[1])

    def test_browser_mode_opens_the_one_time_link_once_with_a_note(self):
        run = self.run_launcher('--browser')
        self.assertEqual(0, run['result'])
        calls = run['calls']
        self.assertEqual(2, len(calls))
        secret = calls[0][1]['SEEKNAL_STUDIO_UI_SECRET']
        self.assertEqual(secret, calls[1][1]['SEEKNAL_STUDIO_UI_SECRET'])
        run['browser'].assert_called_once_with(f'http://127.0.0.1:{run["port"]}/?studio_session={secret}')
        self.assertEqual(1, run['output'].count('--browser isolates Studio less on shared machines'))
        self.assertNotIn(secret, run['output'])


if __name__ == '__main__':
    unittest.main()
