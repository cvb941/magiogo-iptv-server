import atexit
import datetime
import gzip
import os
import sys
from pathlib import Path

import xmltv
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, redirect, render_template
from tqdm import tqdm

from dash_proxy import dash_proxy, proxy_manifest
from magiogo import *
from parse_season_number import parse_season_number

app = Flask(__name__, static_url_path="/", static_folder="public")
app.register_blueprint(dash_proxy)

# Ensure public dir exists
Path("public").mkdir(exist_ok=True)

last_refresh = None


@app.route('/')
def index():
    return render_template("index.html", last_refresh=last_refresh)


@app.route('/channel/<channel_id>')
def channel_redirect(channel_id):
    magio_iptv_server_public_url = os.environ.get('MAGIO_SERVER_PUBLIC_URL', "http://127.0.0.1:5000")
    return proxy_manifest(lambda: magio.channel_stream_info(channel_id), magio_iptv_server_public_url, channel_id)


@app.errorhandler(404)
def page_not_found(e):
    # Redirect all to index page
    return redirect('/')


def gzip_file(file_path):
    with open(file_path, 'rb') as src, gzip.open(f'{file_path}.gz.tmp', 'wb') as dst:
        dst.writelines(src)
    os.replace(f'{file_path}.gz.tmp', f'{file_path}.gz')


def generate_m3u8(channels):
    magio_iptv_server_public_url = os.environ.get('MAGIO_SERVER_PUBLIC_URL', "http://127.0.0.1:5000")
    with open("public/magioPlaylist.m3u8.tmp", "w", encoding="utf-8") as text_file:
        text_file.write("#EXTM3U\n")
        for channel in tqdm(channels, total=len(channels), desc="Generating .m3u8 playlist", unit="ch", file=sys.stdout):
            text_file.write(f'#EXTINF:-1 tvg-id="{channel.id}" tvg-logo="{channel.logo}",{channel.name}\n')
            text_file.write(f"{magio_iptv_server_public_url}/channel/{channel.id}\n")
    os.replace("public/magioPlaylist.m3u8.tmp", "public/magioPlaylist.m3u8")


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
    with open("public/magioGuide.xmltv.tmp", "wb") as guide_file:
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
    os.replace("public/magioGuide.xmltv.tmp", "public/magioGuide.xmltv")
    # Gzip the guide file
    gzip_file("public/magioGuide.xmltv")


def refresh():
    try:
        print("Refreshing playlist and XMLTV guide in the background")
        channels = magio.channels()
        generate_m3u8(channels)
        generate_xmltv(channels)
        print("Refreshing finished!")
        global last_refresh
        last_refresh = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        app.logger.exception("Guide refresh failed; retrying in 5 minutes")
        scheduler.modify_job('guide-refresh', next_run_time=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=5))


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

magio = MagioGo("./storage", magio_username, magio_password, quality, device_type, device_name)

# Start HTTP without waiting for Magio; only one refresh can run at a time.
refresh_hours = int(os.environ.get('MAGIO_GUIDE_REFRESH_HOURS', 12))
next_refresh = datetime.datetime.now(datetime.timezone.utc)
guide_files = [Path('public') / name for name in ('magioPlaylist.m3u8', 'magioGuide.xmltv', 'magioGuide.xmltv.gz')]
if all(path.is_file() and path.stat().st_size for path in guide_files):
    cached_refresh = min(next_refresh.timestamp(), *(path.stat().st_mtime for path in guide_files))
    last_refresh = datetime.datetime.fromtimestamp(cached_refresh).strftime("%d/%m/%Y %H:%M:%S")
    next_refresh = max(next_refresh, datetime.datetime.fromtimestamp(cached_refresh, datetime.timezone.utc)
                       + datetime.timedelta(hours=refresh_hours))
scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(refresh, 'interval', id='guide-refresh',
                  hours=refresh_hours, next_run_time=next_refresh,
                  max_instances=1, coalesce=True)
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))
