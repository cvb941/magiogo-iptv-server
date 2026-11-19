import atexit
import hashlib
import json
import math
import os
import re
import struct
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
HLS_CACHE = {}
HLS_AUDIO_NAMES = {
    'slo': 'Slovak',
    'slk': 'Slovak',
    'ces': 'Czech',
    'cze': 'Czech',
    'eng': 'English',
    'mul': 'Original',
    'qaa': 'Original',
    'mis': 'Original',
    'ads': 'Audio Description',
}


def apply_cors_headers(response):
    origin = request.headers.get('Origin') if has_app_context() else None
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers.add('Vary', 'Origin')
    else:
        response.headers.setdefault('Access-Control-Allow-Origin', '*')
    response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Range, Content-Type, Accept, Origin'
    response.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
    return response


@dash_proxy.after_request
def _dash_proxy_cors(response):
    return apply_cors_headers(response)
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
                'live_windows': entry.get('live_windows', {}), 'live': bool(entry.get('live', True)),
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
                      'live_windows': entry.get('live_windows', {}), 'live': entry.get('live', True),
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


def _timeline_tracks(root, namespace):
    """Expand each $Time$-addressed SegmentTimeline into (timeline, timescale, [(t, d), ...])."""
    tracks = []
    for template in root.findall('.//' + namespace + 'SegmentTemplate'):
        timescale_str = template.get('timescale')
        timeline = template.find(namespace + 'SegmentTimeline')
        # Rebuilt S elements keep only t and d, which $Number$ addressing would not survive.
        if not timescale_str or timeline is None or '$Time$' not in template.get('media', ''):
            continue
        timescale = int(timescale_str)
        if timescale <= 0:
            continue
        segs = []
        cur = 0
        for segment in timeline.findall(namespace + 'S'):
            repeat = int(segment.get('r', 0))
            if repeat < 0:
                return []
            if 't' in segment.attrib:
                cur = int(segment.get('t'))
            duration = int(segment.get('d'))
            for _ in range(repeat + 1):
                segs.append((cur, duration))
                cur += duration
        if segs:
            tracks.append((timeline, timescale, segs))
    return tracks


def _trim_live_timelines(root, namespace, max_seconds=45):
    """Keep live SegmentTimeline strictly under FFmpeg dashdec's 60s live-edge offset.

    FFmpeg's dashdec.c calculates the starting segment in n_timelines mode as
    (timeline_end - 60 * timescale). When video sums to exactly 60.000s while
    48kHz audio oscillates between 59.989s and 60.011s, FFmpeg picks segment 1
    for video and segment 0 for audio (one segment of A/V skew). Trimming every
    timeline to the same number of trailing segments, <= max_seconds, makes all
    AdaptationSets start on the same segment when, as on Magio, the tracks share
    segment boundaries.
    """
    tracks = _timeline_tracks(root, namespace)
    if not tracks or max(sum(d for _, d in segs) / ts for _, ts, segs in tracks) < 55:
        return
    min_len = min(len(segs) for _, _, segs in tracks)
    keep = 2
    for candidate in range(min_len, 1, -1):
        if all(sum(d for _, d in segs[-candidate:]) / ts <= max_seconds for _, ts, segs in tracks):
            keep = candidate
            break
    for timeline, _, segs in tracks:
        trimmed = segs[-keep:]
        for child in list(timeline):
            timeline.remove(child)
        for idx, (t, d) in enumerate(trimmed):
            elem = ET.SubElement(timeline, namespace + 'S')
            if idx == 0:
                elem.set('t', str(t))
            elem.set('d', str(d))


def _start_over_as_recording(root, namespace):
    """Serve start-over as VOD: FFmpeg plays a dynamic MPD from its live edge, not the programme start."""
    tracks = _timeline_tracks(root, namespace)
    if not tracks:
        return
    root.set('type', 'static')
    for attr in ('minimumUpdatePeriod', 'timeShiftBufferDepth', 'publishTime', 'suggestedPresentationDelay'):
        root.attrib.pop(attr, None)
    if not root.get('mediaPresentationDuration'):
        root.set('mediaPresentationDuration', f'PT{max(sum(d for _, d in segs) / ts for _, ts, segs in tracks):.3f}S')


