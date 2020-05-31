# bude pocuvat na http na channelid a
#  ten posunie do url z magiogo programme_streaminfo
from pathlib import Path

import xmltv
from flask import Flask, redirect, send_file, send_from_directory
from magiogo import *

app = Flask(__name__)
Path("public").mkdir(exist_ok=True)


@app.route('/<file_name>')
def public_files(file_name):
    return send_from_directory("public", filename=file_name)


@app.route('/channel/<channel_id>')
def channel_redirect(channel_id):
    stream_info = magio.channel_stream_info(channel_id)
    return redirect(stream_info.url, code=303)


print("Logging in to Magio Go TV")
magio = MagioGo(os.environ.get('MAGIO_USERNAME'), os.environ.get('MAGIO_PASSWORD'), MagioQuality.extra)

print("Generating .m3u8 playlist")

channels = magio.channels()
channel_ids = list(map(lambda c: c.id, channels))
# streamInfo = magio.channel_stream_info(channels[0].id)
with open("public/magioPlaylist.m3u8", "w", encoding="utf-8") as text_file:
    text_file.write("#EXTM3U\n")
    for channel in channels:
        text_file.write(f'#EXTINF:-1 tvg-id="{channel.id}" tvg-logo="{channel.logo}",{channel.name}\n')
        text_file.write(f"http://127.0.0.1:5000/channel/{channel.id}\n")

print("Generating XMLTV guide")
date_from = datetime.datetime.now() - datetime.timedelta(days=0)
date_to = datetime.datetime.now() + datetime.timedelta(days=1)
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
                # 'category': [(u'Comedy', u'')],
                'channel': channel_id,
                # 'credits': [{'producer': [u'Larry David'], 'actor': [u'Jerry Seinfeld']}],
                'date': str(programme.year),
                'desc': [(programme.description,
                          u'')],
                # 'episode-num': [(u'7 . 1 . 1/1', u'xmltv_ns')],
                'length': {'units': u'seconds', 'length': str(programme.duration)},
                'start': programme.start_time.strftime("%Y%m%d%H%M%S"),
                'stop': programme.end_time.strftime("%Y%m%d%H%M%S"),
                'title': [(programme.title, u'')]}
            writer.addProgramme(programme_dict)

    writer.write(guide_file, True)
