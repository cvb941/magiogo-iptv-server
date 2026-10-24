import importlib
import copy
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

from magiogo import MagioGo, MagioGoException, NetConnectionError, UserInvalidException


class StartupTests(unittest.TestCase):
    def test_restart_reuses_fresh_guide_but_refreshes_stale_or_incomplete_cache(self):
        previous_directory = os.getcwd()
        previous_app = sys.modules.pop('app', None)
        try:
            for state in ('fresh', 'stale', 'incomplete'):
                with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                    os.chdir(directory)
                    Path('public').mkdir()
                    names = ['magioPlaylist.m3u8', 'magioGuide.xmltv', 'magioGuide.xmltv.gz']
                    for name in names:
                        path = Path('public') / name
                        path.write_bytes(b'cached')
                        if state == 'stale':
                            modified = time.time() - 13 * 3600
                            os.utime(path, (modified, modified))
                    if state == 'incomplete':
                        (Path('public') / names[-1]).unlink()
                    entered, release = threading.Event(), threading.Event()
                    client = Mock()
                    client.epg.return_value = {}

                    def channels():
                        entered.set()
                        release.wait(3)
                        return []

                    client.channels.side_effect = channels
                    module = None
                    try:
                        with patch.dict(os.environ, {'MAGIO_USERNAME': 'test', 'MAGIO_PASSWORD': 'test', 'MAGIO_GUIDE_REFRESH_HOURS': '12'}), patch('magiogo.MagioGo', return_value=client), patch('atexit.register'):
                            module = importlib.import_module('app')
                        if state == 'fresh':
                            client.channels.assert_not_called()
                            self.assertIsNotNone(module.last_refresh)
                            due = module.scheduler.get_job('guide-refresh').next_run_time.timestamp()
                            self.assertGreater(due - time.time(), 12 * 3600 - 5)
                            module.app.static_folder = str(Path('public').resolve())
                            with module.app.test_client().get('/magioGuide.xmltv') as response:
                                self.assertEqual(response.data, b'cached')
                        else:
                            self.assertTrue(entered.wait(1))
                    finally:
                        release.set()
                        if module is not None:
                            module.scheduler.shutdown()
                        sys.modules.pop('app', None)
                        os.chdir(previous_directory)
        finally:
            if previous_app is not None:
                sys.modules['app'] = previous_app
            os.chdir(previous_directory)

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
                    self.assertEqual(module.diagnostics.snapshot()['counters']['guide_failures'], 1)
                    self.assertNotIn('guide', module.diagnostics.snapshot()['running'])
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
        self.assertEqual(self.client.diagnostics.snapshot()['counters']['api_errors'], 2)

    def test_guide_requests_are_spaced_across_pages_and_days(self):
        elapsed = [0]
        requested_at = []
        pages = iter(({'success': True, 'totalCount': 152, 'items': [{'programs': []}] * 100},
                      {'success': True, 'totalCount': 152, 'items': [{'programs': []}] * 52},
                      {'success': True, 'totalCount': 0, 'items': []}))
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
        self.assertEqual([call.kwargs['params']['offset'] for call in session.get.call_args_list], [0, 100, 0])
        self.assertTrue(all(call.kwargs['params']['limit'] == '100' for call in session.get.call_args_list))

    def test_guide_pagination_handles_an_api_page_size_cap(self):
        responses = [{'items': [{'programs': []}] * 20, 'totalCount': 25},
                     {'items': [{'programs': []}] * 5, 'totalCount': 25}]
        with patch.object(self.client, '_login'), patch.object(self.client, '_get', side_effect=responses) as api:
            self.client.epg([], datetime(2026, 1, 1), datetime(2026, 1, 1))
        self.assertEqual([call.kwargs['params']['offset'] for call in api.call_args_list], [0, 20])

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
        counters = self.client.diagnostics.snapshot()['counters']
        self.assertEqual(counters['api_requests'], 4)
        self.assertEqual(counters['api_errors'], 3)
        self.assertEqual(counters['api_rate_limits'], 3)

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
            return {'success': True, 'token': {'accessToken': 'saved', 'refreshToken': 'refresh', 'type': 'Bearer', 'expiresIn': int((time.time() + 3600) * 1000)}}

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
        self.client._data.refresh_token = 'saved-refresh'
        self.client._data.type = 'Bearer'
        self.client._data.expires_in = int((time.time() + 86400) * 1000)
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


class MagioAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = MagioGo(self.directory.name, 'test', 'test')
        self.client._data.access_token = 'old-access'
        self.client._data.refresh_token = 'old-refresh'
        self.client._data.type = 'Bearer'
        self.client._data.expires_in = int((time.time() - 60) * 1000)
        self.client._data.stream_qualities = {'channel': {'quality': 'p2', 'expires': time.time() + 3600}}
        self.client._data.other_metadata = {'keep': True}
        self.client._store_session(self.client._data)
        self.path = Path(self.client._storage_file)
        self.previous_bytes = self.path.read_bytes()
        self.previous_data = copy.deepcopy(self.client._data.__dict__)
        self.responses = []
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session.post.side_effect = lambda *args, **kwargs: self.reply(self.responses.pop(0))
        transport = patch.object(self.client, '_request', return_value=self.session)
        transport.start()
        self.addCleanup(transport.stop)
        slot = patch.object(self.client, '_api_slot', side_effect=lambda *args, **kwargs: nullcontext())
        slot.start()
        self.addCleanup(slot.stop)

    @staticmethod
    def reply(action):
        if isinstance(action, Exception):
            raise action
        status, payload = action if isinstance(action, tuple) else (200, action)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        response._content_consumed = True
        response.url = 'https://api.test/auth'
        return response

    @staticmethod
    def authenticated():
        return {'success': True, 'token': {'accessToken': 'new-access', 'refreshToken': 'new-refresh', 'type': 'Bearer', 'expiresIn': int((time.time() + 3600) * 1000)}}

    @staticmethod
    def anonymous():
        return {'success': True, 'token': {'accessToken': 'anonymous-access', 'type': 'Temporary'}}

    def assert_previous_session(self):
        self.assertEqual(self.path.read_bytes(), self.previous_bytes)
        self.assertEqual(self.client._data.__dict__, self.previous_data)
        self.assertEqual(self.client._auth_headers()['Authorization'], 'Bearer old-access')
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_refresh_commits_once_and_preserves_quality_cache_and_other_metadata(self):
        self.responses = [self.authenticated()]
        with patch.object(self.client, '_store_session', wraps=self.client._store_session) as store:
            self.client._login()
        store.assert_called_once()
        self.assertEqual(self.session.post.call_args.args[0].rsplit('/', 1)[-1], 'tokens')
        self.assertEqual(self.client._data.access_token, 'new-access')
        self.assertEqual(self.client._data.refresh_token, 'new-refresh')
        self.assertEqual(self.client._data.stream_qualities, self.previous_data['stream_qualities'])
        self.assertEqual(self.client._data.other_metadata, {'keep': True})
        self.assertEqual(json.loads(self.path.read_text()), self.client._data.__dict__)
        self.assertGreater(self.client._data.authenticated_at, time.time() - 10)
        self.assertEqual(self.client.diagnostics.snapshot()['counters']['auth_successes'], 1)
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_refresh_transport_or_rate_limit_does_not_start_a_new_login(self):
        for action in (requests.ReadTimeout('timeout'), (429, {}), (503, {}), {'success': False, 'errorCode': 'RATE_LIMIT_EXCEEDED'}):
            with self.subTest(action=type(action).__name__):
                self.responses = [action]
                self.session.post.reset_mock()
                with self.assertRaises((NetConnectionError, MagioGoException)):
                    self.client._login()
                self.assertEqual(self.session.post.call_count, 1)
                self.assert_previous_session()

    def test_rejected_refresh_uses_temporary_token_and_saves_only_authenticated_result(self):
        self.responses = [(403, {'success': False, 'errorCode': 'Forbidden'}), self.anonymous(), self.authenticated()]
        with patch.object(self.client, '_store_session', wraps=self.client._store_session) as store:
            self.client._login()
        store.assert_called_once()
        self.assertEqual([call.args[0].rsplit('/', 1)[-1] for call in self.session.post.call_args_list], ['tokens', 'init', 'login'])
        self.assertEqual(self.session.post.call_args.kwargs['headers']['Authorization'], 'Temporary anonymous-access')
        self.assertEqual(self.client._auth_headers()['Authorization'], 'Bearer new-access')
        self.assertEqual(json.loads(self.path.read_text())['stream_qualities'], self.previous_data['stream_qualities'])

    def test_refresh_rate_limit_honors_retry_after_without_registering_a_device(self):
        response = self.reply((429, {}))
        response.headers['Retry-After'] = '60'
        self.session.post.side_effect = lambda *args, **kwargs: response
        with patch('magiogo.time.monotonic', return_value=100), self.assertRaises(NetConnectionError):
            self.client._login()
        self.assertEqual(self.client._api_next_request, 160)
        self.assertEqual(self.session.post.call_count, 1)
        self.assert_previous_session()

    def test_rejected_refresh_in_json_still_recovers_with_a_full_login(self):
        self.responses = [{'success': False, 'errorCode': 'INVALID_REFRESH_TOKEN'}, self.anonymous(), self.authenticated()]
        self.client._login()
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(self.client._data.access_token, 'new-access')
        self.assertEqual(self.client._data.stream_qualities, self.previous_data['stream_qualities'])

    def test_failed_login_after_initialization_preserves_old_memory_and_disk(self):
        for failure in (requests.ReadTimeout('login timeout'), {'success': False, 'errorCode': 'INVALID_CREDENTIALS'}):
            with self.subTest(failure=type(failure).__name__):
                self.responses = [(401, {}), self.anonymous(), failure]
                with patch.object(self.client, '_store_session', wraps=self.client._store_session) as store:
                    with self.assertRaises((NetConnectionError, UserInvalidException)):
                        self.client._login()
                store.assert_not_called()
                self.assert_previous_session()

    def test_failed_first_login_does_not_create_an_anonymous_session_file(self):
        self.path.unlink()
        self.client._data.access_token = ''
        self.client._data.refresh_token = ''
        previous = copy.deepcopy(self.client._data.__dict__)
        self.responses = [self.anonymous(), {'success': False, 'errorCode': 'INVALID_CREDENTIALS'}]
        with self.assertRaises(UserInvalidException):
            self.client._login()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.client._data.__dict__, previous)

    def test_incomplete_or_expired_refresh_response_is_not_committed(self):
        for field, value in (('accessToken', ''), ('refreshToken', None), ('type', None), ('expiresIn', 0), ('expiresIn', True), ('expiresIn', 'later'), ('expiresIn', float('inf'))):
            with self.subTest(field=field, value=value):
                payload = self.authenticated()
                payload['token'][field] = value
                self.responses = [payload]
                with self.assertRaises(MagioGoException):
                    self.client._login()
                self.assert_previous_session()
        self.responses = [{'success': True}]
        with self.assertRaises(MagioGoException):
            self.client._login()
        self.assert_previous_session()

    def test_invalid_initialization_response_does_not_send_credentials_or_change_session(self):
        self.responses = [(403, {}), {'success': True, 'token': {}}]
        with self.assertRaises(MagioGoException):
            self.client._login()
        self.assertEqual([call.args[0].rsplit('/', 1)[-1] for call in self.session.post.call_args_list], ['tokens', 'init'])
        self.assert_previous_session()

    def test_failed_atomic_write_keeps_old_session_and_removes_temporary_file(self):
        self.responses = [self.authenticated()]
        with patch('client.os.replace', side_effect=OSError('disk failed')), self.assertRaises(OSError):
            self.client._login()
        self.assert_previous_session()

    def test_parallel_players_share_one_refresh_and_do_not_see_partial_tokens(self):
        entered, release = threading.Event(), threading.Event()

        def refresh(*args, **kwargs):
            entered.set()
            release.wait(3)
            return self.reply(self.authenticated())

        self.session.post.side_effect = refresh
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.client._login)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.client._login)
                self.assert_previous_session()
            finally:
                release.set()
            first.result(2)
            second.result(2)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.client._data.access_token, 'new-access')

    def test_parallel_players_do_not_publish_anonymous_token_during_relogin(self):
        entered, release = threading.Event(), threading.Event()
        self.responses = [(403, {}), self.anonymous(), self.authenticated()]

        def post(url, **kwargs):
            if url.endswith('/login'):
                entered.set()
                release.wait(3)
            return self.reply(self.responses.pop(0))

        self.session.post.side_effect = post
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.client._login)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.client._login)
                self.assert_previous_session()
            finally:
                release.set()
            first.result(2)
            second.result(2)
        self.assertEqual(self.session.post.call_count, 3)
        self.assertEqual(self.client._data.access_token, 'new-access')