def _rewrite_manifest(content, manifest_url, proxy_base, origins, live_windows=None, prefer_best=False, live=True):
    root = ET.fromstring(content)
    namespace = root.tag[:-3] if root.tag.endswith('}MPD') else ''
    if root.tag != namespace + 'MPD':
        raise ValueError('Expected an MPD')
    if root.get('type') == 'dynamic':
        # Magio serves start-over as a dynamic ARCHIVE manifest spanning the whole programme so far.
        (_trim_live_timelines if live else _start_over_as_recording)(root, namespace)
        for adaptation in root.findall('.//' + namespace + 'AdaptationSet') if prefer_best else []:
            representations = adaptation.findall(namespace + 'Representation')
            if len(representations) > 1 and any(rep.get('height') for rep in representations):
                best = max(representations, key=lambda rep: (int(rep.get('height', 0)), int(rep.get('bandwidth', 0))))
                for representation in representations:
                    if representation is not best:
                        adaptation.remove(representation)
        if live_windows is not None:
            for template in root.findall('.//' + namespace + 'SegmentTemplate'):
                track = re.search(r'Fragments\(([^=]+)=\$Time\$\)', template.get('media', ''))
                if track is None:
                    continue
                end, last = 0, None
                for segment in template.findall(namespace + 'SegmentTimeline/' + namespace + 'S'):
                    repeat = int(segment.get('r', 0))
                    if repeat < 0:
                        last = None
                        break
                    duration = int(segment.get('d'))
                    last = int(segment.get('t', end)) + duration * repeat
                    end = last + duration
                if last is not None:
                    live_windows[track[1]] = last
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
    quality = request.args.get('quality', 'all')
    if quality not in ('all', 'best'):
        return {'error': 'invalid_quality', 'supported': ['all', 'best']}, 400
    prefer_best = quality == 'best'
    key = (str(channel_id) + ('|best' if prefer_best else ''), public_url) if channel_id is not None else None
    with SEGMENT_SESSIONS_LOCK:
        lock = CHANNEL_LOCKS.setdefault(key, threading.Lock()) if key is not None else threading.Lock()
    with lock:
        return _fetch_manifest(stream_info, public_url, key, prefer_best)


def _fetch_manifest(stream_info, public_url, key, prefer_best=False):
    now = time.monotonic()
    with SEGMENT_SESSIONS_LOCK:
        sid = CHANNEL_SESSIONS.get(key)
        cached = SEGMENT_SESSIONS.get(sid)
        source_sid = None
        if key is not None and (cached is None or cached['expires'] <= now):
            source_sid, source = max(
                ((candidate_sid, SEGMENT_SESSIONS[candidate_sid])
                 for (channel, _), candidate_sid in CHANNEL_SESSIONS.items()
                 if str(channel).removesuffix('|best') == key[0].removesuffix('|best')
                 and candidate_sid in SEGMENT_SESSIONS),
                key=lambda candidate: candidate[1]['expires'], default=(None, None),
            )
            if source is not None and source_sid != sid:
                cached = dict(source, manifest_time=float('-inf'))
                sid = uuid.uuid4().hex
            else:
                source_sid = None
        if cached and cached['expires'] + STALE_SESSION_RETENTION_SECONDS <= now:
            cached = None
        if cached:
            _record('session_cache_hits')
            if cached['expires'] > now and now - cached['manifest_time'] < 2:
                cached['expires'] = now + SEGMENT_SESSION_TTL_SECONDS
                _record('manifest_cache_hits')
                return Response(cached['manifest'], mimetype='application/dash+xml')

    if cached:
        url, user_agent, live = cached['manifest_url'], cached['user_agent'], cached.get('live', True)
        origins = list(cached['origins'])
    else:
        stream_info = stream_info() if callable(stream_info) else stream_info
        if stream_info.manifest_type != 'mpd':
            return redirect(stream_info.url, code=303)
        url, user_agent, live = stream_info.url, stream_info.user_agent, getattr(stream_info, 'live', True)
        sid, origins = uuid.uuid4().hex, []

    try:
        with _CDNSession() as session:
            if cached:
                session.cookies.update(cached['cookies'])
            with session.get(url, headers={'User-Agent': user_agent}, timeout=10) as upstream:
                upstream.raise_for_status()
                live_windows = {}
                manifest = _rewrite_manifest(
                    upstream.content, upstream.url,
                    f'{public_url.rstrip("/")}/segproxy/{sid}/', origins, live_windows, prefer_best, live,
                )
                cookies = session.cookies.copy()
                manifest_url = upstream.url
    except requests.RequestException as error:
        current_app.logger.warning('DASH manifest fetch failed: %s', type(error).__name__)
        if cached:
            _invalidate_session(source_sid or sid)
            return _fetch_manifest(stream_info, public_url, key, prefer_best)
        abort(502)
    except (ET.ParseError, ValueError) as error:
        _record('cdn_errors', error_type=type(error).__name__)
        current_app.logger.warning('DASH manifest fallback: %s', error)
        if cached:
            _invalidate_session(source_sid or sid)
            return _fetch_manifest(stream_info, public_url, key, prefer_best)
        return redirect(url, code=303)

    now = time.monotonic()
    with SEGMENT_SESSIONS_LOCK:
        for expired_sid in [key for key, value in SEGMENT_SESSIONS.items()
                            if value['expires'] + STALE_SESSION_RETENTION_SECONDS <= now
                            or (value['expires'] <= now and key not in CHANNEL_SESSIONS.values())]:
            del SEGMENT_SESSIONS[expired_sid]
            HLS_CACHE.pop(expired_sid, None)
        for channel in [key for key, value in CHANNEL_SESSIONS.items() if value not in SEGMENT_SESSIONS]:
            del CHANNEL_SESSIONS[channel]
        SEGMENT_SESSIONS[sid] = {
            'origins': origins, 'cookies': cookies, 'user_agent': user_agent,
            'expires': now + SEGMENT_SESSION_TTL_SECONDS,
            'manifest_url': manifest_url, 'manifest': manifest, 'manifest_time': now,
            'live_windows': live_windows, 'live': live,
        }
        if key is not None:
            CHANNEL_SESSIONS[key] = sid
    if not cached or source_sid is not None:
        _record('sessions_created')
    _persist_sessions(force=True)
    return Response(manifest, mimetype='application/dash+xml')



def _extract_mdhd_timescale(buf):
    """Extract mdhd timescale from an fMP4 init segment."""
    stack = [(0, len(buf))]
    while stack:
        pos, end = stack.pop()
        while pos + 8 <= end:
            size, btype = struct.unpack('>I4s', buf[pos:pos + 8])
            if size < 8 or pos + size > end:
                break
            if btype in (b'moov', b'trak', b'mdia'):
                stack.append((pos + 8, pos + size))
            elif btype == b'mdhd':
                ver = buf[pos + 8]
                if ver == 0 and pos + 24 <= end:
                    return struct.unpack('>I', buf[pos + 20:pos + 24])[0]
                if ver == 1 and pos + 32 <= end:
                    return struct.unpack('>I', buf[pos + 28:pos + 32])[0]
            pos += size
    return None


def _rebase_moof_tfdt(buf, t0_seconds, timescale):
    """Subtract t0_seconds * timescale from every tfdt box in leading moof box(es)."""
    if not timescale or t0_seconds <= 0:
        return buf
    offset_ticks = int(round(t0_seconds * timescale))
    out = bytearray(buf)
    pos = 0
    end = len(out)
    while pos + 8 <= end:
        size, btype = struct.unpack('>I4s', out[pos:pos + 8])
        if size < 8 or pos + size > end:
            break
        if btype == b'moof':
            tpos = pos + 8
            mend = pos + size
            while tpos + 8 <= mend:
                tsize, ttype = struct.unpack('>I4s', out[tpos:tpos + 8])
                if tsize < 8 or tpos + tsize > mend:
                    break
                if ttype == b'traf':
                    ipos = tpos + 8
                    tend = tpos + tsize
                    while ipos + 8 <= tend:
                        isize, itype = struct.unpack('>I4s', out[ipos:ipos + 8])
                        if isize < 8 or ipos + isize > tend:
                            break
                        if itype == b'tfdt' and ipos + 16 <= tend:
                            ver = out[ipos + 8]
                            if ver == 1 and ipos + 20 <= tend:
                                bmdt = struct.unpack('>Q', out[ipos + 12:ipos + 20])[0]
                                new_bmdt = max(0, bmdt - offset_ticks)
                                struct.pack_into('>Q', out, ipos + 12, new_bmdt)
                            elif ver == 0:
                                bmdt = struct.unpack('>I', out[ipos + 12:ipos + 16])[0]
                                new_bmdt = max(0, bmdt - offset_ticks)
                                struct.pack_into('>I', out, ipos + 12, new_bmdt)
                        ipos += isize
                tpos += tsize
        pos += size
    return bytes(out)


