FROM python:3.12-slim

# ffmpeg is used for long-text TTS chunking and long-audio transcription.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd -m bridge && chown -R bridge:bridge /app
USER bridge

ENV SERVER_HOST=127.0.0.1 SERVER_PORT=8083
EXPOSE 8083

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8083/health', timeout=4).status==200 else 1)"

CMD ["python", "server.py"]
