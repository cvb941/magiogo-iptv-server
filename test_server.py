import importlib
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
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
        pages = iter(({'success': True, 'items': [{'programs': []}] * 20},
                      {'success': True, 'items': []}, {'success': True, 'items': []}))
        session = MagicMock()
        session.__enter__.return_value = session
        self.client._api_background_after = 6

        def sleep(seconds):
            elapsed[0] += seconds

        def get(*args, **kwargs):
            requested_at.append(elapsed[0])
            response = MagicMock(status_code=200)
            response.json.return_value = next(pages)
            response.__enter__.return_value = response
            return response

        session.get.side_effect = get
        with patch.object(self.client, '_login'), patch.object(self.client, '_request', return_value=session), patch('magiogo.time.monotonic', side_effect=lambda: elapsed[0]), patch.object(self.client._api_condition, 'wait', side_effect=sleep):
            self.client.epg([], datetime(2026, 1, 1), datetime(2026, 1, 2))
        self.assertEqual(len(requested_at), 3)
        self.assertGreaterEqual(requested_at[0], 6)
        self.assertTrue(all(second - first >= 6 for first, second in zip(requested_at, requested_at[1:])))

    def test_playback_overtakes_waiting_guide_requests(self):
        waiting, playback, release = threading.Event(), threading.Event(), threading.Event()
        order = []
        session = MagicMock()
        session.__enter__.return_value = session
        wait = self.client._api_condition.wait
        self.client._api_next_request = time.monotonic() + .1
        self.client._api_background_after = self.client._api_next_request
        self.client._api_guide_interval = 2

        def get(url, **kwargs):
            order.append(url)
            if url == 'playback':
                playback.set()
                release.wait(3)
            response = MagicMock(status_code=200)
            response.__enter__.return_value = response
            response.json.return_value = {'success': True}
            return response

        def waiting_for_slot(timeout=None):
            waiting.set()
            return wait(timeout)

        session.get.side_effect = get
        with patch.object(self.client, '_request', return_value=session), patch.object(self.client._api_condition, 'wait', side_effect=waiting_for_slot), ThreadPoolExecutor(2) as pool:
            guide = pool.submit(self.client._get, 'epg', background=True)
            self.assertTrue(waiting.wait(1))
            stream = pool.submit(self.client._get, 'playback')
            try:
                self.assertTrue(playback.wait(1))
                self.assertEqual(order, ['playback'])
            finally:
                release.set()
            stream.result(2)
            guide.result(3)
        self.assertEqual(order, ['playback', 'epg'])

    def test_slow_guide_response_does_not_block_playback(self):
        entered, release = threading.Event(), threading.Event()
        session = MagicMock()
        session.__enter__.return_value = session
        self.client._api_background_after = 0

        def get(url, **kwargs):
            if url == 'epg':
                entered.set()
                release.wait(4)
            response = MagicMock(status_code=200)
            response.__enter__.return_value = response
            response.json.return_value = {'success': True}
            return response

        session.get.side_effect = get
        with patch.object(self.client, '_request', return_value=session), ThreadPoolExecutor(2) as pool:
            guide = pool.submit(self.client._get, 'epg', background=True)
            try:
                self.assertTrue(entered.wait(1))
                stream = pool.submit(self.client._get, 'playback')
                self.assertEqual(stream.result(2), {'success': True})
                self.assertFalse(guide.done())
            finally:
                release.set()
            guide.result(1)

    def test_rate_limit_cooldown_is_shared_with_other_requests(self):
        elapsed, requested_at = [0], []
        session = MagicMock()
        session.__enter__.return_value = session
        statuses = iter((429, 429, 429, 200))

        def get(url, **kwargs):
            requested_at.append(elapsed[0])
            response = MagicMock(status_code=next(statuses), headers={'Retry-After': '4'})
            response.__enter__.return_value = response
            response.json.return_value = {'success': True}
            if response.status_code == 429:
                response.raise_for_status.side_effect = requests.HTTPError('rate limited')
            return response

        session.get.side_effect = get
        with patch.object(self.client, '_request', return_value=session), patch('magiogo.time.monotonic', side_effect=lambda: elapsed[0]), patch.object(self.client._api_condition, 'wait', side_effect=lambda delay=None: elapsed.__setitem__(0, elapsed[0] + delay)):
            with self.assertRaises(NetConnectionError):
                self.client._get('playback')
            self.client._get('another-channel')
        self.assertEqual(requested_at, [0, 4, 8, 12])
        self.assertEqual(self.client._api_foreground, 0)

    def test_guide_honors_full_retry_after_and_reduces_its_request_rate(self):
        elapsed, requested_at = [0], []
        self.client._api_background_after = 6
        session = MagicMock()
        session.__enter__.return_value = session
        statuses = iter((429, 200, 200))

        def get(url, **kwargs):
            requested_at.append(elapsed[0])
            response = MagicMock(status_code=next(statuses), headers={'Retry-After': '60'})
            response.__enter__.return_value = response
            response.json.return_value = {'success': True}
            return response

        session.get.side_effect = get
        with patch.object(self.client, '_request', return_value=session), patch('magiogo.time.monotonic', side_effect=lambda: elapsed[0]), patch.object(self.client._api_condition, 'wait', side_effect=lambda delay=None: elapsed.__setitem__(0, elapsed[0] + delay)):
            self.client._get('epg', background=True)
            self.client._get('epg', background=True)
        self.assertEqual(requested_at, [6, 66, 78])
        self.assertEqual(self.client._api_guide_interval, 12)

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

    def test_transient_cdn_404_does_not_request_another_stream_url(self):
        missing, ready = MagicMock(status_code=404), MagicMock(status_code=200)
        missing.__enter__.return_value = missing
        ready.__enter__.return_value = ready
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', return_value={'url': 'https://cdn.test/high.mpd'}) as api, patch('magiogo.requests.get', side_effect=[missing, ready]), patch('magiogo.time.sleep'):
            stream = self.client.channel_stream_info('1', quality_override='p4')
        self.assertEqual(stream.url, 'https://cdn.test/high.mpd')
        self.assertEqual(api.call_count, 1)

    def test_confirmed_cdn_404_still_falls_back_to_medium_quality(self):
        missing = MagicMock(status_code=404)
        missing.__enter__.return_value = missing
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', side_effect=[{'url': 'https://cdn.test/high.mpd'}, {'url': 'https://cdn.test/medium.mpd'}]) as api, patch('magiogo.requests.get', return_value=missing), patch('magiogo.time.sleep'):
            stream = self.client.channel_stream_info('1', quality_override='p4')
        self.assertEqual(stream.url, 'https://cdn.test/medium.mpd')
        self.assertEqual(api.call_args.kwargs['params']['prof'], 'p2')

    def test_verified_fallback_survives_restart_and_expires_without_sliding(self):
        missing, ready = MagicMock(status_code=404), MagicMock(status_code=200)
        missing.__enter__.return_value = missing
        ready.__enter__.return_value = ready
        self.client._data.access_token = 'saved'
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', side_effect=[{'url': 'https://cdn.test/high.mpd'}, {'url': 'https://cdn.test/medium.mpd'}]), patch('magiogo.requests.get', side_effect=[missing, missing, ready]), patch('magiogo.time.sleep'):
            self.client.channel_stream_info('1', quality_override='p4')

        restarted = MagioGo(self.directory.name, 'test', 'test', quality='p4')
        with patch.object(restarted, '_get', return_value={'url': 'https://cdn.test/medium.mpd'}) as api, patch('magiogo.requests.get', return_value=ready):
            restarted.channel_stream_info('1')
            expires = next(iter(restarted._data.stream_qualities.values()))['expires']
            restarted.channel_stream_info('1')
        self.assertEqual([call.kwargs['params']['prof'] for call in api.call_args_list], ['p2', 'p2'])
        self.assertEqual(next(iter(restarted._data.stream_qualities.values()))['expires'], expires)
        self.assertEqual(restarted._data.access_token, 'saved')
        with patch('magiogo.time.time', return_value=expires + 1), patch.object(restarted, '_get', return_value={'url': 'https://cdn.test/high.mpd'}) as api, patch('magiogo.requests.get', return_value=ready):
            restarted.channel_stream_info('1')
        self.assertEqual(api.call_args.kwargs['params']['prof'], 'p4')
        self.assertEqual(restarted._data.stream_qualities, {})

    def test_failed_fallback_is_not_cached(self):
        missing = MagicMock(status_code=404)
        missing.__enter__.return_value = missing
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', return_value={'url': 'https://cdn.test/missing.mpd'}), patch('magiogo.requests.get', return_value=missing), patch('magiogo.time.sleep'):
            self.client.channel_stream_info('1', quality_override='p4')
        self.assertEqual(self.client._data.stream_qualities, {})

    def test_cached_fallback_is_scoped_to_channel_device_and_requested_quality(self):
        missing, ready = MagicMock(status_code=404), MagicMock(status_code=200)
        missing.__enter__.return_value = missing
        ready.__enter__.return_value = ready
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', return_value={'url': 'https://cdn.test/live.mpd'}), patch('magiogo.requests.get', side_effect=[missing, missing, ready]), patch('magiogo.time.sleep'):
            self.client.channel_stream_info('1', quality_override='p4')
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', return_value={'url': 'https://cdn.test/live.mpd'}) as api, patch('magiogo.requests.get', return_value=ready):
            self.client.channel_stream_info('2', quality_override='p4')
            self.client.channel_stream_info('1', quality_override='p5')
            self.client._deviceType = 'OTT_ANDROID'
            self.client.channel_stream_info('1', quality_override='p4')
        self.assertEqual([call.kwargs['params']['prof'] for call in api.call_args_list], ['p4', 'p5', 'p4'])

    def test_missing_cached_fallback_rechecks_requested_quality(self):
        missing, ready = MagicMock(status_code=404), MagicMock(status_code=200)
        missing.__enter__.return_value = missing
        ready.__enter__.return_value = ready
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', return_value={'url': 'https://cdn.test/live.mpd'}), patch('magiogo.requests.get', side_effect=[missing, missing, ready]), patch('magiogo.time.sleep'):
            self.client.channel_stream_info('1', quality_override='p4')
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', return_value={'url': 'https://cdn.test/live.mpd'}) as api, patch('magiogo.requests.get', side_effect=[missing, missing, ready]), patch('magiogo.time.sleep'):
            self.client.channel_stream_info('1', quality_override='p4')
        self.assertEqual([call.kwargs['params']['prof'] for call in api.call_args_list], ['p2', 'p4'])
        self.assertEqual(self.client._data.stream_qualities, {})
