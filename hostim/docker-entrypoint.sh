#!/usr/bin/env bash
#
# خلاصهٔ فارسی: سوپروایزر کانتینر در مسیر Hostim. ترتیب راه‌اندازی:
# بررسی volume → ساخت/خواندن secretها (بدون چاپ در لاگ) → Xvfb → fluxbox →
# x11vnc (فقط loopback) → Chrome → سرور اتوماسیون روی 0.0.0.0:$PORT.
# سپس: سلامت‌سنجی، راه‌اندازی مجدد Chrome در صورت مرگ، خروج با کد ۱ در صورت
# مرگ یک سرویس هسته‌ای (تا پلتفرم کانتینر را restart کند)، و خاموش‌سازی کامل
# پشته با SIGTERM. هیچ cloudflared‌ای اینجا اجرا نمی‌شود.
# راهنمای کامل: hostim/GUIDE.fa.md
#
# Container entrypoint for the Hostim (hostim.dev) deployment track.
#
# This file belongs to the Hostim add-on only. The Google Colab path keeps
# using start_colab_browser.sh, its noVNC/VNC ports and its Cloudflare Quick
# Tunnel; nothing here is sourced by that script and nothing here changes it.
#
# What it does, in order:
#   1. checks that the persistent volume at /data is usable,
#   2. loads the per-deployment secrets, or generates them on first boot and
#      stores them on the volume with mode 0600 (they are never printed),
#   3. starts Xvfb -> fluxbox -> x11vnc -> Chrome -> automation server,
#   4. exposes exactly ONE port, 0.0.0.0:$PORT. x11vnc stays on loopback and
#      Chrome's CDP port is loopback-only, so neither is reachable publicly,
#   5. supervises: relaunches Chrome if it disappears, and exits non-zero when
#      a core service dies so the platform restarts the container,
#   6. shuts the whole process tree down on SIGTERM, inside the grace period
#      the platform allows (Hostim documents 60 seconds on redeploy).
#
# There is deliberately no cloudflared here: Hostim terminates HTTPS on its own
# ingress and hands the app a domain, so a tunnel would only add a second hop.

set -euo pipefail

