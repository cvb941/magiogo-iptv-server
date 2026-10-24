import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import dash_proxy
from diagnostics import Diagnostics, utc_time
from magiogo import MagioGo, MagioGoSessionData


@contextmanager
def diagnostic_app():
    previous_directory = os.getcwd()
    previous_app = sys.modules.pop('app', None)
    module = None
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            Path('public').mkdir()
            for name in ('magioPlaylist.m3u8', 'magioGuide.xmltv', 'magioGuide.xmltv.gz'):
                path = Path('public') / name
                path.write_bytes(b'cached')
                modified = time.time() - 60
                os.utime(path, (modified, modified))
            client = MagioGo('storage', 'private-account', 'private-password')
            stored = MagioGoSessionData()
            stored.access_token, stored.refresh_token, stored.type = 'private-access-token', 'private-refresh-token', 'Bearer'
            stored.expires_in = (time.time() + 3600) * 1000
            stored.authenticated_at = time.time() - 30
            client._store_session(stored)
            with patch.dict(os.environ, {'MAGIO_USERNAME': 'private-account', 'MAGIO_PASSWORD': 'private-password', 'MAGIO_GUIDE_REFRESH_HOURS': '12'}), patch('magiogo.MagioGo', return_value=client), patch('atexit.register'), patch.object(dash_proxy, 'SEGMENT_SESSIONS', {}), patch.object(dash_proxy, 'CHANNEL_SESSIONS', {}):
                module = importlib.import_module('app')
                yield module
        finally:
            if module is not None:
                if module.scheduler.running:
                    module.scheduler.shutdown()
            sys.modules.pop('app', None)
            if previous_app is not None:
                sys.modules['app'] = previous_app
            os.chdir(previous_directory)


