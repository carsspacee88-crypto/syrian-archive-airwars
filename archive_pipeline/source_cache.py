from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .io_utils import load_json, utc_now


class SourceCacheStore:
    """Small durable URL cache without rewriting one ever-growing JSON file.

    V2 rewrote the complete cache at every checkpoint.  SQLite WAL turns each
    checkpoint into one transaction whose cost is proportional to changed URLs.
    The legacy JSON cache is imported once and kept untouched for rollback.
    """

    def __init__(self, path: Path, legacy_json_path: Path | None = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._lock = threading.RLock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_cache (
                cache_key TEXT PRIMARY KEY,
                entry_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.pending: dict[str, dict[str, Any]] = {}
        self._closed = False
        self._migrate_legacy_json_once(legacy_json_path)
        self._set_meta("schema_version", "4.0.0")
        self.connection.commit()

    def _meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM cache_meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO cache_meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def _migrate_legacy_json_once(self, legacy_json_path: Path | None) -> None:
        if self._meta("legacy_json_migrated_at"):
            return
        legacy = load_json(legacy_json_path, {}) if legacy_json_path else {}
        urls = (legacy or {}).get("urls") or {}
        migrated_at = utc_now()
        rows = [
            (
                str(key),
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                migrated_at,
            )
            for key, value in urls.items()
            if key and isinstance(value, dict)
        ]
        if rows:
            self.connection.executemany(
                """
                INSERT INTO source_cache(cache_key, entry_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    entry_json = excluded.entry_json,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
        self._set_meta("schema_version", "4.0.0")
        self._set_meta("legacy_json_migrated_at", migrated_at)
        self._set_meta("legacy_json_rows", str(len(rows)))
        self.connection.commit()

    def get(self, key: str) -> dict[str, Any]:
        if not key:
            return {}
        with self._lock:
            if key in self.pending:
                return dict(self.pending[key])
            row = self.connection.execute(
                "SELECT entry_json FROM source_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if not row:
            return {}
        try:
            value = json.loads(str(row[0]))
        except json.JSONDecodeError:
            return {}
        return dict(value) if isinstance(value, dict) else {}

    def set(self, key: str, entry: dict[str, Any]) -> None:
        if key:
            with self._lock:
                self.pending[key] = dict(entry)

    def flush(self) -> int:
        with self._lock:
            if not self.pending:
                return 0
            updated_at = utc_now()
            rows = [
                (
                    key,
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                    updated_at,
                )
                for key, value in self.pending.items()
            ]
            with self.connection:
                self.connection.executemany(
                    """
                    INSERT INTO source_cache(cache_key, entry_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        entry_json = excluded.entry_json,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )
                self._set_meta("last_flush_at", updated_at)
            self.pending.clear()
            return len(rows)

    def count(self) -> int:
        with self._lock:
            persisted = int(
                self.connection.execute("SELECT COUNT(*) FROM source_cache").fetchone()[0]
            )
            pending_new = sum(
                not self.connection.execute(
                    "SELECT 1 FROM source_cache WHERE cache_key = ?", (key,)
                ).fetchone()
                for key in self.pending
            )
            return persisted + pending_new

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.flush()
            self.connection.close()
            self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
