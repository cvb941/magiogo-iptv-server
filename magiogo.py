import datetime
import random
import time
import requests
import json
import threading
import logging
import copy
import math
from contextlib import contextmanager
from concurrent.futures import CancelledError

try:
    from typing import List
except:
    pass

from builtins import super
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from client import *
from diagnostics import Diagnostics, utc_time
import datetime

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:137.0) Gecko/20100101 Firefox/137.0'


class MagioGoException(Exception):
    def __init__(self, id, text):
        self.id = id
        self.text = text


class ApiCooldownError(NetConnectionError):
    def __init__(self, seconds):
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__('Magio API is temporarily unavailable')


class MagioGoSessionData:
    def __init__(self):
        self.access_token = ''
        self.refresh_token = ''
        self.expires_in = 0
        self.type = ''
        self.stream_qualities = {}
        self.authenticated_at = 0


class MagioGoDevice:
    def __init__(self):
        self.id = ''
        self.name = ''
        self.expiration_time = None
        self.is_this = False


class MagioGoRecording:
    def __init__(self):
        self.id = ''
        self.programme = None


class MagioQuality:
    low = 'p0'
    medium = 'p2'
    high = 'p4'
    extra = 'p5'

    @staticmethod
    def get(index):
        return {0: MagioQuality.low, 1: MagioQuality.medium, 2: MagioQuality.high, 3: MagioQuality.extra}.get(index, MagioQuality.high)


