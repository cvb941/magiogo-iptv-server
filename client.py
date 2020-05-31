# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import json
import os
from datetime import datetime

try:
    from typing import List, Dict, Callable
except:
    pass


class Base:
    def __repr__(self):
        return str(self.__dict__)


class Channel(Base):
    def __init__(self):
        # channel Unique Id
        self.id = ''  # type: str
        # channel name
        self.name = ''
        # channel icon absolute url
        self.logo = ''
        # marks channel as pin protected
        self.is_pin_protected = False
        # if not 0 channel supports archive/catchup/replay
        self.archive_days = 0
        # channel metadata
        self.metadata = {}  # type: Dict[str, int]


class WidevineLicenceKey(Base):
    def __init__(self):
        self.license_server_url = ''
        self.headers = {}
        self.post_data = ''
        self.response = ''

    def to_string(self):
        return '%s|%s|%s|%s' % (self.license_server_url, self.post_data,
                                '&'.join(['%s=%s' % (k, v) for (k, v) in self.headers.items()]), self.response)


class WidevineDRM(Base):
    def __init__(self):
        self.manifest_type = ''  # 'mpd', 'ism' or 'hls'
        self.licence_key = WidevineLicenceKey()
        self.license_data = ''
        self.server_certificate = ''  # base64 encoded string
        self.media_renewal_url = ''
        self.media_renewal_time = 0


class StreamInfo(Base):
    def __init__(self):
        self.url = ''
        self.drm = None  # type: None or WidevineDRM
        self.max_bandwidth = None
        self.user_agent = ''
        self.headers = {}


class Programme(Base):
    def __init__(self):
        self.id = ''  # type: str
        # Programme Start Time in UTC
        self.start_time = None  # type: datetime or None
        # Programme End Time in UTC
        self.end_time = None  # type: datetime or None
        self.title = ''
        self.description = ''
        self.thumbnail = ''
        self.poster = ''
        self.duration = 0
        self.genres = []  # type: List[str]
        self.actors = []  # type: List[str]
        self.directors = []  # type: List[str]
        self.writers = []  # type: List[str]
        self.producers = []  # type: List[str]
        self.seasonNo = None
        self.episodeNo = None
        self.year = None  # type: int or None
        self.is_replyable = False
        # programme metadata
        self.metadata = {}  # type: Dict[str, int]


class IPTVException(Exception):
    pass


class UserNotDefinedException(IPTVException):
    pass


class UserInvalidException(IPTVException):
    pass


class StreamNotResolvedException(IPTVException):
    pass


class NetConnectionError(IPTVException):
    pass


def dummy_progress(progress):
    pass


class IPTVClient(object):
    def __init__(self, storage_dir, file_name):
        self._storage_path = storage_dir
        self._storage_file = os.path.join(self._storage_path, file_name)

    def channels(self, progress=dummy_progress):
        # type: (Callable[[None], int] or None) -> List[Channel]
        raise NotImplementedError("Should have implemented this")

    def channel_stream_info(self, channel_id):
        # type: (str) -> StreamInfo
        raise NotImplementedError("Should have implemented this")

    def programme_stream_info(self, programme_id):
        # type: (str) -> StreamInfo
        raise NotImplementedError("Should have implemented this")

    def epg(self, channels, from_date, to_date, progress=dummy_progress):
        # type: (List[str], datetime, datetime, Callable[[None], int] or None) -> Dict[str, List[Programme]]
        raise NotImplementedError("Should have implemented this")

    def archive_days(self):
        # type: () -> int
        raise NotImplementedError("Should have implemented this")

    def _store_session(self, data):
        if not os.path.exists(self._storage_path):
            os.makedirs(self._storage_path)
        with open(self._storage_file, 'w') as f:
            json.dump(data.__dict__, f)

    def _load_session(self, data):
        if os.path.exists(self._storage_file):
            with open(self._storage_file, 'r') as f:
                data.__dict__ = json.load(f)
