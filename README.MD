# Magio GO IPTV Server
- [ ] what
- [ ] why
- [ ] how

bude pocuvat na http na channelid a
ten posunie do url z magiogo programme_streaminfo

## Setup instructions
You can run this program either directly with Python 3
or on Docker.

The credentials to Magio GO are passed through environment variables,
so they need to be set depending on the platform accordingly.

MAGIO_SERVER_PUBLIC_URL is used in the generated .m3u8 file in the stream URLs.
It should be the publicly (or privately) reachable URL from IPTV clients, to this program.

MAGIO_GUIDE_DAYS signifies how many days from today should be loaded into the XMLTV guide.

MAGIO_GUIDE_REFRESH_HOURS is the number of hours between the guide and channel playlist updates.

### Python
```shell script
set MAGIO_USERNAME=your_username
set MAGIO_PASSWORD=your_password
# optional, default is http://127.0.0.1:5000
set MAGIO_SERVER_PUBLIC_URL=http://127.0.0.1:5000
# optional, default is 7
set MAGIO_GUIDE_DAYS=7
# optional, default is 12
set MAGIO_GUIDE_REFRESH_HOURS=12
flask run
```


### Docker
```shell script
docker build -t magiogo-iptv-server .
docker run -p 5000:5000 --env MAGIO_USERNAME=your_username --env MAGIO_PASSWORD=your_password --env MAGIO_SERVER_PUBLIC_URL=http://127.0.0.1:5000 --env MAGIO_GUIDE_DAYS=7 --env MAGIO_GUIDE_REFRESH_HOURS=12 --name magiogo-iptv-server magiogo-iptv-server
```

### Docker Compose
```yaml
version: '3.3'
services:
    magiogo-iptv-server:
        build: .
        ports:
            - '5000:5000'
        environment:
            - MAGIO_USERNAME=your_username
            - MAGIO_PASSWORD=your_password
            - MAGIO_SERVER_PUBLIC_URL=http://127.0.0.1:5000
            - MAGIO_GUIDE_DAYS=7
            - MAGIO_GUIDE_REFRESH_HOURS=12
        container_name: magiogo-iptv-server
        image: magiogo-iptv-server
```

## Season parsing
season number is always none, trying to parse from title

## Credits
Sorien
https://github.com/Sorien/plugin.video.iptv.magiogo