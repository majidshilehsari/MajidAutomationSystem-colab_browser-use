#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STATE_DIR=${COLAB_BROWSER_STATE_DIR:-"$SCRIPT_DIR/.runtime"}
LOG_DIR="$STATE_DIR/logs"
PROFILE_DIR="$STATE_DIR/chrome-profile"
DISPLAY_ID=:1
VNC_PORT=5901
NOVNC_PORT=6080
CDP_PORT=9222
WEB_ROOT="$STATE_DIR/www"
AUTOMATION_DIR="$SCRIPT_DIR/automation"

# The sidebar API is served from the same port as noVNC, because the Cloudflare
# tunnel exposes only one URL. When it runs, this name is also the pid file.
WEB_MODE=automation

required_commands=(
  Xvfb cloudflared curl dbus-launch fluxbox google-chrome openssl
  pgrep python3 ss websockify x11vnc
)
missing_commands=()
for required_command in "${required_commands[@]}"; do
  command -v "$required_command" >/dev/null 2>&1 || missing_commands+=("$required_command")
done
if (( ${#missing_commands[@]} > 0 )); then
  printf 'Missing required commands: %s\n' "${missing_commands[*]}" >&2
  printf 'Run %s/install.sh first.\n' "$SCRIPT_DIR" >&2
  exit 1
fi

mkdir -p "$LOG_DIR" "$PROFILE_DIR" "$WEB_ROOT"
chmod 700 "$STATE_DIR" "$LOG_DIR" "$PROFILE_DIR"

start_chrome() {
  nohup env DISPLAY="$DISPLAY_ID" google-chrome \
    --no-sandbox \
    --disable-dev-shm-usage \
    --use-gl=swiftshader \
    --enable-unsafe-swiftshader \
    --no-first-run \
    --no-default-browser-check \
    --remote-debugging-port="$CDP_PORT" \
    --user-data-dir="$PROFILE_DIR" \
    --window-size=1366,768 \
    --start-maximized \
    https://www.google.com/ \
    >"$LOG_DIR/chrome.log" 2>&1 &
  printf '%s\n' "$!" >"$STATE_DIR/chrome.pid"
}

stop_owned_process() {
  local name="$1"
  local expected="$2"
  local pid_file="$STATE_DIR/$name.pid"
  local pid
  local command_line

  [[ -f "$pid_file" ]] || return 0
  pid=$(<"$pid_file")
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    command_line=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
    if [[ "$command_line" == *"$expected"* ]]; then
      kill "$pid" 2>/dev/null || true
      for _ in {1..20}; do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
      done
    fi
  fi
  rm -f "$pid_file"
}

# Repeated runs replace only processes previously recorded by this workflow.
stop_owned_process cloudflared cloudflared
stop_owned_process automation "automation/server.py"
stop_owned_process websockify websockify
stop_owned_process x11vnc x11vnc
stop_owned_process chrome google-chrome
stop_owned_process fluxbox fluxbox
stop_owned_process xvfb Xvfb

if ss -ltn | grep -Eq ":(${VNC_PORT}|${NOVNC_PORT})[[:space:]]"; then
  printf 'Port %s or %s is already occupied by another process.\n' "$VNC_PORT" "$NOVNC_PORT" >&2
  exit 1
fi

rm -f "$LOG_DIR"/*.log "$STATE_DIR/vnc.pass"

nohup Xvfb "$DISPLAY_ID" -screen 0 1366x768x24 -ac -noreset \
  >"$LOG_DIR/xvfb.log" 2>&1 &
printf '%s\n' "$!" >"$STATE_DIR/xvfb.pid"

for _ in {1..30}; do
  [[ -S /tmp/.X11-unix/X1 ]] && break
  sleep 0.2
done
if [[ ! -S /tmp/.X11-unix/X1 ]]; then
  printf 'The virtual display did not start. See %s.\n' "$LOG_DIR/xvfb.log" >&2
  exit 1
fi

nohup env DISPLAY="$DISPLAY_ID" dbus-launch --exit-with-session fluxbox \
  >"$LOG_DIR/fluxbox.log" 2>&1 &
printf '%s\n' "$!" >"$STATE_DIR/fluxbox.pid"

VNC_PASSWORD=$(openssl rand -hex 4)
x11vnc -storepasswd "$VNC_PASSWORD" "$STATE_DIR/vnc.pass" >/dev/null
chmod 600 "$STATE_DIR/vnc.pass"

nohup x11vnc -display "$DISPLAY_ID" -rfbport "$VNC_PORT" -localhost \
  -forever -shared -rfbauth "$STATE_DIR/vnc.pass" -noxdamage \
  >"$LOG_DIR/x11vnc.log" 2>&1 &
printf '%s\n' "$!" >"$STATE_DIR/x11vnc.pid"

start_chrome

start_automation_server() {
  nohup python3 "$AUTOMATION_DIR/server.py" \
    --listen-host 127.0.0.1 --listen-port "$NOVNC_PORT" \
    --vnc-host 127.0.0.1 --vnc-port "$VNC_PORT" \
    --web-root "$WEB_ROOT" --novnc-dir /usr/share/novnc \
    --data-dir "$STATE_DIR/automation" \
    --control-script "$SCRIPT_DIR/browser_control.sh" \
    --display "$DISPLAY_ID" --cdp-port "$CDP_PORT" \
    --token "$AUTOMATION_TOKEN" \
    >"$LOG_DIR/automation.log" 2>&1 &
  printf '%s\n' "$!" >"$STATE_DIR/automation.pid"
}

start_plain_websockify() {
  nohup websockify --web="$WEB_ROOT" \
    "127.0.0.1:$NOVNC_PORT" "127.0.0.1:$VNC_PORT" \
    >"$LOG_DIR/websockify.log" 2>&1 &
  printf '%s\n' "$!" >"$STATE_DIR/websockify.pid"
}

wait_for_vnc_html() {
  local attempts=${1:-60}
  for _ in $(seq 1 "$attempts"); do
    curl -fsS "http://127.0.0.1:$NOVNC_PORT/vnc.html" >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  return 1
}

# The automation server serves noVNC, the sidebar and the API on one port. If it
# cannot start, fall back to plain websockify so the browser still works and
# only the sidebar is missing.
AUTOMATION_TOKEN=$(openssl rand -hex 8)
start_automation_server
if ! wait_for_vnc_html 40; then
  printf 'Automation server did not answer; falling back to plain noVNC.\n' >&2
  printf 'See %s. The automation sidebar will be unavailable.\n' \
    "$LOG_DIR/automation.log" >&2
  stop_owned_process automation "automation/server.py"
  WEB_MODE=websockify
  start_plain_websockify
  if ! wait_for_vnc_html 40; then
    printf 'noVNC did not start. See %s.\n' "$LOG_DIR/websockify.log" >&2
    exit 1
  fi
fi

nohup cloudflared tunnel --url "http://127.0.0.1:$NOVNC_PORT" \
  --protocol http2 --no-autoupdate \
  >"$LOG_DIR/cloudflared.log" 2>&1 &
printf '%s\n' "$!" >"$STATE_DIR/cloudflared.pid"

PUBLIC_URL=
for _ in {1..60}; do
  PUBLIC_URL=$(grep -Eo 'https://[-a-z0-9]+\.trycloudflare\.com' \
    "$LOG_DIR/cloudflared.log" 2>/dev/null | tail -n 1 || true)
  [[ -n "$PUBLIC_URL" ]] && break
  sleep 0.5
done

if [[ -z "$PUBLIC_URL" ]]; then
  printf 'The public tunnel did not return a URL. See %s.\n' "$LOG_DIR/cloudflared.log" >&2
  exit 1
fi

printf 'BROWSER_URL=%s/vnc.html?autoconnect=true&resize=scale&path=websockify\n' "$PUBLIC_URL"
printf 'VNC_PASSWORD=%s\n' "$VNC_PASSWORD"
if [[ "$WEB_MODE" == automation ]]; then
  printf 'AUTOMATION_TOKEN=%s\n' "$AUTOMATION_TOKEN"
else
  printf 'AUTOMATION_TOKEN=unavailable (plain noVNC mode)\n'
fi
printf 'STATE_DIR=%s\n' "$STATE_DIR"

if [[ "${1:-}" == "--wait" ]]; then
  cleanup() {
    "$SCRIPT_DIR/stop_colab_browser.sh" >/dev/null 2>&1 || true
  }
  trap cleanup EXIT INT TERM
  printf 'Supervisor is running; keep this terminal session open.\n'
  while true; do
    for service in xvfb fluxbox x11vnc "$WEB_MODE" cloudflared; do
      service_pid=$(<"$STATE_DIR/$service.pid")
      if ! kill -0 "$service_pid" 2>/dev/null; then
        printf '%s stopped unexpectedly; shutting down the stack.\n' "$service" >&2
        exit 1
      fi
    done
    if ! pgrep -f -- "--user-data-dir=$PROFILE_DIR" >/dev/null 2>&1; then
      printf 'Chrome stopped; relaunching it.\n'
      start_chrome
    fi
    sleep 15
  done
fi

