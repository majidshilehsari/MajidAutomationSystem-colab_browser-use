"""Persistent store for the coworker agent: settings, jobs, scripts, audit log.

The rest of the automation keeps its data in small JSON files (flows, shots,
texts). That is the right shape for a handful of documents, but it gives the
agent nowhere to put its own state: scheduled jobs, scripts it wrote, what it
was told, and a record of what it did. SQLite is in the standard library, needs
no server process and no new dependency, and it survives a redeploy because it
lives on the same volume as everything else.

Secrets are deliberately NOT stored in the database. A database file is easy to
copy, easy to include in a backup and easy to open with any tool, so secret
values go to a separate 0600 file and only their *names* appear in settings.
Nothing here ever writes a secret value into the audit log.

Every method is safe to call from the API thread, the scheduler thread and the
script runner thread: one connection, guarded by a lock.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional


SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    is_secret  INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    schedule    TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    payload     TEXT NOT NULL DEFAULT '{}',
    enabled     INTEGER NOT NULL DEFAULT 1,
    timeout     REAL NOT NULL DEFAULT 600,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    last_run_at REAL,
    last_status TEXT,
    last_error  TEXT,
    next_run_at REAL
);

CREATE TABLE IF NOT EXISTS scripts (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'python3',
    code        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    timeout     REAL NOT NULL DEFAULT 60,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    approved_at REAL,
    last_run_at REAL,
    last_exit   INTEGER,
    last_output TEXT,
    run_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'note',
    body       TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     REAL NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit (at);
CREATE INDEX IF NOT EXISTS idx_jobs_next ON jobs (enabled, next_run_at);
CREATE INDEX IF NOT EXISTS idx_scripts_status ON scripts (status);
"""

# Job schedule kinds. "at" fires once at a unix timestamp, "every" fires on an
# interval in seconds, "cron" is the familiar five-field expression.
JOB_KINDS = ("at", "every", "cron")

# A script that has not been approved by a human cannot run. This is the whole
# point of the gate: the agent proposes, the operator disposes.
SCRIPT_PENDING = "pending"
SCRIPT_APPROVED = "approved"
SCRIPT_REJECTED = "rejected"

# Fields a caller may set on a job; anything else is ignored so a malformed
# request cannot smuggle a column name into the SQL.
_JOB_FIELDS = ("name", "kind", "schedule", "target", "payload", "enabled",
               "timeout", "next_run_at")
_SCRIPT_FIELDS = ("name", "language", "code", "timeout")


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:12]
    return "%s%s" % (prefix, raw) if prefix else raw


