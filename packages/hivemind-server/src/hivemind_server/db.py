"""SQLite access layer for Hivemind.

Concurrency model (see plan): WAL + synchronous=NORMAL + busy_timeout; every write runs inside
`BEGIN IMMEDIATE` and inserts a provenance `tx` row. Writers are serialized in-process by a lock
(SQLite is single-writer anyway) which avoids most SQLITE_BUSY churn; we still retry on
SQLITE_BUSY / SQLITE_BUSY_SNAPSHOT with backoff+jitter for any out-of-process contention.
Each thread gets its own connection (uvicorn/mcp run sync tool fns on worker threads).
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SENTINEL = 9223372036854775807  # tx_to for the current (open) version

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON for content hashing (sorted keys, tight separators)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Conflict(Exception):
    """Optimistic-concurrency failure (expected head != actual). Maps to HTTP 409."""


class NotFound(Exception):
    pass


class Invalid(Exception):
    """Bad request: validation, unknown type, illegal edge, etc. Maps to HTTP 400/422."""


class Tx:
    """Handle to an open write transaction: the provenance tx_id + a live cursor."""

    __slots__ = ("tx_id", "cur", "time")

    def __init__(self, tx_id: int, cur: sqlite3.Cursor, tstamp: str):
        self.tx_id = tx_id
        self.cur = cur
        self.time = tstamp


class Database:
    def __init__(self, path: str | os.PathLike, *, apply_schema: bool = True):
        self.path = str(path)
        self._write_lock = threading.Lock()
        self._local = threading.local()
        if apply_schema:
            self.apply_schema()

    # ── connections ────────────────────────────────────────────────────────────
    def _new_conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0, isolation_level=None,
                              check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA wal_autocheckpoint=1000")
        return con

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._new_conn()
            self._local.conn = c
        return c

    def apply_schema(self) -> None:
        con = self.conn()
        with self._write_lock:
            con.executescript(_SCHEMA_PATH.read_text())

    # ── reads (autocommit; WAL lets readers run concurrently with the writer) ────
    @contextmanager
    def read(self) -> Iterator[sqlite3.Cursor]:
        cur = self.conn().cursor()
        try:
            yield cur
        finally:
            cur.close()

    # ── writes (serialized in-process; BEGIN IMMEDIATE; provenance tx row) ───────
    @contextmanager
    def write(self, agent_id: str, reason: Optional[str] = None,
              meta: Optional[dict] = None) -> Iterator[Tx]:
        con = self.conn()
        attempts = 0
        while True:
            attempts += 1
            self._write_lock.acquire()
            try:
                con.execute("BEGIN IMMEDIATE")
                cur = con.cursor()
                tstamp = now_iso()
                cur.execute(
                    "INSERT INTO tx(tx_time, agent_id, reason, meta) VALUES(?,?,?,?)",
                    (tstamp, agent_id, reason, canonical_json(meta or {})),
                )
                tx = Tx(cur.lastrowid, cur, tstamp)
                yield tx
                con.execute("COMMIT")
                return
            except sqlite3.OperationalError as e:
                con.execute("ROLLBACK")
                msg = str(e).lower()
                if ("busy" in msg or "locked" in msg) and attempts <= 6:
                    time.sleep(min(0.05 * 2 ** attempts, 1.0) * (0.5 + random.random()))
                    continue
                raise
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            finally:
                self._write_lock.release()

    # ── small helpers ────────────────────────────────────────────────────────────
    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.read() as cur:
            row = cur.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def meta_set_in_tx(self, cur: sqlite3.Cursor, key: str, value: str) -> None:
        cur.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
