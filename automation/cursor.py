"""A visible cursor: a big yellow pointer and a ripple when a click lands.

Watching an automated desktop over VNC, the two things you lose are *where the
pointer is* and *whether a click actually happened*. The stock X cursor is a
handful of black-and-white pixels and a click is completely silent.

Both are fixed here without touching X at all, because everything the operator
watches is a Chrome page:

* the pointer is replaced by a **CSS cursor** generated in-process - a yellow
  arrow with a dark outline, drawn into a PNG at whatever size is wanted and
  injected as a `<style>` rule with `!important`, so it wins over the `pointer`
  and `text` cursors pages set themselves;
* a click draws an expanding yellow ring at the click position.

Coordinates arrive in **desktop** space (that is what `xdotool` clicks on) and
are converted inside the page with the same arithmetic `detect.py` uses to go
the other way, so the ring lands where the pointer actually was.

Nothing here needs an image library, an X cursor theme, or a package that may
not exist in the image: the PNG is written byte by byte with `zlib`.
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Default pointer size in CSS pixels. The stock cursor is roughly 16-24 px, so
#: this is clearly larger without covering the thing being clicked.
DEFAULT_SIZE = 44
MIN_SIZE = 16
MAX_SIZE = 128

#: Yellow with a dark outline: readable on both white pages and dark ones.
DEFAULT_FILL = "#ffd400"
DEFAULT_OUTLINE = "#1b1b1b"
DEFAULT_RIPPLE = "#ffd400"

#: The classic X11 left_ptr outline, in a 32x32 box, tip first.
_ARROW: Sequence[Tuple[float, float]] = (
    (1.0, 1.0), (1.0, 25.0), (7.2, 19.2), (11.4, 28.6),
    (15.8, 26.6), (11.6, 17.4), (19.6, 17.2),
)

CURSOR_STYLE_ID = "mas-cursor-style"
RIPPLE_CLASS = "mas-click-ripple"


# ---------------------------------------------------------------------------
# a tiny PNG writer (RGBA, no dependencies)
# ---------------------------------------------------------------------------
def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(width: int, height: int, rows: List[List[Tuple[int, int, int, int]]]) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBBB", *pixel) for pixel in row)
                   for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b""))


def _hex_rgb(value: str, fallback: str) -> Tuple[int, int, int]:
    text = str(value or fallback).strip()
    if not text.startswith("#"):
        text = "#" + text
    try:
        digits = text[1:]
        if len(digits) == 3:
            digits = "".join(char * 2 for char in digits)
        number = int(digits, 16)
        return ((number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF)
    except ValueError:
        return _hex_rgb(fallback, fallback)


def _inside(polygon: Sequence[Tuple[float, float]], x: float, y: float) -> bool:
    """Even-odd point-in-polygon, which is all a seven-vertex arrow needs."""
    hits = 0
    count = len(polygon)
    for index in range(count):
        x1, y1 = polygon[index]
        x2, y2 = polygon[(index + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < crossing:
                hits += 1
    return bool(hits % 2)


def arrow_png(size: int = DEFAULT_SIZE, fill: str = DEFAULT_FILL,
              outline: str = DEFAULT_OUTLINE) -> bytes:
    """Draw the pointer as a PNG: a filled arrow one pixel-outlined in dark."""
    size = int(max(MIN_SIZE, min(MAX_SIZE, size or DEFAULT_SIZE)))
    scale = size / 32.0
    polygon = [(x * scale, y * scale) for x, y in _ARROW]
    fill_rgb = _hex_rgb(fill, DEFAULT_FILL) + (255,)
    outline_rgb = _hex_rgb(outline, DEFAULT_OUTLINE) + (255,)
    transparent = (0, 0, 0, 0)

    # Sample the polygon at pixel centres, then outline by dilation: a pixel is
    # outline if it is outside the arrow but touches one that is inside.
    mask = [[_inside(polygon, x + 0.5, y + 0.5) for x in range(size)]
            for y in range(size)]
    rows: List[List[Tuple[int, int, int, int]]] = []
    for y in range(size):
        row: List[Tuple[int, int, int, int]] = []
        for x in range(size):
            if mask[y][x]:
                row.append(fill_rgb)
            elif any(mask[min(size - 1, max(0, y + dy))][min(size - 1, max(0, x + dx))]
                     for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                row.append(outline_rgb)
            else:
                row.append(transparent)
        rows.append(row)
    return _png(size, size, rows)


def png_size(data: bytes) -> Tuple[int, int]:
    """Read a PNG's dimensions straight out of its IHDR chunk."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    width, height = struct.unpack(">II", data[16:24])
    return (int(width), int(height))


