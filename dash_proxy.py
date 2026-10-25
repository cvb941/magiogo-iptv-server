import atexit
import hashlib
import json
import math
import os
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from http.cookiejar import Cookie
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from flask import Blueprint, Response, abort, current_app, has_app_context, redirect, request, stream_with_context

ET.register_namespace('', 'urn:mpeg:dash:schema:mpd:2011')

dash_proxy = Blueprint('dash_proxy', __name__)
SEGMENT_SESSIONS = {}
SEGMENT_SESSIONS_LOCK = threading.Lock()
SEGMENT_SESSION_TTL_SECONDS = 30 * 60
STALE_SESSION_RETENTION_SECONDS = 24 * 60 * 60
CHANNEL_SESSIONS = {}
CHANNEL_LOCKS = {}
CDN_ADAPTER = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
atexit.register(CDN_ADAPTER.close)


def _record(event, amount=1, **fields):
    metrics = current_app.extensions.get('diagnostics') if has_app_context() else None
    if metrics is not None:
        metrics.record(event, amount, **fields)


def session_counts():
    with SEGMENT_SESSIONS_LOCK:
        live = {sid for sid, entry in SEGMENT_SESSIONS.items() if entry['expires'] > time.monotonic()}
        return {'valid_sessions': len(live), 'cached_channels': sum(sid in live for sid in CHANNEL_SESSIONS.values())}


def init_session_cache(app, path, context):
    cache = {
        'path': Path(path), 'context': hashlib.sha256(json.dumps(context).encode()).hexdigest(),
        'lock': threading.Lock(), 'written_at': float('-inf'),
    }
    app.extensions['dash_session_cache'] = cache
    try:
        data = json.loads(cache['path'].read_text(encoding='utf-8'))
        if data['version'] != 1 or data['context'] != cache['context']:
            return
        now, wall = time.monotonic(), time.time()
        sessions = {}
        for sid, entry in data['sessions'].items():
            remaining = float(entry['expires']) - wall
            if not math.isfinite(remaining) or remaining + STALE_SESSION_RETENTION_SECONDS <= 0:
                continue
            url = urlsplit(entry['manifest_url'])
            if uuid.UUID(sid).hex != sid or url.scheme not in ('http', 'https') or not url.netloc:
                raise ValueError('Invalid cached session')
            if not isinstance(entry['user_agent'], str) or not isinstance(entry['origins'], list) or not entry['origins']:
                raise ValueError('Invalid cached session')
            if any(urlsplit(origin).scheme not in ('http', 'https') or not urlsplit(origin).netloc for origin in entry['origins']):
                raise ValueError('Invalid cached origin')
            cookies = requests.cookies.RequestsCookieJar()
            for values in entry['cookies']:
                values = dict(values)
                values['rest'] = values.pop('_rest', {})
                cookies.set_cookie(Cookie(**values))
            sessions[sid] = {
                'manifest_url': entry['manifest_url'], 'user_agent': entry['user_agent'],
                'origins': entry['origins'], 'cookies': cookies,
                'expires': now + min(remaining, SEGMENT_SESSION_TTL_SECONDS),
                'manifest': b'', 'manifest_time': float('-inf'),
            }
        channels = {(channel, public_url): sid for channel, public_url, sid in data['channels'] if sid in sessions}
        with SEGMENT_SESSIONS_LOCK:
            SEGMENT_SESSIONS.update(sessions)
            CHANNEL_SESSIONS.update(channels)
        app.logger.info('Restored %d DASH sessions', len(sessions))
        if sessions:
            with app.app_context():
                _record('sessions_restored', len(sessions))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        with app.app_context():
            _record('cache_errors', error_type=type(error).__name__)
        app.logger.warning('Could not restore DASH session cache')


def _persist_sessions(force=False):
    cache = current_app.extensions.get('dash_session_cache')
    if cache is None:
        return
    with cache['lock']:
        now, wall = time.monotonic(), time.time()
        if not force and now - cache['written_at'] < 30:
            return
        with SEGMENT_SESSIONS_LOCK:
            sessions = {
                sid: {'manifest_url': entry['manifest_url'], 'user_agent': entry['user_agent'],
                      'origins': list(entry['origins']), 'cookies': [dict(cookie.__dict__) for cookie in entry['cookies']],
                      'expires': wall + entry['expires'] - now}
                for sid, entry in SEGMENT_SESSIONS.items()
                if entry['expires'] + STALE_SESSION_RETENTION_SECONDS > now
                and (entry['expires'] > now or sid in CHANNEL_SESSIONS.values())
            }
            channels = [[*key, sid] for key, sid in CHANNEL_SESSIONS.items() if sid in sessions]
        temporary = None
        try:
            cache['path'].parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache['path'].parent, delete=False) as output:
                temporary = output.name
                json.dump({'version': 1, 'context': cache['context'], 'sessions': sessions, 'channels': channels}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, cache['path'])
            cache['written_at'] = now
        except (OSError, TypeError, ValueError) as error:
            _record('cache_errors', error_type=type(error).__name__)
            current_app.logger.warning('Could not persist DASH session cache')
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass


