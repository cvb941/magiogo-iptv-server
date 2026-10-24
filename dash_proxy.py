import threading
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from flask import Blueprint, Response, abort, current_app, redirect, request, stream_with_context

ET.register_namespace('', 'urn:mpeg:dash:schema:mpd:2011')

dash_proxy = Blueprint('dash_proxy', __name__)
SEGMENT_SESSIONS = {}
SEGMENT_SESSIONS_LOCK = threading.Lock()
SEGMENT_SESSION_TTL_SECONDS = 30 * 60
CHANNEL_SESSIONS = {}
CHANNEL_LOCKS = {}


def _rewrite_manifest(content, manifest_url, proxy_base, origins):
    root = ET.fromstring(content)
    namespace = root.tag[:-3] if root.tag.endswith('}MPD') else ''
    if root.tag != namespace + 'MPD':
        raise ValueError('Expected an MPD')

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
        if cached and cached['expires'] <= now:
            cached = None
        if cached:
            cached['expires'] = now + SEGMENT_SESSION_TTL_SECONDS
            if now - cached['manifest_time'] < 2:
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
        with requests.Session() as session:
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
            with SEGMENT_SESSIONS_LOCK:
                CHANNEL_SESSIONS.pop(key, None)
            return _fetch_manifest(stream_info, public_url, key)
        abort(502)
    except (ET.ParseError, ValueError) as error:
        current_app.logger.warning('DASH manifest fallback: %s', error)
        return redirect(url, code=303)

    now = time.monotonic()
    with SEGMENT_SESSIONS_LOCK:
        for expired_sid in [key for key, value in SEGMENT_SESSIONS.items() if value['expires'] <= now]:
            del SEGMENT_SESSIONS[expired_sid]
        SEGMENT_SESSIONS[sid] = {
            'origins': origins, 'cookies': cookies, 'user_agent': user_agent,
            'expires': now + SEGMENT_SESSION_TTL_SECONDS,
            'manifest_url': manifest_url, 'manifest': manifest, 'manifest_time': now,
        }
        if key is not None:
            CHANNEL_SESSIONS[key] = sid
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
            del SEGMENT_SESSIONS[sid]
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
        upstream = requests.get(target_url, cookies=session_info['cookies'], headers=headers, stream=True, timeout=15)
    except requests.RequestException:
        abort(502)

    def chunks():
        try:
            yield from upstream.iter_content(chunk_size=65536)
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