APP_DIR=${APP_DIR:-/app}
DATA_DIR=${DATA_DIR:-/data}
PORT=${PORT:-6080}
VNC_PORT=${VNC_PORT:-5901}
CDP_PORT=${CDP_PORT:-9222}
DISPLAY_ID=${DISPLAY:-:1}
SCREEN_SIZE=${SCREEN:-1366x768x24}
START_URL=${START_URL:-https://www.google.com/}
EXTRA_CHROME_FLAGS=${EXTRA_CHROME_FLAGS:-}
# Keyboard: noVNC forwards raw keys to X, so the X layout decides which
# character Chrome receives. "us,fa" gives English plus Persian with Alt+Shift
# as the toggle, which is what a Windows-trained operator already reaches for.
XKB_MODEL=${XKB_MODEL:-pc104}
XKB_LAYOUTS=${XKB_LAYOUTS:-us,fa}
XKB_OPTIONS=${XKB_OPTIONS:-grp:alt_shift_toggle}
# A fresh Xvfb session starts with NumLock off, so the keypad sends arrows
# instead of digits. "on" (default) enables it; anything else leaves it alone.
NUMLOCK=${NUMLOCK:-on}
# The coworker agent's own browser. It runs on a SECOND display and a SECOND
# Chrome profile so it can never fight the automation for the one mouse and
# keyboard on :1, and so `wmctrl -a "Google Chrome"` cannot pick the wrong
# window: both browsers would carry that title. Set AI_BROWSER=0 to skip it
# entirely (the agent still works with an HTTP API key instead).
AI_BROWSER=${AI_BROWSER:-1}
AI_DISPLAY=${AI_DISPLAY:-:2}
AI_SCREEN_SIZE=${AI_SCREEN:-1366x768x24}
AI_CDP_PORT=${AI_CDP_PORT:-9223}
AI_START_URL=${AI_START_URL:-https://chat.deepseek.com/}
# Absolute HTTPS origin used in the links the agent sends out (Telegram
# handoffs). Empty means "derive it from Hostim's BUILTIN_DOMAIN".
PUBLIC_BASE=${PUBLIC_BASE:-}
NOVNC_DIR=${NOVNC_DIR:-/usr/share/novnc}
CHECK_INTERVAL=${CHECK_INTERVAL:-15}
STARTUP_TIMEOUT=${STARTUP_TIMEOUT:-120}
SHUTDOWN_GRACE_SECONDS=${SHUTDOWN_GRACE_SECONDS:-25}

# Ephemeral on purpose. automation/server.py copies the installed noVNC tree
# into the web root once and then reuses it; keeping that copy in the container
# (instead of on the volume) means an image upgrade can never leave a stale
# noVNC build being served next to new sidebar assets.
WEB_ROOT=${WEB_ROOT:-/tmp/automation-www}

LOG_DIR="$DATA_DIR/logs"
PROFILE_DIR="$DATA_DIR/chrome-profile"
AI_PROFILE_DIR="$DATA_DIR/ai-profile"
AUTOMATION_DIR="$DATA_DIR/automation"
SECRETS_FILE="$DATA_DIR/secrets.env"
VNC_PASS_FILE="$DATA_DIR/vnc.pass"
CONTROL_SCRIPT="$APP_DIR/browser_control.sh"
SERVER_SCRIPT="$APP_DIR/automation/server.py"

export DISPLAY="$DISPLAY_ID"

declare -A SERVICE_PID=()

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

die() {
  log "FATAL: $*"
  # Services started before this point must not be left behind as orphans.
  stop_all
  exit 1
}

# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------
check_prerequisites() {
  local missing=()
  # The `websockify` command line is NOT required: this entrypoint has no plain
  # websockify fallback, so only the Python module matters, and that is checked
  # separately below with an error message that names the real problem.
  local required=(Xvfb curl dbus-launch fluxbox google-chrome openssl pgrep
                  python3 x11vnc xdotool wmctrl scrot)
  local command
  for command in "${required[@]}"; do
    command -v "$command" >/dev/null 2>&1 || missing+=("$command")
  done
  if (( ${#missing[@]} > 0 )); then
    die "missing commands: ${missing[*]} (the image is incomplete)"
  fi

  if ! python3 -c 'import websockify.websocketproxy' 2>/dev/null; then
    die "the websockify Python module is not importable. automation/server.py
       drives websockify's request handler itself, so without it there is no
       noVNC proxy and no sidebar. Install it in the image:
       apt-get install websockify, or: pip install websockify"
  fi

  [[ -f "$SERVER_SCRIPT" ]] || die "not found: $SERVER_SCRIPT"
  [[ -x "$CONTROL_SCRIPT" ]] || die "not executable: $CONTROL_SCRIPT"
  [[ -d "$NOVNC_DIR" ]] || die "noVNC is not installed at $NOVNC_DIR"

  # A volume that is not writable yet is the single most common Hostim
  # startup failure, so say exactly what is wrong instead of failing later
  # inside Chrome or the API.
  mkdir -p "$DATA_DIR" 2>/dev/null || true
  if [[ ! -d "$DATA_DIR" ]] || [[ ! -w "$DATA_DIR" ]]; then
    die "$DATA_DIR is not writable. On Hostim a fresh volume becomes writable
       only after it is attached to an app and that app has started once.
       Check the volume mount path in the app's Volumes tab, or run
       'chmod -R a+rwX /volumes/<volume-name>' from the Bastion."
  fi

  if [[ ! "$PORT" =~ ^[0-9]+$ ]]; then
    die "PORT must be a number, got: '$PORT'"
  fi
  if [[ "$PORT" -lt 1024 ]]; then
    die "PORT=$PORT is a privileged port and this container does not run as
       root. Use 1024 or higher (the default is 6080) and set the same number
       as the app's httpPort on Hostim."
  fi
}

prepare_directories() {
  mkdir -p "$LOG_DIR" "$PROFILE_DIR" "$AUTOMATION_DIR" "$WEB_ROOT" "$AI_PROFILE_DIR"
  chmod 700 "$LOG_DIR" "$PROFILE_DIR" "$AI_PROFILE_DIR" 2>/dev/null || true
  rm -f "$LOG_DIR"/*.log 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------
# AUTOMATION_TOKEN and the VNC password are per-deployment secrets. They are
# generated once, stored on the volume with mode 0600 so they survive a
# redeploy, and never written to stdout: container logs are collected and kept
# by the platform (Hostim keeps 7 days), so printing a secret there would leak
# it into a place the operator does not control.
load_or_create_secrets() {
  # An environment value provided by the platform must win over the file, so
  # the password can be rotated from Hostim's Envs tab without needing a shell
  # inside the container. Capture it FIRST: sourcing the file below assigns
  # these very same names and would otherwise silently overwrite whatever the
  # platform injected (that bug shipped once already).
  local env_token="${AUTOMATION_TOKEN:-}"
  local env_pass="${VNC_PASSWORD:-}"
  local file_token="" file_pass=""

  if [[ -f "$SECRETS_FILE" ]]; then
    # The file contains only KEY=value lines that this script wrote itself.
    # shellcheck disable=SC1090
    source "$SECRETS_FILE"
    file_token="${AUTOMATION_TOKEN:-}"
    file_pass="${VNC_PASSWORD:-}"
  fi

  AUTOMATION_TOKEN="${env_token:-$file_token}"
  VNC_PASSWORD="${env_pass:-$file_pass}"

  # 8 hex characters for the VNC password: x11vnc truncates it to 8 bytes
  # anyway, and this matches what start_colab_browser.sh generates.
  if [[ -z "$AUTOMATION_TOKEN" ]]; then
    AUTOMATION_TOKEN=$(openssl rand -hex 8)
  fi
  if [[ -z "$VNC_PASSWORD" ]]; then
    VNC_PASSWORD=$(openssl rand -hex 4)
  fi

  # Keep the file in sync with the values actually in use. Otherwise a later
  # restart without the env override would fall back to a stale password and
  # nobody could tell which one x11vnc is using.
  if [[ ! -f "$SECRETS_FILE" || "$AUTOMATION_TOKEN" != "$file_token" ||
    "$VNC_PASSWORD" != "$file_pass" ]]; then
    (umask 077; printf 'AUTOMATION_TOKEN=%s\nVNC_PASSWORD=%s\n' \
      "$AUTOMATION_TOKEN" "$VNC_PASSWORD" >"$SECRETS_FILE")
    chmod 600 "$SECRETS_FILE" 2>/dev/null || true
  fi

  export AUTOMATION_TOKEN VNC_PASSWORD

  x11vnc -storepasswd "$VNC_PASSWORD" "$VNC_PASS_FILE" >/dev/null
  chmod 600 "$VNC_PASS_FILE" 2>/dev/null || true

  log "secrets: $SECRETS_FILE (mode 0600, not printed to the log)"
  log "         read them with: hostim exec <app> -- cat $SECRETS_FILE"
}

# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------
# spawn <name> <logfile|-> <command...>
# "-" sends the child's output to this container's stdout/stderr, which is what
# `hostim logs` shows. The automation server uses that so API and proxy errors
# are visible without shelling into the container; the noisy desktop services
# write to files on the volume instead.
spawn() {
  local name="$1" target="$2"
  shift 2
  if [[ "$target" == "-" ]]; then
    "$@" &
  else
    "$@" >>"$target" 2>&1 &
  fi
  SERVICE_PID[$name]=$!
  # Drop it from bash's job table. The pid is tracked in SERVICE_PID and that
  # is all this script needs; leaving it in the job table makes bash print
  # "line NN: <pid> Killed" to the container log every time a service is
  # signalled, which reads like a crash even during an orderly shutdown.
  disown "${SERVICE_PID[$name]}" 2>/dev/null || true
}

alive() {
  local name="$1"
  local pid="${SERVICE_PID[$name]:-}"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

terminate() {
  local name="$1"
  local pid="${SERVICE_PID[$name]:-}"
  [[ -n "$pid" ]] || return 0
  kill -TERM "$pid" 2>/dev/null || true
}

force_kill() {
  local name="$1"
  local pid="${SERVICE_PID[$name]:-}"
  [[ -n "$pid" ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------
start_xvfb() {
  spawn xvfb "$LOG_DIR/xvfb.log" \
    Xvfb "$DISPLAY_ID" -screen 0 "$SCREEN_SIZE" -ac -noreset
  local _attempt
  for _attempt in $(seq 1 30); do
    [[ -S "/tmp/.X11-unix/X${DISPLAY_ID#:}" ]] && break
    sleep 0.2
  done
  [[ -S "/tmp/.X11-unix/X${DISPLAY_ID#:}" ]] \
    || die "the virtual display did not start; see $LOG_DIR/xvfb.log"
  log "Xvfb on $DISPLAY_ID ($SCREEN_SIZE)"
}

start_window_manager() {
  spawn fluxbox "$LOG_DIR/fluxbox.log" \
    dbus-launch --exit-with-session fluxbox
  log "fluxbox started"
}

prepare_desktop() {
  # A calmer, Windows-10-flavoured desktop: our own fluxbox style (dark taskbar
  # at the bottom, gradient background, Persian right-click menu) plus a few
  # session keys (Persian workspace names, toolbar layout). Only cosmetic: if
  # anything here fails the window manager still starts with its defaults.
  local home_dir="${HOME:-/home/automation}"
  local fb_dir="$home_dir/.fluxbox"
  mkdir -p "$fb_dir" 2>/dev/null || true
  if [[ -f "$APP_DIR/hostim/fluxbox-style" ]]; then
    cp -f "$APP_DIR/hostim/fluxbox-style" "$fb_dir/mas-style" 2>/dev/null || true
  fi
  if [[ -f "$APP_DIR/hostim/fluxbox-menu" ]]; then
    cp -f "$APP_DIR/hostim/fluxbox-menu" "$fb_dir/mas-menu" 2>/dev/null || true
  fi
  # An optional wallpaper image on the volume wins over the gradient.
  if [[ -n "${DESKTOP_BACKGROUND:-}" && -f "${DESKTOP_BACKGROUND}" ]]; then
    {
      printf '\nbackground: fullscreen\n'
      printf 'background.pixmap: %s\n' "$DESKTOP_BACKGROUND"
    } >> "$fb_dir/mas-style" 2>/dev/null || true
    log "desktop background: $DESKTOP_BACKGROUND"
  fi
  python3 - "$fb_dir" <<'PY' 2>/dev/null || log "WARN: desktop keys not written"
import sys
from pathlib import Path
fb = Path(sys.argv[1])
init = fb / "init"
keys = {
    "session.styleFile": str(fb / "mas-style"),
    "session.menuFile": str(fb / "mas-menu"),
    "session.screen0.toolbar.visible": "true",
    "session.screen0.toolbar.autoHide": "false",
    "session.screen0.toolbar.placement": "BottomCenter",
    "session.screen0.toolbar.widthPercent": "100",
    "session.screen0.toolbar.height": "34",
    "session.screen0.toolbar.tools":
        "prevworkspace, workspacename, nextworkspace, iconbar, systemtray, clock",
    "session.screen0.workspaceNames":
        "ورک‌اسپیس ۱, ورک‌اسپیس ۲, ورک‌اسپیس ۳, ورک‌اسپیس ۴",
    "session.screen0.tabs.usePixmap": "false",
    "session.screen0.focusModel": "ClickFocus",
    "session.autoRaise": "true",
}
lines = init.read_text(encoding="utf-8").splitlines() if init.exists() else []
out, seen = [], set()
for line in lines:
    name = line.split(":", 1)[0].strip()
    if name in keys:
        out.append("%s: %s" % (name, keys[name]))
        seen.add(name)
    else:
        out.append(line)
for name, value in keys.items():
    if name not in seen:
        out.append("%s: %s" % (name, value))
init.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
  log "desktop style prepared (taskbar + Persian menu + gradient background)"
}

start_vnc() {
  # -localhost keeps 5901 private: the only way in is the automation server's
  # /websockify proxy on the single published HTTP port.
  spawn x11vnc "$LOG_DIR/x11vnc.log" \
    x11vnc -display "$DISPLAY_ID" -rfbport "$VNC_PORT" -localhost \
      -forever -shared -rfbauth "$VNC_PASS_FILE" -noxdamage
  local _attempt
  for _attempt in $(seq 1 40); do
    # bash's /dev/tcp is a connect-only probe: unlike `curl telnet://`, which
    # stays attached and would block this script forever.
    if (exec 3<>"/dev/tcp/127.0.0.1/$VNC_PORT") 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  alive x11vnc || die "x11vnc exited; see $LOG_DIR/x11vnc.log"
  log "x11vnc on 127.0.0.1:$VNC_PORT (private)"
}

configure_keyboard() {
  # Persian typing and NumLock.
  #
  # The operator types through noVNC, which sends raw key events to the X
  # server; the character that reaches Chrome is decided by the X keyboard
  # layout, not by the operator's own OS layout. Without an "fa" group there is
  # simply no way to produce Persian characters, and without NumLock the
  # numeric keypad emits arrows instead of digits.
  #
  # Both are best-effort on purpose: a missing tool costs one convenience and
  # must never stop the container from serving the desktop.
  local args=(-model "$XKB_MODEL" -layout "$XKB_LAYOUTS")
  if [[ -n "$XKB_OPTIONS" ]]; then
    args+=(-option "$XKB_OPTIONS")
  fi

  if command -v setxkbmap >/dev/null 2>&1; then
    if setxkbmap "${args[@]}" 2>>"$LOG_DIR/keyboard.log"; then
      log "keyboard layouts: $XKB_LAYOUTS (toggle: ${XKB_OPTIONS:-none})"
    else
      log "WARN: setxkbmap failed; Persian typing may not work (see $LOG_DIR/keyboard.log)"
    fi
  else
    log "WARN: setxkbmap is missing; the X layout stays at its default"
  fi

  if [[ "$NUMLOCK" == "on" ]]; then
    if command -v numlockx >/dev/null 2>&1; then
      if numlockx on 2>>"$LOG_DIR/keyboard.log"; then
        log "NumLock: on"
      else
        log "WARN: 'numlockx on' failed; the keypad will send arrows (see $LOG_DIR/keyboard.log)"
      fi
    else
      log "WARN: numlockx is missing; the keypad will send arrows"
    fi
  fi
}

start_ai_browser() {
  # The coworker agent's own browser: a second Xvfb plus a second Chrome, on
  # their own display, profile and debugging port. It is how the agent can ask a
  # chat website something without an API key - and keeping it off :1 is what
  # stops it from stealing focus from the automation.
  if [[ "$AI_BROWSER" != "1" ]]; then
    log "AI_BROWSER=$AI_BROWSER: the agent browser stays off (the agent can still use an HTTP API key)"
    return 0
  fi

  local _attempt
  if [[ ! -S "/tmp/.X11-unix/X${AI_DISPLAY#:}" ]]; then
    spawn ai-xvfb "$LOG_DIR/ai-xvfb.log" \
      Xvfb "$AI_DISPLAY" -screen 0 "$AI_SCREEN_SIZE" -ac -noreset
    for _attempt in $(seq 1 30); do
      [[ -S "/tmp/.X11-unix/X${AI_DISPLAY#:}" ]] && break
      sleep 0.2
    done
  fi
  if [[ ! -S "/tmp/.X11-unix/X${AI_DISPLAY#:}" ]]; then
    # Never fatal: the automation on :1 is the product, this is an optional aid.
    log "WARN: the agent display $AI_DISPLAY did not start (see $LOG_DIR/ai-xvfb.log); the agent browser stays off"
    return 0
  fi

  if ! alive ai-chrome; then
    spawn ai-chrome "$LOG_DIR/ai-chrome.log" \
      env DISPLAY="$AI_DISPLAY" HOME="${HOME:-/home/automation}" google-chrome \
        --no-sandbox \
        --disable-dev-shm-usage \
        --use-gl=swiftshader \
        --enable-unsafe-swiftshader \
        --no-first-run \
        --no-default-browser-check \
        --remote-debugging-port="$AI_CDP_PORT" \
        --user-data-dir="$AI_PROFILE_DIR" \
        --window-size=1366,768 \
        "$AI_START_URL"
  fi
  for _attempt in $(seq 1 40); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$AI_CDP_PORT") 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  log "agent browser on $AI_DISPLAY (CDP on 127.0.0.1:$AI_CDP_PORT, profile $AI_PROFILE_DIR)"
  log "         it is NOT in the noVNC view; log in to the chat site once from"
  log "         the sidebar: ایجنت همکار → کلیدها → «تصویر نمایش ایجنت»"
}

start_chrome() {
  # Same flags as the Colab launcher: software WebGL, no first-run dialogs, and
  # --disable-dev-shm-usage, which matters even more here because a Kubernetes
  # pod's /dev/shm cannot be sized from the app settings.
  # EXTRA_CHROME_FLAGS is intentionally word-split into separate flags.
  # shellcheck disable=SC2086
  spawn chrome "$LOG_DIR/chrome.log" \
    env DISPLAY="$DISPLAY_ID" HOME="${HOME:-/home/automation}" google-chrome \
      --no-sandbox \
      --disable-dev-shm-usage \
      --use-gl=swiftshader \
      --enable-unsafe-swiftshader \
      --no-first-run \
      --no-default-browser-check \
      --remote-debugging-port="$CDP_PORT" \
      --user-data-dir="$PROFILE_DIR" \
      --window-size=1366,768 \
      $EXTRA_CHROME_FLAGS \
      "$START_URL"
  fix_chrome_geometry &
  log "Chrome started (CDP on 127.0.0.1:$CDP_PORT, profile $PROFILE_DIR)"
}

# Chrome plus a window-manager title bar is taller than the 1366x768 display,
# so the tab strip can end up above the visible top edge. Pin the frame to the
# top-left once the window manager has mapped it, leaving room for the fluxbox
# toolbar. Best effort, exactly like start_colab_browser.sh.
fix_chrome_geometry() {
  local _attempt
  for _attempt in {1..40}; do
    wmctrl -lx 2>/dev/null | grep -qi 'chrome' && break
    sleep 0.25
  done
  wmctrl -r 'Google Chrome' -b remove,maximized_vert,maximized_horz 2>/dev/null || true
  wmctrl -r 'Google Chrome' -e 0,0,0,1366,744 2>/dev/null || true
}

chrome_alive() {
  if alive chrome; then
    return 0
  fi
  pgrep -f -- "--user-data-dir=$PROFILE_DIR" >/dev/null 2>&1
}

start_server() {
  # 0.0.0.0 is required: the platform routes the app's httpPort to the pod IP,
  # not to loopback. This is the one binding that differs from Colab.
  #
  # The token is NOT passed on the command line. automation/server.py reads it
  # from AUTOMATION_TOKEN (already exported above), and a command-line argument
  # would be world-readable through /proc/<pid>/cmdline for the whole life of
  # the process. The Colab launcher does pass --token; this track does not.
  local extra=(--ai-cdp-port "$AI_CDP_PORT" --ai-display "$AI_DISPLAY")
  if [[ -n "$PUBLIC_BASE" ]]; then
    extra+=(--public-base "$PUBLIC_BASE")
  fi
  spawn automation - \
    python3 "$SERVER_SCRIPT" \
      --listen-host 0.0.0.0 --listen-port "$PORT" \
      --vnc-host 127.0.0.1 --vnc-port "$VNC_PORT" \
      --web-root "$WEB_ROOT" --novnc-dir "$NOVNC_DIR" \
      --data-dir "$AUTOMATION_DIR" \
      --control-script "$CONTROL_SCRIPT" \
      --display "$DISPLAY_ID" --cdp-port "$CDP_PORT" "${extra[@]}"
  log "automation server starting on 0.0.0.0:$PORT"
}

wait_for_http() {
  local path="$1" attempts="${2:-80}"
  local _attempt
  for _attempt in $(seq 1 "$attempts"); do
    if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:$PORT$path" 2>/dev/null; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

report_listeners() {
  if command -v ss >/dev/null 2>&1; then
    log "listening sockets:"
    ss -ltn 2>/dev/null | awk 'NR>1 {print "           " $4}' | sort -u | while read -r line; do
      log "  $line"
    done
    log "expected: 0.0.0.0:$PORT public, 127.0.0.1:$VNC_PORT and 127.0.0.1:$CDP_PORT private"
  fi
}

announce() {
  log "READY: noVNC + sidebar + JSON API + VNC WebSocket on port $PORT"
  if [[ -n "${BUILTIN_DOMAIN:-}" ]]; then
    log "open: https://${BUILTIN_DOMAIN}/vnc.html?autoconnect=true&resize=scale&path=websockify"
  elif [[ -n "${PUBLIC_BASE_URL:-}" ]]; then
    log "open: ${PUBLIC_BASE_URL%/}/vnc.html?autoconnect=true&resize=scale&path=websockify"
  else
    log "open: https://<this app's domain>/vnc.html?autoconnect=true&resize=scale&path=websockify"
  fi
  log "health check path for the platform: /automation/api/info"
  log "the VNC password and AUTOMATION_TOKEN are in $SECRETS_FILE"
}

# ---------------------------------------------------------------------------
# shutdown and supervision
# ---------------------------------------------------------------------------
SHUTTING_DOWN=no

# Terminates the tree in dependency order, waits out the grace budget, then
# force-kills whatever is left. Used by the SIGTERM handler AND by every
# failure exit, so no code path leaves services running behind it. Inside a
# container the kernel would clean up anyway; this keeps the script honest when
# it is run directly (a smoke test, a bare VM, `docker run` without an init).
stop_all() {
  local order=(automation chrome x11vnc fluxbox xvfb)
  local name

  for name in "${order[@]}"; do
    terminate "$name"
  done
  # Chrome leaves renderers and zygotes behind when only the launcher is
  # signalled, so sweep the whole profile group as well.
  pkill -TERM -f -- "--user-data-dir=$PROFILE_DIR" 2>/dev/null || true

  local deadline=$(( $(date +%s) + SHUTDOWN_GRACE_SECONDS ))
  local running
  while (( $(date +%s) < deadline )); do
    running=0
    for name in "${order[@]}"; do
      if alive "$name"; then running=1; fi
    done
    if (( running == 0 )); then break; fi
    sleep 0.5
  done

  for name in "${order[@]}"; do
    force_kill "$name"
  done
  pkill -KILL -f -- "--user-data-dir=$PROFILE_DIR" 2>/dev/null || true
}

shutdown() {
  trap - TERM INT
  SHUTTING_DOWN=yes
  log "SIGTERM received; stopping the process tree (budget ${SHUTDOWN_GRACE_SECONDS}s)"
  stop_all
  log "shutdown complete"
  exit 0
}

supervise() {
  local core=(xvfb fluxbox x11vnc automation)
  local name
  while true; do
    # `wait` on a backgrounded sleep keeps the loop interruptible: with a plain
    # foreground sleep, a trapped SIGTERM would not run until it finished.
    sleep "$CHECK_INTERVAL" &
    wait $! || true
    if [[ "$SHUTTING_DOWN" == yes ]]; then return 0; fi

    for name in "${core[@]}"; do
      if ! alive "$name"; then
        log "FATAL: $name stopped unexpectedly; shutting the stack down so the platform restarts the container"
        stop_all
        exit 1
      fi
    done

    if ! chrome_alive; then
      log "Chrome is gone; relaunching it"
      start_chrome
    fi

    # Not part of `core`: the agent's browser is an aid, so losing it must cost
    # a relaunch and never the whole container.
    if [[ "$AI_BROWSER" == "1" ]] && ! alive ai-chrome; then
      log "the agent browser is gone; relaunching it"
      start_ai_browser
    fi
  done
}

# ---------------------------------------------------------------------------
main() {
  log "=== Majid Automation System - Hostim entrypoint ==="
  check_prerequisites
  prepare_directories
  load_or_create_secrets

  trap shutdown TERM INT

  start_xvfb
  prepare_desktop
  start_window_manager
  start_vnc
  configure_keyboard
  start_chrome
  start_server

  if ! wait_for_http "/automation/api/info" "$(( STARTUP_TIMEOUT * 4 ))"; then
    log "the automation server did not answer on port $PORT"
    log "its own output is on this container's log; shutting down and exiting"
    stop_all
    exit 1
  fi
  if ! wait_for_http "/vnc.html" 40; then
    log "WARNING: /vnc.html is not being served; noVNC will not load"
  fi

  # After the product is healthy, not before: the agent's browser is an aid, and
  # its startup must never delay or endanger the main path.
  start_ai_browser

  report_listeners
  announce
  supervise
}

main "$@"