class MagioGo(IPTVClient):

    def __init__(self, storage_dir, user_name, password, quality=MagioQuality.medium, device_type='OTT_STB', device_name='Magio IPTV Server'):
        self._user_name = user_name
        self._password = password
        self._quality = quality
        self._device = device_name
        self._deviceType = device_type
        self._appVersion = '4.1.1' if self._deviceType == 'OTT_ANDROID' else '0.0.0'
        self._data = MagioGoSessionData()
        self.diagnostics = Diagnostics()
        self._auth_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._api_condition = threading.Condition()
        self._api_foreground = 0
        self._api_next_request = 0
        self._api_guide_interval = 6
        self._api_background_after = time.monotonic() + self._api_guide_interval
        super().__init__(storage_dir, '%s.session' % self._user_name)

    def _check_response(self, resp):
        if resp.get('success', False):
            return

        error_code = resp.get('errorCode')
        self.diagnostics.record('api_errors', error_type='UserInvalidException' if error_code == 'INVALID_CREDENTIALS' else 'MagioGoException')
        if error_code == 'RATE_LIMIT_EXCEEDED':
            self.diagnostics.record('api_rate_limits')
        if error_code == 'INVALID_CREDENTIALS':
            raise UserInvalidException()
        raise MagioGoException(error_code, resp.get('errorMessage'))

    @staticmethod
    def _token_session(resp, previous=None, authenticated=True):
        token = resp.get('token')
        fields = ('accessToken', 'type', 'refreshToken') if authenticated else ('accessToken', 'type')
        if not isinstance(token, dict) or any(not isinstance(token.get(field), str) or not token[field] for field in fields):
            raise MagioGoException('INVALID_TOKEN_RESPONSE', 'Incomplete Magio authentication response')
        expires = token.get('expiresIn', 0)
        if authenticated and (type(expires) not in (int, float) or not time.time() * 1000 < expires < float('inf')):
            raise MagioGoException('INVALID_TOKEN_RESPONSE', 'Invalid Magio token expiration')
        data = copy.deepcopy(previous) if previous is not None else MagioGoSessionData()
        data.access_token = token['accessToken']
        data.type = token['type']
        data.refresh_token = token.get('refreshToken', '')
        data.expires_in = expires
        return data

    def _auth_headers(self, data=None):
        data = self._data if data is None else data
        return {
            'Authorization': f"{data.type} {data.access_token}",
            'X-ClientId': '-1',
            'Origin': 'https://www.magiogo.sk',
            'Pragma': 'no-cache',
            'Referer': 'https://www.magiogo.sk/',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'cross-site',
            'User-Agent': UA,
        }

    def authentication_status(self):
        data = self._data.__dict__
        try:
            with open(self._storage_file, encoding='utf-8') as source:
                saved = json.load(source)
            if isinstance(saved, dict):
                data = saved
        except (OSError, ValueError):
            pass
        expires = data.get('expires_in', 0)
        try:
            expires = expires / 1000 if type(expires) in (int, float) else 0
        except OverflowError:
            expires = 0
        present = all(isinstance(data.get(key), str) and data[key] for key in ('access_token', 'refresh_token', 'type'))
        return {
            'token_unexpired': bool(present and time.time() < expires < float('inf')),
            'expires_at': utc_time(expires), 'last_success': utc_time(data.get('authenticated_at')),
        }

    @staticmethod
    def _request():
        session = requests.Session()
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=2, status=0, backoff_factor=1, backoff_max=5,
            respect_retry_after_header=False, raise_on_status=False,
        )))
        return session

    @contextmanager
    def _api_slot(self, background=False, max_wait=None):
        with self._api_condition:
            if not background:
                self._api_foreground += 1
                self._api_condition.notify_all()
        try:
            with self._api_condition:
                while True:
                    if self._stop_event.is_set():
                        raise CancelledError()
                    delay = self._api_next_request - time.monotonic()
                    if background:
                        delay = max(delay, self._api_background_after - time.monotonic())
                        if self._api_foreground:
                            self._api_condition.wait()
                            continue
                    if delay <= 0:
                        break
                    if max_wait is not None and delay > max_wait:
                        raise ApiCooldownError(delay)
                    self._api_condition.wait(delay)
                now = time.monotonic()
                self._api_next_request = now + 1
                self._api_background_after = now + self._api_guide_interval
            self.diagnostics.record('api_requests')
            yield
        finally:
            with self._api_condition:
                if not background:
                    self._api_foreground -= 1
                self._api_condition.notify_all()

    def stop(self):
        self._stop_event.set()
        with self._api_condition:
            self._api_condition.notify_all()

    def _apply_api_cooldown(self, response, attempt=0):
        if response.status_code >= 400:
            self.diagnostics.record('api_errors', status=response.status_code)
        if response.status_code not in (429, 500, 502, 503, 504):
            return False
        delay = Retry().get_retry_after(response)
        delay = delay if delay is not None else min(2 ** (attempt + 1), 5)
        with self._api_condition:
            self._api_next_request = max(self._api_next_request, time.monotonic() + delay)
            if response.status_code == 429:
                self.diagnostics.record('api_rate_limits', status=429)
                self._api_guide_interval = min(self._api_guide_interval * 2, 60)
                self._api_background_after = max(self._api_background_after, self._api_next_request)
            self._api_condition.notify_all()
        logging.getLogger(__name__).warning('Magio API returned %s; next request in %.1fs', response.status_code, delay)
        return True

    def _get(self, url, params=None, background=False, max_wait=None, **kwargs):
        kwargs.setdefault('timeout', (5, 15))
        try:
            with self._request() as session:
                for attempt in range(3):
                    with self._api_slot(background, max_wait), session.get(url, params=params, **kwargs) as response:
                        retry = self._apply_api_cooldown(response, attempt)
                        if retry and max_wait is not None and self._api_next_request - time.monotonic() > max_wait:
                            raise ApiCooldownError(self._api_next_request - time.monotonic())
                        if retry and attempt < 2:
                            continue
                        response.raise_for_status()
                        resp = response.json()
                        break
            self._check_response(resp)
            return resp
        except requests.RequestException as err:
            if not isinstance(err, requests.HTTPError):
                self.diagnostics.record('api_errors', error_type=type(err).__name__)
            raise NetConnectionError(str(err))

    def _post(self, url, data=None, json=None, max_wait=None, **kwargs):
        kwargs.setdefault('timeout', (5, 15))
        try:
            with self._api_slot(max_wait=max_wait), self._request() as session:
                with session.post(url, data=data, json=json, **kwargs) as response:
                    self._apply_api_cooldown(response)
                    if max_wait is not None and response.status_code in (429, 503):
                        raise ApiCooldownError(self._api_next_request - time.monotonic())
                    response.raise_for_status()
                    resp = response.json()
            self._check_response(resp)
            return resp
        except requests.RequestException as err:
            if not isinstance(err, requests.HTTPError):
                self.diagnostics.record('api_errors', error_type=type(err).__name__)
            raise NetConnectionError(str(err)) from err

    def _init_and_login(self, max_wait=None):
        # INIT: register this "device"
        initialized = self._post(
            'https://skgo.magio.tv/v2/auth/init',
            max_wait=max_wait,
                params={
                    'dsid': f"Netscape.{int(time.time())}.{random.random()}",
                    'deviceName': self._device,
                    'deviceType': self._deviceType,
                    'osVersion': '0.0.0',
                    'appVersion': self._appVersion,
                    'language': 'SK'
            },
            headers={
                'X-ClientId': '-1',
                'Origin': 'https://www.magiogo.sk',
                'Pragma': 'no-cache',
                'Referer': 'https://www.magiogo.sk/',
                'Sec-Fetch-Mode': 'cors',
                'Sec-Fetch-Site': 'cross-site',
                'User-Agent': UA,
            }
        )

        login_body = json.dumps(
            {'loginOrNickname': self._user_name, 'password': self._password},
            separators=(',', ':')
        )
        temporary = self._token_session(initialized, authenticated=False)
        return self._post(
            'https://skgo.magio.tv/v2/auth/login',
            max_wait=max_wait,
            data=login_body,
            headers={
                **self._auth_headers(temporary),
                'Content-Type': 'application/json',
            }
        )

    def _login(self, max_wait=None):
        if max_wait is not None and self._api_next_request - time.monotonic() > max_wait:
            raise ApiCooldownError(self._api_next_request - time.monotonic())
        with self._auth_lock:
            if not self._user_name or not self._password:
                raise UserNotDefinedException
            saved = copy.deepcopy(self._data)
            self._load_session(saved)
            has_tokens = all(isinstance(value, str) and value for value in (saved.access_token, saved.refresh_token, saved.type))
            if has_tokens and type(saved.expires_in) in (int, float) and time.time() * 1000 < saved.expires_in < float('inf'):
                self._data = saved
                return
            with self.diagnostics.operation('auth'):
                if has_tokens:
                    try:
                        response = self._post(
                            'https://skgo.magio.tv/v2/auth/tokens',
                            max_wait=max_wait,
                            json={'refreshToken': saved.refresh_token},
                            headers=self._auth_headers(saved)
                        )
                    except (MagioGoException, UserInvalidException, NetConnectionError) as error:
                        if isinstance(error, NetConnectionError):
                            rejected = getattr(error.__cause__, 'response', None)
                            if rejected is None or rejected.status_code not in (401, 403):
                                raise
                        elif isinstance(error, MagioGoException) and error.id == 'RATE_LIMIT_EXCEEDED':
                            raise
                        response = self._init_and_login(max_wait=max_wait)
                else:
                    response = self._init_and_login(max_wait=max_wait)
                candidate = self._token_session(response, saved)
                candidate.authenticated_at = time.time()
                self._store_session(candidate)
                self._data = candidate

    def channels(self, progress=dummy_progress):
        self._login()

        progress(0)

        resp = self._get('https://skgo.magio.tv/v2/television/channels',
                         params={'list': 'LIVE', 'queryScope': 'LIVE'},
                         headers=self._auth_headers())
        ret = []
        for i in resp['items']:
            i = i['channel']
            c = Channel()
            c.id = str(i['channelId'])
            c.name = i['name']
            c.logo = i['logoUrl']
            if i['hasArchive']:
                c.archive_days = self.archive_days()
            ret.append(c)

        progress(100)

        return ret

    def channel_stream_info(self, channel_id, programme_id=None, quality_override=None):
        self._login(max_wait=5)
        requested = quality_override or self._quality
        cache_key = json.dumps([self._deviceType, self._device, requested, str(channel_id)])
        with self._auth_lock:
            cached = self._data.stream_qualities.get(cache_key, {})
            cache_valid = cached.get('quality') == MagioQuality.medium and cached.get('expires', 0) > time.time()
            quality = MagioQuality.medium if cache_valid else requested
        qualities = [quality] if quality == MagioQuality.medium else [quality, MagioQuality.medium]
        for quality in qualities:
            resp = self._get('https://skgo.magio.tv/v2/television/stream-url',
                         max_wait=5,
                         params={'service': 'LIVE',
                                 'name': self._device,
                                 'devtype': self._deviceType,
                                 'id': channel_id,
                                 'prof': quality,
                                 'ecid': '',
                                 'drm': 'verimatrix'},
                         headers=self._auth_headers())
            si = StreamInfo()
            si.url = resp['url']
            si.manifest_type = 'mpd' if si.url.find('.mpd') > 0 else 'm3u'
            si.user_agent = UA
            if requested == MagioQuality.medium:
                return si

            # Confirm a fallback before remembering it; a single CDN 404 can be transient.
            for attempt in range(2):
                with requests.get(si.url, headers={'User-Agent': si.user_agent}, stream=True, timeout=(5, 15)) as response:
                    status = response.status_code
                if status != 404:
                    break
                if attempt == 0:
                    time.sleep(.5)
            if quality != MagioQuality.medium and status == 404:
                continue
            with self._auth_lock:
                if quality != requested or cache_key in self._data.stream_qualities:
                    self._data.stream_qualities.pop(cache_key, None)
                    if quality != requested and 200 <= status < 300:
                        self._data.stream_qualities = {key: value for key, value in self._data.stream_qualities.items()
                                                       if value.get('expires', 0) > time.time()}
                        expires = cached['expires'] if cache_valid else time.time() + 3600
                        self._data.stream_qualities[cache_key] = {'quality': quality, 'expires': expires}
                    try:
                        self._store_session(self._data)
                    except OSError:
                        pass  # This optional cache must not interrupt playback.
            if quality != requested and status == 404 and quality == qualities[0]:
                return self.channel_stream_info(channel_id, programme_id, requested)
            return si

    def programme_stream_info(self, programme_id):
        self._login()
        resp = self._get('https://skgo.magio.tv/v2/television/stream-url',
                         params={'service': 'ARCHIVE',
                                 'name': self._device,
                                 'devtype': self._deviceType,
                                 'id': programme_id,
                                 'prof': self._quality,
                                 'ecid': '',
                                 'drm': 'verimatrix'},
                         headers=self._auth_headers())
        si = StreamInfo()
        si.url = resp['url']
        si.manifest_type = 'mpd' if si.url.find('.mpd') > 0 else 'm3u'
        si.user_agent = UA
        return si

    @staticmethod
    def _strptime(date_string, format):
        # https://forum.kodi.tv/showthread.php?tid=112916 it's insane !!!
        try:
            return datetime.datetime.strptime(date_string, format)
        except TypeError:
            import time as ptime
            return datetime.datetime(*(ptime.strptime(date_string, format)[0:6]))

    def epg(self, channels, from_date, to_date, progress=dummy_progress):
        self._login()
        ret = {}

        progress(0)

        from_date = from_date.replace(hour=0, minute=0, second=0, microsecond=0)
        to_date = to_date.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        now = datetime.datetime.utcnow()

        days = int((to_date - from_date).days)

        for n in range(days):
            progress((100 // days) * (n + 1))

            current_day = from_date + datetime.timedelta(n)
            filter = 'startTime=ge=%sT00:00:00.000Z;startTime=le=%sT00:59:59.999Z' % (
                current_day.strftime("%Y-%m-%d"), (current_day + datetime.timedelta(days=1)).strftime("%Y-%m-%d"))

            fetch_more = True
            offset = 0
            while fetch_more:
                resp = self._get('https://skgo.magio.tv/v2/television/epg',
                                 params={'filter': filter, 'limit': '100', 'offset': offset, 'list': 'LIVE'},
                                 headers=self._auth_headers(), background=True)

                offset += len(resp['items'])
                fetch_more = bool(resp['items']) and offset < resp['totalCount']

                for i in resp['items']:
                    for p in i['programs']:
                        channel = str(p['channel']['id'])

                        if channel not in channels:
                            continue

                        if channel not in ret:
                            ret[channel] = []

                        programme = self._programme_data(p['program'])
                        programme.start_time = datetime.datetime.utcfromtimestamp(p['startTimeUTC'] / 1000)
                        programme.end_time = datetime.datetime.utcfromtimestamp(p['endTimeUTC'] / 1000)
                        programme.duration = p['duration']
                        programme.is_replyable = (programme.start_time > (now - datetime.timedelta(days=7))) and (programme.end_time < now)

                        ret[channel].append(programme)

        return ret

    @staticmethod
    def _programme_data(pi):

        def safe_int(value, default=None):
            try:
                return int(value)
            except (ValueError, TypeError):
                return default

        programme = Programme()
        programme.id = pi['programId']
        programme.title = pi['title']
        programme.description = "%s\n%s" % (pi['episodeTitle'] or '', pi['description'] or '')

        pv = pi['programValue']
        if pv['episodeId'] is not None:
            programme.episodeNo = safe_int(pv['episodeId'])
        if pv['seasonNumber'] is not None:
            programme.seasonNo = safe_int(pv['seasonNumber'])
        if pv['creationYear'] is not None:
            programme.year = safe_int(pv['creationYear'])
        for i in pi['images']:
            programme.thumbnail = i
            break
        for i in pi['images']:
            if "_VERT" in i:
                programme.poster = i
                break
        for d in pi['programRole']['directors']:
            programme.directors.append(d['fullName'])
        for a in pi['programRole']['actors']:
            programme.actors.append(a['fullName'])
        if pi['programCategory'] is not None:
            for c in pi['programCategory']['subCategories']:
                programme.genres.append(c['desc'])

        return programme

    def archive_days(self):
        return 7

    def devices(self):
        # type: () -> List[MagioGoDevice]
        def make_device(i, is_this):
            device = MagioGoDevice()
            device.id = str(i['id'])
            device.name = i['name']
            device.expiration_time = self._strptime(i['verimatrixExpirationTime'], "%Y-%m-%dT%H:%M:%S.%fZ")
            device.is_this = is_this
            return device

        self._login()
        resp = self._get('https://skgo.magio.tv/home/listDevices', headers=self._auth_headers())

        devices = [make_device(i, False) for i in resp['items']]

        if resp['thisDevice']:
            devices.append(make_device(resp['thisDevice'], True))
        return devices

    def disconnect_device(self, device_id):
        # type: (str) -> None
        self._login()
        self._get('https://skgo.magio.tv/home/deleteDevice', params={'id': device_id}, headers=self._auth_headers())

    def recordings(self):
        # type: () -> List[MagioGoRecording]
        ret = []
        self._login()
        resp = self._get('https://skgo.magio.tv/v2/television/recordings',
                         params={'platform': 'go', 'planned': 'false'},
                         headers=self._auth_headers())
        for i in resp['items']:
            p = i['schedule']

            recording = MagioGoRecording()
            recording.id = str(i['id'])

            programme = self._programme_data(p['program'])
            programme.id = str(p['id'])
            programme.start_time = datetime.datetime.fromtimestamp(p['startTimeUTC'] / 1000)
            programme.end_time = datetime.datetime.fromtimestamp(p['endTimeUTC'] / 1000)
            programme.duration = p['duration']

            recording.programme = programme

            ret.append(recording)
        return ret

    def add_recording(self, schedule_id, channel_id):
        self._get('https://skgo.magio.tv/television/addProgramRecording',
                  params={'scheduleId': schedule_id, 'channelID': channel_id, 'type': 'SIMPLE', 'storage': 'go'},
                  headers=self._auth_headers())

    def delete_recording(self, recording_id):
        # type: (str) -> None
        self._login()
        self._get('https://skgo.magio.tv/television/deleteRecording',
                  params={'recordingIds': recording_id, 'storage': 'go'},
                  headers=self._auth_headers())

    def recording_stream_info(self, recording_id):
        # type: (str) -> StreamInfo
        self._login()

        resp = self._get('https://skgo.magio.tv/v2/television/stream-url',
                         params={'service': 'DVR', 'id': recording_id, 'prof': self._quality, 'ecid': '', 'drm': 'verimatrix'},
                         headers=self._auth_headers())

        si = StreamInfo()
        si.url = resp['url']
        si.manifest_type = 'mpd' if si.url.find('.mpd') > 0 else 'm3u'
        si.user_agent = UA
        return si
