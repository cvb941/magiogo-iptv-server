import importlib
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

from magiogo import MagioGo, NetConnectionError


class StartupTests(unittest.TestCase):
    def test_http_and_streaming_are_available_while_initial_refresh_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        client = Mock()

        def channels():
            entered.set()
            release.wait(5)
            return []

        client.channels.side_effect = channels
        client.epg.return_value = {}
        client.channel_stream_info.return_value = Mock(manifest_type='m3u', url='http://cdn.test/live.m3u8')
        previous_directory = os.getcwd()
        previous_app = sys.modules.pop('app', None)
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            module = None
            try:
                with patch.dict(os.environ, {'MAGIO_USERNAME': 'test', 'MAGIO_PASSWORD': 'test'}), patch('magiogo.MagioGo', return_value=client), patch('atexit.register'):
                    started = time.monotonic()
                    module = importlib.import_module('app')
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertTrue(entered.wait(1))
                    http = module.app.test_client()
                    self.assertEqual(http.get('/').status_code, 200)
                    self.assertEqual(http.get('/channel/1').status_code, 303)
                    self.assertFalse(release.is_set())
                    release.set()
                    deadline = time.monotonic() + 2
                    while module.last_refresh is None and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertIsNotNone(module.last_refresh)
                    old_guide = Path('public/magioGuide.xmltv').read_bytes()
                    with patch.object(module.xmltv, 'Writer') as writer, self.assertLogs(module.app.logger, level='ERROR'):
                        writer.return_value.write.side_effect = OSError('write failed')
                        module.refresh()
                    self.assertEqual(Path('public/magioGuide.xmltv').read_bytes(), old_guide)
                    retry = module.scheduler.get_job('guide-refresh').next_run_time
                    self.assertLess((retry - module.datetime.datetime.now(module.datetime.timezone.utc)).total_seconds(), 301)
            finally:
                release.set()
                if module is not None:
                    module.scheduler.shutdown()
                sys.modules.pop('app', None)
                if previous_app is not None:
                    sys.modules['app'] = previous_app
                os.chdir(previous_directory)


class MagioRequestTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = MagioGo(self.directory.name, 'test', 'test')

    def test_api_timeouts_are_bounded_and_reported_as_network_errors(self):
        for method in ('get', 'post'):
            with self.subTest(method=method):
                session = MagicMock()
                getattr(session, method).side_effect = requests.ReadTimeout('upstream stalled')
                with patch.object(self.client, '_request', return_value=session):
                    session.__enter__.return_value = session
                    with self.assertRaises(NetConnectionError):
                        getattr(self.client, '_' + method)('https://api.test')
                self.assertEqual(getattr(session, method).call_args.kwargs['timeout'], (5, 15))
                session.__exit__.assert_called()

    def test_guide_requests_are_spaced_across_pages_and_days(self):
        elapsed = [0]
        requested_at = []
        pages = iter(({'items': [{'programs': []}] * 20}, {'items': []}, {'items': []}))

        def sleep(seconds):
            elapsed[0] += seconds

        def get(*args, **kwargs):
            requested_at.append(elapsed[0])
            return next(pages)

        with patch.object(self.client, '_login'), patch.object(self.client, '_get', side_effect=get), patch('magiogo.time.sleep', side_effect=sleep):
            self.client.epg([], datetime(2026, 1, 1), datetime(2026, 1, 2))
        self.assertEqual(len(requested_at), 3)
        self.assertGreaterEqual(requested_at[0], 2)
        self.assertTrue(all(second - first >= 2 for first, second in zip(requested_at, requested_at[1:])))

    def test_simultaneous_login_does_not_register_two_devices(self):
        entered, release = threading.Event(), threading.Event()

        def login():
            entered.set()
            release.wait(2)
            self.client._data.access_token = 'saved'

        with patch.object(self.client, '_load_session'), patch.object(self.client, '_init_and_login', side_effect=login) as init:
            threads = [threading.Thread(target=self.client._login) for _ in range(2)]
            try:
                threads[0].start()
                self.assertTrue(entered.wait(1))
                threads[1].start()
            finally:
                release.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(2)
            self.assertEqual(init.call_count, 1)

    def test_quality_probe_does_not_download_the_manifest_body(self):
        response = MagicMock(status_code=200)
        response.__enter__.return_value = response
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', return_value={'url': 'https://cdn.test/live.mpd'}), patch('magiogo.requests.get', return_value=response) as get:
            stream = self.client.channel_stream_info('1', quality_override='p4')
        self.assertEqual(stream.url, 'https://cdn.test/live.mpd')
        self.assertTrue(get.call_args.kwargs['stream'])
        self.assertEqual(get.call_args.kwargs['timeout'], (5, 15))
        response.__exit__.assert_called()
