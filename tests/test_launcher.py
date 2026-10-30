import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

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
            launch.wait_ready(process, 'http://127.0.0.1:1', timeout=.2)

    def test_readiness_requires_http_success(self):
        process = unittest.mock.Mock()
        process.poll.return_value = None
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 200
        with patch.object(launch.urllib.request, 'urlopen', return_value=response) as request:
            launch.wait_ready(process, 'http://127.0.0.1:18766', timeout=1)
        self.assertEqual(request.call_args.args[0], 'http://127.0.0.1:18766/api/state')


if __name__ == '__main__':
    unittest.main()