class AgentStore:
    """SQLite store for everything the coworker agent owns."""

    def __init__(self, data_dir: str, clock: Any = time.time) -> None:
        self.data_dir = data_dir
        self._clock = clock
        self._lock = threading.RLock()
        base = os.path.join(data_dir, "automation")
        os.makedirs(base, exist_ok=True)
        self.db_path = os.path.join(base, "agent.db")
        self.secrets_path = os.path.join(base, "agent-secrets.json")
        self._db = sqlite3.connect(self.db_path, check_same_thread=False,
                                   timeout=15.0)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)))
            self._db.commit()

    # -- plumbing -------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except sqlite3.Error:
                pass

    def _query(self, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(sql, tuple(args)).fetchall()
        return [dict(row) for row in rows]

    def _execute(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock:
            cursor = self._db.execute(sql, tuple(args))
            self._db.commit()
            return cursor.rowcount

    # -- audit ----------------------------------------------------------
    def audit(self, actor: str, action: str, detail: str = "") -> None:
        """Record that something happened. Never pass a secret in `detail`."""
        self._execute(
            "INSERT INTO audit (at, actor, action, detail) VALUES (?, ?, ?, ?)",
            (round(self._clock(), 3), actor, action, detail[:2000]))

    def list_audit(self, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        return self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # -- settings -------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value, is_secret FROM settings WHERE key = ?",
                           (key,))
        if not rows:
            return default
        if rows[0]["is_secret"]:
            # The value lives in the secret file, not in the database.
            return self.get_secret(key, default)
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return rows[0]["value"]

    def set_setting(self, key: str, value: Any, secret: bool = False,
                    actor: str = "operator") -> None:
        now = round(self._clock(), 3)
        if secret:
            self.set_secret(key, value if isinstance(value, str) else str(value))
            # Only the fact that a secret exists is recorded in the database.
            self._execute(
                "INSERT OR REPLACE INTO settings (key, value, is_secret, updated_at)"
                " VALUES (?, ?, 1, ?)", (key, "***", now))
            self.audit(actor, "setting.secret.stored", key)
            return
        self._execute(
            "INSERT OR REPLACE INTO settings (key, value, is_secret, updated_at)"
            " VALUES (?, ?, 0, ?)",
            (key, json.dumps(value, ensure_ascii=False), now))
        self.audit(actor, "setting.stored", key)

    def delete_setting(self, key: str, actor: str = "operator") -> None:
        self._execute("DELETE FROM settings WHERE key = ?", (key,))
        secrets = self._read_secrets()
        if key in secrets:
            del secrets[key]
            self._write_secrets(secrets)
        self.audit(actor, "setting.deleted", key)

    def list_settings(self) -> Dict[str, Any]:
        """All settings, with secret values masked as a boolean."""
        out: Dict[str, Any] = {}
        for row in self._query("SELECT key, value, is_secret FROM settings"):
            if row["is_secret"]:
                out[row["key"]] = {"secret": True,
                                   "set": bool(self.get_secret(row["key"], ""))}
                continue
            try:
                out[row["key"]] = json.loads(row["value"])
            except (TypeError, ValueError):
                out[row["key"]] = row["value"]
        return out

    # -- secrets (0600 file, never in the database) ---------------------
    def _read_secrets(self) -> Dict[str, str]:
        try:
            with open(self.secrets_path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_secrets(self, secrets: Dict[str, str]) -> None:
        os.makedirs(os.path.dirname(self.secrets_path), exist_ok=True)
        tmp = "%s.tmp" % self.secrets_path
        # Written through a 0600 temporary file so a half-written secret file
        # can never be readable, and so a crash cannot leave it truncated.
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(secrets, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.secrets_path)

    def get_secret(self, key: str, default: str = "") -> str:
        with self._lock:
            value = self._read_secrets().get(key)
        return default if value is None else value

    def set_secret(self, key: str, value: str) -> None:
        with self._lock:
            secrets = self._read_secrets()
            secrets[key] = value
            self._write_secrets(secrets)

    # -- jobs -----------------------------------------------------------
    def list_jobs(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM jobs"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at"
        return [self._decode_job(row) for row in self._query(sql)]

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._decode_job(rows[0]) if rows else None

    def _decode_job(self, row: Dict[str, Any]) -> Dict[str, Any]:
        job = dict(row)
        try:
            job["payload"] = json.loads(job.get("payload") or "{}")
        except (TypeError, ValueError):
            job["payload"] = {}
        job["enabled"] = bool(job.get("enabled"))
        return job

    def upsert_job(self, data: Dict[str, Any], job_id: Optional[str] = None,
                   actor: str = "operator") -> Dict[str, Any]:
        kind = str(data.get("kind") or "every")
        if kind not in JOB_KINDS:
            raise ValueError("job kind must be one of: %s" % ", ".join(JOB_KINDS))
        now = round(self._clock(), 3)
        payload = data.get("payload")
        payload_json = json.dumps(payload if isinstance(payload, dict) else {},
                                  ensure_ascii=False)
        if job_id:
            existing = self.get_job(job_id)
            if existing is None:
                raise KeyError("no job with id %s" % job_id)
            merged = dict(existing)
            for field in _JOB_FIELDS:
                if field in data:
                    merged[field] = data[field]
            self._execute(
                "UPDATE jobs SET name=?, kind=?, schedule=?, target=?, payload=?,"
                " enabled=?, timeout=?, next_run_at=?, updated_at=? WHERE id=?",
                (str(merged.get("name") or "job"), kind,
                 str(merged.get("schedule") or ""), str(merged.get("target") or ""),
                 payload_json, 1 if merged.get("enabled") else 0,
                 float(merged.get("timeout") or 600), merged.get("next_run_at"),
                 now, job_id))
            self.audit(actor, "job.updated", "%s (%s)" % (job_id, kind))
            return self.get_job(job_id) or {}
        job_id = new_id("job-")
        self._execute(
            "INSERT INTO jobs (id, name, kind, schedule, target, payload, enabled,"
            " timeout, created_at, updated_at, next_run_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, str(data.get("name") or "job"), kind,
             str(data.get("schedule") or ""), str(data.get("target") or ""),
             payload_json, 1 if data.get("enabled", True) else 0,
             float(data.get("timeout") or 600), now, now, data.get("next_run_at")))
        self.audit(actor, "job.created", "%s (%s)" % (job_id, kind))
        return self.get_job(job_id) or {}

    def delete_job(self, job_id: str, actor: str = "operator") -> bool:
        removed = self._execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        self.audit(actor, "job.deleted", job_id)
        return bool(removed)

    def record_job_run(self, job_id: str, status: str, error: str = "",
                       next_run_at: Optional[float] = None) -> None:
        self._execute(
            "UPDATE jobs SET last_run_at=?, last_status=?, last_error=?,"
            " next_run_at=? WHERE id=?",
            (round(self._clock(), 3), status, error[:2000], next_run_at, job_id))

    def set_job_next_run(self, job_id: str, next_run_at: Optional[float]) -> None:
        self._execute("UPDATE jobs SET next_run_at=? WHERE id=?",
                      (next_run_at, job_id))

    # -- scripts --------------------------------------------------------
    def list_scripts(self) -> List[Dict[str, Any]]:
        return self._query("SELECT * FROM scripts ORDER BY created_at")

    def get_script(self, script_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM scripts WHERE id = ?", (script_id,))
        return rows[0] if rows else None

    def create_script(self, data: Dict[str, Any], actor: str = "agent",
                      status: str = SCRIPT_PENDING) -> Dict[str, Any]:
        script_id = new_id("scr-")
        now = round(self._clock(), 3)
        self._execute(
            "INSERT INTO scripts (id, name, language, code, status, timeout,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (script_id, str(data.get("name") or "script"),
             str(data.get("language") or "python3"), str(data.get("code") or ""),
             status, float(data.get("timeout") or 60), now, now))
        self.audit(actor, "script.created",
                   "%s (%d bytes, status=%s)" % (script_id, len(data.get("code") or ""),
                                                 status))
        return self.get_script(script_id) or {}

    def update_script(self, script_id: str, data: Dict[str, Any],
                      actor: str = "operator") -> Optional[Dict[str, Any]]:
        existing = self.get_script(script_id)
        if existing is None:
            return None
        merged = dict(existing)
        for field in _SCRIPT_FIELDS:
            if field in data:
                merged[field] = data[field]
        # Editing code invalidates the approval: an approved script that changes
        # is a different script, and silently keeping the approval would let the
        # agent swap the body of something a human already signed off on.
        status = str(merged.get("status") or SCRIPT_PENDING)
        if "code" in data and status == SCRIPT_APPROVED:
            status = SCRIPT_PENDING
            merged["approved_at"] = None
        self._execute(
            "UPDATE scripts SET name=?, language=?, code=?, timeout=?, status=?,"
            " approved_at=?, updated_at=? WHERE id=?",
            (str(merged.get("name") or "script"), str(merged.get("language") or "python3"),
             str(merged.get("code") or ""), float(merged.get("timeout") or 60),
             status, merged.get("approved_at"), round(self._clock(), 3), script_id))
        self.audit(actor, "script.updated", script_id)
        return self.get_script(script_id)

    def set_script_status(self, script_id: str, status: str,
                          actor: str = "operator") -> Optional[Dict[str, Any]]:
        if status not in (SCRIPT_PENDING, SCRIPT_APPROVED, SCRIPT_REJECTED):
            raise ValueError("unknown script status: %s" % status)
        existing = self.get_script(script_id)
        if existing is None:
            return None
        approved_at = round(self._clock(), 3) if status == SCRIPT_APPROVED else None
        self._execute("UPDATE scripts SET status=?, approved_at=?, updated_at=?"
                      " WHERE id=?",
                      (status, approved_at, round(self._clock(), 3), script_id))
        self.audit(actor, "script.%s" % status, script_id)
        return self.get_script(script_id)

    def record_script_run(self, script_id: str, exit_code: int, output: str) -> None:
        self._execute(
            "UPDATE scripts SET last_run_at=?, last_exit=?, last_output=?,"
            " run_count = run_count + 1, updated_at=? WHERE id=?",
            (round(self._clock(), 3), int(exit_code), output[-20000:],
             round(self._clock(), 3), script_id))
        self.audit("runner", "script.executed", "%s exit=%d" % (script_id, exit_code))

    def delete_script(self, script_id: str, actor: str = "operator") -> bool:
        removed = self._execute("DELETE FROM scripts WHERE id = ?", (script_id,))
        self.audit(actor, "script.deleted", script_id)
        return bool(removed)

    # -- notes (the agent's own memory) ---------------------------------
    def add_note(self, body: str, kind: str = "note",
                 actor: str = "agent") -> Dict[str, Any]:
        note_id = new_id("note-")
        now = round(self._clock(), 3)
        self._execute(
            "INSERT INTO notes (id, kind, body, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)", (note_id, kind, body[:20000], now, now))
        self.audit(actor, "note.added", "%s (%s)" % (note_id, kind))
        return {"id": note_id, "kind": kind, "body": body[:20000],
                "created_at": now, "updated_at": now}

    def list_notes(self, kind: Optional[str] = None,
                   limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        if kind:
            return self._query("SELECT * FROM notes WHERE kind = ?"
                               " ORDER BY created_at DESC LIMIT ?", (kind, limit))
        return self._query("SELECT * FROM notes ORDER BY created_at DESC LIMIT ?",
                           (limit,))

    def delete_note(self, note_id: str, actor: str = "operator") -> bool:
        removed = self._execute("DELETE FROM notes WHERE id = ?", (note_id,))
        self.audit(actor, "note.deleted", note_id)
        return bool(removed)

    # -- kill switch ----------------------------------------------------
    def kill_switch_engaged(self) -> bool:
        """True when the operator has stopped the agent from acting at all."""
        return bool(self.get_setting("killSwitch", False))

    def set_kill_switch(self, engaged: bool, actor: str = "operator") -> None:
        self.set_setting("killSwitch", bool(engaged), actor=actor)
        self.audit(actor, "killSwitch.%s" % ("on" if engaged else "off"), "")