@dash_proxy.route('/segproxy/<sid>/<int:origin_id>/', defaults={'subpath': ''}, methods=['GET', 'HEAD', 'OPTIONS'])
@dash_proxy.route('/segproxy/<sid>/<int:origin_id>/<path:subpath>', methods=['GET', 'HEAD', 'OPTIONS'])
def segment_proxy(sid, origin_id, subpath):
    if request.method == 'OPTIONS':
        return Response(status=204)
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
    fragment = re.search(r'/Fragments\(([^=]+)=(\d+)\)', subpath)
    if fragment and int(fragment[2]) > session_info.get('live_windows', {}).get(fragment[1], float('inf')):
        # DASH demuxers can request an unlisted future fragment and then replay it on reload.
        time.sleep(.5)
        return Response(status=404, headers={'Retry-After': '1'})
    hls_kind = request.args.get('_hls_kind')
    hls_init = request.args.get('_hls_init') == '1'
    hls_t0 = request.args.get('_hls_t0')
    if request.query_string:
        from urllib.parse import parse_qsl, urlencode
        fwd_params = [(k, v) for k, v in parse_qsl(request.query_string.decode('ascii'), keep_blank_values=True)
                      if not k.startswith('_hls_')]
        if fwd_params:
            target_url += '?' + urlencode(fwd_params)
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
            if hls_init and hls_kind and upstream.status_code == 200:
                data = upstream.content
                ts = _extract_mdhd_timescale(data)
                if ts:
                    with SEGMENT_SESSIONS_LOCK:
                        entry = HLS_CACHE.get(sid)
                        if entry is not None:
                            entry.setdefault('timescales', {})[hls_kind] = ts
                yield data
                return
            if hls_t0 and hls_kind and upstream.status_code == 200:
                t0_sec = float(hls_t0)
                with SEGMENT_SESSIONS_LOCK:
                    entry = HLS_CACHE.get(sid) or {}
                    ts = entry.get('timescales', {}).get(hls_kind)
                if ts is None:
                    ts = 48000 if hls_kind == 'audio' else 10000000
                it = upstream.iter_content(chunk_size=65536)
                first = next(it, b'')
                if first:
                    yield _rebase_moof_tfdt(first, t0_sec, ts)
                yield from it
                return
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


