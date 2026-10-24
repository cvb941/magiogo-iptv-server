import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(os.name == 'posix' and shutil.which('ffmpeg') and importlib.util.find_spec('gunicorn'),
                     'Requires POSIX, Gunicorn and FFmpeg (available in the Docker test target)')
class StreamingTests(unittest.TestCase):
    def test_real_ffmpeg_decodes_before_and_after_a_gunicorn_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            media = path / 'media'
            media.mkdir()
            subprocess.run([
                'ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=25',
                '-f', 'lavfi', '-i', 'sine=sample_rate=48000', '-t', '12', '-c:v', 'libx264',
                '-preset', 'ultrafast', '-g', '25', '-threads', '1', '-c:a', 'aac',
                '-f', 'dash', '-seg_duration', '1', str(media / 'manifest.mpd'),
            ], check=True, capture_output=True, timeout=20)
            requests, denied = [], []

            class CDN(SimpleHTTPRequestHandler):
                def do_GET(self):
                    requests.append(time.monotonic())
                    if self.path == '/start.mpd':
                        self.send_response(302)
                        self.send_header('Location', '/media/manifest.mpd')
                        self.end_headers()
                    elif self.path != '/media/manifest.mpd' and self.headers.get('Cookie') != 'CDNAuth=fixture':
                        denied.append(self.path)
                        self.send_error(403)
                    else:
                        super().do_GET()

                def end_headers(self):
                    if self.path == '/media/manifest.mpd':
                        self.send_header('Set-Cookie', 'CDNAuth=fixture; Path=/')
                    super().end_headers()

                def log_message(self, *_):
                    pass

            cdn = ThreadingHTTPServer(('127.0.0.1', 0), partial(CDN, directory=directory))
            cdn_thread = threading.Thread(target=cdn.serve_forever, daemon=True)
            cdn_thread.start()
            (path / 'fixture_wsgi.py').write_text('''import os
from pathlib import Path
from types import SimpleNamespace
from magiogo import MagioGo

def no_api(*args, **kwargs):
    raise AssertionError('Integration tests must not contact Magio')

def stream(self, channel):
    with Path('sdk-calls').open('a') as output:
        output.write('stream\\n')
    return SimpleNamespace(url=os.environ['TEST_CDN_URL']+'/start.mpd', manifest_type='mpd', user_agent='fixture')

MagioGo._request = no_api
MagioGo.channels = lambda self: []
MagioGo.epg = lambda self, *args: {}
MagioGo.channel_stream_info = stream
from app import app
''', encoding='utf-8')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            env = dict(os.environ, MAGIO_USERNAME='test', MAGIO_PASSWORD='test', PYTHONPATH=str(ROOT),
                       GUNICORN_CMD_ARGS='', MAGIO_SERVER_PUBLIC_URL=url,
                       TEST_CDN_URL=f'http://127.0.0.1:{cdn.server_port}')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            backend = None
            with (path / 'backend.log').open('w+') as logs:
                def start_backend():
                    process = subprocess.Popen([
                        sys.executable, '-m', 'gunicorn', '--config', str(ROOT / 'gunicorn.conf.py'),
                        '--bind', f'127.0.0.1:{port}', 'fixture_wsgi:app',
                    ], cwd=directory, env=env, stdout=logs, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        deadline = time.monotonic() + 8
                        while process.poll() is None and time.monotonic() < deadline:
                            try:
                                with opener.open(url + '/health', timeout=.3) as response:
                                    if json.load(response) == {'status': 'ok'}:
                                        return process
                            except OSError:
                                pass
                            time.sleep(.05)
                        logs.seek(0)
                        self.fail(logs.read())
                    except BaseException:
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
                        raise

                try:
                    backend = start_backend()
                    bases = []
                    for iteration in range(2):
                        if iteration:
                            backend.terminate()
                            self.assertEqual(backend.wait(timeout=5), 0)
                            backend = start_backend()
                            recovered = time.monotonic()
                            with opener.open(bases[0] + 'init-stream0.m4s', timeout=2) as response:
                                self.assertGreater(len(response.read()), 0)
                        with opener.open(url + '/channel/fixture', timeout=2) as response:
                            bases.append(ET.fromstring(response.read()).find('{*}BaseURL').text)
                        result = subprocess.run([
                            'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
                            '-i', url + '/channel/fixture', '-map', '0:v:0', '-map', '0:a:0',
                            '-threads', '1', '-fps_mode:v', 'passthrough', '-t', '2', '-f', 'framecrc', 'pipe:1',
                        ], capture_output=True, timeout=12)
                        self.assertEqual(result.returncode, 0, result.stderr.decode())
                        self.assertFalse(result.stderr)
                        self.assertGreaterEqual(sum(line.startswith(b'0,') for line in result.stdout.splitlines()), 45)
                        self.assertGreaterEqual(sum(line.startswith(b'1,') for line in result.stdout.splitlines()), 85)
                    self.assertEqual(bases[0], bases[1])
                    with opener.open(url + '/status', timeout=1) as response:
                        status = json.load(response)
                    self.assertEqual(status['sessions']['restored'], 1)
                    self.assertEqual(status['api']['requests'], 0)
                    self.assertTrue(any(requested > recovered for requested in requests))
                    self.assertFalse(denied, 'Cookies were lost while following redirects or restarting')
                    self.assertEqual((path / 'sdk-calls').read_text().splitlines(), ['stream'])
                finally:
                    if backend is not None and backend.poll() is None:
                        backend.terminate()
                        try:
                            backend.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(backend.pid, signal.SIGKILL)
                            backend.wait(timeout=3)
                    cdn.shutdown()
                    cdn.server_close()
                    cdn_thread.join(2)
