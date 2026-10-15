"""Supabase Storage snapshot sync — round 15.

Colab sessions are ephemeral: when the runtime is recycled, ``.runtime/``
(the agent SQLite database, the flow library, settings) is gone.  This module
gives the data a home outside the machine: a private Supabase Storage bucket.

Design rules, learned from the secret-vault work:

* **The app never depends on Supabase.**  Restore failures, paused free-tier
  projects and network errors are logged and swallowed — the panel always
  starts, with or without the cloud.
* **Restore happens before any store opens the database**, and only when the
  local data is missing or empty (a fresh Colab session).  A machine that
  already has data (Hostim's volume) is authoritative; nothing overwrites it
  unless the operator forces a restore from the panel.
* **Secrets stay out of snapshots**: ``secrets.env`` and the VNC credential are
  never archived.  The Supabase key itself is masked in every status output.

Storage REST API only (no new dependencies): ``POST/GET /storage/v1/object``
with ``Authorization: Bearer <key>`` and ``apikey`` headers.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import logging
import os
import sqlite3
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

LOG = logging.getLogger("automation.supabase")

DEFAULT_BUCKET = "mas-snapshots"
LATEST_NAME = "snapshot-latest.tar.gz"
HISTORY_PREFIX = "history/snapshot-"
HISTORY_KEEP = 5

#: Paths archived on every snapshot, relative to the data dir.
DB_REL = os.path.join("automation", "agent.db")
FLOWS_REL = "flows"

#: Files that must never be archived even if they sit inside the data dir.
NEVER_ARCHIVE = ("secrets.env", "vnc.pass")


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return "…"
    return key[:6] + "…" + key[-4:]


class SupabaseError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def parse_connection(text: str) -> Dict[str, str]:
    """Turn ONE pasted blob into ``{"url", "key"}``.

    The operator should not have to juggle two credentials:

    * a legacy ``service_role`` JWT carries the project reference in its
      ``iss``/``ref`` claims, so the URL is derived and nothing else is asked;
    * ``<url> <key>`` pasted together (space, comma or newline, any order)
      also works — that covers the new ``sb_secret_…`` keys, which do NOT
      embed the project reference;
    * a bare key whose format hides the URL raises a 400 that tells the
      operator exactly what to paste instead.
    """
    raw = str(text or "").strip()
    if not raw:
        raise SupabaseError("متن اتصال Supabase خالی است.", 400)
    url = ""
    key = ""
    for part in [p for p in re.split(r"[\s,;]+", raw) if p]:
        if part.startswith("http://") or part.startswith("https://"):
            url = part.rstrip("/")
        elif not key:
            key = part
    if not key:
        raise SupabaseError("کلید Supabase در متن پیدا نشد.", 400)
    if not url and key.startswith("eyJ") and key.count(".") == 2:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        try:
            claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        except Exception:  # noqa: BLE001 - not a JWT after all
            claims = {}
        iss = str(claims.get("iss") or "")
        ref = str(claims.get("ref") or "")
        if iss.startswith("http"):
            url = iss.split("/auth")[0].rstrip("/")
        elif ref:
            url = "https://%s.supabase.co" % ref
    if not url:
        raise SupabaseError(
            "این کلید نشانی پروژه را همراه ندارد؛ نشانی و کلید را با هم در یک "
            "متن بچسبانید، مثلاً: https://xxxx.supabase.co sb_secret_…", 400)
    return {"url": url, "key": key}


def inject_settings(db_path: str, rows: Dict[str, Any]) -> None:
    """Upsert settings rows straight into a (staged) agent.db file.

    Used by the panel-connect flow: the credentials just typed must survive
    the restore that follows, because the snapshot's own settings table
    predates them (and on Colab there is no env cell to fall back on).
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL,"
            "is_secret INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL)")
        now = time.time()
        for name, spec in rows.items():
            value, secret = spec if isinstance(spec, tuple) else (spec, False)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, is_secret, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (str(name), str(value), 1 if secret else 0, now))
        conn.commit()
    finally:
        conn.close()


def _http(url: str, method: str = "GET", key: str = "", data: Optional[bytes] = None,
          headers: Optional[Dict[str, str]] = None, timeout: float = 60.0) -> Any:
    request = urllib.request.Request(url, data=data, method=method)
    if key:
        request.add_header("Authorization", "Bearer " + key)
        request.add_header("apikey", key)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    return urllib.request.urlopen(request, timeout=timeout)


