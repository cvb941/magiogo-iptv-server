import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent


class GunicornConfigTests(unittest.TestCase):
    def test_configuration_rejects_process_local_state_being_forked_or_duplicated(self):
        spec = importlib.util.spec_from_file_location('gunicorn_config_test', ROOT / 'gunicorn.conf.py')
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        config.on_starting(SimpleNamespace(cfg=SimpleNamespace(workers=1, preload_app=False)))
        for workers, preload in ((2, False), (1, True)):
            with self.subTest(workers=workers, preload=preload), self.assertRaises(RuntimeError):
                config.on_starting(SimpleNamespace(cfg=SimpleNamespace(workers=workers, preload_app=preload)))


@unittest.skipUnless(os.name == 'posix' and importlib.util.find_spec('gunicorn'), 'Gunicorn requires POSIX')
class GunicornLifecycleTests(unittest.TestCase):
    def test_real_worker_serves_health_and_cancels_a_waiting_guide_on_sigterm(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'fixture_wsgi.py').write_text('''import os,time
from pathlib import Path
from magiogo import MagioGo

def waiting_channels(self):
    with self._api_condition:
        self._api_next_request=time.monotonic()+3600
    with Path('guide-started').open('a') as output:
        output.write(str(os.getpid())+'\\n')
    try:
        return self._get('http://127.0.0.1:1/never-requested')
    finally:
        Path('guide-stopped').write_text('stopped')

MagioGo.channels=waiting_channels
from app import app
''', encoding='utf-8')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            env = dict(os.environ, MAGIO_USERNAME='test', MAGIO_PASSWORD='test', PYTHONPATH=str(ROOT), GUNICORN_CMD_ARGS='')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with (path / 'server.log').open('w+') as logs:
                process = subprocess.Popen([
                    sys.executable, '-m', 'gunicorn', '--config', str(ROOT / 'gunicorn.conf.py'),
                    '--bind', f'127.0.0.1:{port}', 'fixture_wsgi:app',
                ], cwd=directory, env=env, stdout=logs, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    deadline = time.monotonic() + 12
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            logs.seek(0)
                            self.fail(logs.read())
                        try:
                            with opener.open(f'http://127.0.0.1:{port}/health', timeout=.5) as response:
                                if json.load(response) == {'status': 'ok'} and (path / 'guide-started').exists():
                                    break
                        except OSError:
                            pass
                        time.sleep(.05)
                    else:
                        self.fail('Gunicorn did not become ready')
                    with opener.open(f'http://127.0.0.1:{port}/status', timeout=1) as response:
                        status = json.load(response)
                    self.assertTrue(status['guide']['refreshing'])
                    self.assertEqual(status['api']['requests'], 0)
                    self.assertEqual(len((path / 'guide-started').read_text().splitlines()), 1)
                    started = time.monotonic()
                    process.terminate()
                    self.assertEqual(process.wait(timeout=5), 0)
                    self.assertLess(time.monotonic() - started, 4)
                    self.assertTrue((path / 'guide-stopped').exists())
                    self.assertTrue((path / 'storage/dash-sessions.json').exists())
                    logs.seek(0)
                    self.assertNotIn('Traceback', logs.read())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
