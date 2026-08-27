#!/usr/bin/env bash
set -euo pipefail

export DISPLAY=${DISPLAY:-:1}
SCREENSHOT_DIR=${BROWSER_SCREENSHOT_DIR:-"$PWD/screen_shots"}
DEFAULT_SCREENSHOT_NAME=browser-screen.png

usage() {
  cat <<'EOF'
Usage: browser_control.sh COMMAND [ARGS]

Commands:
  screenshot [FILE]       Capture the desktop into ./screen_shots/.
  position                Print the current mouse position.
  move X Y                Move the pointer.
  click X Y [BUTTON]      Move and click (left, middle, right, or 1-5).
  doubleclick X Y         Move and double-click.
  drag X1 Y1 X2 Y2 [BTN] Drag between two points (default: left button).
  type TEXT               Type text using virtual key presses.
  paste TEXT              Paste text through the X clipboard (Unicode-safe).
  key KEY...              Send one or more xdotool key combinations.
  scroll AMOUNT           Scroll down if positive, up if negative.
  url URL                 Focus Chrome, enter a URL, and navigate.
  windows                 List visible windows.
  active                  Print the active window title.
  focus TITLE             Focus the first window matching TITLE.
EOF
}

require_display() {
  if ! xdpyinfo -display "$DISPLAY" >/dev/null 2>&1; then
    printf 'No X desktop is available on DISPLAY=%s. Start the Colab browser first.\n' "$DISPLAY" >&2
    exit 1
  fi
}

require_integer() {
  [[ "$1" =~ ^-?[0-9]+$ ]] || {
    printf 'Expected an integer, got: %s\n' "$1" >&2
    exit 2
  }
}

button_number() {
  case "$1" in
    left) printf '1' ;;
    middle) printf '2' ;;
    right) printf '3' ;;
    1|2|3|4|5) printf '%s' "$1" ;;
    *)
      printf 'Unknown mouse button: %s\n' "$1" >&2
      exit 2
      ;;
  esac
}

command=${1:-}
[[ -n "$command" ]] || {
  usage
  exit 2
}
shift

case "$command" in
  help|-h|--help)
    usage
    exit 0
    ;;
esac

require_display

case "$command" in
  screenshot)
    requested_name=${1:-$DEFAULT_SCREENSHOT_NAME}
    mkdir -p "$SCREENSHOT_DIR"
    output="$SCREENSHOT_DIR/$(basename -- "$requested_name")"
    scrot --overwrite "$output"
    printf '%s\n' "$output"
    ;;
  position)
    xdotool getmouselocation --shell
    ;;
  move)
    [[ $# -eq 2 ]] || { usage >&2; exit 2; }
    require_integer "$1"
    require_integer "$2"
    xdotool mousemove --sync "$1" "$2"
    ;;
  click)
    [[ $# -ge 2 && $# -le 3 ]] || { usage >&2; exit 2; }
    require_integer "$1"
    require_integer "$2"
    button=$(button_number "${3:-left}")
    xdotool mousemove --sync "$1" "$2" click --clearmodifiers "$button"
    ;;
  doubleclick)
    [[ $# -eq 2 ]] || { usage >&2; exit 2; }
    require_integer "$1"
    require_integer "$2"
    xdotool mousemove --sync "$1" "$2" click --clearmodifiers --repeat 2 --delay 120 1
    ;;
  drag)
    [[ $# -ge 4 && $# -le 5 ]] || { usage >&2; exit 2; }
    require_integer "$1"
    require_integer "$2"
    require_integer "$3"
    require_integer "$4"
    button=$(button_number "${5:-left}")
    xdotool mousemove --sync "$1" "$2"
    xdotool mousedown "$button"
    xdotool mousemove --sync "$3" "$4"
    xdotool mouseup "$button"
    ;;
  type)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    xdotool type --clearmodifiers --delay 15 -- "$*"
    ;;
  paste)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    printf '%s' "$*" | xclip -selection clipboard
    xdotool key --clearmodifiers ctrl+v
    ;;
  key)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    xdotool key --clearmodifiers "$@"
    ;;
  scroll)
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    require_integer "$1"
    amount=$1
    if (( amount > 0 )); then
      xdotool click --repeat "$amount" --delay 60 5
    elif (( amount < 0 )); then
      xdotool click --repeat "$((-amount))" --delay 60 4
    fi
    ;;
  url)
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    wmctrl -a 'Google Chrome'
    sleep 0.2
    xdotool key --clearmodifiers ctrl+l
    sleep 0.2
    xdotool key --clearmodifiers ctrl+a
    printf '%s' "$1" | xclip -selection clipboard
    sleep 0.2
    xdotool key --clearmodifiers ctrl+v
    sleep 0.2
    xdotool key --clearmodifiers Return
    ;;
  windows)
    wmctrl -lx
    ;;
  active)
    xdotool getactivewindow getwindowname
    ;;
  focus)
    [[ $# -ge 1 ]] || { usage >&2; exit 2; }
    wmctrl -a "$*"
    ;;
  *)
    printf 'Unknown command: %s\n' "$command" >&2
    usage >&2
    exit 2
    ;;
esac