class DiagnosticsTests(unittest.TestCase):
    def test_parallel_recording_does_not_lose_events(self):
        metrics = Diagnostics()

        def record(_):
            for _ in range(200):
                metrics.record('cdn_requests')

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(record, range(20)))
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['counters']['cdn_requests'], 4000)
        snapshot['counters']['cdn_requests'] = -1
        snapshot['last']['cdn_requests']['at'] = 'changed'
        self.assertEqual(metrics.snapshot()['counters']['cdn_requests'], 4000)
        self.assertNotEqual(metrics.snapshot()['last']['cdn_requests']['at'], 'changed')

    def test_operation_reports_running_success_and_failure_without_exception_text(self):
        metrics = Diagnostics()
        with metrics.operation('auth'):
            self.assertEqual(metrics.snapshot()['running'], ['auth'])
        with self.assertRaises(RuntimeError):
            with metrics.operation('guide'):
                raise RuntimeError('secret-cookie https://private.test/?token=private-token')
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot['running'], [])
        self.assertEqual(snapshot['counters']['auth_successes'], 1)
        self.assertEqual(snapshot['counters']['guide_failures'], 1)
        self.assertEqual(snapshot['last']['guide_failures']['error_type'], 'RuntimeError')
        self.assertNotIn('private-token', json.dumps(snapshot))
        self.assertNotIn('secret-cookie', json.dumps(snapshot))

    def test_expected_shutdown_is_not_reported_as_a_failed_guide_refresh(self):
        metrics = Diagnostics()
        with self.assertRaises(CancelledError):
            with metrics.operation('guide'):
                raise CancelledError()
        self.assertEqual(metrics.snapshot()['counters']['guide_failures'], 0)
        self.assertEqual(metrics.snapshot()['running'], [])

    def test_shutdown_flushes_cache_and_stops_scheduler_once(self):
        with diagnostic_app() as module:
            with patch.object(module.magio, 'stop', wraps=module.magio.stop) as stop:
                module.shutdown()
                module.shutdown()
            stop.assert_called_once()
            self.assertFalse(module.scheduler.running)
            self.assertTrue(Path('storage/dash-sessions.json').exists())

    def test_health_and_status_are_local_and_do_not_change_api_counters(self):
        with diagnostic_app() as module:
            client = module.app.test_client()
            before = module.diagnostics.snapshot()['counters']
            with patch.object(module.magio, '_login', side_effect=AssertionError('unexpected login')) as login, patch.object(module.magio, '_request', side_effect=AssertionError('unexpected upstream request')) as request:
                for _ in range(10):
                    health = client.get('/health')
                    status = client.get('/status')
                    self.assertEqual(health.json, {'status': 'ok'})
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(status.status_code, 200)
                    self.assertEqual(status.headers['Cache-Control'], 'no-store')
                login.assert_not_called()
                request.assert_not_called()
            self.assertEqual(module.diagnostics.snapshot()['counters'], before)
            self.assertEqual(module.magio._data.access_token, '')
            self.assertTrue(status.json['authentication']['token_unexpired'])
            self.assertIsNotNone(status.json['authentication']['last_success'])
            self.assertTrue(status.json['guide']['available'])
            self.assertGreaterEqual(status.json['guide']['age_seconds'], 60)
            self.assertFalse(status.json['guide']['stale'])

    def test_status_shows_upstream_failures_without_making_docker_unhealthy(self):
        with diagnostic_app() as module:
            module.diagnostics.record('api_rate_limits', status=429)
            module.diagnostics.record('api_errors', status=429)
            module.diagnostics.record('cdn_errors', status=503)
            with self.assertRaises(OSError):
                with module.diagnostics.operation('guide'):
                    self.assertTrue(module.app.test_client().get('/status').json['guide']['refreshing'])
                    raise OSError('private upstream URL')
            result = module.app.test_client().get('/status').json
            self.assertEqual(result['api']['rate_limits'], 1)
            self.assertEqual(result['api']['last_error']['http_status'], 429)
            self.assertEqual(result['cdn']['last_error']['http_status'], 503)
            self.assertEqual(result['guide']['failures'], 1)
            self.assertFalse(result['guide']['refreshing'])
            self.assertEqual(result['guide']['last_failure']['error_type'], 'OSError')
            self.assertEqual(module.app.test_client().get('/health').status_code, 200)

    def test_status_remains_available_while_authentication_holds_its_lock(self):
        with diagnostic_app() as module, ThreadPoolExecutor(1) as pool:
            def status():
                with module.app.test_client() as client:
                    return client.get('/status').json

            with module.magio._auth_lock, module.diagnostics.operation('auth'):
                self.assertTrue(pool.submit(status).result(1)['authentication']['refreshing'])

    def test_status_excludes_credentials_cookies_and_signed_stream_urls(self):
        with diagnostic_app() as module:
            sid = 'private-session-id'
            dash_proxy.SEGMENT_SESSIONS[sid] = {
                'expires': time.monotonic() + 1800,
                'manifest_url': 'https://private-cdn.test/live.mpd?signature=private-signature',
                'cookies': {'private-cookie': 'private-cookie-value'},
            }
            dash_proxy.CHANNEL_SESSIONS[('private-channel', 'http://proxy.test')] = sid
            response = module.app.test_client().get('/status')
            self.assertEqual(response.json['sessions']['cached_channels'], 1)
            self.assertEqual(response.json['sessions']['valid_sessions'], 1)
            for secret in ('private-account', 'private-password', 'private-access-token', 'private-refresh-token', 'private-session-id', 'private-signature', 'private-cookie-value'):
                self.assertNotIn(secret, response.get_data(as_text=True))

    def test_missing_guide_and_corrupt_session_are_reported_without_changing_files(self):
        with diagnostic_app() as module:
            Path(module.magio._storage_file).write_text('{broken')
            Path('public/magioGuide.xmltv.gz').unlink()
            status = module.app.test_client().get('/status').json
            self.assertFalse(status['authentication']['token_unexpired'])
            self.assertFalse(status['guide']['available'])
            self.assertTrue(status['guide']['stale'])
            self.assertEqual(Path(module.magio._storage_file).read_text(), '{broken')
            self.assertEqual(module.app.test_client().get('/health').status_code, 200)

    def test_invalid_authentication_timestamps_do_not_break_status(self):
        with diagnostic_app() as module:
            path = Path(module.magio._storage_file)
            data = json.loads(path.read_text())
            data['expires_in'] = 10 ** 400
            data['authenticated_at'] = 'unknown'
            path.write_text(json.dumps(data))
            response = module.app.test_client().get('/status')
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json['authentication']['expires_at'])
            self.assertIsNone(response.json['authentication']['last_success'])
        self.assertIsNone(utc_time(float('nan')))
