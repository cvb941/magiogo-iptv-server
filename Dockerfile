FROM python:3.14-alpine

WORKDIR /app

COPY requirements.txt .

RUN apk add --no-cache --virtual .build-deps git \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

COPY . .

RUN addgroup -S app && adduser -S app -G app \
    && chown -R app:app /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import http.client,json,sys; c=http.client.HTTPConnection('127.0.0.1',5000,timeout=2); c.request('GET','/health'); r=c.getresponse(); sys.exit(r.status!=200 or json.load(r).get('status')!='ok')"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