def _parse_hls_tracks(content, manifest_url, proxy_base, origins):
    """Translate a raw DASH MPD into HLS v7 fMP4 video/audio track descriptors."""
    root = ET.fromstring(content)
    namespace = root.tag[:root.tag.index('}') + 1] if root.tag.startswith('{') else ''
    is_dynamic = root.get('type') == 'dynamic'

    def proxy_url(url):
        resolved = urljoin(manifest_url, url)
        parts = urlsplit(resolved)
        origin = urlunsplit((parts.scheme, parts.netloc, '', '', ''))
        if origin not in origins:
            origins.append(origin)
        origin_id = origins.index(origin)
        path = parts.path.lstrip('/')
        query = f'?{parts.query}' if parts.query else ''
        return f'{proxy_base}{origin_id}/{path}{query}'

    mpd_base = manifest_url
    base_el = root.find(namespace + 'BaseURL')
    if base_el is not None and base_el.text:
        mpd_base = urljoin(manifest_url, base_el.text.strip())

    videos = []
    audios = []
    for aset in root.findall('.//' + namespace + 'AdaptationSet'):
        ctype = (aset.get('contentType') or '').lower()
        mime = (aset.get('mimeType') or '').lower()
        if 'video' in ctype or 'video' in mime:
            kind = 'video'
        elif 'audio' in ctype or 'audio' in mime:
            kind = 'audio'
        else:
            continue

        aset_base = mpd_base
        aset_base_el = aset.find(namespace + 'BaseURL')
        if aset_base_el is not None and aset_base_el.text:
            aset_base = urljoin(mpd_base, aset_base_el.text.strip())

        reps = aset.findall(namespace + 'Representation')
        if not reps:
            continue
        rep = max(reps, key=lambda r: int(r.get('bandwidth') or 0))
        rep_base = aset_base
        rep_base_el = rep.find(namespace + 'BaseURL')
        if rep_base_el is not None and rep_base_el.text:
            rep_base = urljoin(aset_base, rep_base_el.text.strip())

        st = rep.find(namespace + 'SegmentTemplate')
        if st is None:
            st = aset.find(namespace + 'SegmentTemplate')
        if st is None:
            continue
        tl = st.find(namespace + 'SegmentTimeline')
        if tl is None:
            continue

        timescale = int(st.get('timescale') or 1)
        bw = rep.get('bandwidth') or '0'
        rid = rep.get('id') or ''
        init_tpl = (st.get('initialization') or '').replace('$RepresentationID$', rid).replace('$Bandwidth$', bw)
        media_tpl = (st.get('media') or '').replace('$RepresentationID$', rid).replace('$Bandwidth$', bw)
        if not init_tpl or not media_tpl:
            continue

        def add_param(u, params):
            return u + ('&' if '?' in u else '?') + params

        init_url = add_param(proxy_url(urljoin(rep_base, init_tpl)), f'_hls_kind={kind}&_hls_init=1')
        segments = []
        t = 0
        t0_sec = None
        for s in tl.findall(namespace + 'S'):
            if s.get('t') is not None:
                t = int(s.get('t'))
            if t0_sec is None:
                t0_sec = t / timescale
            d = int(s.get('d'))
            repeat = int(s.get('r') or 0)
            if repeat < 0:
                repeat = 0
            for _ in range(repeat + 1):
                seg_rel = media_tpl.replace('$Time$', str(t))
                seg_u = proxy_url(urljoin(rep_base, seg_rel))
                if t0_sec and t0_sec > 86400:
                    seg_u = add_param(seg_u, f'_hls_kind={kind}&_hls_t0={t0_sec:.6f}')
                segments.append((d / timescale, seg_u))
                t += d

        if not segments:
            continue

        codecs = rep.get('codecs') or aset.get('codecs') or ''
        lang = (aset.get('lang') or '').lower()
        track = {
            'init': init_url,
            'segments': segments,
            'bandwidth': int(bw) if bw.isdigit() else 0,
            'codecs': codecs,
            'width': rep.get('width'),
            'height': rep.get('height'),
            'lang': lang,
            'name': HLS_AUDIO_NAMES.get(lang, lang.upper() if lang else 'Audio'),
        }
        if kind == 'video':
            videos.append(track)
        else:
            audios.append(track)

    if not videos:
        raise ValueError('No video tracks in DASH manifest')
    videos.sort(key=lambda v: v['bandwidth'], reverse=True)
    return {'video': [videos[0]], 'audio': audios}, is_dynamic


def _render_hls_master(tracks, public_url, sid):
    base = public_url.rstrip('/')
    lines = ['#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-INDEPENDENT-SEGMENTS']
    audios = tracks['audio']
    default_idx = next((i for i, a in enumerate(audios) if a['lang'] in ('slo', 'slk')), 0)
    audio_codecs = audios[default_idx]['codecs'] if audios else ''
    for i, a in enumerate(audios):
        is_def = 'YES' if i == default_idx else 'NO'
        lang_attr = f',LANGUAGE="{a["lang"]}"' if a['lang'] else ''
        lines.append(
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{a["name"]}"{lang_attr},'
            f'DEFAULT={is_def},AUTOSELECT=YES,URI="{base}/hls/{sid}/audio/{i}.m3u8"'
        )
    v = tracks['video'][0]
    codecs = ','.join(c for c in (v['codecs'], audio_codecs) if c)
    attrs = [f'BANDWIDTH={v["bandwidth"] or 3400000}']
    if v.get('width') and v.get('height'):
        attrs.append(f'RESOLUTION={v["width"]}x{v["height"]}')
    if codecs:
        attrs.append(f'CODECS="{codecs}"')
    if audios:
        attrs.append('AUDIO="audio"')
    lines.append(f'#EXT-X-STREAM-INF:{",".join(attrs)}')
    lines.append(f'{base}/hls/{sid}/video/0.m3u8')
    return ('\n'.join(lines) + '\n').encode('utf-8')


