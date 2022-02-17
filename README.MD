# Magio GO IPTV Server
This server application generates an .m3u8 playlist and XMLTV guide from the
online TV Magio GO from Slovak Telekom.

A valid subscription to the Magio GO service is required. The application authenticates with Magio
servers with provided credentials. This counts as a device to the maximum device
count limit.

## Why was this created?

Magio GO provides already provides client applications for Web, Mobile and TV platform.
However, there may be devices that are not supported. This application makes it possible to watch Magio GO in an
IPTV client of your choice, VLC, Plex, Jellyfin and any players that support the .m3u8 format.
 
Plex includes a Live TV & DVR feature. It however doesn't support .m3u8 playlists directly.
An M3U proxy application, such as [xTeVe](https://xteve.de/) is needed. It consumes multiple
.m3u8 playlists and much more, while acting as a DVB tuner device for Plex.
This way, you can watch and record online IPTV (and now also Magio GO) inside Plex.

![](preview_plexDVR.jpg?raw=true)

## How does it work
This application builds on top of Sorien's work of [MagioGo Kodi addon](https://github.com/Sorien/plugin.video.iptv.magiogo),
which integrates Magio GO in Kodi. It handles the authentication with Magio servers
and retrieval of stream and EPG information.

Magio GO IPTV Server works as a web server, listening for stream requests.
When a request for a particular channel arrives, it retrieves the stream URL from
Magio servers using the authenticated connection and redirects the client to this URL.

Moreover, the app generates and hosts an .m3u8 playlist with the list of 
channels gathered from Magio, together with URLs pointing to the web server as
explained above.

Along with the .m3u8 playlist, .xmltv and .xmltv gzipped XMLTV formatted 
files are generated from the Magio EPG.
 
After launching the program, the generated files are available at:

`127.0.0.1:5000/magioPlaylist.m3u8`

`127.0.0.1:5000/magioGuide.xmltv`

`127.0.0.1:5000/magioGuide.xmltv.gz`


## Setup instructions
You can run this program either directly with Python 3
or on Docker.

The credentials to Magio GO and other options are passed through environment variables,
so they need to be set accordingly, depending on the platform.

**MAGIO_SERVER_PUBLIC_URL** is used in the generated .m3u8 file in the stream URLs.
It should be a publicly (or privately) reachable URL from IPTV clients, to this program.

**MAGIO_GUIDE_DAYS** signifies how many days from today should be loaded
into the XMLTV guide on refresh.

**MAGIO_GUIDE_REFRESH_HOURS** is the number of hours between the guide and channel playlist updates.

**MAGIO_QUALITY** determines the quality of the video stream. Values can be LOW/MEDIUM/HIGH/EXTRA.

### Python
```shell script
# install dependencies
pip install -r requirements.txt

# set options
set MAGIO_USERNAME=your_username
set MAGIO_PASSWORD=your_password
# optional, default is http://127.0.0.1:5000
set MAGIO_SERVER_PUBLIC_URL=http://127.0.0.1:5000
# optional, default is 7
set MAGIO_GUIDE_DAYS=7
# optional, default is 12
set MAGIO_GUIDE_REFRESH_HOURS=12
# optional, default is HIGH
set MAGIO_QUALITY=HIGH

# run the application
flask run --host=0.0.0.0
```


### Docker
```shell script
docker run -p 5000:5000 --env MAGIO_USERNAME=your_username --env MAGIO_PASSWORD=your_password --env MAGIO_SERVER_PUBLIC_URL=http://127.0.0.1:5000 --env MAGIO_GUIDE_DAYS=7 --env MAGIO_GUIDE_REFRESH_HOURS=12 --name magiogo-iptv-server cvb941/magiogo-iptv-server
```

### Docker Compose
```yaml
version: '3'
services:
    magiogo-iptv-server:
        image: cvb941/magiogo-iptv-server
        ports:
            - '5000:5000'
        environment:
            - MAGIO_USERNAME=your_username
            - MAGIO_PASSWORD=your_password
            - MAGIO_SERVER_PUBLIC_URL=http://127.0.0.1:5000
            - MAGIO_GUIDE_DAYS=7
            - MAGIO_GUIDE_REFRESH_HOURS=12
            - MAGIO_QUALITY=HIGH
        container_name: magiogo-iptv-server
```

## Season parsing included
EPG data for shows from Magio GO contains episode numbers, but for some reason, the season
numbers are always null.

Since in most cases the season number is included in the title (e.g. Bones IX.),
we can parse the season number from there (e.g. Bones IX. -> 9).

In order not to confuse DVR programs like Plex, the season numbering is then
removed from the title. This way, recorded episodes from different seasons do not
have different titles and are considered to be the same show by Plex.

That being said, the episode numbering data from Magio is sometimes completely missing for
some shows, which unfortunately causes DVRs like Plex to consider them as movies.
## Credits
Sorien - https://github.com/Sorien/plugin.video.iptv.magiogo

rmich - Additional fixes (https://www.xbmc-kodi.cz/prispevek-magio-go-addon?pid=104154#pid104154)
