"""Append-only JSON indexes for what a session produced.

Two kinds are kept: every screenshot the system took, by whatever route
(detect, a run step, a failure, the manual button), and every piece of text it
extracted from a page. Both are plain JSON files under ``<data>/archive`` so
they survive a run and can be listed without rescanning directories.

Writes go to a temporary file and are renamed into place, so a Colab runtime
that dies mid-write cannot leave a truncated index behind.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional


class Archive:
    """One JSON list, newest last on disk, newest first when listed."""

    def __init__(self, path: str, limit: int = 500):
        self.path = path
        self.limit = limit
        self._lock = threading.RLock()

    # -- reading --------------------------------------------------------
    def _read(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def list(self) -> List[Dict[str, Any]]:
        """Newest first, which is how a human reads a history."""
        with self._lock:
            rows = self._read()
        rows.sort(key=lambda row: row.get("createdAt") or 0, reverse=True)
        return rows

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for row in self._read():
                if row.get("id") == entry_id:
                    return row
        return None

    # -- writing --------------------------------------------------------
    def add(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(entry)
        row.setdefault("id", uuid.uuid4().hex[:12])
        row.setdefault("createdAt", time.time())
        with self._lock:
            rows = self._read()
            rows.append(row)
            if len(rows) > self.limit:
                rows = rows[-self.limit:]
            self._write(rows)
        return row

    def delete(self, entry_id: str) -> bool:
        with self._lock:
            rows = self._read()
            kept = [row for row in rows if row.get("id") != entry_id]
            if len(kept) == len(rows):
                return False
            self._write(kept)
            return True

    def clear(self) -> int:
        with self._lock:
            count = len(self._read())
            self._write([])
            return count

    def _write(self, rows: List[Dict[str, Any]]) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


def shot_archive(data_dir: str, limit: int = 500) -> Archive:
    return Archive(os.path.join(data_dir, "archive", "shots.json"), limit=limit)


def text_archive(data_dir: str, limit: int = 300) -> Archive:
    return Archive(os.path.join(data_dir, "archive", "texts.json"), limit=limit)
