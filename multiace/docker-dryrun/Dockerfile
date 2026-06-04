FROM python:3.11-slim

WORKDIR /app

COPY multiace/web/backend/requirements.txt /tmp/multiace-web-requirements.txt
RUN pip install --no-cache-dir -r /tmp/multiace-web-requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1
