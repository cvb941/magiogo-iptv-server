import atexit
import datetime
import gzip
import os
import re
import sys
import time
import threading
import uuid
from pathlib import Path

import requests
import xmltv
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, abort, redirect, render_template, request, stream_with_context
from tqdm import tqdm

from magiogo import *
from parse_season_number import parse_season_number

app = Flask(__name__, static_url_path="/", static_folder="public")

# Ensure public dir exists
Path("public").mkdir(exist_ok=True)

last_refresh = None

# DASH segment proxy sessions: sid -> {base, cookies, user_agent, expires}
#
# Magio's CDN authorizes video/audio fragment requests via a session
# cookie set on the manifest response (the manifest URL itself carries a
# signed query string, but the SegmentTemplate paths inside it don't -
# only the cookie ties them back to that session). ffmpeg's dash demuxer
# also resolves those relative SegmentTemplate paths against the URL it
# originally requested rather than the URL it ends up at after following
# redirects, so a bare 303 to the real CDN manifest breaks fragment
# fetches ("Invalid data found when processing input"). Fetching the
# manifest here, rewriting its BaseURL to point back at us, and proxying
# fragment requests through with the captured cookie sidesteps both
# problems.
SEGMENT_SESSIONS = {}
SEGMENT_SESSIONS_LOCK = threading.Lock()
SEGMENT_SESSION_TTL_SECONDS = 30 * 60


def _cleanup_segment_sessions():
    now = time.time()
    with SEGMENT_SESSIONS_LOCK:
        expired = [sid for sid, s in SEGMENT_SESSIONS.items() if s['expires'] < now]
        for sid in expired:
            del SEGMENT_SESSIONS[sid]


@app.route('/')
def index():
    return render_template("index.html", last_refresh=last_refresh)


@app.route('/channel/<channel_id>')
def channel_redirect(channel_id):
    stream_info = magio.channel_stream_info(channel_id)

    if stream_info.manifest_type != 'mpd':
        return redirect(stream_info.url, code=303)

    session = requests.Session()
    resp = session.get(stream_info.url, headers={'User-Agent': stream_info.user_agent}, timeout=10)
    resp.raise_for_status()

    base_dir = resp.url.rsplit('/', 1)[0] + '/'

    sid = uuid.uuid4().hex
    _cleanup_segment_sessions()
    with SEGMENT_SESSIONS_LOCK:
        SEGMENT_SESSIONS[sid] = {
            'base': base_dir,
            'cookies': session.cookies,
            'user_agent': stream_info.user_agent,
            'expires': time.time() + SEGMENT_SESSION_TTL_SECONDS,
        }

    magio_iptv_server_public_url = os.environ.get('MAGIO_SERVER_PUBLIC_URL', "http://127.0.0.1:5000")
    proxy_base = f"{magio_iptv_server_public_url}/segproxy/{sid}/"

    manifest = resp.text
    manifest, count = re.subn(r'(<MPD\b[^>]*>)', r'\1<BaseURL>' + proxy_base + '</BaseURL>', manifest, count=1)
    if count == 0:
        # Unexpected manifest shape - fall back to the old behaviour rather than serving something broken.
        return redirect(stream_info.url, code=303)

    return Response(manifest, mimetype='application/dash+xml')


@app.route('/segproxy/<sid>/<path:subpath>')
def segment_proxy(sid, subpath):
    with SEGMENT_SESSIONS_LOCK:
        session_info = SEGMENT_SESSIONS.get(sid)

    if session_info is None:
        abort(404)

    target_url = session_info['base'] + subpath
    if request.query_string:
        target_url += '?' + request.query_string.decode()

    upstream = requests.get(
        target_url,
        cookies=session_info['cookies'],
        headers={'User-Agent': session_info['user_agent']},
        stream=True,
        timeout=15,
    )

    return Response(
        stream_with_context(upstream.iter_content(chunk_size=65536)),
        status=upstream.status_code,
        content_type=upstream.headers.get('Content-Type', 'application/octet-stream'),
    )


@app.errorhandler(404)
def page_not_found(e):
    # Redirect all to index page
    return redirect('/')


def gzip_file(file_path):
    with open(file_path, 'rb') as src, gzip.open(f'{file_path}.gz', 'wb') as dst:
        dst.writelines(src)


def generate_m3u8(channels):
    magio_iptv_server_public_url = os.environ.get('MAGIO_SERVER_PUBLIC_URL', "http://127.0.0.1:5000")
    with open("public/magioPlaylist.m3u8", "w", encoding="utf-8") as text_file:
        text_file.write("#EXTM3U\n")
        for channel in tqdm(channels, total=len(channels), desc="Generating .m3u8 playlist", unit="ch", file=sys.stdout):
            text_file.write(f'#EXTINF:-1 tvg-id="{channel.id}" tvg-logo="{channel.logo}",{channel.name}\n')
            text_file.write(f"{magio_iptv_server_public_url}/channel/{channel.id}\n")


