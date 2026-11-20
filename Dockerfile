FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    fonts-dejavu-core \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    flask \
    flask-cors \
    werkzeug \
    yt-dlp

COPY server.py index.html logo.png ./

EXPOSE 5000

CMD ["python", "server.py"]