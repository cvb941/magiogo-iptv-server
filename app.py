import atexit
import gzip
import re
from pathlib import Path
from time import strftime

import roman as roman
import xmltv
from flask import Flask, redirect, send_file, send_from_directory, url_for, render_template, g
from magiogo import *
from apscheduler.schedulers.background import BlockingScheduler, BackgroundScheduler

app = Flask(__name__)

# Ensure public dir exists
Path("public").mkdir(exist_ok=True)

last_refresh = None


@app.route('/')
def index():
    return render_template("index.html", last_refresh=last_refresh)


@app.route('/<file_name>')
def public_files(file_name):
    return send_from_directory("public", filename=file_name)


@app.route('/channel/<channel_id>')
def channel_redirect(channel_id):
    stream_info = magio.channel_stream_info(channel_id)
    return redirect(stream_info.url, code=303)


@app.errorhandler(404)
def page_not_found(e):
    # Redirect all to index page
    return redirect('/')


def gzip_file(file_path):
    with open(file_path, 'rb') as src, gzip.open(f'{file_path}.gz', 'wb') as dst:
        dst.writelines(src)


def parse_season_number(show_title):
    """Tries to parse season number in roman numbers from show title using regex. Bones IX. -> 9"""
    regex = r" M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\.?$"
    matches = re.search(regex, show_title)
    if matches:
        roman_num = matches.group()
        # Remove leading space and trailing dot if present
        roman_num = roman_num.strip(' .')
        # Convert roman numeral to arabic
        return roman.fromRoman(roman_num)
    else:
        return None


def generate_m3u8(channels):
    magio_iptv_server_public_url = os.environ.get('MAGIO_SERVER_PUBLIC_URL', "http://127.0.0.1:5000")
    with open("public/magioPlaylist.m3u8", "w", encoding="utf-8") as text_file:
        text_file.write("#EXTM3U\n")
        for channel in channels:
            text_file.write(f'#EXTINF:-1 tvg-id="{channel.id}" tvg-logo="{channel.logo}",{channel.name}\n')
            text_file.write(f"{magio_iptv_server_public_url}/channel/{channel.id}\n")


def generate_xmltv(channels):
    date_from = datetime.datetime.now() - datetime.timedelta(days=0)
    date_to = datetime.datetime.now() + datetime.timedelta(days=int(os.environ.get('MAGIO_GUIDE_DAYS', 7)))
    channel_ids = list(map(lambda c: c.id, channels))
    epg = magio.epg(channel_ids, date_from, date_to)
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
                        programme.seasonNo = parse_season_number(programme.title)

                    programme_dict['episode-num'] = [
                        (f'{(programme.seasonNo or 1) - 1} . {(programme.episodeNo or 1) - 1} . 0', u'xmltv_ns')]

                writer.addProgramme(programme_dict)

        writer.write(guide_file, True)
    # Gzip the guide file
    gzip_file("public/magioGuide.xmltv")


def refresh():
    channels = magio.channels()

    print("Generating .m3u8 playlist")
    generate_m3u8(channels)

    print("Generating XMLTV guide")
    generate_xmltv(channels)

    print("Refreshing finished!")
    global last_refresh
    last_refresh = datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")


# Initial playlist and xmltv load
print("Logging in to Magio Go TV")
magio = MagioGo(os.environ.get('MAGIO_USERNAME'), os.environ.get('MAGIO_PASSWORD'), MagioQuality.extra)
refresh()

# Load new playlist and xmltv everyday
scheduler = BackgroundScheduler()
scheduler.add_job(refresh, 'interval', hours=int(os.environ.get('MAGIO_GUIDE_REFRESH_HOURS', 12)))
scheduler.start()
atexit.register(lambda: scheduler.shutdown())
