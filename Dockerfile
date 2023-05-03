FROM python:3.11-alpine
RUN apk add git

WORKDIR /app

COPY . .

RUN pip install -r requirements.txt

ENV PYTHONUNBUFFERED=1
EXPOSE 5000
CMD ["flask", "run", "--host", "0.0.0.0"]