def generate_xmltv(channels):
    date_from = datetime.datetime.now() - datetime.timedelta(days=0)
    date_to = datetime.datetime.now() + datetime.timedelta(days=int(os.environ.get('MAGIO_GUIDE_DAYS', 7)))
    channel_ids = list(map(lambda c: c.id, channels))
    with tqdm(total=100, desc="Generating XMLTV guide", unit="pct", file=sys.stdout) as bar:
        last_progress = {'value': 0}

        def epg_progress(percent):
            percent = max(0, min(100, int(percent)))
            if percent <= last_progress['value']:
                return
            bar.update(percent - last_progress['value'])
            last_progress['value'] = percent

        epg = magio.epg(channel_ids, date_from, date_to, progress=epg_progress)
        epg_progress(100)
    with open("public/magioGuide.xmltv", "wb") as guide_file:
        writer = xmltv.Writer(
            date=datetime.datetime.now().strftime("%Y%m%d%H%M%S"),
            generator_info_name="MagioGoIPTVServer",
            generator_info_url="",
            source_info_name="Magio GO Guide",
            source_info_url="https://skgo.magio.tv/v2/television/epg")
        # Write channels
        for channel in channels:
            channel_dict = {'display-name': [(channel.name, u'sk')],
                            'icon': [{'src': channel.logo}],
                            'id': channel.id}
            writer.addChannel(channel_dict)
        # Write programmes
        for (channel_id, programmes) in epg.items():
            for programme in programmes:
                programme_dict = {
                    'category': [(genre, u'en') for genre in programme.genres],
                    'channel': channel_id,
                    'credits': {'producer': [producer for producer in programme.producers],
                                'actor': [actor for actor in programme.actors],
                                'writer': [writer for writer in programme.writers],
                                'director': [director for director in programme.directors]},
                    'date': str(programme.year),
                    'desc': [(programme.description,
                              u'')],
                    'icon': [{'src': programme.poster}, {'src': programme.thumbnail}],
                    'length': {'units': u'seconds', 'length': str(programme.duration)},
                    'start': programme.start_time.strftime("%Y%m%d%H%M%S"),
                    'stop': programme.end_time.strftime("%Y%m%d%H%M%S"),
                    'title': [(programme.title, u'')]}

                # Define episode info only if provided
                if programme.episodeNo is not None:
                    # Since seasonNo seems to be always null, try parsing the season from the title (e.g. Kosti X. = 10)
                    if programme.seasonNo is None:
                        (show_title_sans_season, programme.seasonNo) = parse_season_number(programme.title)
                        programme_dict['title'] = [(show_title_sans_season, u'')]

                    programme_dict['episode-num'] = [
                        (f'{(programme.seasonNo or 1) - 1} . {(programme.episodeNo or 1) - 1} . 0', u'xmltv_ns')]

                writer.addProgramme(programme_dict)

        writer.write(guide_file, True)
    # Gzip the guide file
    gzip_file("public/magioGuide.xmltv")


def refresh():
    channels = magio.channels()

    generate_m3u8(channels)

    generate_xmltv(channels)

    print("Refreshing finished!")
    global last_refresh
    last_refresh = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")


# Quality config
qualityString = os.environ.get('MAGIO_QUALITY', "HIGH")
qualityMapping = {"LOW": MagioQuality.low, "MEDIUM": MagioQuality.medium, "HIGH": MagioQuality.high, "EXTRA": MagioQuality.extra}
quality = qualityMapping[qualityString]
print(f"Stream quality configured to: {qualityString} ({quality})")
device_type = os.environ.get('MAGIO_DEVICE_TYPE', "OTT_STB")
print(f"Device type configured to: {device_type}")
device_name = os.environ.get('MAGIO_DEVICE_NAME', "Magio IPTV Server")
print(f"Device name configured to: {device_name}")

# Initial playlist and xmltv load
magio_username = os.environ.get('MAGIO_USERNAME')
magio_password = os.environ.get('MAGIO_PASSWORD')
missing_credentials = [name for name, value in (('MAGIO_USERNAME', magio_username), ('MAGIO_PASSWORD', magio_password)) if not value]
if missing_credentials:
    raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing_credentials)}")

print("Logging in to Magio Go TV")
magio = MagioGo("./storage", magio_username, magio_password, quality, device_type, device_name)
refresh()

# Load new playlist and xmltv everyday
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(refresh, 'interval', hours=int(os.environ.get('MAGIO_GUIDE_REFRESH_HOURS', 12)))
scheduler.start()
atexit.register(lambda: scheduler.shutdown())
