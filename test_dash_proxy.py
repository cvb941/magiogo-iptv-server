import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urljoin, urlsplit

import requests
from flask import Flask, redirect

import dash_proxy as proxy


class DashProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class CDN(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append((self.path, self.headers))
                if self.path == '/start.mpd':
                    self.send_response(302)
                    self.send_header('Location', '/live/manifest.mpd')
                    self.end_headers()
                    return
                if self.path == '/live/manifest.mpd':
                    status, body = 200, cls.manifest.encode()
                    headers = {'Content-Type': 'application/dash+xml', 'Set-Cookie': 'CDNAuth=ok; Path=/'}
                elif self.headers.get('Cookie') != 'CDNAuth=ok':
                    status, body, headers = 403, b'', {}
                elif self.headers.get('Range') == 'bytes=2-5':
                    status, body = 206, b'2345'
                    headers = {'Content-Type': 'video/mp4', 'Content-Range': 'bytes 2-5/10', 'Accept-Ranges': 'bytes', 'ETag': '"segment"'}
                else:
                    status, body, headers = 200, b'0123456789', {'Content-Type': 'video/mp4'}
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), CDN)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.cdn = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        type(self).requests = []
        type(self).manifest = '<MPD><Period><AdaptationSet><SegmentTemplate media="fragment.m4s"/></AdaptationSet></Period></MPD>'
        proxy.SEGMENT_SESSIONS.clear()
        proxy.CHANNEL_SESSIONS.clear()
        proxy.CHANNEL_LOCKS.clear()
        self.now = 1000
        clock = patch.object(proxy, 'time', SimpleNamespace(monotonic=lambda: self.now))
        clock.start()
        self.addCleanup(clock.stop)
        self.stream = SimpleNamespace(url=self.cdn + '/start.mpd', manifest_type='mpd', user_agent='Magio review')
        self.stream_info = Mock(side_effect=lambda: self.stream)
        self.app = Flask(__name__)
        self.app.register_blueprint(proxy.dash_proxy)
        self.app.add_url_rule('/channel/<channel>', view_func=lambda channel: proxy.proxy_manifest(self.stream_info, 'http://proxy.test/', channel))
        self.app.register_error_handler(404, lambda error: redirect('/'))
        self.client = self.app.test_client()

    def manifest_root(self):
        response = self.client.get('/channel/1')
        self.assertEqual(response.status_code, 200)
        return ET.fromstring(response.data)

    def segment_path(self):
        root = self.manifest_root()
        base = root.find('BaseURL').text
        media = root.find('Period/AdaptationSet/SegmentTemplate').get('media')
        return urlsplit(urljoin(base, media)).path

    def test_relative_segment_uses_final_manifest_url_and_cookies(self):
        path = self.segment_path()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b'0123456789')
        upstream_path, headers = self.requests[-1]
        self.assertEqual(upstream_path, '/live/fragment.m4s')
        self.assertEqual(headers['Cookie'], 'CDNAuth=ok')
        self.assertEqual(headers['User-Agent'], 'Magio review')

    def test_byte_ranges_and_response_headers_are_preserved(self):
        response = self.client.get(self.segment_path(), headers={'Range': 'bytes=2-5', 'If-Range': '"segment"'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b'2345')
        self.assertEqual(response.headers['Content-Range'], 'bytes 2-5/10')
        self.assertEqual(response.headers['Content-Length'], '4')
        self.assertEqual(response.headers['Accept-Ranges'], 'bytes')
        self.assertEqual(response.headers['ETag'], '"segment"')
        self.assertEqual(self.requests[-1][1]['If-Range'], '"segment"')

    def test_live_manifest_refresh_keeps_session_and_reuses_stream_url(self):
        first = self.client.get('/channel/1').data
        self.assertEqual(self.client.get('/channel/1').data, first)
        self.assertEqual(len(self.requests), 2)
        self.now += 4
        type(self).manifest = '<MPD publishTime="updated"><Period/></MPD>'
        updated = self.client.get('/channel/1').data
        self.assertNotEqual(first, updated)
        self.assertEqual(ET.fromstring(first).find('BaseURL').text, ET.fromstring(updated).find('BaseURL').text)
        self.assertEqual(self.stream_info.call_count, 1)
        self.assertEqual(self.requests[-1][0], '/live/manifest.mpd')
        self.assertEqual(self.requests[-1][1]['Cookie'], 'CDNAuth=ok')

    def test_simultaneous_players_share_one_channel_session(self):
        def open_channel(_):
            with self.app.test_client() as client:
                return client.get('/channel/1').data

        with ThreadPoolExecutor(max_workers=2) as pool:
            manifests = list(pool.map(open_channel, range(2)))
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual(self.stream_info.call_count, 1)

    def test_failed_cached_manifest_refresh_requests_a_fresh_stream_url(self):
        first = self.manifest_root().find('BaseURL').text
        self.now += 4
        with patch.object(proxy.requests.Session, 'get', side_effect=requests.ConnectionError):
            self.assertEqual(self.client.get('/channel/1').status_code, 502)
        self.assertEqual(self.stream_info.call_count, 2)
        recovered = self.manifest_root().find('BaseURL').text
        self.assertNotEqual(first, recovered)

    def test_active_session_survives_cleanup_after_an_hour(self):
        path = self.segment_path()
        for _ in range(3):
            self.now += 1700
            response = self.client.get(path)
            self.assertEqual(response.data, b'0123456789')
            self.assertEqual(self.client.get('/channel/2').status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_expired_and_unknown_sessions_return_errors_without_redirects(self):
        path = self.segment_path()
        self.now += 1801
        response = self.client.get(path)
        self.assertEqual(response.status_code, 410)
        self.assertNotIn('Location', response.headers)
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_cleanup_removes_idle_sessions_only(self):
        self.segment_path()
        self.now += 1801
        self.segment_path()
        self.assertEqual(len(proxy.SEGMENT_SESSIONS), 1)

    def test_nested_absolute_base_url_stays_on_proxy(self):
        type(self).manifest = f'<MPD><Period><BaseURL>{self.cdn}/video/</BaseURL><AdaptationSet><SegmentTemplate media="fragment.m4s"/></AdaptationSet></Period></MPD>'
        root = self.manifest_root()
        base = urljoin(root.find('BaseURL').text, root.find('Period/BaseURL').text)
        path = urlsplit(urljoin(base, 'fragment.m4s'))
        self.assertEqual(path.netloc, 'proxy.test')
        response = self.client.get(path.path)
        self.assertEqual(response.data, b'0123456789')
        self.assertEqual(self.requests[-1][0], '/video/fragment.m4s')

    def test_inherited_template_keeps_representation_relative_base(self):
        type(self).manifest = '<MPD><Period><BaseURL>../media/</BaseURL><AdaptationSet><SegmentTemplate media="fragment.m4s"/><Representation><BaseURL>video/</BaseURL></Representation></AdaptationSet></Period></MPD>'
        root = self.manifest_root()
        base = root.find('Period/AdaptationSet/Representation/BaseURL').text
        response = self.client.get(urlsplit(urljoin(base, 'fragment.m4s')).path)
        self.assertEqual(response.data, b'0123456789')
        self.assertEqual(self.requests[-1][0], '/media/video/fragment.m4s')

    def test_absolute_template_preserves_placeholders_and_query(self):
        type(self).manifest = f'<MPD><Period><AdaptationSet><SegmentTemplate media="{self.cdn}/video/$Number%05d$.m4s?token=a&amp;quality=high"/></AdaptationSet></Period></MPD>'
        root = self.manifest_root()
        media = root.find('Period/AdaptationSet/SegmentTemplate').get('media')
        self.assertIn('$Number%05d$', media)
        self.assertEqual(urlsplit(media).netloc, 'proxy.test')
        url = urlsplit(media.replace('$Number%05d$', '00001'))
        response = self.client.get(url.path + '?' + url.query)
        self.assertEqual(response.data, b'0123456789')
        self.assertEqual(self.requests[-1][0], '/video/00001.m4s?token=a&quality=high')

    def test_namespaced_manifest_and_live_refresh_location(self):
        type(self).manifest = f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Location>{self.cdn}/live/manifest.mpd</Location><Period/></MPD>'
        response = self.client.get('/channel/1')
        self.assertIn(b'<MPD ', response.data)
        root = self.manifest_root()
        ns = '{urn:mpeg:dash:schema:mpd:2011}'
        self.assertIsNotNone(root.find(ns + 'BaseURL'))
        self.assertIsNone(root.find(ns + 'Location'))

    def test_unsupported_manifests_fall_back_without_leaking_sessions(self):
        for manifest in ('invalid XML', '<html/>', '<MPD><BaseURL>a/</BaseURL><BaseURL>b/</BaseURL></MPD>', '<MPD><SegmentTemplate media="/video/$Number$.m4s"/></MPD>'):
            with self.subTest(manifest=manifest):
                type(self).manifest = manifest
                response = self.client.get('/channel/1')
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers['Location'], self.stream.url)
                self.assertFalse(proxy.SEGMENT_SESSIONS)

    def test_hls_is_redirected_without_fetching_manifest(self):
        self.stream.manifest_type = 'm3u'
        response = self.client.get('/channel/1')
        self.assertEqual(response.status_code, 303)
        self.assertFalse(self.requests)

    def test_upstream_failure_returns_bad_gateway(self):
        path = self.segment_path()
        with patch.object(proxy.requests, 'get', side_effect=requests.ConnectionError):
            self.assertEqual(self.client.get(path).status_code, 502)

    def test_disconnect_closes_upstream_response(self):
        path = self.segment_path()
        upstream = Mock(status_code=200, headers={'Content-Type': 'video/mp4'})
        upstream.iter_content.return_value = iter((b'first chunk', b'second chunk'))
        with patch.object(proxy.requests, 'get', return_value=upstream):
            response = self.client.get(path, buffered=False)
            response.close()
        upstream.close.assert_called()

    def test_invalid_origin_is_rejected(self):
        path = self.segment_path().replace('/0/', '/999/')
        self.assertEqual(self.client.get(path).status_code, 404)
