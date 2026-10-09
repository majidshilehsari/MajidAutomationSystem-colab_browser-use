#!/usr/bin/env bash
#
# خلاصهٔ فارسی: پروب سلامت داخل کانتینر. دو بررسی HTTP انجام می‌دهد:
# /automation/api/info (تنها مسیر بدون توکن) و /vnc.html. خروجی ۰ یعنی سالم.
# Hostim این اسکریپت را صدا نمی‌زند؛ پروب خودِ پلتفرم یک GET روی
# «Health check path» است که باید /automation/api/info تنظیم شود. این اسکریپت
# برای docker/compose و برای بررسی دستی با hostim exec است. با
# HEALTHCHECK_DEEP=1 نمایش X و CDP کروم را هم بررسی می‌کند.
#
# In-container health probe for the Hostim deployment track.
#
# Two different consumers:
#
#   * `docker run` / `docker compose` call it through the image HEALTHCHECK.
#   * Hostim does NOT call it. Hostim's readiness probe is an HTTP GET from the
#     platform to the app's "Health check path", which should be set to
#     /automation/api/info - the same first check this script makes.
#
# Exit 0 means healthy; anything else means unhealthy.

set -euo pipefail

PORT=${PORT:-6080}
CDP_PORT=${CDP_PORT:-9222}
TIMEOUT=${HEALTHCHECK_TIMEOUT:-4}
BASE="http://127.0.0.1:${PORT}"

# /automation/api/info is the only API route that does not require the
# X-Automation-Token header, which is exactly what makes it usable as a probe:
# the platform cannot authenticate. It answers 200 as soon as the HTTP front
# (noVNC + sidebar + API + VNC WebSocket on one port) is up.
curl -fsS --max-time "$TIMEOUT" -o /dev/null "${BASE}/automation/api/info"

# noVNC itself must be served from the same port, or the human-facing page is
# broken even though the API answers.
curl -fsS --max-time "$TIMEOUT" -o /dev/null "${BASE}/vnc.html"

# Opt-in deeper probe for a human debugging through `hostim exec`. Left off by
# default: the X display and Chrome are restarted by the entrypoint supervisor,
# and a probe that fails during that window would mark the app unhealthy for a
# condition that heals by itself.
if [[ "${HEALTHCHECK_DEEP:-0}" == "1" ]]; then
  xdpyinfo -display "${DISPLAY:-:1}" >/dev/null
  curl -fsS --max-time "$TIMEOUT" -o /dev/null \
    "http://127.0.0.1:${CDP_PORT}/json/version"
fi
