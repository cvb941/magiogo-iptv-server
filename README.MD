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
Magio servers using the authenticated connection. DASH manifests and segments are
proxied with the CDN session cookies so FFmpeg-based players can open them.
HLS streams and unsupported DASH manifests use a redirect to the original URL.

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
You can run this program either directly with Python 3.15
or on Docker.

The credentials to Magio GO and other options are passed through environment variables,
so they need to be set accordingly, depending on the platform.

**MAGIO_SERVER_PUBLIC_URL** is used in the generated .m3u8 file in the stream URLs.
It should be a publicly (or privately) reachable URL from IPTV clients, to this program.

**MAGIO_GUIDE_DAYS** signifies how many days from today should be loaded
into the XMLTV guide on refresh.

**MAGIO_GUIDE_REFRESH_HOURS** is the number of hours between the guide and channel playlist updates.

**MAGIO_QUALITY** determines the quality of the video stream. Values can be LOW/MEDIUM/HIGH/EXTRA.

**MAGIO_DEVICE_TYPE** sets the device type sent to Magio APIs. The default is OTT_STB.
You can also use **OTT_ANDROID** for a mobile profile.

**MAGIO_DEVICE_NAME** sets the device name sent to Magio APIs. The default is Magio IPTV Server.

The HTTP server starts immediately while playlist and guide updates run in the background.
After a restart, complete cached files are reused until the configured refresh interval expires.
Failed updates are retried after five minutes; existing files remain available until their replacements are ready.
Playback requests take priority over guide updates. Magio API calls are spaced at least one second apart,
with a six-second gap before guide requests and a shared cooldown honoring `Retry-After` after rate limits.
Guide requests slow down further if Magio still reports a rate limit.
Guide pages request up to 100 channels per call.
When a channel's requested quality is unavailable, a verified medium-quality fallback is remembered
for one hour in `storage`; the requested quality is checked again after that period.
Verified DASH URLs, CDN cookies and proxy session IDs are saved privately in `storage/dash-sessions.json`.
They survive restarts for up to 30 minutes of inactivity, scoped to the account, device and quality settings.
Restored channels fetch a fresh CDN manifest without calling Magio; rejected sessions request a new stream URL.
Authentication updates are saved atomically only after a complete login or token refresh succeeds.
Failed attempts preserve the previous session; token files are written with owner-only permissions.

`/health` checks the local HTTP server and is used by the Docker healthcheck. Upstream outages do not
make the container unhealthy. `/status` reports authentication and guide timestamps, API rate limits,
CDN proxy errors, and session cache usage without making upstream requests or exposing credentials.
Counters reset on restart; timestamps use UTC. Successful authentication timestamps are saved with
the session after the next login or token refresh, so older sessions can initially show `null`.

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
# optional, default is OTT_STB
set MAGIO_DEVICE_TYPE=OTT_STB
# optional, default is Magio IPTV Server
set MAGIO_DEVICE_NAME=Magio IPTV Server

# run the application
flask run --host=0.0.0.0
```

On Linux, use `gunicorn --config gunicorn.conf.py app:app` for production.
The Docker image uses this command with one threaded worker and 16 threads.
A small `gthread` subclass drains closing sockets in request threads so FFmpeg clients
cannot stall the worker's accept loop.
Keep preloading disabled and the worker count at one: the guide scheduler and live session maps
belong to that worker. Shutdown cancels queued API work and saves CDN sessions before exiting.
Gunicorn allows up to eight seconds to drain requests; Compose gives the container 15 seconds to stop.
The Flask command remains available for local development on Windows.

Idle DASH sessions expire after 30 minutes. Their private CDN URL and cookies are kept for another
24 hours as candidates: opening the channel first fetches a current manifest from CDN, and only
contacts Magio if that fails. Expired candidates cannot serve segments until revalidated.
If Magio requests a cooldown longer than five seconds, playback returns `503` with `Retry-After`
instead of holding the HTTP request open beyond Threadfin's startup timeout. Guide refreshes
continue to respect the full upstream cooldown.

### Docker
```shell script
docker run -p 5000:5000 --env MAGIO_USERNAME=your_username --env MAGIO_PASSWORD=your_password --env MAGIO_SERVER_PUBLIC_URL=http://127.0.0.1:5000 --env MAGIO_GUIDE_DAYS=7 --env MAGIO_GUIDE_REFRESH_HOURS=12 --env MAGIO_DEVICE_TYPE=OTT_STB --env MAGIO_DEVICE_NAME="Magio IPTV Server" --name magiogo-iptv-server cvb941/magiogo-iptv-server
```

### Docker Compose
The image runs as UID/GID `10001:10001`. Create writable bind mounts before starting it;
for existing installations, stop the service and adjust ownership of the two mounted directories.
Session files should remain private (`0600`, with the storage directory `0700`).

```shell
mkdir -p storage public
sudo chown -R 10001:10001 storage public
sudo chmod 700 storage
```

```yaml
version: '3'
services:
    magiogo-iptv-server:
        image: cvb941/magiogo-iptv-server
        ports:
            - '5000:5000'
        volumes:
            - ./storage:/app/storage
            - ./public:/app/public
        environment:
            - MAGIO_USERNAME=your_username
            - MAGIO_PASSWORD=your_password
            - MAGIO_SERVER_PUBLIC_URL=http://127.0.0.1:5000
            - MAGIO_GUIDE_DAYS=7
            - MAGIO_GUIDE_REFRESH_HOURS=12
            - MAGIO_QUALITY=HIGH
            - MAGIO_DEVICE_TYPE=OTT_STB
            - MAGIO_DEVICE_NAME=Magio IPTV Server
        container_name: magiogo-iptv-server
        stop_grace_period: 15s
