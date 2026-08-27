#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  printf 'This installer must run as root. Google Colab normally uses root.\n' >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates \
  curl \
  dbus-x11 \
  fluxbox \
  fonts-liberation \
  iproute2 \
  novnc \
  openssl \
  procps \
  scrot \
  websockify \
  wmctrl \
  x11-utils \
  xclip \
  xdotool \
  x11vnc \
  xvfb

if ! command -v google-chrome >/dev/null 2>&1; then
  chrome_deb=$(mktemp --suffix=.deb /tmp/google-chrome-stable.XXXXXX)
  curl -fsSL -o "$chrome_deb" \
    https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  apt-get install -y -qq "$chrome_deb"
  rm -f "$chrome_deb"
fi

if ! command -v cloudflared >/dev/null 2>&1; then
  curl -fsSL -o /usr/local/bin/cloudflared \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod 0755 /usr/local/bin/cloudflared
fi

printf 'Installed Chrome: '
google-chrome --version
printf 'Installed cloudflared: '
cloudflared --version
printf 'Computer-use dependencies are ready.\n'

