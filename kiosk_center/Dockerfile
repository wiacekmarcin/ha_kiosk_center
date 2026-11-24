ARG BUILD_FROM
FROM ${BUILD_FROM}

# Python z bibliotekami z pakietów Alpine (bez kompilacji) i ffmpeg do konwersji dźwięku
RUN apk add --no-cache python3 py3-aiohttp py3-paho-mqtt ffmpeg

WORKDIR /app
COPY center /app/center

CMD ["python3", "-u", "-m", "center"]
