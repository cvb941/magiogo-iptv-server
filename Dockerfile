FROM python:3.14-alpine AS runtime

WORKDIR /app

COPY requirements.txt .

RUN apk add --no-cache --virtual .build-deps git \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

COPY . .

RUN addgroup -S -g 10001 app && adduser -S -u 10001 -G app app \
    && mkdir -p storage public \
    && chown -R app:app storage public

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 5000
USER app:app
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import http.client,json,sys; c=http.client.HTTPConnection('127.0.0.1',5000,timeout=2); c.request('GET','/health'); r=c.getresponse(); sys.exit(r.status!=200 or json.load(r).get('status')!='ok')"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]

FROM runtime AS test
USER root
RUN apk add --no-cache ffmpeg
USER app:app
CMD ["python", "-m", "unittest", "-v"]

FROM runtime AS production