def hotspot(size: int) -> Tuple[int, int]:
    """Where the tip of the arrow is, in CSS pixels."""
    size = int(max(MIN_SIZE, min(MAX_SIZE, size or DEFAULT_SIZE)))
    scale = size / 32.0
    return (int(round(_ARROW[0][0] * scale)), int(round(_ARROW[0][1] * scale)))


def cursor_data_url(size: int = DEFAULT_SIZE, fill: str = DEFAULT_FILL,
                    outline: str = DEFAULT_OUTLINE) -> str:
    image = arrow_png(size, fill, outline)
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


# ---------------------------------------------------------------------------
# the JavaScript that gets injected
# ---------------------------------------------------------------------------
#: Replaces the page cursor. `!important` on every element is deliberate: pages
#: set `cursor: pointer` and `cursor: text` themselves and would otherwise win.
_CURSOR_CSS = """
(function () {
  var old = document.getElementById(%s);
  if (old) { old.parentNode.removeChild(old); }
  var style = document.createElement('style');
  style.id = %s;
  style.textContent = '* { cursor: url(%s) %d %d, auto !important; }';
  (document.head || document.documentElement).appendChild(style);
  return JSON.stringify({ok: true, size: %d});
})()
"""

#: One expanding ring at the click position. Desktop coordinates are converted
#: with the same arithmetic detect.py uses in the other direction.
_RIPPLE_JS = """
(function () {
  var x = %(x)s, y = %(y)s;
  var dpr = window.devicePixelRatio || 1;
  var chrome = Math.max(0, window.outerHeight - window.innerHeight);
  var px = (x - window.screenX) / dpr;
  var py = (y - window.screenY - chrome) / dpr;
  if (!(px >= 0 && py >= 0 && px <= window.innerWidth && py <= window.innerHeight)) {
    return JSON.stringify({ok: false, reason: 'outside the viewport'});
  }
  var size = %(size)s;
  var ring = document.createElement('div');
  ring.className = %(klass)s;
  ring.style.cssText = [
    'position: fixed', 'z-index: 2147483647', 'pointer-events: none',
    'left: ' + (px - size / 2) + 'px', 'top: ' + (py - size / 2) + 'px',
    'width: ' + size + 'px', 'height: ' + size + 'px', 'border-radius: 50%%',
    'border: 3px solid %(color)s', 'background: %(fillbg)s',
    'opacity: 0.95', 'transition: transform 420ms ease-out, opacity 420ms ease-out',
    'transform: scale(0.25)'
  ].join(';');
  (document.body || document.documentElement).appendChild(ring);
  window.requestAnimationFrame(function () {
    ring.style.transform = 'scale(1)';
    ring.style.opacity = '0';
  });
  window.setTimeout(function () {
    if (ring.parentNode) { ring.parentNode.removeChild(ring); }
  }, 600);
  return JSON.stringify({ok: true, pageX: Math.round(px), pageY: Math.round(py)});
})()
"""


def cursor_js(size: int = DEFAULT_SIZE, fill: str = DEFAULT_FILL,
              outline: str = DEFAULT_OUTLINE) -> str:
    url = cursor_data_url(size, fill, outline)
    hot_x, hot_y = hotspot(size)
    return _CURSOR_CSS % (json.dumps(CURSOR_STYLE_ID), json.dumps(CURSOR_STYLE_ID),
                          json.dumps(url), hot_x, hot_y, int(size))