def _render_hls_media(track, is_dynamic, started_dynamic):
    max_dur = max((d for d, _ in track['segments']), default=10.0)
    target_dur = max(1, math.ceil(max_dur))
    playlist_type = 'EVENT' if started_dynamic else 'VOD'
    lines = [
        '#EXTM3U',
        '#EXT-X-VERSION:7',
        f'#EXT-X-TARGETDURATION:{target_dur}',
        '#EXT-X-MEDIA-SEQUENCE:0',
        f'#EXT-X-PLAYLIST-TYPE:{playlist_type}',
    ]
    if started_dynamic:
        lines.append('#EXT-X-START:TIME-OFFSET=0.0')
    lines.append(f'#EXT-X-MAP:URI="{track["init"]}"')
    for dur, url in track['segments']:
        lines.append(f'#EXTINF:{dur:.6f},')
        lines.append(url)
    if not is_dynamic:
        lines.append('#EXT-X-ENDLIST')
    return ('\n'.join(lines) + '\n').encode('utf-8')


def proxy_hls_master(stream_info, public_url, channel_id=None):
    key = (f'hls:{channel_id}', public_url) if channel_id is not None else None
    with SEGMENT_SESSIONS_LOCK:
        lock = CHANNEL_LOCKS.setdefault(key, threading.Lock()) if key is not None else threading.Lock()
    with lock:
        now = time.monotonic()
        with SEGMENT_SESSIONS_LOCK:
            sid = CHANNEL_SESSIONS.get(key) if key is not None else None
            cached = SEGMENT_SESSIONS.get(sid) if sid else None
            if cached and cached['expires'] <= now:
                cached = None
                sid = None
            hls_entry = HLS_CACHE.get(sid) if sid else None
            if cached and hls_entry and now - hls_entry['time'] < 3.0:
                cached['expires'] = now + SEGMENT_SESSION_TTL_SECONDS
                body = _render_hls_master(hls_entry['tracks'], public_url, sid)
                resp = Response(body, mimetype='application/vnd.apple.mpegurl', headers={'Cache-Control': 'no-cache'})
                return apply_cors_headers(resp)

        if cached:
            url, user_agent = cached['manifest_url'], cached['user_agent']
            origins = list(cached['origins'])
        else:
            info = stream_info() if callable(stream_info) else stream_info
            if info.manifest_type != 'mpd':
                return redirect(info.url, code=303)
            url, user_agent = info.url, info.user_agent
            sid, origins = uuid.uuid4().hex, []

        try:
            with _CDNSession() as session:
                if cached:
                    session.cookies.update(cached['cookies'])
                with session.get(url, headers={'User-Agent': user_agent}, timeout=10) as upstream:
                    upstream.raise_for_status()
                    tracks, is_dynamic = _parse_hls_tracks(
                        upstream.content, upstream.url,
                        f'{public_url.rstrip("/")}/segproxy/{sid}/', origins,
                    )
                    cookies = session.cookies.copy()
                    manifest_url = upstream.url
        except requests.RequestException as error:
            current_app.logger.warning('HLS DASH fetch failed: %s', type(error).__name__)
            abort(502)
        except (ET.ParseError, ValueError) as error:
            current_app.logger.warning('HLS DASH parse failed: %s', error)
            abort(502)

        now = time.monotonic()
        started_dynamic = (HLS_CACHE.get(sid) or {}).get('started_dynamic', is_dynamic)
        with SEGMENT_SESSIONS_LOCK:
            SEGMENT_SESSIONS[sid] = {
                'origins': origins, 'cookies': cookies, 'user_agent': user_agent,
                'expires': now + SEGMENT_SESSION_TTL_SECONDS,
                'manifest_url': manifest_url, 'manifest': b'', 'manifest_time': now,
                'live_windows': {},
            }
            HLS_CACHE[sid] = {
                'tracks': tracks,
                'is_dynamic': is_dynamic,
                'started_dynamic': started_dynamic,
                'time': now,
                'public_url': public_url,
            }
            if key is not None:
                CHANNEL_SESSIONS[key] = sid
        _persist_sessions(force=True)
        body = _render_hls_master(tracks, public_url, sid)
        resp = Response(body, mimetype='application/vnd.apple.mpegurl', headers={'Cache-Control': 'no-cache'})
        return apply_cors_headers(resp)


