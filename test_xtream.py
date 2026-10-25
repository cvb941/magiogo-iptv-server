import base64
import datetime as dt
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, Response

from client import Channel, Programme
from magiogo import ApiCooldownError, MagioGo
from xtream import Xtream, init_xtream


class XtreamTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, XTREAM_USERNAME='iptv', XTREAM_PASSWORD='local-secret', XTREAM_PUBLIC_URL='https://iptv.test:5678')
        env.start()
        self.addCleanup(env.stop)
        self.magio = MagioGo(self.directory.name, 'cloud-account', 'cloud-secret')
        self.context = ['cloud-account', 'OTT_STB', 'Magio IPTV Server', 'p2']
        channel = Channel()
        channel.id, channel.name, channel.logo, channel.archive_days = '4099', 'Jednotka HD', 'https://image.test/logo.png', 7
        self.magio.channels = Mock(return_value=[channel])
        self.magio.channel_epg = Mock(return_value=[])
        self.app = Flask(__name__)
        self.service = init_xtream(self.app, self.magio, self.directory.name, self.context)
        self.client = self.app.test_client()
        self.query = {'username': 'iptv', 'password': 'local-secret'}
        self.now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)

    def programme(self, offset=-120, minutes=60, schedule='12345678901'):
        programme = Programme()
        programme.title, programme.description = 'Správy – Život', 'Čerstvé správy zo Slovenska.'
        programme.start_time = (self.now + dt.timedelta(minutes=offset)).replace(tzinfo=None)
        programme.end_time = programme.start_time + dt.timedelta(minutes=minutes)
        if schedule:
            programme.metadata['schedule_id'] = schedule
        return programme

    def api(self, action='', **params):
        return self.client.get('/player_api.php', query_string=dict(self.query, action=action, **params))

    def test_disabled_and_unauthorized_endpoints_never_contact_magio(self):
        paths = ['/player_api.php', '/get.php', '/xmltv.php', '/streaming/timeshift.php', '/live/iptv/wrong/4099.mpd']
        for path in paths:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        with patch.dict(os.environ, XTREAM_PASSWORD=''):
            self.assertEqual(self.api().status_code, 404)
        self.magio.channels.assert_not_called()
        self.magio.channel_epg.assert_not_called()

    def test_account_info_uses_local_credentials_and_correct_public_address(self):
        response = self.api()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['user_info']['auth'], 1)
        self.assertEqual(response.json['user_info']['allowed_output_formats'], ['mpd'])
        self.assertEqual(response.json['server_info']['url'], 'iptv.test')
        self.assertEqual(response.json['server_info']['port'], '5678')
        self.assertEqual(response.json['server_info']['timezone'], 'UTC')
        self.assertNotIn('cloud-secret', response.get_data(as_text=True))
        self.magio.channels.assert_not_called()

    def test_channels_are_cached_persisted_and_scoped_to_the_account(self):
        first = self.api('get_live_streams')
        self.assertEqual(first.json[0]['stream_id'], 4099)
        self.assertEqual(first.json[0]['tv_archive'], 1)
        self.assertEqual(first.json[0]['tv_archive_duration'], 7)
        self.assertEqual(first.json[0]['container_extension'], 'mpd')
        self.api('get_live_streams')
        self.magio.channels.assert_called_once_with(max_wait=5)
        self.assertEqual(Xtream(self.magio, self.directory.name, self.context).channels(), self.service.channels())
        self.assertEqual(Xtream(self.magio, self.directory.name, ['different-account']).data['channels'], [])
        self.assertEqual(self.api('get_live_streams', category_id='999').json, [])
        self.assertEqual(self.api('get_vod_streams').json, [])
        if os.name == 'posix':
            self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)

    def test_catchup_table_has_only_finished_available_airings_and_base64_utf8(self):
        past = self.programme()
        self.magio.channel_epg.return_value = [self.programme(-8 * 1440), past, self.programme(-180, schedule=None),
                                             self.programme(-10), self.programme(120)]
        response = self.api('get_simple_data_table', stream_id='4099')
        rows = response.json['epg_listings']
        self.assertEqual(len(rows), 1)
        self.assertEqual(base64.b64decode(rows[0]['title']).decode(), past.title)
        self.assertEqual(base64.b64decode(rows[0]['description']).decode(), past.description)
        self.assertEqual(rows[0]['has_archive'], 1)
        self.assertEqual(rows[0]['now_playing'], 0)
        self.assertLess(int(rows[0]['id']), 2**31)
        self.assertEqual(int(rows[0]['start_timestamp']), int(past.start_time.replace(tzinfo=dt.UTC).timestamp()))
        self.assertNotIn('12345678901', json.dumps(rows))
        self.api('get_simple_data_table', stream_id='4099')
        self.magio.channel_epg.assert_called_once()
        restored = Xtream(self.magio, self.directory.name, self.context)
        self.assertEqual(restored.programmes(restored.channel('4099')), self.service.programmes(self.service.channel('4099')))
        self.magio.channel_epg.assert_called_once()

    def test_short_epg_is_current_and_future_and_limits_are_validated(self):
        self.assertEqual(self.api('get_short_epg', stream_id='4099', limit='bad').status_code, 400)
        self.magio.channel_epg.assert_not_called()
        self.magio.channel_epg.return_value = [self.programme(), self.programme(-10), self.programme(120)]
        response = self.api('get_short_epg', stream_id='4099', limit='1')
        self.assertEqual(len(response.json['epg_listings']), 1)
        self.assertEqual(response.json['epg_listings'][0]['now_playing'], 1)
        self.assertEqual(response.json['epg_listings'][0]['has_archive'], 0)
        self.assertEqual(self.api('get_short_epg', stream_id='4099', limit='bad').status_code, 400)

    def test_archive_metadata_survives_cache_and_api_without_exposing_schedule_id(self):
        programme = self.programme()
        programme.metadata.update(media_type='episode', series_name='Kosti', episode_title='Pilot')
        programme.description = 'Pilot\nSamotný popis epizódy.'
        programme.year, programme.seasonNo, programme.episodeNo = 2005, 1, 1
        programme.poster, programme.thumbnail = 'https://image.test/poster.jpg', 'https://image.test/still.jpg'
        programme.genres, programme.actors, programme.directors = ['Krimi'], ['Herec'], ['Režisér']
        self.magio.channel_epg.return_value = [programme]
        response = self.api('get_simple_data_table', stream_id='4099')
        self.assertEqual(base64.b64decode(response.json['epg_listings'][0]['description']).decode(), 'Samotný popis epizódy.')
        metadata = response.json['epg_listings'][0]['programme_metadata']
        self.assertEqual((metadata['type'], metadata['series_name'], metadata['season'], metadata['episode']),
                         ('episode', 'Kosti', 1, 1))
        self.assertEqual(metadata['poster'], programme.poster)
        self.assertEqual(metadata['actors'], ['Herec'])
        self.assertNotIn('12345678901', json.dumps(metadata))
        restored = Xtream(self.magio, self.directory.name, self.context)
        self.assertEqual(restored.programmes(restored.channel('4099'))[0]['metadata'], metadata)
        self.assertEqual(restored.programmes(restored.channel('4099'))[0]['description'], 'Samotný popis epizódy.')

    def test_old_epg_cache_without_metadata_remains_readable(self):
        self.service.channel('4099')
        self.service.data['epg']['4099'] = {'at': time.time(), 'programmes': [{
            'id': 1, 'schedule_id': 'private', 'title': 'Movie', 'description': '',
            'start': int(self.now.timestamp())-7200, 'end': int(self.now.timestamp())-3600}]}
        self.assertEqual(self.api('get_simple_data_table', stream_id='4099').json['epg_listings'][0]['programme_metadata'], None)
        self.magio.channel_epg.assert_not_called()

    def test_archive_uses_schedule_id_and_a_separate_proxy_cache_key(self):
        programme = self.programme()
        self.magio.channel_epg.return_value = [programme]
        self.magio.programme_stream_info = Mock()
        start = programme.start_time.strftime('%Y-%m-%d:%H-%M')
        def manifest(source, url, key):
            source()
            self.assertEqual(url, 'https://iptv.test:5678')
            self.assertEqual(key, 'archive:4099:12345678901')
            return Response('<MPD/>', mimetype='application/dash+xml')
        with patch('xtream.proxy_manifest', side_effect=manifest):
            response = self.client.get('/streaming/timeshift.php', query_string=dict(self.query, stream='4099', start=start, duration='60'))
            self.assertEqual(response.mimetype, 'application/dash+xml')
            self.assertEqual(response.status_code, 200)
            response = self.client.get(f'/timeshift/iptv/local-secret/60/{start}/4099.mpd')
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.magio.programme_stream_info.call_count, 2)
        self.magio.programme_stream_info.assert_called_with('12345678901')

    def test_invalid_future_expired_and_partial_archive_requests_are_rejected(self):
        self.magio.channel_epg.return_value = [self.programme(), self.programme(120)]
        start = self.programme().start_time.strftime('%Y-%m-%d:%H-%M')
        self.assertEqual(self.client.get('/streaming/timeshift.php', query_string=dict(self.query, stream='4099', start=start, duration='5')).status_code, 416)
        for start, duration, code in [('bad', '60', 400), (start, 'bad', 400), (start, '1441', 400),
                                      (self.programme(120).start_time.strftime('%Y-%m-%d:%H-%M'), '60', 404),
                                      ((self.now - dt.timedelta(days=9)).strftime('%Y-%m-%d:%H-%M'), '60', 404)]:
            response = self.client.get('/streaming/timeshift.php', query_string=dict(self.query, stream='4099', start=start, duration=duration))
            self.assertEqual(response.status_code, code)
        self.assertEqual(self.api('get_simple_data_table', stream_id='999').status_code, 404)

    def test_concurrent_epg_requests_share_one_upstream_fetch(self):
        channel = self.service.channel('4099')
        def fetch(*args):
            time.sleep(.05)
            return [self.programme()]
        self.magio.channel_epg.side_effect = fetch
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.service.programmes(channel), range(8)))
        self.magio.channel_epg.assert_called_once()
        self.assertTrue(all(result == results[0] for result in results))

    def test_stale_epg_survives_cooldown_and_restart_then_refreshes_after_retry(self):
        self.magio.channel_epg.return_value = [self.programme(), self.programme(-8 * 1440)]
        self.assertEqual(len(self.api('get_simple_data_table', stream_id='4099').json['epg_listings']), 1)
        self.service.data['epg']['4099']['at'] = time.time() - 3600
        self.magio.channel_epg.side_effect = ApiCooldownError(60)
        for _ in range(3):
            response = self.api('get_simple_data_table', stream_id='4099')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json['epg_listings']), 1)
        self.assertEqual(self.magio.channel_epg.call_count, 2)
        restored = Xtream(self.magio, self.directory.name, self.context)
        self.assertEqual(len(restored.programmes(restored.channel('4099'))), 2)
        self.assertEqual(self.magio.channel_epg.call_count, 2)
        retry_at = self.service.data['epg']['4099']['retry_at']
        self.magio.channel_epg.side_effect = None
        updated = self.programme()
        updated.title = 'Obnovené EPG'
        self.magio.channel_epg.return_value = [updated]
        with patch('xtream.time.time', return_value=retry_at + 1):
            response = self.api('get_simple_data_table', stream_id='4099')
        self.assertEqual(base64.b64decode(response.json['epg_listings'][0]['title']).decode(), updated.title)
        self.assertEqual(self.magio.channel_epg.call_count, 3)
        self.assertNotIn('retry_at', self.service.data['epg']['4099'])

    def test_unavailable_epg_without_a_recent_cache_still_returns_retry_after(self):
        self.magio.channel_epg.side_effect = ApiCooldownError(60)
        response = self.api('get_simple_data_table', stream_id='4099')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Retry-After'], '60')
        self.magio.channel_epg.side_effect = None
        self.magio.channel_epg.return_value = [self.programme()]
        self.api('get_simple_data_table', stream_id='4099')
        self.service.data['epg']['4099'].update(at=time.time() - 25 * 3600, retry_at=time.time() + 3600)
        self.magio.channel_epg.side_effect = ApiCooldownError(60)
        self.assertEqual(self.api('get_simple_data_table', stream_id='4099').status_code, 503)

    def test_cooldown_is_retryable_without_exposing_upstream_details(self):
        self.magio.channels.side_effect = ApiCooldownError(60)
        response = self.api('get_live_streams')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Retry-After'], '60')
        self.assertNotIn('cloud-', response.get_data(as_text=True))

    def test_live_alias_playlist_and_unsupported_output(self):
        with patch('xtream.proxy_manifest', return_value=Response('<MPD/>', mimetype='application/dash+xml')) as manifest:
            self.assertEqual(self.client.get('/iptv/local-secret/4099').status_code, 200)
            self.assertEqual(self.client.get('/live/iptv/local-secret/4099.mpd').status_code, 200)
            self.assertEqual(manifest.call_args.args[2], '4099')
        playlist = self.client.get('/get.php', query_string=self.query)
        self.assertIn('/live/iptv/local-secret/4099.mpd', playlist.get_data(as_text=True))
        self.assertEqual(self.client.get('/live/iptv/local-secret/4099.ts').status_code, 415)
        self.assertEqual(self.client.get('/get.php', query_string=dict(self.query, output='ts')).status_code, 400)