def clear_cursor_js() -> str:
    return ("(function(){var n=document.getElementById(%s);"
            "if(n){n.parentNode.removeChild(n);return JSON.stringify({ok:true});}"
            "return JSON.stringify({ok:false});})()" % json.dumps(CURSOR_STYLE_ID))


def ripple_js(x: int, y: int, color: str = DEFAULT_RIPPLE, size: int = 46) -> str:
    rgb = _hex_rgb(color, DEFAULT_RIPPLE)
    return _RIPPLE_JS % {
        "x": int(x), "y": int(y), "size": int(max(12, min(200, size))),
        "klass": json.dumps(RIPPLE_CLASS),
        "color": json.dumps(color if str(color).startswith("#") else DEFAULT_RIPPLE),
        "fillbg": json.dumps("rgba(%d,%d,%d,0.28)" % rgb),
    }


# ---------------------------------------------------------------------------
# driving it through CDP
# ---------------------------------------------------------------------------
class CursorFx:
    """Installs the pointer and the click ripple into the automation browser.

    Failures are swallowed and reported, never raised: a cosmetic effect must
    not be able to break a run, and CDP is simply not there in every setup.
    """

    def __init__(self, cdp: Any = None, port: int = 9222, config: Optional[Dict[str, Any]] = None):
        self.cdp = cdp
        self.port = int(port)
        self.config: Dict[str, Any] = dict(config or {})

    # -- configuration --------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def size(self) -> int:
        return int(self.config.get("size", DEFAULT_SIZE) or DEFAULT_SIZE)

    def fill(self) -> str:
        return str(self.config.get("color", DEFAULT_FILL) or DEFAULT_FILL)

    def outline(self) -> str:
        return str(self.config.get("outline", DEFAULT_OUTLINE) or DEFAULT_OUTLINE)

    def ripple_enabled(self) -> bool:
        return bool(self.config.get("ripple", True))

    def configure(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Swap the settings and reinstall, so saving in the sidebar changes
        the pointer immediately instead of at the next run.

        The old cursor CSS is removed first: an install is a `<style>` append,
        and two of them would fight over `!important`.
        """
        if config is not None:
            self.config = dict(config)
        self.uninstall()
        return self.install()

    def describe(self) -> Dict[str, Any]:
        return {"enabled": self.enabled(), "size": self.size(),
                "color": self.fill(), "outline": self.outline(),
                "ripple": self.ripple_enabled(),
                "cursorDataUrlLength": len(cursor_data_url(self.size(), self.fill(),
                                                           self.outline()))}

    # -- injection ------------------------------------------------------
    def _evaluate(self, expression: str) -> Dict[str, Any]:
        if self.cdp is None:
            return {"ok": False, "reason": "no cdp module"}
        try:
            result = self.cdp.evaluate(expression, port=self.port, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
            return {"ok": False, "reason": str(exc)}
        value = (result or {}).get("value")
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return {"ok": False, "reason": value[:200]}
        return {"ok": False, "reason": "no value"}

    def install(self) -> Dict[str, Any]:
        """Put the yellow pointer on the page. Safe to call repeatedly."""
        if not self.enabled():
            return self.uninstall()
        return self._evaluate(cursor_js(self.size(), self.fill(), self.outline()))

    def uninstall(self) -> Dict[str, Any]:
        return self._evaluate(clear_cursor_js())

    def click_effect(self, x: int, y: int) -> Dict[str, Any]:
        """Draw the ring for a click that is about to happen at desktop x,y."""
        if not self.enabled() or not self.ripple_enabled():
            return {"ok": False, "reason": "disabled"}
        return self._evaluate(ripple_js(x, y, self.fill(), max(30, self.size() + 2)))