@dash_proxy.route('/hls/<sid>/<kind>/<int:track_idx>.m3u8', methods=['GET', 'HEAD', 'OPTIONS'])
def hls_media_playlist(sid, kind, track_idx):
    if request.method == 'OPTIONS':
        return Response(status=204)
    if kind not in ('video', 'audio'):
        return Response(status=404)

    now = time.monotonic()
    with SEGMENT_SESSIONS_LOCK:
        session_info = SEGMENT_SESSIONS.get(sid)
        if session_info is None:
            return Response(status=404)
        if session_info['expires'] <= now:
            return Response(status=410)
        session_info['expires'] = now + SEGMENT_SESSION_TTL_SECONDS
        hls_entry = HLS_CACHE.get(sid)
        need_refresh = hls_entry is None or (hls_entry['is_dynamic'] and now - hls_entry['time'] >= 3.0)
        lock = CHANNEL_LOCKS.setdefault(('hls_sid', sid), threading.Lock())

    if need_refresh:
        with lock:
            now = time.monotonic()
            with SEGMENT_SESSIONS_LOCK:
                session_info = SEGMENT_SESSIONS.get(sid)
                if session_info is None:
                    return Response(status=404)
                hls_entry = HLS_CACHE.get(sid)
                still_need = hls_entry is None or (hls_entry['is_dynamic'] and now - hls_entry['time'] >= 3.0)
                if still_need:
                    url = session_info['manifest_url']
                    user_agent = session_info['user_agent']
                    cookies = session_info['cookies']
                    origins = list(session_info['origins'])
                    public_url = (hls_entry or {}).get('public_url') or request.url_root.rstrip('/')
                    started_dynamic = (hls_entry or {}).get('started_dynamic')
            if still_need:
                try:
                    with _CDNSession() as session:
                        session.cookies.update(cookies)
                        with session.get(url, headers={'User-Agent': user_agent}, timeout=10) as upstream:
                            upstream.raise_for_status()
                            tracks, is_dynamic = _parse_hls_tracks(
                                upstream.content, upstream.url,
                                f'{public_url.rstrip("/")}/segproxy/{sid}/', origins,
                            )
                            new_cookies = session.cookies.copy()
                            new_url = upstream.url
                except (requests.RequestException, ET.ParseError, ValueError) as error:
                    current_app.logger.warning('HLS refresh failed for %s: %s', sid, type(error).__name__)
                    if hls_entry is None:
                        abort(502)
                else:
                    if started_dynamic is None:
                        started_dynamic = is_dynamic
                    with SEGMENT_SESSIONS_LOCK:
                        if sid in SEGMENT_SESSIONS:
                            SEGMENT_SESSIONS[sid]['origins'] = origins
                            SEGMENT_SESSIONS[sid]['cookies'] = new_cookies
                            SEGMENT_SESSIONS[sid]['manifest_url'] = new_url
                            SEGMENT_SESSIONS[sid]['expires'] = time.monotonic() + SEGMENT_SESSION_TTL_SECONDS
                        hls_entry = {
                            'tracks': tracks,
                            'is_dynamic': is_dynamic,
                            'started_dynamic': started_dynamic,
                            'time': time.monotonic(),
                            'public_url': public_url,
                            'timescales': (HLS_CACHE.get(sid) or {}).get('timescales', {}),
                        }
                        HLS_CACHE[sid] = hls_entry

    tracks_list = hls_entry['tracks'].get(kind, [])
    if track_idx < 0 or track_idx >= len(tracks_list):
        return Response(status=404)
    body = _render_hls_media(tracks_list[track_idx], hls_entry['is_dynamic'], hls_entry['started_dynamic'])
    return Response(body, mimetype='application/vnd.apple.mpegurl', headers={'Cache-Control': 'no-cache'})
