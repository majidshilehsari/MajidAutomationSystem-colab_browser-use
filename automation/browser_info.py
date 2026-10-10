# خلاصهٔ فارسی: «هر چه مربوط به کروم است» در یک ماژول: تب‌های باز (از پورت
# دیباگ)، نسخهٔ مرورگر، و سابقهٔ کامل پروفایل (URLها، بازدیدها، جست‌وجوها،
# دانلودها) با خواندن مستقیم فایل History خودِ کروم. برای اینکه فایل قفل‌شده
# خوانده نشود، اول یک کپی موقت گرفته می‌شود و همان خوانده می‌شود. «تب‌های
# بسته‌شده» از تفریق سابقهٔ اخیر از تب‌های باز به دست می‌آید (فایل Session
# باینری کروم پارس نمی‌شود - این محدودیت صریحاً در خروجی هم گفته می‌شود).
# راهنمای کامل: hostim/GUIDE.fa.md (زیربخش مرورگر)

"""Read-only knowledge about the Chrome instances on this machine.

Two browsers exist in the Hostim image: the automation Chrome on display :1
(CDP 9222) and the agent's own Chrome on display :2 (CDP 9223). Each keeps a
profile under /data, and each profile holds a SQLite ``History`` database that
survives restarts - which is where "what was open before" comes from.

Rules this module obeys:

* It never opens the live database directly. Chrome holds locks and may be in
  the middle of a write, so the file (plus its WAL) is copied to a temporary
  directory first and the copy is read.
* It never invents columns. Chrome's schema changes between releases, so every
  query is built from the columns that actually exist (``PRAGMA table_info``).
* Failures are returned as ``{"ok": false, "error": ...}`` rather than raised,
  because the agent asks these questions while a page may be mid-navigation.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from automation import cdp

#: microseconds between the Windows epoch (1601) and the Unix epoch (1970)
_WINDOWS_EPOCH_OFFSET = 11644473600 * 1000000


class BrowserError(Exception):
    """A browser question that should reach the caller as an HTTP status."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def chrome_time_to_unix(value: Any) -> Optional[float]:
    """Convert a Chrome timestamp (µs since 1601) to seconds since 1970."""
    try:
        micros = float(value)
    except (TypeError, ValueError):
        return None
    if micros <= 0:
        return None
    return (micros - _WINDOWS_EPOCH_OFFSET) / 1000000.0


def chrome_time_text(value: Any, local: bool = True) -> str:
    stamp = chrome_time_to_unix(value)
    if stamp is None or stamp <= 0:
        return ""
    converter = time.localtime if local else time.gmtime
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", converter(stamp))
    except (ValueError, OverflowError, OSError):
        return ""


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------
def default_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or "/data")


