FROM python:3.14-alpine

WORKDIR /app

COPY requirements.txt .

RUN apk add --no-cache --virtual .build-deps git \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

COPY . .

RUN addgroup -S app && adduser -S app -G app \
    && chown -R app:app /app

USER app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 5000
CMD ["flask", "run", "--host", "0.0.0.0"]