class ArchiveApiTests(unittest.TestCase):
    def test_content_classification_uses_provider_category_and_preserves_episode_identity(self):
        for category, title, series_id, expected, season in [
            ('MSEPGC_MV', 'Film', None, 'movie', None),
            ('MSEPGC_Series', 'Kosti III.', 'series-1', 'episode', 3),
            ('MSEPGC_Kids', 'Rozprávka II', 'series-2', 'episode', 2),
            ('MSEPGC_News', 'Správy', 'news-1', 'other', None),
            ('MSEPGC_Sports', 'Futbal', None, 'other', None),
            ('EPG_CATEGORIES_V2_ID.14', 'Film', 'movie-1', 'movie', None),
            ('EPG_CATEGORIES_V2_ID.1G', 'Poirot VIII.', 'series-3', 'episode', 8),
            ('EPG_CATEGORIES_V2_ID.1J', 'Alžbetin dvor', 'series-4', 'episode', None),
            ('EPG_CATEGORIES_V2_ID.52', 'Rozprávka II', 'series-5', 'episode', 2),
            ('EPG_CATEGORIES_V2_ID.2G', 'Peniaze', 'news-2', 'other', None),
            (None, 'Nezaradené', None, 'other', None),
        ]:
            with self.subTest(category=category):
                data = {'programId': 77, 'title': title, 'episodeTitle': 'Epizóda', 'description': 'Popis',
                        'programValue': {'episodeId': None if expected == 'movie' else '5', 'seasonNumber': None, 'creationYear': '2019', 'seriesId': series_id},
                        'images': [], 'programRole': {'directors': [], 'actors': []},
                        'programCategory': {'mscname': category, 'subCategories': []}}
                programme = MagioGo._programme_data(data)
                self.assertEqual(programme.description, 'Popis')
                self.assertEqual(programme.metadata['media_type'], expected)
                self.assertEqual(programme.seasonNo, season)
                self.assertEqual(programme.episodeNo, None if expected == 'movie' else 5)
                if expected == 'episode':
                    self.assertEqual(programme.metadata['series_name'], title.split(' ')[0] if season else title)

    def test_channel_epg_paginates_filters_channels_and_preserves_schedule_id(self):
        with tempfile.TemporaryDirectory() as directory:
            magio = MagioGo(directory, 'test', 'test')
            data = {'programId': 77, 'title': 'Správy', 'episodeTitle': '', 'description': '',
                    'programValue': {'episodeId': None, 'seasonNumber': None, 'creationYear': None},
                    'images': [], 'programRole': {'directors': [], 'actors': []}, 'programCategory': None}
            now = dt.datetime.now(dt.UTC)
            airing = {'channel': {'id': 4099}, 'scheduleId': 12345678901, 'program': data,
                      'startTimeUTC': int((now - dt.timedelta(hours=2)).timestamp() * 1000),
                      'endTimeUTC': int((now - dt.timedelta(hours=1)).timestamp() * 1000), 'duration': 3600}
            pages = [{'items': [{'programs': [dict(airing, channel={'id': 999})]}], 'totalCount': 2},
                     {'items': [{'programs': [airing]}], 'totalCount': 2}]
            with patch.object(magio, '_login'), patch.object(magio, '_get', side_effect=pages) as get:
                result = magio.channel_epg('4099', now - dt.timedelta(days=7), now)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].id, 77)
            self.assertEqual(result[0].metadata['schedule_id'], '12345678901')
            self.assertTrue(result[0].is_replyable)
            self.assertEqual([call.kwargs['params']['offset'] for call in get.call_args_list], [0, 1])
            self.assertTrue(all(call.kwargs['max_wait'] == 5 for call in get.call_args_list))
            self.assertIn('channel.id=in=(4099);endTime=ge=', get.call_args.kwargs['params']['filter'])
            self.assertNotIn('list', get.call_args.kwargs['params'])
            with patch.object(magio, '_login') as login, self.assertRaises(ValueError):
                magio.channel_epg('4099 or true', now, now)
            login.assert_not_called()
