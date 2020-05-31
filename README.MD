# Magio GO IPTV Server
- [ ] what
- [ ] why
- [ ] how

## Setup instructions
You can run this program either directly with Python 3
or on Docker.

The credentials to Magio GO are passed through environment variables,
so they need to be set depending on the platform accordingly.

MAGIO_SERVER_PUBLIC_URL is used in the generated .m3u8 file in the stream URLs.
It should be the publicly (or privately) reachable URL from IPTV clients, to this program.

### Python
```shell script
set MAGIO_USERNAME=your_username
set MAGIO_PASSWORD=your_password
# optional, default is http://127.0.0.1:5000
set MAGIO_SERVER_PUBLIC_URL="http://127.0.0.1:5000"
flask run
```


### Docker
```shell script
docker build -t magiogo-iptv-server .
docker run -p 5000:5000 --env MAGIO_USERNAME=your_username --env MAGIO_PASSWORD=your_password --env MAGIO_SERVER_PUBLIC_URL="http://127.0.0.1:5000" --name magiogo-iptv-server magiogo-iptv-server
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
            - MAGIO_SERVER_PUBLIC_URL="http://127.0.0.1:5000"
        container_name: magiogo-iptv-server
        image: magiogo-iptv-server
```

## Credits
Sorien
https://github.com/Sorien/plugin.video.iptv.magiogo