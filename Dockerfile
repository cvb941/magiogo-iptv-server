FROM python:3.8-alpine

WORKDIR /app

COPY . .

RUN apk add mercurial
RUN pip install -r requirements.txt

ENTRYPOINT ["flask", "run", "--host=0.0.0.0"]

EXPOSE 5000