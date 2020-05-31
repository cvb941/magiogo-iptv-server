#Magio GO IPTV Server


##Instructions
You can run this program either directly with Python 3
or on Docker.

###Python


###Docker
```
docker build -t magiogo-iptv-server .
&& docker run -p 5678:5000 --env MAGIO_USERNAME=your_username --env MAGIO_PASSWORD=your_password --env MAGIO_SERVER_PUBLIC_URL="http://127.0.0.1:5000" --name magiogo-iptv-server magiogo-iptv-server
```


MAGIO_USERNAME

MAGIO_PASSWORD