FROM python:3.9-alpine
RUN apk add git

WORKDIR /app

COPY . .

RUN pip install -r requirements.txt

ENV PYTHONUNBUFFERED=1
EXPOSE 5000
CMD ["flask", "run", "--host", "0.0.0.0"]