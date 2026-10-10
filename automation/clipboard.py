"""Two-way clipboard bridge between the operator's browser and the X desktop.

noVNC forwards keystrokes but it does not carry clipboard contents: Ctrl+C on
the operator's own computer never reaches the X clipboard, and Ctrl+C inside
the virtual desktop never reaches the operator's machine. This module is the
explicit bridge — the panel reads/writes text through the API and the server
moves it into or out of the X clipboard with xclip (xsel as fallback), on the
same DISPLAY the graphical Chrome lives on.

Security note: the clipboard can hold passwords, so both directions sit
behind the regular API auth (the operator's token / the agent key) and every
use is audited by the caller, never with the text itself in the audit line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Dict, Any

DEFAULT_DISPLAY = ":1"
MAX_TEXT = 200_000  # a clipboard is not a file transfer


class ClipboardError(Exception):
    """Raised with an operator-readable message."""


def _display() -> str:
    return os.environ.get("DISPLAY") or DEFAULT_DISPLAY


def _tool() -> str:
    for name in ("xclip", "xsel"):
        if shutil.which(name):
            return name
    raise ClipboardError("no clipboard tool in the image (need xclip or xsel)")


def supported() -> Dict[str, Any]:
    return {"ok": True,
            "tool": shutil.which("xclip") and "xclip" or (
                shutil.which("xsel") and "xsel" or ""),
            "display": _display()}


def read_text(timeout: float = 5.0) -> str:
    """Return the current X clipboard content as text."""
    tool = _tool()
    args = ([tool, "-selection", "clipboard", "-o"] if tool == "xclip"
            else [tool, "--clipboard", "--output"])
    env = dict(os.environ, DISPLAY=_display())
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout,
                              env=env)
    except subprocess.TimeoutExpired:
        raise ClipboardError("خواندن کلیپ‌بورد دسکتاپ طول کشید؛ دوباره تلاش کن.")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:160]
        if "target not available" in stderr or not stderr:
            raise ClipboardError("کلیپ‌بورد دسکتاپ خالی است.")
        raise ClipboardError("کلیپ‌بورد دسکتاپ خوانده نشد: %s" % stderr)
    return proc.stdout.decode("utf-8", "replace")


def write_text(text: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Put text on the X clipboard, ready for Ctrl+V inside the desktop."""
    text = str(text or "")
    if len(text) > MAX_TEXT:
        raise ClipboardError("متن کلیپ‌بورد حداکثر %d نویسه است." % MAX_TEXT)
    tool = _tool()
    # xclip forks into the background to keep serving the selection, which is
    # exactly what we want; xsel needs --input explicitly.
    args = ([tool, "-selection", "clipboard"] if tool == "xclip"
            else [tool, "--clipboard", "--input"])
    env = dict(os.environ, DISPLAY=_display())
    try:
        proc = subprocess.run(args, input=text.encode("utf-8"),
                              timeout=timeout, env=env,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        raise ClipboardError("نوشتن در کلیپ‌بورد دسکتاپ طول کشید.")
    if proc.returncode != 0:
        raise ClipboardError("در کلیپ‌بورد دسکتاپ نوشته نشد: %s"
                             % proc.stderr.decode("utf-8", "replace")
                             .strip()[:160])
    return {"ok": True, "length": len(text), "display": _display()}