def known_profiles(data_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """The Chrome profiles this deployment knows about.

    The paths follow hostim/docker-entrypoint.sh; the Colab launcher keeps its
    own profile under $HOME/.config/google-chrome, which is added if present.
    """
    base = Path(data_dir) if data_dir else default_data_dir()
    ports = (int(os.environ.get("CDP_PORT") or 9222),
             int(os.environ.get("AI_CDP_PORT") or 9223))
    profiles = [
        {"key": "automation", "fa": "کروم اتوماسیون (نمایش اصلی)",
         "dir": str(base / "chrome-profile"), "cdpPort": ports[0],
         "display": os.environ.get("DISPLAY") or ":1"},
        {"key": "agent", "fa": "کروم ایجنت (نمایش جدا)",
         "dir": str(base / "ai-profile"), "cdpPort": ports[1],
         "display": os.environ.get("AI_DISPLAY") or ":2"},
    ]
    home = Path(os.environ.get("HOME") or "/home/automation")
    colab = home / ".config" / "google-chrome"
    if colab.exists():
        profiles.append({"key": "colab", "fa": "پروفایل پیش‌فرض کروم (مسیر Colab)",
                         "dir": str(colab), "cdpPort": ports[0],
                         "display": os.environ.get("DISPLAY") or ":1"})
    for profile in profiles:
        root = Path(profile["dir"])
        profile["exists"] = root.exists()
        profile["historyPaths"] = [str(p) for p in history_candidates(root)]
    return profiles


def history_candidates(profile_dir: Path) -> List[Path]:
    """Every place a History file could be inside one profile directory."""
    out: List[Path] = []
    for name in ("Default", "Profile 1", "Profile 2", "Guest Profile"):
        candidate = profile_dir / name / "History"
        if candidate.exists():
            out.append(candidate)
    direct = profile_dir / "History"
    if direct.exists():
        out.append(direct)
    return out


def resolve_profile(key: str = "", data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Pick one profile by key, else the first one that actually exists."""
    profiles = known_profiles(data_dir)
    if key:
        for profile in profiles:
            if profile["key"] == key:
                return profile
        raise BrowserError("no Chrome profile named %r" % key, 404)
    for profile in profiles:
        if profile["exists"] and profile["historyPaths"]:
            return profile
    return profiles[0]


# ---------------------------------------------------------------------------
# live tabs (CDP HTTP endpoints)
# ---------------------------------------------------------------------------
def browser_version(port: int, timeout: float = 3.0) -> Dict[str, Any]:
    """``/json/version``: the browser release, protocol and websocket url."""
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/json/version")
        response = conn.getresponse()
        raw = response.read()
        conn.close()
    except OSError as exc:
        return {"ok": False, "port": port,
                "error": "the debugging port %d did not answer: %s" % (port, exc)}
    if response.status != 200:
        return {"ok": False, "port": port, "error": "debugging port answered %d"
                % response.status}
    import json
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return {"ok": False, "port": port, "error": "invalid JSON: %s" % exc}
    payload = dict(payload)
    payload["ok"] = True
    payload["port"] = port
    return payload


def open_tabs(port: int, timeout: float = 3.0) -> Dict[str, Any]:
    """The tabs that are open right now, plus what kind of target each is."""
    try:
        targets = cdp.list_targets(port=port, timeout=timeout)
    except cdp.CdpError as exc:
        return {"ok": False, "port": port, "error": str(exc), "tabs": []}
    tabs: List[Dict[str, Any]] = []
    others: List[Dict[str, Any]] = []
    for target in targets:
        row = {
            "id": str(target.get("id") or ""),
            "type": str(target.get("type") or ""),
            "title": str(target.get("title") or ""),
            "url": str(target.get("url") or ""),
            "favicon": str(target.get("faviconUrl") or ""),
            "attached": bool(target.get("webSocketDebuggerUrl")),
            "description": str(target.get("description") or ""),
        }
        if row["type"] in ("page", "tab"):
            tabs.append(row)
        else:
            others.append(row)
    return {"ok": True, "port": port, "tabs": tabs, "otherTargets": others,
            "count": len(tabs)}


def tab_action(action: str, target_id: str = "", url: str = "",
               port: int = 9222, timeout: float = 5.0) -> Dict[str, Any]:
    """Open, activate or close a tab through the CDP HTTP endpoints."""
    import http.client
    import json
    action = str(action or "").lower()
    if action not in ("new", "activate", "close"):
        raise BrowserError("action must be new, activate or close", 400)
    if action == "new":
        path = "/json/new"
        if url:
            path += "?url=" + _quote(url)
        method = "PUT"
    else:
        if not target_id:
            raise BrowserError("this action needs a target id", 400)
        path = "/json/%s/%s" % (action, target_id)
        method = "GET"
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request(method, path)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
    except OSError as exc:
        raise BrowserError("the debugging port %d did not answer: %s" % (port, exc))
    text = raw.decode("utf-8", "replace").strip()
    if response.status >= 400:
        raise BrowserError("Chrome answered %d for %s: %s"
                           % (response.status, path, text[:200]))
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {"raw": text[:400]}
    if isinstance(payload, dict):
        payload = {"ok": True, "action": action, "target": payload}
    else:
        payload = {"ok": True, "action": action, "target": {"id": target_id}}
    payload["port"] = port
    return payload


def _quote(text: str) -> str:
    from urllib.parse import quote
    return quote(str(text), safe=":/?&=%#")


# ---------------------------------------------------------------------------
# history (SQLite inside the profile)
# ---------------------------------------------------------------------------
class _HistoryCopy:
    """A throwaway copy of Chrome's History database, safe to read."""

    def __init__(self, source: Path) -> None:
        self.source = Path(source)
        self.tmp = Path(tempfile.mkdtemp(prefix="mas-history-"))
        self.copy = self.tmp / "History"

    def open(self) -> sqlite3.Connection:
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(self.source) + suffix)
            if candidate.exists():
                try:
                    shutil.copy2(str(candidate), str(self.copy) + suffix)
                except OSError:
                    continue
        try:
            return sqlite3.connect("file:%s?mode=ro" % self.copy, uri=True,
                                   timeout=1.0)
        except sqlite3.Error:
            # A half-written copy is still worth reading without the WAL.
            return sqlite3.connect("file:%s?immutable=1" % self.copy, uri=True)

    def discard(self) -> None:
        shutil.rmtree(str(self.tmp), ignore_errors=True)


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [str(row[1]) for row in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.Error:
        return []


def _select(conn: sqlite3.Connection, table: str, wanted: List[str],
            where: str = "", order: str = "", limit: int = 100,
            args: List[Any] = ()) -> List[Dict[str, Any]]:
    """Build a SELECT from the columns that really exist in this table."""
    have = _columns(conn, table)
    if not have:
        return []
    columns = [name for name in wanted if name in have]
    if not columns:
        return []
    sql = "SELECT %s FROM %s" % (", ".join(columns), table)
    if where:
        sql += " WHERE " + where
    if order:
        sql += " ORDER BY " + order
    sql += " LIMIT %d" % max(1, min(1000, int(limit)))
    try:
        rows = conn.execute(sql, tuple(args)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(zip(columns, row)) for row in rows]


def read_history(profile_dir: str = "", limit: int = 200, query: str = "",
                 days: float = 0.0, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Visits, searches and downloads straight out of Chrome's own database."""
    try:
        profile = resolve_profile(profile_dir, data_dir)
    except BrowserError as exc:
        return {"ok": False, "error": str(exc), "visits": [], "searches": [],
                "downloads": []}
    paths = profile.get("historyPaths") or []
    if not paths:
        return {"ok": False, "profile": profile["key"], "dir": profile["dir"],
                "error": "no History database in this profile (Chrome has not "
                         "written one yet)",
                "visits": [], "searches": [], "downloads": []}
    source = Path(paths[0])
    limit = max(1, min(1000, int(limit or 200)))
    needle = str(query or "").strip().lower()
    since_micros = 0.0
    if days and days > 0:
        since_micros = ((time.time() - float(days) * 86400.0) * 1000000.0
                        + _WINDOWS_EPOCH_OFFSET)
    out: Dict[str, Any] = {"ok": True, "profile": profile["key"], "dir": profile["dir"],
                           "historyPath": str(source), "limit": limit, "query": needle,
                           "days": float(days or 0)}
    copy = _HistoryCopy(source)
    try:
        conn = copy.open()
        try:
                where = []
                args: List[Any] = []
                if needle:
                    where.append("(lower(u.url) LIKE ? OR lower(u.title) LIKE ?)")
                    args += ["%%%s%%" % needle, "%%%s%%" % needle]
                if since_micros:
                    where.append("v.visit_time >= ?")
                    args.append(since_micros)
                clause = (" WHERE " + " AND ".join(where)) if where else ""
                joined = ("SELECT v.visit_time AS visit_time, v.transition AS transition,"
                          " u.url AS url, u.title AS title, u.visit_count AS visit_count,"
                          " u.typed_count AS typed_count, u.last_visit_time AS"
                          " last_visit_time FROM visits v JOIN urls u ON u.id = v.url"
                          + clause + " ORDER BY v.visit_time DESC LIMIT %d" % limit)
                rows: List[Dict[str, Any]] = []
                try:
                    cursor = conn.execute(joined, tuple(args))
                    names = [str(d[0]) for d in cursor.description or []]
                    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
                except sqlite3.Error as exc:
                    out["visitsError"] = str(exc)
                out["visits"] = [_visit_row(row) for row in rows]

                search_columns = _columns(conn, "keyword_search_terms")
                search_order = ("last_visit_time DESC"
                                if "last_visit_time" in search_columns else "")
                searches = _select(conn, "keyword_search_terms",
                                   ["term", "normal_term", "url_id", "last_visit_time"],
                                   order=search_order, limit=limit)
                out["searches"] = [{
                    "term": str(row.get("term") or row.get("normal_term") or ""),
                    "timeText": chrome_time_text(row.get("last_visit_time")),
                    "at": chrome_time_to_unix(row.get("last_visit_time")),
                } for row in searches if str(row.get("term") or "").strip()]

                download_columns = _columns(conn, "downloads")
                download_order = ("start_time DESC"
                                  if "start_time" in download_columns else "")
                downloads = _select(conn, "downloads",
                                    ["id", "current_path", "target_path", "start_time",
                                     "total_bytes", "received_bytes", "state",
                                     "tab_url", "mime_type", "danger_type"],
                                    order=download_order, limit=limit)
                chain = {}
                if _columns(conn, "downloads_url_chain"):
                    for row in _select(conn, "downloads_url_chain",
                                       ["id", "url", "chain_index"], limit=1000):
                        chain.setdefault(row.get("id"), []).append(
                            (row.get("chain_index") or 0, str(row.get("url") or "")))
                out["downloads"] = [{
                    "id": row.get("id"),
                    "path": str(row.get("current_path") or row.get("target_path") or ""),
                    "url": (str(row.get("tab_url") or "")
                            or (sorted(chain.get(row.get("id")) or [])[-1][1]
                                if chain.get(row.get("id")) else "")),
                    "bytes": int(row.get("total_bytes") or row.get("received_bytes") or 0),
                    "state": _download_state(row.get("state")),
                    "mime": str(row.get("mime_type") or ""),
                    "timeText": chrome_time_text(row.get("start_time")),
                    "at": chrome_time_to_unix(row.get("start_time")),
                } for row in downloads]

                out["totals"] = {
                    "urls": _count(conn, "urls"),
                    "visits": _count(conn, "visits"),
                    "searches": _count(conn, "keyword_search_terms"),
                    "downloads": _count(conn, "downloads"),
                }
                first = _scalar(conn, "SELECT min(visit_time) FROM visits")
                out["oldestVisitText"] = chrome_time_text(first)
        finally:
            conn.close()
    except OSError as exc:
        return dict(out, ok=False, error="the History database could not be copied: "
                    "%s" % exc, visits=[], searches=[], downloads=[])
    except sqlite3.Error as exc:
        return dict(out, ok=False, error="the History database could not be read: "
                    "%s" % exc, visits=[], searches=[], downloads=[])
    finally:
        copy.discard()
    return out


def _visit_row(row: Dict[str, Any]) -> Dict[str, Any]:
    at = chrome_time_to_unix(row.get("visit_time"))
    return {
        "url": str(row.get("url") or ""),
        "title": str(row.get("title") or ""),
        "at": at,
        "timeText": chrome_time_text(row.get("visit_time")),
        "visitCount": int(row.get("visit_count") or 0),
        "typedCount": int(row.get("typed_count") or 0),
        "lastVisitText": chrome_time_text(row.get("last_visit_time")),
        "transition": _transition_text(row.get("transition")),
    }


#: Chrome packs the navigation type into the low byte of `visits.transition`
_TRANSITIONS = {
    0: "لینک", 1: "تایپ آدرس", 2: "بوکمارک", 3: "اتوماسیون/دیگر",
    4: "رفرش", 5: "ارسال فرم", 6: "بازگشت/جلو", 7: "تغییر مسیر",
}


def _transition_text(value: Any) -> str:
    try:
        code = int(value) & 0xFF
    except (TypeError, ValueError):
        return ""
    return _TRANSITIONS.get(code, "کد %d" % code)


def _download_state(value: Any) -> str:
    states = {0: "در حال انجام", 1: "کامل", 2: "لغو شده", 3: "قطع شده",
              4: "ناقص"}
    try:
        return states.get(int(value), "نامشخص")
    except (TypeError, ValueError):
        return "نامشخص"


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _columns(conn, table):
        return 0
    try:
        return int(conn.execute("SELECT count(*) FROM %s" % table).fetchone()[0])
    except sqlite3.Error:
        return 0


def _scalar(conn: sqlite3.Connection, sql: str) -> Any:
    try:
        row = conn.execute(sql).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


# ---------------------------------------------------------------------------
# the combined answer the agent pane renders
# ---------------------------------------------------------------------------
def closed_tabs(open_urls: List[str], visits: List[Dict[str, Any]],
                hours: float = 24.0) -> List[Dict[str, Any]]:
    """Recently visited pages that no longer have an open tab.

    Chrome's real "recently closed tabs" list lives in a binary SNSS session
    file; this is derived from history instead, which is documented in the UI
    so nobody believes it is a session restore list.
    """
    now = time.time()
    window = now - float(hours) * 3600.0
    still_open = {_normalise(u) for u in open_urls if u}
    out: List[Dict[str, Any]] = []
    seen = set()
    for visit in visits:
        url = str(visit.get("url") or "")
        key = _normalise(url)
        at = visit.get("at")
        if not url or key in still_open or key in seen:
            continue
        if at and float(at) < window:
            continue
        seen.add(key)
        out.append({"url": url, "title": str(visit.get("title") or ""),
                    "at": at, "timeText": visit.get("timeText") or "",
                    "transition": visit.get("transition") or ""})
    return out


def _normalise(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    for prefix in ("https://www.", "http://www.", "https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.lower()


def summary(profile: str = "", port: Optional[int] = None, limit: int = 200,
            query: str = "", days: float = 0.0,
            data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """One call = tabs + version + history + derived closed tabs."""
    try:
        chosen = resolve_profile(profile, data_dir)
    except BrowserError as exc:
        return {"ok": False, "error": str(exc)}
    cdp_port = int(port or chosen.get("cdpPort") or 9222)
    tabs = open_tabs(cdp_port)
    version = browser_version(cdp_port)
    history = read_history(chosen["key"], limit=limit, query=query, days=days,
                           data_dir=data_dir)
    visits = history.get("visits") or []
    closed = closed_tabs([t["url"] for t in tabs.get("tabs") or []], visits,
                         hours=max(1.0, float(days) * 24.0) if days else 24.0)
    return {
        "ok": bool(tabs.get("ok") or history.get("ok")),
        "profile": chosen,
        "profiles": known_profiles(data_dir),
        "cdpPort": cdp_port,
        "version": version,
        "tabs": tabs,
        "history": history,
        "closedTabs": closed,
        "closedTabsNote": ("Chrome's own session file (SNSS) is not parsed; this list is "
                           "derived from the history of the last window, so a tab closed "
                           "long ago or with history disabled will not appear."),
        "collectedAt": time.time(),
        "collectedAtText": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }
