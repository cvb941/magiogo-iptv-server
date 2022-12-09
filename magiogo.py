import datetime
import random
import time
import requests

try:
    from typing import List
except:
    pass

from builtins import super
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from client import *
import datetime

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:83.0) Gecko/20100101 Firefox/83.0'


class MagioGoException(Exception):
    def __init__(self, id, text):
        self.id = id
        self.text = text


class MagioGoSessionData:
    def __init__(self):
        self.access_token = ''
        self.refresh_token = ''
        self.expires_in = 0
        self.type = ''


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

    def __init__(self, storage_dir, user_name, password, quality=MagioQuality.medium):
        self._user_name = user_name
        self._password = password
        self._quality = quality
        self._device = 'Magio IPTV Server'
        self._data = MagioGoSessionData()
        super().__init__(storage_dir, '%s.session' % self._user_name)

    def _check_response(self, resp):
        if resp['success']:
            if 'token' in resp:
                self._data.access_token = resp['token']['accessToken']
                self._data.refresh_token = resp['token']['refreshToken']
                self._data.expires_in = resp['token']['expiresIn']
                self._data.type = resp['token']['type']
                self._store_session(self._data)
        else:
            self._store_session(MagioGoSessionData())
            error_code = resp['errorCode']
            if error_code == 'INVALID_CREDENTIALS':
                raise UserInvalidException()
            raise MagioGoException(resp['errorCode'], resp['errorMessage'])

    def _auth_headers(self):
        return {'Authorization': self._data.type + ' ' + self._data.access_token,
                'Origin': 'https://www.magiogo.sk', 'Pragma': 'no-cache', 'Referer': 'https://www.magiogo.sk/',
                'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'cross-site', 'User-Agent': UA}

    @staticmethod
    def _request():
        session = requests.Session()
        session.mount('https://', HTTPAdapter(max_retries=Retry(total=5, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])))
        return session

    def _get(self, url, params=None, **kwargs):

        try:
            resp = self._request().get(url, params=params, **kwargs).json()
            self._check_response(resp)
            return resp
        except requests.exceptions.ConnectionError as err:
            raise NetConnectionError(str(err))

    def _post(self, url, data=None, json=None, **kwargs):
        try:
            resp = self._request().post(url, data=data, json=json, **kwargs).json()
            self._check_response(resp)
            return resp
        except requests.exceptions.ConnectionError as err:
            raise NetConnectionError(str(err))

    def _login(self):
        if (self._user_name == '') or (self._password == ''):
            raise UserNotDefinedException

        self._load_session(self._data)

        if not self._data.access_token:
            self._post('https://skgo.magio.tv/v2/auth/init',
                       params={'dsid': 'Netscape.' + str(int(time.time())) + '.' + str(random.random()),
                               'deviceName': self._device,
                               'deviceType': 'OTT_STB',
                               'osVersion': '0.0.0',
                               'appVersion': '0.0.0',
                               'language': 'SK'},
                       headers={'Origin': 'https://www.magiogo.sk', 'Pragma': 'no-cache',
                                'Referer': 'https://www.magiogo.sk/', 'User-Agent': UA,
                                'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Site': 'cross-site'})

            self._post('https://skgo.magio.tv/v2/auth/login',
                       json={'loginOrNickname': self._user_name, 'password': self._password},
                       headers=self._auth_headers())

        if self._data.refresh_token and self._data.expires_in < int(time.time() * 1000):
            self._post('https://skgo.magio.tv/v2/auth/tokens',
                       json={'refreshToken': self._data.refresh_token},
                       headers=self._auth_headers())

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

    def channel_stream_info(self, channel_id, programme_id=None):
        self._login()
        resp = self._get('https://skgo.magio.tv/v2/television/stream-url',
                         params={'service': 'TIMESHIFT',
                                 'name': self._device,
                                 'devtype': 'OTT_STB',
                                 'id': channel_id,
                                 'prof': self._quality,
                                 'ecid': '',
                                 'drm': 'verimatrix'},
                         headers=self._auth_headers())
        si = StreamInfo()
        si.url = resp['url']
        si.manifest_type = 'mpd' if si.url.find('.mpd') > 0 else 'm3u'
        si.user_agent = UA
        return si

    def programme_stream_info(self, programme_id):
        self._login()
        resp = self._get('https://skgo.magio.tv/v2/television/stream-url',
                         params={'service': 'ARCHIVE',
                                 'name': self._device,
                                 'devtype': 'OTT_STB',
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
                                 params={'filter': filter, 'limit': '20', 'offset': offset * 20, 'list': 'LIVE'},
                                 headers=self._auth_headers())

                fetch_more = len(resp['items']) == 20
                offset = offset + 1

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
