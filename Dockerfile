FROM python:3.8-alpine

WORKDIR /app

COPY . .

RUN apk add mercurial
RUN pip install -r requirements.txt

ENTRYPOINT ["flask", "run"]

EXPOSE 5000