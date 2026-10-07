#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
STATE_DIR=${COLAB_BROWSER_STATE_DIR:-"$SCRIPT_DIR/.runtime"}

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
    fi
  fi
  rm -f "$pid_file"
}

stop_owned_process cloudflared cloudflared
stop_owned_process automation "automation/server.py"
stop_owned_process websockify websockify
stop_owned_process x11vnc x11vnc
stop_owned_process chrome google-chrome
stop_owned_process fluxbox fluxbox
stop_owned_process xvfb Xvfb

printf 'Colab browser services stopped.\n'

