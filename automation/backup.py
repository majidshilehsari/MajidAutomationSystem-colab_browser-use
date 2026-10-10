# خلاصهٔ فارسی: پشتیبان‌گیری پیوسته از دیتابیس ایجنت (agent.db) و جریان‌های
# ذخیره‌شده. هر پشتیبان یک فایل tar.gz است که:
#   * یک snapshot سازگار از SQLite (با API رسمی backup، نه کپی خام فایل قفل‌شده)،
#   * همهٔ جریان‌های JSON،
#   * و فقط با درخواست صریح کاربر، فایل secrets.env
# را نگه می‌دارد. فایل‌ها در <data>/backups می‌مانند (روی volume، یعنی با
# redeploy پاک نمی‌شوند)، می‌شود دانلود/حذف کرد، می‌شود تعداد نگه‌داشته‌شده را
# محدود کرد، و می‌شود نسخه را با تلگرام به یک کانال/چت مشخص فرستاد - دستی یا
# زمان‌بندی‌شده از طریق job با action=backup.
# راهنمای کامل: hostim/GUIDE.fa.md (بخش دیتابیس)

"""Backups of the agent database and the saved flows.

Why this exists: the whole agent brain lives in one SQLite file on the Hostim
volume. The platform takes daily volume snapshots, but they are project-wide,
kept for seven days and cannot be restored from the console yet, so an operator
who wants "give me yesterday's chat back" needs something they control.

Design notes:

* The database is copied with SQLite's own backup API, never with a file copy.
  A file copy of a live database can capture a half-written page.
* The archive is a tar.gz, so one download contains everything and can be
  inspected with plain ``tar tvf``.
* ``secrets.env`` (Telegram tokens, DeepSeek cookies, the agent key) is NOT in
  the archive unless the operator explicitly asks for it, because a backup that
  can be uploaded to a Telegram channel must not silently carry credentials.
* Nothing here knows about HTTP: the api layer calls in and gets plain dicts.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from automation.agent_store import JOB_KINDS

BACKUP_DIR_NAME = "backups"
ARCHIVE_PREFIX = "mas-backup-"
DEFAULT_KEEP = 7
MAX_KEEP = 200
#: settings keys read/written by :class:`BackupManager`
SETTING_ENABLED = "backup.enabled"
SETTING_KEEP = "backup.keep"
SETTING_TARGET = "backup.telegramTarget"
SETTING_CHANNEL = "backup.telegramChannel"
SETTING_SECRETS = "backup.includeSecrets"
SETTING_LABEL = "backup.label"
SETTING_NOTIFY = "backup.notify"
SETTING_LAST = "backup.last"
#: the action used by the single automatic-backup job this manager owns
JOB_TARGET = "backup"


class BackupError(Exception):
    """A backup problem that should reach the caller as an HTTP status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _now_text(when: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


def _stamp(when: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(when))


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f GB" % value


class BackupManager:
    """Create, list, prune, download and ship backups."""

    def __init__(self, store: Any, data_dir: Optional[Path] = None,
                 notifier: Any = None, clock: Callable[[], float] = time.time,
                 flows_dir: Optional[Path] = None, hub: Any = None) -> None:
        self.store = store
        self.notifier = notifier
        self.hub = hub
        self._clock = clock
        base = Path(data_dir) if data_dir else Path(store.db_path).parent
        self.dir = Path(base) / BACKUP_DIR_NAME
        self.flows_dir = Path(flows_dir) if flows_dir else None

    # -- discovery ------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return Path(self.store.db_path)

    def secrets_path(self) -> Optional[Path]:
        raw = getattr(self.store, "secrets_path", "")
        return Path(raw) if raw else None

    def ensure_dir(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    # -- what is in the database ----------------------------------------
    def table_rows(self) -> Dict[str, int]:
        """Row count per table; a read-only peek that never fails loudly."""
        counts: Dict[str, int] = {}
        try:
            conn = sqlite3.connect(str(self.db_path))
        except sqlite3.Error as exc:
            return {"error": str(exc)}  # type: ignore[dict-item]
        try:
            rows = conn.execute(
                "select name from sqlite_master where type='table' "
                "and name not like 'sqlite_%' order by name").fetchall()
            for (name,) in rows:
                try:
                    counts[str(name)] = int(conn.execute(
                        'select count(*) from "%s"' % str(name).replace('"', '""')
                    ).fetchone()[0])
                except sqlite3.Error:
                    counts[str(name)] = -1
        except sqlite3.Error as exc:
            counts["error"] = str(exc)  # type: ignore[assignment]
        finally:
            conn.close()
        return counts

    def flow_names(self) -> List[str]:
        if self.flows_dir is None:
            return []
        try:
            return sorted(p.stem for p in self.flows_dir.glob("*.json"))
        except OSError:
            return []

    def stats(self) -> Dict[str, Any]:
        """Everything the «دیتابیس» pane shows before a single backup exists."""
        db_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        flows = self.flow_names()
        flow_bytes = 0
        for name in flows:
            try:
                flow_bytes += (self.flows_dir / ("%s.json" % name)).stat().st_size
            except OSError:
                pass
        backups = self.list()
        last = backups[0] if backups else None
        total = sum(int(b.get("size") or 0) for b in backups)
        return {
            "dir": str(self.dir),
            "dirExists": self.dir.exists(),
            "dbPath": str(self.db_path),
            "dbBytes": db_bytes,
            "dbSizeText": human_bytes(db_bytes),
            "tables": self.table_rows(),
            "flows": len(flows),
            "flowNames": flows[:50],
            "flowBytes": flow_bytes,
            "secretsPath": str(self.secrets_path() or ""),
            "backups": len(backups),
            "backupBytes": total,
            "backupSizeText": human_bytes(total),
            "lastBackup": last,
            "config": self.config(),
        }

    # -- creation --------------------------------------------------------
    def create(self, include_secrets: Optional[bool] = None,
               actor: str = "operator", label: str = "") -> Dict[str, Any]:
        """Write one tar.gz backup and return its metadata."""
        self.ensure_dir()
        if include_secrets is None:
            include_secrets = bool(self.setting(SETTING_SECRETS))
        when = self._clock()
        name = "%s%s.tar.gz" % (ARCHIVE_PREFIX, _stamp(when))
        path = self.dir / name

        staged = Path(tempfile.mkdtemp(prefix="mas-backup-"))
        try:
            snapshot = staged / "agent.db"
            self._snapshot_db(snapshot)
            members: List[Path] = [snapshot]
            flows_root = staged / "flows"
            if self.flows_dir is not None and self.flows_dir.exists():
                flows_root.mkdir(parents=True, exist_ok=True)
                for flow in sorted(self.flows_dir.glob("*.json")):
                    try:
                        shutil.copy2(str(flow), str(flows_root / flow.name))
                        members.append(flows_root / flow.name)
                    except OSError:
                        continue
            secrets = self.secrets_path()
            if include_secrets and secrets is not None and secrets.exists():
                try:
                    shutil.copy2(str(secrets), str(staged / secrets.name))
                    members.append(staged / secrets.name)
                except OSError:
                    include_secrets = False
            meta = {
                "createdAt": when,
                "createdAtText": _now_text(when),
                "actor": actor,
                "label": str(label or self.setting(SETTING_LABEL) or ""),
                "includeSecrets": bool(include_secrets),
                "dbBytes": int(self.db_path.stat().st_size) if self.db_path.exists() else 0,
                "tables": self.table_rows(),
                "flows": self.flow_names(),
                "generator": "MajidAutomationSystem",
            }
            (staged / "backup.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            members.append(staged / "backup.json")

            with tarfile.open(str(path), "w:gz") as archive:
                for member in members:
                    archive.add(str(member),
                                arcname=str(member.relative_to(staged)))
        except (OSError, sqlite3.Error, tarfile.TarError) as exc:
            try:
                path.unlink()
            except OSError:
                pass
            raise BackupError("the backup could not be written: %s" % exc, 500)
        finally:
            shutil.rmtree(str(staged), ignore_errors=True)

        size = int(path.stat().st_size)
        info = {
            "name": name,
            "path": str(path),
            "size": size,
            "sizeText": human_bytes(size),
            "createdAt": when,
            "createdAtText": _now_text(when),
            "actor": actor,
            "label": str(meta.get("label") or ""),
            "includeSecrets": bool(include_secrets),
            "flows": len(meta.get("flows") or []),
            "tables": meta.get("tables") or {},
        }
        self.store.set_setting(SETTING_LAST,
                               {k: info[k] for k in ("name", "createdAt", "size",
                                                     "includeSecrets")})
        self.store.audit(actor, "backup.created", "%s (%d bytes)" % (name, size))
        return info

    def _snapshot_db(self, target: Path) -> None:
        """Copy the live database through SQLite's backup API."""
        if not self.db_path.exists():
            raise BackupError("the agent database does not exist yet: %s" % self.db_path,
                              409)
        source = sqlite3.connect(str(self.db_path))
        try:
            destination = sqlite3.connect(str(target))
            try:
                with destination:
                    source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

    # -- listing and safe names ------------------------------------------
    def list(self) -> List[Dict[str, Any]]:
        """Newest first. The directory is the source of truth."""
        if not self.dir.exists():
            return []
        out: List[Dict[str, Any]] = []
        try:
            entries = list(self.dir.glob(ARCHIVE_PREFIX + "*.tar.gz"))
        except OSError:
            return []
        for entry in entries:
            try:
                stat = entry.stat()
            except OSError:
                continue
            out.append({
                "name": entry.name,
                "path": str(entry),
                "size": int(stat.st_size),
                "sizeText": human_bytes(int(stat.st_size)),
                "createdAt": stat.st_mtime,
                "createdAtText": _now_text(stat.st_mtime),
                "download": "/automation/api/agent/backup-download?name=%s" % entry.name,
            })
        out.sort(key=lambda row: (-float(row["createdAt"]), row["name"]))
        return out

    def safe_path(self, name: str) -> Path:
        """Refuse anything that is not a plain file name inside the directory."""
        candidate = str(name or "").strip()
        if not candidate or "/" in candidate or "\\" in candidate or candidate in (".", ".."):
            raise BackupError("that is not a backup file name", 400)
        path = (self.dir / candidate).resolve()
        root = self.dir.resolve()
        if not str(path).startswith(str(root) + os.sep):
            raise BackupError("that is not a backup file name", 400)
        if not path.exists() or not path.is_file():
            raise BackupError("no backup named %s" % candidate, 404)
        return path

    def read(self, name: str) -> bytes:
        path = self.safe_path(name)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise BackupError("the backup could not be read: %s" % exc, 500)

    def delete(self, name: str, actor: str = "operator") -> Dict[str, Any]:
        path = self.safe_path(name)
        size = int(path.stat().st_size)
        try:
            path.unlink()
        except OSError as exc:
            raise BackupError("the backup could not be deleted: %s" % exc, 500)
        self.store.audit(actor, "backup.deleted", path.name)
        return {"deleted": path.name, "size": size, "remaining": len(self.list())}

    def prune(self, keep: Optional[int] = None, actor: str = "system") -> Dict[str, Any]:
        """Keep the newest ``keep`` archives and remove the rest."""
        limit = self.keep() if keep is None else int(keep)
        limit = max(1, min(MAX_KEEP, limit))
        backups = self.list()
        if len(backups) <= limit:
            return {"deleted": [], "kept": len(backups), "keep": limit}
        removed: List[str] = []
        for row in backups[limit:]:
            try:
                Path(row["path"]).unlink()
                removed.append(row["name"])
            except OSError:
                continue
        if removed:
            self.store.audit(actor, "backup.pruned",
                             "%d file(s), keeping %d" % (len(removed), limit))
        return {"deleted": removed, "kept": len(backups) - len(removed), "keep": limit}

    # -- configuration ----------------------------------------------------
    def setting(self, key: str) -> Any:
        try:
            return self.store.get_setting(key)
        except Exception:  # a settings problem must never break a backup
            return None

    def keep(self) -> int:
        raw = self.setting(SETTING_KEEP)
        try:
            value = int(raw) if raw not in (None, "") else DEFAULT_KEEP
        except (TypeError, ValueError):
            value = DEFAULT_KEEP
        return max(1, min(MAX_KEEP, value))

    def job(self) -> Optional[Dict[str, Any]]:
        """The one automatic-backup job, or None.

        Jobs get server-generated ids, so this is found by its action rather
        than by a fixed id - which also means an operator can edit it in the
        scheduler pane and the database pane still recognises it.
        """
        try:
            jobs = self.store.list_jobs()
        except Exception:
            return None
        for row in jobs:
            payload = row.get("payload") or {}
            if str(payload.get("action") or "") == JOB_TARGET:
                return row
        return None

    def config(self) -> Dict[str, Any]:
        job = self.job() or {}
        last = self.setting(SETTING_LAST)
        return {
            "enabled": bool(job.get("enabled")),
            "schedule": str(job.get("schedule") or ""),
            "scheduleKind": str(job.get("kind") or ""),
            "nextRunAt": job.get("next_run_at"),
            "lastJobStatus": str(job.get("last_status") or ""),
            "lastJobError": str(job.get("last_error") or ""),
            "keep": self.keep(),
            "telegramTarget": str(self.setting(SETTING_TARGET) or ""),
            "telegramChannel": str(self.setting(SETTING_CHANNEL) or ""),
            "includeSecrets": bool(self.setting(SETTING_SECRETS)),
            "label": str(self.setting(SETTING_LABEL) or ""),
            "last": last if isinstance(last, dict) else None,
            "jobId": str(job.get("id") or ""),
            "notifyAfterBackup": bool(self.setting(SETTING_NOTIFY)),
        }

    def configure(self, values: Dict[str, Any], actor: str = "operator") -> Dict[str, Any]:
        """Persist backup settings; ``enabled``/``schedule`` drive the job."""
        if not isinstance(values, dict):
            raise BackupError("settings must be an object", 400)
        if "keep" in values:
            try:
                keep = int(values["keep"])
            except (TypeError, ValueError):
                raise BackupError("«keep» must be a number", 400)
            if keep < 1 or keep > MAX_KEEP:
                raise BackupError("«keep» must be between 1 and %d" % MAX_KEEP, 400)
            self.store.set_setting(SETTING_KEEP, keep)
        for key, name in (("telegramTarget", SETTING_TARGET),
                          ("telegramChannel", SETTING_CHANNEL),
                          ("label", SETTING_LABEL)):
            if key in values:
                text = str(values[key] or "").strip()
                if len(text) > 200:
                    raise BackupError("«%s» is too long" % key, 400)
                self.store.set_setting(name, text)
        for key, name in (("includeSecrets", SETTING_SECRETS), ("notify", SETTING_NOTIFY)):
            if key in values:
                self.store.set_setting(name, bool(values[key] in (True, "1", 1, "true")))
        if "enabled" in values or "schedule" in values:
            self._apply_schedule(values, actor)
        self.store.audit(actor, "backup.config",
                         ", ".join(sorted(str(k) for k in values)))
        return self.config()

    def _apply_schedule(self, values: Dict[str, Any], actor: str) -> None:
        """Create, update or remove the single automatic-backup job."""
        existing = self.job() or {}
        enabled = values.get("enabled")
        enabled = bool(existing.get("enabled")) if enabled is None else bool(enabled)
        kind = str(values.get("scheduleKind") or existing.get("kind") or "cron").strip()
        if kind not in JOB_KINDS:
            raise BackupError("scheduleKind must be one of: %s" % ", ".join(JOB_KINDS),
                              400)
        schedule = str(values.get("schedule")
                       or existing.get("schedule") or "").strip()
        if not enabled:
            if existing and self.hub is not None:
                self.hub.delete_job(str(existing["id"]), actor=actor)
            return
        if self.hub is None:
            raise BackupError("the scheduler is not attached, so automatic backups "
                              "cannot be turned on", 501)
        if not schedule:
            raise BackupError("a schedule is required (for example «0 3 * * *» "
                              "for every night at 03:00)", 400)
        data = {
            "name": "پشتیبان‌گیری خودکار از دیتابیس",
            "kind": kind,
            "schedule": schedule,
            "target": JOB_TARGET,
            "enabled": True,
            "timeout": 300,
            "payload": {"action": JOB_TARGET},
        }
        try:
            self.hub.save_job(data, job_id=str(existing["id"]) if existing else None,
                              actor=actor)
        except Exception as exc:  # the scheduler owns the real validation
            raise BackupError("the automatic schedule was rejected: %s" % exc, 400)

    # -- shipping ---------------------------------------------------------
    def send(self, name: str = "", target: str = "", channel: str = "",
             actor: str = "operator") -> Dict[str, Any]:
        """Upload one backup to Telegram (newest first if no name is given)."""
        if self.notifier is None:
            raise BackupError("no Telegram transport is attached", 501)
        backups = self.list()
        if not backups:
            raise BackupError("there is no backup to send yet - take one first", 409)
        row = None
        if name:
            for candidate in backups:
                if candidate["name"] == name:
                    row = candidate
                    break
            if row is None:
                raise BackupError("no backup named %s" % name, 404)
        else:
            row = backups[0]
        destination = str(target or self.setting(SETTING_TARGET) or "").strip()
        if not destination:
            raise BackupError("pick a Telegram destination first (a chat id, "
                              "@channelname or a saved target)", 400)
        used_channel = str(channel or self.setting(SETTING_CHANNEL) or "").strip()
        if used_channel and used_channel not in ("bot", "account"):
            raise BackupError("channel must be bot or account", 400)
        wanted: Any = destination
        try:
            wanted = int(destination)
        except (TypeError, ValueError):
            pass
        saved = []
        try:
            saved = self.notifier.targets()
        except Exception:
            saved = []
        chosen = [item for item in saved if item.get("id") == wanted]
        if not chosen:
            # A raw @username or numeric id typed in the pane is accepted too,
            # because making people save a target first is one click too many.
            chosen = [{"id": wanted, "title": str(destination)}]
        caption = "🗄 پشتیبان دیتابیس: %s\n📦 %s\n🕒 %s" % (
            row["name"], row["sizeText"], row["createdAtText"])
        try:
            result = self.notifier.send_document(row["path"], caption=caption,
                                                 targets=chosen,
                                                 channel=used_channel or None,
                                                 purpose="backup")
        except BackupError:
            raise
        except Exception as exc:
            raise BackupError("Telegram refused the file: %s" % exc, 502)
        self.store.audit(actor, "backup.sent",
                         "%s -> %s" % (row["name"], destination))
        result = dict(result or {})
        result["backup"] = {"name": row["name"], "sizeText": row["sizeText"]}
        return result

    def newest_name(self) -> str:
        backups = self.list()
        return backups[0]["name"] if backups else ""