def _invalidate_session(sid):
    with SEGMENT_SESSIONS_LOCK:
        keys = [key for key, value in CHANNEL_SESSIONS.items() if value == sid]
        for key in keys:
            del CHANNEL_SESSIONS[key]
    if keys:
        _record('sessions_invalidated')
        _persist_sessions(force=True)


class _CDNSession(requests.Session):
    def request(self, method, url, **kwargs):
        _record('cdn_requests')
        try:
            response = super().request(method, url, **kwargs)
        except requests.RequestException as error:
            _record('cdn_errors', error_type=type(error).__name__)
            raise
        if response.status_code >= 400:
            _record('cdn_errors', status=response.status_code)
        return response

    def get_adapter(self, url):
        # Borrow the shared pool without sharing cookies or closing it with this session.
        if url.lower().startswith(('http://', 'https://')):
            return CDN_ADAPTER
        return super().get_adapter(url)


def _scale_archive_timeline(template, namespace):
    if not template.get('timescale'):
        return
    fields = [(element, name) for element, names in
              [(template, ('timescale', 'presentationTimeOffset'))] +
              [(s, ('t', 'd')) for s in template.findall(f'{namespace}SegmentTimeline/{namespace}S')]
              for name in names if name in element.attrib]
    values = [int(element.get(name)) for element, name in fields]
    factor = 1
    while max(values) // factor > (2**63 - 1) // 1000:
        factor *= 10
    urls = ('media', 'initialization', 'index', 'bitstreamSwitching')
    if factor == 1 or any(value % factor for value in values) or any('$Time%' in template.get(name, '') for name in urls):
        return
    # FFmpeg multiplies timeline ticks by 1000 when seeking; epoch-based video ticks overflow int64.
    for (element, name), value in zip(fields, values):
        element.set(name, str(value // factor))
    for name in urls:
        if name in template.attrib:
            template.set(name, template.get(name).replace('$Time$', '$Time$' + '0' * (len(str(factor)) - 1)))


def _rewrite_manifest(content, manifest_url, proxy_base, origins):
    root = ET.fromstring(content)
    namespace = root.tag[:-3] if root.tag.endswith('}MPD') else ''
    if root.tag != namespace + 'MPD':
        raise ValueError('Expected an MPD')
    if root.get('type', 'static') == 'static':
        for template in root.findall('.//' + namespace + 'SegmentTemplate'):
            _scale_archive_timeline(template, namespace)

    def proxy_url(url):
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.netloc:
            raise ValueError('Unsupported segment URL')
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin not in origins:
            origins.append(origin)
        path = urlunsplit(('', '', parts.path or '/', parts.query, parts.fragment))
        return f'{proxy_base}{origins.index(origin)}{path}'

    url_attributes = {
        'SegmentTemplate': ('media', 'initialization', 'index', 'bitstreamSwitching'),
        'SegmentURL': ('media', 'index'),
        'Initialization': ('sourceURL',),
        'RepresentationIndex': ('sourceURL',),
        'BitstreamSwitching': ('sourceURL',),
    }
    stack = [(root, manifest_url)]
    while stack:
        element, base = stack.pop()
        bases = element.findall(namespace + 'BaseURL')
        if len(bases) > 1 or '{http://www.w3.org/XML/1998/namespace}base' in element.attrib:
            raise ValueError('Unsupported base URL inheritance')
        if bases:
            base = urljoin(base, (bases[0].text or '').strip())
            bases[0].text = proxy_url(base)
        elif element is root:
            element.insert(0, ET.Element(namespace + 'BaseURL'))
            element[0].text = proxy_url(urljoin(base, '.'))

        for attribute in url_attributes.get(element.tag.removeprefix(namespace), ()):
            value = element.get(attribute)
            if not value:
                continue
            parts = urlsplit(value)
            if parts.scheme or parts.netloc:
                element.set(attribute, proxy_url(urljoin(base, value)))
            elif parts.path.startswith('/') or '..' in parts.path.split('/'):
                # These depend on the representation's inherited base URL.
                raise ValueError('Unsupported segment URL inheritance')
        stack.extend((child, base) for child in element if child.tag != namespace + 'BaseURL')

    # Keep live manifest refreshes on /channel, where cookies are captured again.
    for location in root.findall(namespace + 'Location'):
        root.remove(location)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def proxy_manifest(stream_info, public_url, channel_id=None):
    key = (channel_id, public_url) if channel_id is not None else None
    with SEGMENT_SESSIONS_LOCK:
        lock = CHANNEL_LOCKS.setdefault(key, threading.Lock()) if key is not None else threading.Lock()
    with lock:
        return _fetch_manifest(stream_info, public_url, key)


def _fetch_manifest(stream_info, public_url, key):
    now = time.monotonic()
    with SEGMENT_SESSIONS_LOCK:
        sid = CHANNEL_SESSIONS.get(key)
        cached = SEGMENT_SESSIONS.get(sid)
        if cached and cached['expires'] + STALE_SESSION_RETENTION_SECONDS <= now:
            cached = None
        if cached:
            _record('session_cache_hits')
            if cached['expires'] > now and now - cached['manifest_time'] < 2:
                cached['expires'] = now + SEGMENT_SESSION_TTL_SECONDS
                _record('manifest_cache_hits')
                return Response(cached['manifest'], mimetype='application/dash+xml')

    if cached:
        url, user_agent = cached['manifest_url'], cached['user_agent']
        origins = list(cached['origins'])
    else:
        stream_info = stream_info() if callable(stream_info) else stream_info
        if stream_info.manifest_type != 'mpd':
            return redirect(stream_info.url, code=303)
        url, user_agent = stream_info.url, stream_info.user_agent
        sid, origins = uuid.uuid4().hex, []

    try:
        with _CDNSession() as session:
            if cached:
                session.cookies.update(cached['cookies'])
            with session.get(url, headers={'User-Agent': user_agent}, timeout=10) as upstream:
                upstream.raise_for_status()
                manifest = _rewrite_manifest(
                    upstream.content, upstream.url,
                    f'{public_url.rstrip("/")}/segproxy/{sid}/', origins,
                )
                cookies = session.cookies.copy()
                manifest_url = upstream.url
    except requests.RequestException as error:
        current_app.logger.warning('DASH manifest fetch failed: %s', type(error).__name__)
        if cached:
            _invalidate_session(sid)
            return _fetch_manifest(stream_info, public_url, key)
        abort(502)
    except (ET.ParseError, ValueError) as error:
        _record('cdn_errors', error_type=type(error).__name__)
        current_app.logger.warning('DASH manifest fallback: %s', error)
        if cached:
            _invalidate_session(sid)
            return _fetch_manifest(stream_info, public_url, key)
        return redirect(url, code=303)

    now = time.monotonic()
    with SEGMENT_SESSIONS_LOCK:
        for expired_sid in [key for key, value in SEGMENT_SESSIONS.items()
                            if value['expires'] + STALE_SESSION_RETENTION_SECONDS <= now
                            or (value['expires'] <= now and key not in CHANNEL_SESSIONS.values())]:
            del SEGMENT_SESSIONS[expired_sid]
        for channel in [key for key, value in CHANNEL_SESSIONS.items() if value not in SEGMENT_SESSIONS]:
            del CHANNEL_SESSIONS[channel]
        SEGMENT_SESSIONS[sid] = {
            'origins': origins, 'cookies': cookies, 'user_agent': user_agent,
            'expires': now + SEGMENT_SESSION_TTL_SECONDS,
            'manifest_url': manifest_url, 'manifest': manifest, 'manifest_time': now,
        }
        if key is not None:
            CHANNEL_SESSIONS[key] = sid
    if not cached:
        _record('sessions_created')
    _persist_sessions(force=True)
    return Response(manifest, mimetype='application/dash+xml')


@dash_proxy.route('/segproxy/<sid>/<int:origin_id>/', defaults={'subpath': ''})
@dash_proxy.route('/segproxy/<sid>/<int:origin_id>/<path:subpath>')
def segment_proxy(sid, origin_id, subpath):
    now = time.monotonic()
    with SEGMENT_SESSIONS_LOCK:
        session_info = SEGMENT_SESSIONS.get(sid)
        if session_info is None:
            return Response(status=404)
        if session_info['expires'] <= now:
            return Response(status=410)
        if origin_id >= len(session_info['origins']):
            return Response(status=404)
        session_info['expires'] = now + SEGMENT_SESSION_TTL_SECONDS

    target_url = session_info['origins'][origin_id] + '/' + subpath
    if request.query_string:
        target_url += '?' + request.query_string.decode('ascii')
    headers = {'User-Agent': session_info['user_agent'], 'Accept-Encoding': 'identity'}
    headers.update((name, request.headers[name]) for name in ('Range', 'If-Range') if name in request.headers)
    try:
        with _CDNSession() as session:
            upstream = session.get(target_url, cookies=session_info['cookies'], headers=headers, stream=True, timeout=15)
    except requests.RequestException:
        abort(502)

    if upstream.status_code in (401, 403, 410):
        _invalidate_session(sid)
    else:
        _persist_sessions()

    def chunks():
        try:
            yield from upstream.iter_content(chunk_size=65536)
        except requests.RequestException as error:
            _record('cdn_errors', error_type=type(error).__name__)
            raise
        finally:
            upstream.close()

    response = Response(stream_with_context(chunks()), status=upstream.status_code)
    for name in ('Content-Type', 'Content-Range', 'Accept-Ranges', 'ETag', 'Last-Modified'):
        if name in upstream.headers:
            response.headers[name] = upstream.headers[name]
    if 'Content-Length' in upstream.headers and not upstream.headers.get('Content-Encoding'):
        response.headers['Content-Length'] = upstream.headers['Content-Length']
    response.call_on_close(upstream.close)
    return response
