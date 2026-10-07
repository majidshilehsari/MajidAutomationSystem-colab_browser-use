"""Page detection and the "remembered pages" store.

A detection is deliberately manual: the sidebar has a Detect button, because
walking the DOM and writing a screenshot costs real time on a software rendered
Colab desktop and should never happen on a timer.

Each detection is remembered under ``<data>/pages`` keyed by the page (host and
path), so the human can hand an AI the state of a page they visited earlier.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import cdp

MAX_ELEMENTS = 250
MAX_TEXT_CHARS = 4000

#: Collects the interactive elements plus the numbers needed to map page
#: coordinates onto desktop coordinates.
PAGE_SCRIPT = r"""
(() => {
  const dpr = window.devicePixelRatio || 1;
  const chromeHeight = Math.max(0, (window.outerHeight - window.innerHeight));
  const seen = new Set();
  const selectorFor = (el) => {
    if (el.id) { return '#' + CSS.escape(el.id); }
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 5) {
      let part = node.tagName.toLowerCase();
      if (node.classList && node.classList.length) {
        part += '.' + Array.from(node.classList).slice(0, 2).map(c => CSS.escape(c)).join('.');
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
        if (siblings.length > 1) { part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')'; }
      }
      parts.unshift(part);
      node = node.parentElement;
    }
    return parts.join(' > ');
  };
  const nodes = document.querySelectorAll(
    'a, button, input, textarea, select, summary, [role="button"], [role="link"], ' +
    '[role="tab"], [role="menuitem"], [onclick], [contenteditable="true"]');
  const elements = [];
  for (const el of nodes) {
    if (elements.length >= %d) { break; }
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) { continue; }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') { continue; }
    const inView = rect.top >= 0 && rect.left >= 0 &&
                   rect.bottom <= window.innerHeight && rect.right <= window.innerWidth;
    const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
                  el.getAttribute('placeholder') || '').replace(/\s+/g, ' ').trim().slice(0, 120);
    const selector = selectorFor(el);
    if (seen.has(selector)) { continue; }
    seen.add(selector);
    elements.push({
      tag: el.tagName.toLowerCase(),
      selector: selector,
      text: text,
      type: el.getAttribute('type') || null,
      href: (el.getAttribute('href') || '').slice(0, 200) || null,
      visible: inView,
      page: {x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + rect.height / 2)},
      size: {w: Math.round(rect.width), h: Math.round(rect.height)},
      desktop: {
        x: Math.round(window.screenX + (rect.left + rect.width / 2) * dpr),
        y: Math.round(window.screenY + chromeHeight + (rect.top + rect.height / 2) * dpr)
      }
    });
  }
  return {
    url: location.href,
    title: document.title,
    scroll: {x: window.scrollX, y: window.scrollY},
    window: {
      screenX: window.screenX, screenY: window.screenY,
      innerWidth: window.innerWidth, innerHeight: window.innerHeight,
      outerWidth: window.outerWidth, outerHeight: window.outerHeight,
      devicePixelRatio: dpr
    },
    bodyText: (document.body && document.body.innerText || '').replace(/\n{3,}/g, '\n\n').slice(0, %d),
    elementCount: nodes.length,
    elements: elements
  };
})()
""" % (MAX_ELEMENTS, MAX_TEXT_CHARS)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str, limit: int = 40) -> str:
    slug = _SLUG_RE.sub("-", (value or "").lower()).strip("-")
    return slug[:limit] or "page"


class Detector:
    name = "x11+cdp"

    def __init__(self, backend, data_dir: str, viewport: Dict[str, int],
                 cdp_port: int = cdp.CDP_PORT, cdp_module=cdp):
        self.backend = backend
        self.data_dir = data_dir
        self.viewport = viewport
        self.cdp_port = cdp_port
        self.cdp = cdp_module
        self.pages_dir = os.path.join(data_dir, "pages")
        self.shots_dir = os.path.join(data_dir, "shots")
        self.index_path = os.path.join(self.pages_dir, "index.json")
        os.makedirs(self.pages_dir, exist_ok=True)
        os.makedirs(self.shots_dir, exist_ok=True)
        self._lock = threading.Lock()

    # -- primitives -----------------------------------------------------
    def screenshot_now(self, name: Optional[str] = None) -> Optional[str]:
        name = name or "shot-%d.png" % int(time.time() * 1000)
        previous = getattr(self.backend, "screenshot_dir", None)
        try:
            self.backend.screenshot_dir = self.shots_dir
            rc, out, err = self.backend.screenshot(name)
        finally:
            self.backend.screenshot_dir = previous
        if rc != 0:
            return None
        path = out.strip() or os.path.join(self.shots_dir, name)
        return path if os.path.exists(path) else os.path.join(self.shots_dir, name)

    def cdp_available(self) -> bool:
        try:
            return self.cdp.pick_page_target(self.cdp_port) is not None
        except Exception:
            return False

    def window_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {"active": None, "windows": []}
        rc, out, _ = self.backend.active_window()
        if rc == 0 and out:
            info["active"] = out
        rc, out, _ = self.backend.windows()
        if rc == 0 and out:
            for line in out.splitlines():
                # wmctrl -lx prints: id desktop class host title (title has spaces)
                parts = line.split(None, 4)
                if len(parts) == 5:
                    info["windows"].append({"id": parts[0], "desktop": parts[1],
                                            "class": parts[2], "host": parts[3],
                                            "title": parts[4]})
                elif len(parts) == 4:
                    info["windows"].append({"id": parts[0], "desktop": parts[1],
                                            "class": parts[2], "host": "",
                                            "title": parts[3]})
        return info

    def clipboard_text(self) -> str:
        """Fallback page text when the debugging port is not reachable."""
        rc, out, _ = self.backend.page_text()
        if rc != 0:
            return ""
        return (out or "")[:MAX_TEXT_CHARS]

    # -- detection ------------------------------------------------------
    def detect(self, include_dom: bool = True) -> Dict[str, Any]:
        captured_at = time.time()
        windows = self.window_info()
        shot = self.screenshot_now("detect-%d.png" % int(captured_at * 1000))

        snapshot: Dict[str, Any] = {
            "capturedAt": captured_at,
            "viewport": self.viewport,
            "screenshot": os.path.relpath(shot, self.data_dir) if shot else None,
            "activeWindow": windows["active"],
            "windows": windows["windows"][:40],
            "cdp": False,
            "url": None,
            "title": windows["active"],
            "text": "",
            "elements": [],
            "error": None,
        }

        dom: Optional[Dict[str, Any]] = None
        if include_dom:
            try:
                result = self.cdp.evaluate(PAGE_SCRIPT, port=self.cdp_port)
                dom = result.get("value") if isinstance(result.get("value"), dict) else None
                snapshot["url"] = result.get("url")
                snapshot["title"] = result.get("title") or snapshot["title"]
                snapshot["cdp"] = dom is not None
            except Exception as exc:
                snapshot["error"] = "cdp: %s" % exc

        if dom:
            snapshot["url"] = dom.get("url") or snapshot["url"]
            snapshot["title"] = dom.get("title") or snapshot["title"]
            snapshot["text"] = (dom.get("bodyText") or "")[:MAX_TEXT_CHARS]
            snapshot["elements"] = dom.get("elements", [])
            snapshot["elementCount"] = dom.get("elementCount", len(snapshot["elements"]))
            snapshot["scroll"] = dom.get("scroll")
            snapshot["window"] = dom.get("window")
        else:
            snapshot["text"] = self.clipboard_text()
            if not snapshot["url"]:
                snapshot["url"] = _url_from_title(snapshot["title"])

        snapshot["pageKey"] = page_key(snapshot.get("url"), snapshot.get("title"))
        snapshot["id"] = "%s-%d" % (slugify(snapshot["pageKey"], 30), int(captured_at))
        self._save(snapshot)
        return snapshot

    # -- memory ---------------------------------------------------------
    def _read_index(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.index_path):
            return []
        try:
            with open(self.index_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            text = snapshot.get("text") or ""
            path = os.path.join(self.pages_dir, snapshot["id"] + ".json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)

            index = self._read_index()
            entry = {
                "id": snapshot["id"],
                "pageKey": snapshot["pageKey"],
                "title": snapshot.get("title"),
                "url": snapshot.get("url"),
                "capturedAt": snapshot["capturedAt"],
                "screenshot": snapshot.get("screenshot"),
                "elements": len(snapshot.get("elements", [])),
                "cdp": snapshot.get("cdp", False),
                # The index stays small: only a preview of the page text.
                "textChars": len(text),
                "textPreview": text[:300],
            }
            index.append(entry)
            index = index[-200:]
            tmp = self.index_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(index, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.index_path)

    def pages(self) -> List[Dict[str, Any]]:
        index = self._read_index()
        index.sort(key=lambda item: item.get("capturedAt", 0), reverse=True)
        return index

    def load(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        if "/" in snapshot_id or snapshot_id in ("", ".", ".."):
            return None
        path = os.path.join(self.pages_dir, snapshot_id + ".json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None


def page_key(url: Optional[str], title: Optional[str]) -> str:
    if url:
        try:
            parsed = urlparse(url)
        except ValueError:
            parsed = None
        if parsed and parsed.netloc:
            path = parsed.path.rstrip("/")
            return (parsed.netloc + path)[:120]
    return (title or "unknown-page")[:120]


def _url_from_title(title: Optional[str]) -> Optional[str]:
    """Chrome puts the URL in the window title for some pages; best effort."""
    if not title:
        return None
    match = re.search(r"https?://\S+", title)
    return match.group(0) if match else None
