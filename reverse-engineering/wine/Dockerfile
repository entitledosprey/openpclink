FROM debian:bookworm-slim
RUN dpkg --add-architecture i386 && apt-get update && apt-get install -y --no-install-recommends \
    wine wine32:i386 wine64 xvfb xdotool x11-utils imagemagick python3 python3-serial socat procps fonts-dejavu-core
ENV WINEDEBUG=-all
ENV WINEPREFIX=/root/.wine
ENV WINEARCH=win32
