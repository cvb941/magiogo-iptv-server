import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import math
import os
import tempfile
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import requests
from flask import Blueprint, Response, current_app, request, send_file
from werkzeug.exceptions import NotFound

from client import IPTVException
from dash_proxy import proxy_manifest
from magiogo import ApiCooldownError, MagioGoException


class Xtream:
    def __init__(self, magio, storage_dir, context):
        self.magio = magio
        self.path = Path(storage_dir) / 'xtream.json'
        self.context = hashlib.sha256(json.dumps(context).encode()).hexdigest()
        self.lock = threading.RLock()
        self.channel_lock = threading.Lock()
        self.epg_locks = {}
        self.data = {'context': self.context, 'channels': [], 'updated': 0, 'epg': {}}
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            if saved.get('context') == self.context and isinstance(saved.get('channels'), list) and isinstance(saved.get('epg'), dict):
                self.data = saved
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @property
    def enabled(self):
        return bool(os.environ.get('XTREAM_USERNAME') and os.environ.get('XTREAM_PASSWORD'))

    def save(self):
        temporary = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as file:
                temporary = file.name
                json.dump(self.data, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            self.magio.diagnostics.record('cache_errors', error_type=type(error).__name__)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def update_channels(self, channels):
        if not self.enabled:
            return
        with self.lock:
            self.data['channels'] = [dict(id=str(c.id), name=c.name, logo=c.logo, archive_days=c.archive_days) for c in channels]
            self.data['updated'] = time.time()
            self.save()

    def channels(self):
        with self.channel_lock:
            if not self.data['channels'] or time.time() - self.data.get('updated', 0) > 12 * 3600:
                self.update_channels(self.magio.channels(max_wait=5))
            return list(self.data['channels'])

    def channel(self, channel_id):
        channel = next((c for c in self.channels() if c['id'] == str(channel_id)), None)
        if channel is None:
            raise NotFound()
        return channel

    def programmes(self, channel):
        key = channel['id']
        with self.lock:
            lock = self.epg_locks.setdefault(key, threading.Lock())
        with lock:
            cached = self.data['epg'].get(key, {})
            age = time.time() - cached.get('at', 0)
            if age > 15 * 60 and (time.time() >= cached.get('retry_at', 0) or age > 24 * 3600):
                now = dt.datetime.now(dt.UTC)
                try:
                    programmes = self.magio.channel_epg(key, now - dt.timedelta(days=channel['archive_days']), now + dt.timedelta(days=1))
                except (IPTVException, MagioGoException, requests.RequestException) as error:
                    if not cached.get('programmes') or time.time() - cached['at'] > 24 * 3600:
                        raise
                    cached = dict(cached, retry_at=time.time() + max(60, getattr(error, 'retry_after', 300)))
                    logging.getLogger(__name__).warning('Xtream EPG refresh failed (%s); using cached programmes', type(error).__name__)
                else:
                    rows = [{
                        'id': zlib.crc32(f"{key}:{p.metadata.get('schedule_id', '')}:{p.start_time}".encode()) & 0x7fffffff,
                        'schedule_id': p.metadata.get('schedule_id'), 'title': p.title, 'description': p.description,
                        'start': int(p.start_time.replace(tzinfo=dt.UTC).timestamp()),
                        'end': int(p.end_time.replace(tzinfo=dt.UTC).timestamp()),
                        'metadata': {
                            'type': p.metadata.get('media_type', 'other'), 'original_title': p.metadata.get('original_title', ''),
                            'series_name': p.metadata.get('series_name', ''), 'episode_title': p.metadata.get('episode_title', ''),
                            'year': p.year, 'season': p.seasonNo, 'episode': p.episodeNo,
                            'poster': p.poster, 'thumbnail': p.thumbnail, 'genres': p.genres,
                            'actors': p.actors, 'directors': p.directors,
                        },
                    } for p in programmes]
                    cached = {'at': time.time(), 'programmes': rows}
                with self.lock:
                    self.data['epg'][key] = cached
                    self.data['epg'] = {k: v for k, v in self.data['epg'].items() if time.time() - v['at'] < 24 * 3600}
                    self.save()
            for row in cached.get('programmes', []):
                subtitle = (row.get('metadata') or {}).get('episode_title')
                if subtitle:
                    row['description'] = row['description'].removeprefix(subtitle + '\n')
            return cached.get('programmes', [])


def init_xtream(app, magio, storage_dir, context):
    service = Xtream(magio, storage_dir, context)
    app.extensions['xtream'] = service
    api = Blueprint('xtream', __name__)

    @api.before_request
    def authenticate():
        if not service.enabled:
            return {'error': 'not_found'}, 404
        values = request.view_args or {}
        username = values.get('username', request.values.get('username', ''))
        password = values.get('password', request.values.get('password', ''))
        if not (hmac.compare_digest(username.encode(), os.environ['XTREAM_USERNAME'].encode()) &
                hmac.compare_digest(password.encode(), os.environ['XTREAM_PASSWORD'].encode())):
            return {'user_info': {'auth': 0}}, 401

    @api.after_request
    def no_store(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    def unavailable(error):
        if isinstance(error, ApiCooldownError):
            return {'error': 'upstream_unavailable'}, 503, {'Retry-After': str(error.retry_after)}
        current_app.logger.warning('Xtream request failed: %s', type(error).__name__)
        return {'error': 'upstream_unavailable'}, 503, {'Retry-After': '5'}

    for error in (IPTVException, MagioGoException, requests.RequestException, OSError):
        api.register_error_handler(error, unavailable)

    @api.errorhandler(404)
    def not_found(error):
        return {'error': 'not_found'}, 404

    def public_url():
        return (os.environ.get('XTREAM_PUBLIC_URL') or request.host_url).rstrip('/')

    def has_archive(channel, programme):
        now = time.time()
        return bool(programme['schedule_id'] and channel['archive_days'] and
                    now - channel['archive_days'] * 86400 < programme['start'] < programme['end'] <= now)

    @api.route('/player_api.php', methods=['GET', 'POST'])
    @api.route('/player_api', methods=['GET', 'POST'])
    def player_api():
        action = request.values.get('action', '')
        if action in ('', 'get_account_info'):
            url = urlsplit(public_url())
            now = dt.datetime.now(dt.UTC)
            return {
                'user_info': {'username': os.environ['XTREAM_USERNAME'], 'password': os.environ['XTREAM_PASSWORD'],
                              'auth': 1, 'status': 'Active', 'exp_date': None, 'is_trial': '0',
                              'active_cons': '0', 'created_at': '0', 'max_connections': '0',
                              'allowed_output_formats': ['mpd']},
                'server_info': {'url': url.hostname, 'port': str(url.port or (443 if url.scheme == 'https' else 80)),
                                'https_port': str(url.port or 443) if url.scheme == 'https' else '',
                                'server_protocol': url.scheme, 'rtmp_port': '0', 'timezone': 'UTC',
                                'timestamp_now': int(now.timestamp()), 'time_now': now.strftime('%Y-%m-%d %H:%M:%S')},
            }
        if action == 'get_live_categories':
            return [{'category_id': '1', 'category_name': 'Magio GO', 'parent_id': 0}]
        if action in ('get_vod_categories', 'get_vod_streams', 'get_series_categories', 'get_series'):
            return []
        if action == 'get_live_streams':
            if request.values.get('category_id', '1') not in ('', '1'):
                return []
            return [dict(num=i, name=c['name'], stream_type='live', stream_id=int(c['id']), stream_icon=c['logo'],
                         epg_channel_id=c['id'], added='0', category_id='1', container_extension='mpd',
                         custom_sid='', tv_archive=int(c['archive_days'] > 0), direct_source='',
                         tv_archive_duration=c['archive_days'], is_adult='0')
                    for i, c in enumerate(service.channels(), 1)]
        if action in ('get_short_epg', 'get_simple_data_table', 'get_simple_date_table'):
            if action == 'get_short_epg':
                try:
                    limit = min(100, max(1, int(request.values.get('limit', '4'))))
                except ValueError:
                    return {'error': 'invalid_limit'}, 400
            channel = service.channel(request.values.get('stream_id', ''))
            rows = service.programmes(channel)
            if action == 'get_short_epg':
                rows = [p for p in rows if p['end'] > time.time()][:limit]
            else:
                rows = [p for p in rows if has_archive(channel, p)]
            return {'epg_listings': [{
                'id': str(p['id']), 'epg_id': channel['id'], 'channel_id': channel['id'], 'lang': 'sk',
                'title': base64.b64encode(p['title'].encode()).decode(),
                'description': base64.b64encode(p['description'].encode()).decode(),
                'start': dt.datetime.fromtimestamp(p['start'], dt.UTC).strftime('%Y-%m-%d %H:%M:%S'),
                'end': dt.datetime.fromtimestamp(p['end'], dt.UTC).strftime('%Y-%m-%d %H:%M:%S'),
                'start_timestamp': str(p['start']), 'stop_timestamp': str(p['end']),
                'now_playing': int(p['start'] <= time.time() < p['end']), 'has_archive': int(has_archive(channel, p)),
                'programme_metadata': p.get('metadata'),
            } for p in rows]}
        return {'error': 'unsupported_action'}, 400

    @api.route('/<username>/<password>/<int:channel_id>')
    @api.route('/<username>/<password>/<int:channel_id>.mpd')
    @api.route('/live/<username>/<password>/<int:channel_id>')
    @api.route('/live/<username>/<password>/<int:channel_id>.mpd')
    def live(username, password, channel_id):
        service.channel(channel_id)
        return proxy_manifest(lambda: magio.channel_stream_info(str(channel_id)), public_url(), str(channel_id))

    @api.route('/live/<username>/<password>/<int:channel_id>.<extension>')
    @api.route('/timeshift/<username>/<password>/<duration>/<start>/<int:channel_id>.<extension>')
    def unsupported_output(username, password, channel_id, extension, duration=None, start=None):
        return {'error': 'unsupported_output', 'supported': ['mpd']}, 415

    @api.route('/streaming/timeshift.php')
    @api.route('/timeshift/<username>/<password>/<duration>/<start>/<int:channel_id>')
    @api.route('/timeshift/<username>/<password>/<duration>/<start>/<int:channel_id>.mpd')
    def timeshift(username=None, password=None, duration=None, start=None, channel_id=None):
        try:
            start = dt.datetime.strptime(start or request.args.get('start', ''), '%Y-%m-%d:%H-%M').replace(tzinfo=dt.UTC).timestamp()
            minutes = int(duration or request.args.get('duration', '0'))
            if not 1 <= minutes <= 1440:
                raise ValueError()
        except ValueError:
            return {'error': 'invalid_timeshift_range'}, 400
        channel = service.channel(channel_id if channel_id is not None else request.args.get('stream', ''))
        if not channel['archive_days']:
            return {'error': 'archive_unavailable'}, 404
        programme = next((p for p in service.programmes(channel) if int(p['start'] // 60) == int(start // 60)), None)
        if programme is None or not has_archive(channel, programme):
            return {'error': 'archive_unavailable'}, 404
        if abs(minutes - math.ceil((programme['end'] - programme['start']) / 60)) > 1:
            return {'error': 'programme_boundaries_required'}, 416
        schedule_id = programme['schedule_id']
        return proxy_manifest(lambda: magio.programme_stream_info(schedule_id), public_url(), f"archive:{channel['id']}:{schedule_id}")

    @api.route('/xmltv.php')
    def xmltv():
        path = Path('public/magioGuide.xmltv')
        if not path.is_file():
            return {'error': 'guide_not_ready'}, 503, {'Retry-After': '5'}
        return send_file(path.resolve(), mimetype='application/xml')

    @api.route('/get.php')
    def playlist():
        if request.args.get('output', 'mpd') != 'mpd':
            return {'error': 'unsupported_output', 'supported': ['mpd']}, 400
        username, password = (quote(os.environ[key], safe='') for key in ('XTREAM_USERNAME', 'XTREAM_PASSWORD'))
        epg = public_url() + '/xmltv.php?' + urlencode({'username': os.environ['XTREAM_USERNAME'], 'password': os.environ['XTREAM_PASSWORD']})
        lines = [f'#EXTM3U x-tvg-url="{epg}"']
        for c in service.channels():
            name = c['name'].replace('\r', '').replace('\n', '')
            logo = c['logo'].replace('"', '%22').replace('\r', '').replace('\n', '')
            lines.extend([f'#EXTINF:-1 tvg-id="{c["id"]}" tvg-logo="{logo}" group-title="Magio GO",{name}',
                          f'{public_url()}/live/{username}/{password}/{c["id"]}.mpd'])
        return Response('\n'.join(lines) + '\n', mimetype='application/x-mpegURL')

    app.register_blueprint(api)
    return service