```

### Threadfin

Select FFmpeg as the Magio playlist's buffer. These FFmpeg options copy video and
resample audio to avoid repeated audio timestamps when the live DASH manifest updates:

```text
-hide_banner -nostats -loglevel info -analyzeduration 250000 -probesize 262144 -reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 2 -rw_timeout 15000000 -i [URL] -sn -dn -c:v copy -c:a aac -b:a 192k -af aresample=async=1:first_pts=0 -muxdelay 0 -muxpreload 0 -flush_packets 1 -f mpegts pipe:1
```

Keep `-nostats` when using informational logging: FFmpeg's progress output uses carriage
returns rather than newlines. Threadfin's line reader can stop draining that output after
several minutes, filling the stderr pipe and blocking playback.

### Xtream API and Jellyfin archive

Enable the optional API with separate local credentials:

```text
XTREAM_USERNAME=iptv
XTREAM_PASSWORD=your_random_url_safe_password
# Optional: address reachable from Jellyfin and its players; otherwise the request address is used.
XTREAM_PUBLIC_URL=http://your-server:5000
```

Add these environment variables to the Magio server container. Use a different password from
your Magio account. The API is disabled unless both credentials are set.

Install [Jellyfin.Xtream](https://github.com/Kevinjil/Jellyfin.Xtream) using its plugin repository:

```text
https://kevinjil.github.io/Jellyfin.Xtream/repository.json
```

In the plugin's **Credentials** tab, enter the Magio server's base URL and the local credentials
above. In **Live TV**, select the channels and enable **Show the catch-up channel to users**.
The **Xtream Catch-up** section lists channels, days and finished programmes. It is separate from
Jellyfin's Live TV guide. The plugin also exposes selected live channels; selecting the same channels
as an existing Threadfin tuner can produce duplicates.

This adapter serves native **DASH (`mpd`)**, which Jellyfin probes and can seek within for archive
programmes. High-precision epoch timelines are rescaled exactly to avoid integer overflow in FFmpeg's
seek calculations, preserving the media timestamps and original CDN segment names.
It does not convert streams to MPEG-TS or HLS; clients requiring `ts` or `m3u8` output
are unsupported. Existing M3U/XMLTV and Threadfin endpoints continue to work independently.

Implemented endpoints: `/player_api.php`, `/get.php`, `/xmltv.php`, live DASH URLs and
`/streaming/timeshift.php` (also `/timeshift/{username}/{password}/{duration}/{start}/{channel}.mpd`).
Times use UTC; duration is in minutes and start uses `YYYY-MM-DD:HH-MM`. Archive requests select
a complete EPG programme within the channel's archive window, up to seven days. Arbitrary time ranges
are rejected. `get_simple_data_table` lists available finished programmes; `get_short_epg` lists
current and upcoming programmes. VOD and series lists are empty.

Channel and per-channel EPG caches are stored privately in `storage/xtream.json` and reused after
restarts. EPG is fetched on demand and cached for 15 minutes. The API honors the existing Magio
cooldown and returns `503` with `Retry-After` when upstream is unavailable. Xtream URLs contain the
local credentials, so use HTTPS or a trusted private network and avoid logging request URLs.

### Tests

CI uses Python 3.15.1 (also recorded in `.python-version`) and runs the complete suite as the application user in the
production Alpine environment with external networking disabled. The Docker test target adds
FFmpeg to verify actual audio/video decoding through Gunicorn, CDN cookies and redirects,
and archive seeking across a backend restart with restored sessions, including epoch-based timelines.
The production image does not include FFmpeg.

```shell
docker build --target test -t magio-test .
docker run --rm --network none magio-test
```

To verify an installed Threadfin, run this on its Docker host with no other active streams:

```shell
python3 tools/threadfin_smoke.py --playlist /path/to/threadfin.m3u --channel "Jednotka HD" --backend-url http://127.0.0.1:5678
```

This restarts the backend container while reading the same Threadfin HTTP connection and
decoding its video and audio. It checks continued playback, retained FFmpeg process,
restored CDN sessions and no new Magio API requests. It prints timings without signed URLs or cookies.
Use `--duration 720` for a twelve-minute stability check. If other channels are active,
add `--source-channel-id` with the tested channel's Magio ID to scope the FFmpeg process check.

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

## License

[MIT](LICENSE).