def _fingerprint(data_dir: str) -> str:
    """Cheap change detector: size+mtime of the db and every flow file."""
    parts = []
    for rel in (DB_REL,):
        path = os.path.join(data_dir, rel)
        try:
            info = os.stat(path)
            parts.append("%s:%d:%d" % (rel, info.st_size, int(info.st_mtime)))
        except OSError:
            parts.append(rel + ":missing")
    flows_dir = os.path.join(data_dir, FLOWS_REL)
    try:
        names = sorted(os.listdir(flows_dir))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            info = os.stat(os.path.join(flows_dir, name))
            parts.append("%s:%d:%d" % (name, info.st_size, int(info.st_mtime)))
        except OSError:
            continue
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


class SupabaseSync:
    """Snapshot/restore against one private Storage bucket, plus a timer."""

    def __init__(self, data_dir: str, settings: Optional[Callable[[], Dict[str, Any]]] = None,
                 clock: Any = time.time) -> None:
        self.data_dir = str(data_dir)
        self._settings = settings
        self._clock = clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_fingerprint = ""
        self._state: Dict[str, Any] = {
            "lastSyncAt": None, "lastResult": None, "lastError": None,
            "restoredAt": None, "uploads": 0, "downloads": 0,
        }

    # -- configuration ---------------------------------------------------
    def config(self) -> Dict[str, Any]:
        """Environment wins over panel settings: env is what boot-restore can
        rely on before any database exists."""
        stored: Dict[str, Any] = {}
        if self._settings is not None:
            try:
                stored = self._settings() or {}
            except Exception:  # noqa: BLE001 - settings must never break sync
                stored = {}
        url = (os.environ.get("SUPABASE_URL") or stored.get("supabase.url") or "").strip().rstrip("/")
        key = (os.environ.get("SUPABASE_KEY") or stored.get("supabase.key") or "").strip()
        bucket = (os.environ.get("SUPABASE_BUCKET") or stored.get("supabase.bucket")
                  or DEFAULT_BUCKET).strip()
        enabled_env = os.environ.get("SUPABASE_SYNC", "").strip().lower()
        if enabled_env in ("0", "off", "false", "no"):
            enabled = False
        elif enabled_env in ("1", "on", "true", "yes"):
            enabled = True
        else:
            enabled = bool(stored.get("supabase.enabled", True))
        try:
            every = int(stored.get("supabase.everyMin") or 10)
        except (TypeError, ValueError):
            every = 10
        every = max(1, min(every, 1440))
        return {"url": url, "key": key, "bucket": bucket,
                "enabled": bool(enabled and url and key), "everyMin": every,
                "keyMasked": mask_key(key),
                "source": "env" if os.environ.get("SUPABASE_URL") else
                          ("settings" if stored.get("supabase.url") else "")}

    # -- bucket ------------------------------------------------------------
    def ensure_bucket(self, cfg: Dict[str, Any]) -> None:
        url = "%s/storage/v1/bucket/%s" % (cfg["url"], cfg["bucket"])
        try:
            with _http(url, "GET", cfg["key"], timeout=20):
                return
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SupabaseError(
                    "کلید Supabase پذیرفته نشد (HTTP %d)؛ کلید secret یا "
                    "service_role را دوباره بررسی کن." % exc.code, 403)
            if exc.code not in (400, 404):
                raise SupabaseError("cannot reach Supabase: HTTP %d" % exc.code, 502)
        except Exception as exc:  # noqa: BLE001
            raise SupabaseError("cannot reach Supabase: %s" % type(exc).__name__, 502)
        body = json.dumps({"id": cfg["bucket"], "name": cfg["bucket"],
                           "public": False}).encode("utf-8")
        try:
            with _http("%s/storage/v1/bucket" % cfg["url"], "POST", cfg["key"],
                       data=body, headers={"Content-Type": "application/json"},
                       timeout=20):
                LOG.info("created the private bucket %r", cfg["bucket"])
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                return  # created concurrently
            raise SupabaseError("cannot create the bucket: HTTP %d" % exc.code, 502)

    # -- archive -----------------------------------------------------------
    def build_archive(self) -> bytes:
        """tar.gz of the agent database and the flow library, plus meta.json."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            db_path = os.path.join(self.data_dir, DB_REL)
            if os.path.exists(db_path):
                self._checkpoint(db_path)
                tar.add(db_path, arcname=DB_REL)
            flows_dir = os.path.join(self.data_dir, FLOWS_REL)
            if os.path.isdir(flows_dir):
                for name in sorted(os.listdir(flows_dir)):
                    if not name.endswith(".json"):
                        continue
                    if name in NEVER_ARCHIVE:
                        continue
                    tar.add(os.path.join(flows_dir, name),
                            arcname="%s/%s" % (FLOWS_REL, name))
            meta = json.dumps({"createdAt": self._clock(), "latest": LATEST_NAME,
                               "note": "MajidAutomationSystem data snapshot"},
                              ensure_ascii=False).encode("utf-8")
            info = tarfile.TarInfo("meta.json")
            info.size = len(meta)
            info.mtime = int(self._clock())
            tar.addfile(info, io.BytesIO(meta))
        return buf.getvalue()

    @staticmethod
    def _checkpoint(db_path: str) -> None:
        """Fold the WAL into the main file so the tar holds one consistent db."""
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            LOG.warning("wal checkpoint failed (%s); archiving as-is", exc)

    def upload(self, archive: bytes, cfg: Dict[str, Any]) -> Dict[str, Any]:
        self.ensure_bucket(cfg)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(self._clock()))
        targets = [LATEST_NAME, HISTORY_PREFIX + stamp + ".tar.gz"]
        for name in targets:
            url = "%s/storage/v1/object/%s/%s" % (cfg["url"], cfg["bucket"], name)
            try:
                with _http(url, "POST", cfg["key"], data=archive,
                           headers={"Content-Type": "application/gzip",
                                    "x-upsert": "true"}, timeout=120):
                    pass
            except urllib.error.HTTPError as exc:
                raise SupabaseError("upload failed: HTTP %d" % exc.code, 502)
            except Exception as exc:  # noqa: BLE001
                raise SupabaseError("upload failed: %s" % type(exc).__name__, 502)
        self._prune_history(cfg)
        return {"uploaded": len(archive), "targets": targets}

    def _prune_history(self, cfg: Dict[str, Any]) -> None:
        body = json.dumps({"prefix": "history", "limit": 1000,
                           "sortBy": {"column": "name", "order": "asc"}}).encode()
        try:
            with _http("%s/storage/v1/object/list/%s" % (cfg["url"], cfg["bucket"]),
                       "POST", cfg["key"], data=body,
                       headers={"Content-Type": "application/json"},
                       timeout=30) as response:
                listing = json.loads(response.read().decode("utf-8") or "[]")
        except Exception:  # noqa: BLE001 - pruning is best-effort
            return
        names = sorted(str(item.get("name")) for item in listing
                       if isinstance(item, dict) and item.get("name"))
        stale = names[:-HISTORY_KEEP] if len(names) > HISTORY_KEEP else []
        if not stale:
            return
        payload = json.dumps({"prefixes": ["history/" + n for n in stale]}).encode()
        try:
            with _http("%s/storage/v1/object/%s" % (cfg["url"], cfg["bucket"]),
                       "DELETE", cfg["key"], data=payload,
                       headers={"Content-Type": "application/json"}, timeout=30):
                pass
        except Exception:  # noqa: BLE001
            LOG.warning("pruning old snapshots failed; keeping them")

    def download_latest(self, cfg: Dict[str, Any]) -> Optional[bytes]:
        url = "%s/storage/v1/object/%s/%s" % (cfg["url"], cfg["bucket"], LATEST_NAME)
        try:
            with _http(url, "GET", cfg["key"], timeout=120) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404):
                return None  # no snapshot yet
            raise SupabaseError("download failed: HTTP %d" % exc.code, 502)
        except Exception as exc:  # noqa: BLE001
            raise SupabaseError("download failed: %s" % type(exc).__name__, 502)
        return data

    # -- restore ------------------------------------------------------------
    @staticmethod
    def extract_safe(archive: bytes, dest_dir: str) -> int:
        """Extract into dest_dir, refusing anything that escapes it."""
        count = 0
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                rel = member.name
                if rel.startswith("/") or ".." in rel.split("/"):
                    raise SupabaseError("refusing an unsafe archive member: %r" % rel, 400)
                if os.path.basename(rel) in NEVER_ARCHIVE:
                    continue
                target = os.path.join(dest_dir, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    continue
                with open(target, "wb") as handle:
                    handle.write(source.read())
                count += 1
        return count

    def restore(self, archive: bytes, inject: Optional[Dict[str, Any]] = None) -> int:
        """Swap the snapshot into the live data dir, file by file (atomic).

        ``inject`` adds settings rows to the staged agent.db *before* the
        swap, so credentials entered moments ago survive the restore.
        """
        with tempfile.TemporaryDirectory(prefix="mas-restore-") as staging:
            count = self.extract_safe(archive, staging)
            if inject:
                staged_db = os.path.join(staging, DB_REL)
                if os.path.exists(staged_db):
                    inject_settings(staged_db, inject)
            for root, _dirs, files in os.walk(staging):
                for name in files:
                    src = os.path.join(root, name)
                    rel = os.path.relpath(src, staging)
                    dst = os.path.join(self.data_dir, rel)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    os.replace(src, dst)
        with self._lock:
            self._state["restoredAt"] = self._clock()
            self._last_fingerprint = _fingerprint(self.data_dir)
        return count

    # -- one sync cycle -------------------------------------------------------
    def sync_now(self, force: bool = False) -> Dict[str, Any]:
        cfg = self.config()
        if not cfg["enabled"]:
            raise SupabaseError("Supabase sync is not configured", 409)
        stamp = _fingerprint(self.data_dir)
        with self._lock:
            unchanged = (stamp == self._last_fingerprint)
        if unchanged and not force:
            result = {"skipped": "no change since the last upload"}
            self._finish(cfg, result, None)
            return result
        try:
            archive = self.build_archive()
            result = self.upload(archive, cfg)
            self._finish(cfg, result, None)
            with self._lock:
                self._last_fingerprint = stamp
            return result
        except SupabaseError as exc:
            self._finish(cfg, None, str(exc))
            raise

    def _finish(self, cfg: Dict[str, Any], result: Any, error: Optional[str]) -> None:
        with self._lock:
            self._state["lastSyncAt"] = self._clock()
            self._state["lastResult"] = result
            self._state["lastError"] = error
            if result and not error and result.get("uploaded"):
                # a skipped (unchanged) cycle must not count as an upload
                self._state["uploads"] = int(self._state.get("uploads") or 0) + 1

    def status(self) -> Dict[str, Any]:
        cfg = self.config()
        with self._lock:
            data = dict(self._state)
        data.update({
            "configured": bool(cfg["url"] and cfg["key"]),
            "enabled": cfg["enabled"],
            "url": cfg["url"],
            "bucket": cfg["bucket"] if cfg["url"] else "",
            "key": cfg["keyMasked"],          # masked, always
            "source": cfg["source"],
            "everyMin": cfg["everyMin"],
            "running": bool(self._thread and self._thread.is_alive()),
        })
        return data

    # -- background timer ---------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop,
                                            name="supabase-sync", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            cfg = self.config()
            if cfg["enabled"]:
                try:
                    self.sync_now()
                except SupabaseError as exc:
                    LOG.warning("periodic sync failed: %s", exc)
                except Exception as exc:  # noqa: BLE001 - the loop must survive
                    LOG.warning("periodic sync crashed: %s", exc)
            # Sleep in small slices so stop() and config changes are noticed.
            for _ in range(int(cfg["everyMin"] * 60 / 5) or 120):
                if self._stop.wait(5):
                    return


def restore_on_boot(data_dir: str) -> Optional[Dict[str, Any]]:
    """Restore the cloud snapshot into a *fresh* data dir, before any store
    opens the database.  Environment credentials only (the settings database
    is exactly what does not exist yet).  Never raises: a paused free-tier
    project or a dead network must not stop the panel from starting.
    """
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_KEY") or "").strip()
    if not url or not key:
        return None
    bucket = (os.environ.get("SUPABASE_BUCKET") or DEFAULT_BUCKET).strip()
    force = os.environ.get("SUPABASE_FORCE_RESTORE", "").strip().lower() in ("1", "on", "true")
    db_path = os.path.join(data_dir, DB_REL)
    has_local = False
    try:
        has_local = os.path.getsize(db_path) > 0
    except OSError:
        has_local = False
    if has_local and not force:
        LOG.info("local data found; skipping the Supabase restore "
                 "(set SUPABASE_FORCE_RESTORE=1 to override)")
        return {"skipped": "local data present"}
    sync = SupabaseSync(data_dir)
    cfg = {"url": url, "key": key, "bucket": bucket, "enabled": True, "everyMin": 10}
    try:
        archive = sync.download_latest(cfg)
        if archive is None:
            LOG.info("no Supabase snapshot yet; starting fresh")
            return {"restored": 0}
        count = sync.restore(archive)
        LOG.info("restored %d file(s) from Supabase bucket %r", count, bucket)
        return {"restored": count}
    except SupabaseError as exc:
        LOG.warning("Supabase restore failed (%s); starting with local data", exc)
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Supabase restore crashed (%s); starting with local data", exc)
        return {"error": type(exc).__name__}
