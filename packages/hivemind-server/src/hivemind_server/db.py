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


LEGACY_USER = "legacy:unknown"


class Tx:
    """Handle to an open write transaction: the provenance tx_id + a live cursor.

    `user`/`device` are the resolved caller, carried here so a write path that stamps an
    author_user column does not have to read the contextvar a second time (and cannot disagree
    with the tx row about who wrote it).
    """

    __slots__ = ("tx_id", "cur", "time", "user", "device")

    def __init__(self, tx_id: int, cur: sqlite3.Cursor, tstamp: str,
                 user: str = LEGACY_USER, device: str = ""):
        self.tx_id = tx_id
        self.cur = cur
        self.time = tstamp
        self.user = user
        self.device = device


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

    # Columns added to tables that may already exist on a deployed database. schema.sql uses
    # CREATE TABLE IF NOT EXISTS, which silently does nothing for an existing table, so additive
    # columns must be applied explicitly. Additive only — never drop or retype here.
    # Every authorship column below is NULLABLE with no default and no backfill, on purpose: the
    # live graph is ~124k nodes / ~381k node_version rows / ~1.5M tx rows in a 7.8 GB file, where a
    # NOT NULL column or an UPDATE at startup rewrites every row. NULL therefore means "written
    # before authorship existed", which reads out as legacy:unknown. Backfilling is a separate,
    # explicit admin command with a dry run, not a side effect of a deploy.
    _MIGRATIONS = (
        ("skill_link", "source", "TEXT NOT NULL DEFAULT 'auto'"),
        ("skill_link", "score", "REAL"),
        ("tool_link", "source", "TEXT NOT NULL DEFAULT 'auto'"),
        ("tool_link", "score", "REAL"),
        ("tx", "user_id", "TEXT"),
        ("tx", "device", "TEXT"),
        ("node_version", "author_user", "TEXT"),
        ("edge_version", "author_user", "TEXT"),
        ("node", "created_by", "TEXT"),
        ("skill_version", "author_user", "TEXT"),
        ("trap", "author_user", "TEXT"),
        ("tool_version", "author_user", "TEXT"),
        ("guide_proposal", "author_user", "TEXT"),
        ("graph_task", "status_mode", "TEXT NOT NULL DEFAULT 'sidecar'"),
        ("chat_message", "task_node_id", "TEXT REFERENCES node(node_id)"),
        ("chat_cursor", "message_id", "TEXT"),
    )

    # Tables from a removed feature. Dropped on startup so a database that predates the removal
    # converges on the same shape as a fresh one. The v1 polling bus was replaced by WebSocket
    # push (docs/bus.md); its data was ephemeral by design, so there is nothing to migrate.
    _DROPPED = ("bus_ref", "bus_request", "bus_message", "bus_membership", "bus_capability",
                "bus_session")

    def apply_schema(self) -> None:
        con = self.conn()
        with self._write_lock:
            # Migrations run BEFORE the schema script, not after. schema.sql may contain an index
            # over a migrated column, and on an already-created table CREATE TABLE IF NOT EXISTS
            # is a silent no-op while CREATE INDEX is not — it fails with "no such column". This
            # order is safe both ways: on a fresh database every PRAGMA below returns empty, so
            # every migration is skipped and the script creates the columns itself.
            for table in self._DROPPED:
                con.execute(f"DROP TABLE IF EXISTS {table}")
            for table, column, decl in self._MIGRATIONS:
                try:
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                except sqlite3.OperationalError:
                    continue                      # table not created yet on this database
                if cols and column not in cols:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            con.executescript(_SCHEMA_PATH.read_text())
            # Before status_mode existed, marked work_item nodes already versioned their
            # statuses. The new column defaults to sidecar so generic markers remain safe;
            # classify the old rows once, atomically, before any claim can be used. A meta
            # marker makes this crash-retryable if the process exits between schema DDL and
            # migration: DDL is autocommitted but these row changes and their marker are one tx.
            marker = "graph_task_status_mode_migrated_v1"
            if con.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone() is None:
                from . import schemas as _schemas
                con.execute("BEGIN IMMEDIATE")
                try:
                    # Re-checked INSIDE the transaction; the test above is only a fast path. Two
                    # processes opening the same project DB (a restart overlapping the outgoing
                    # server, or a second Database() elsewhere) both saw no marker. The loser of
                    # BEGIN IMMEDIATE then repeated the scan and its INSERT hit the primary key,
                    # and because apply_schema runs from Database.__init__ that exception aborted
                    # STARTUP rather than one query. ON CONFLICT covers the same race between the
                    # re-check and the insert.
                    if con.execute("SELECT 1 FROM meta WHERE key=?",
                                   (marker,)).fetchone() is not None:
                        con.execute("COMMIT")
                        return
                    rows = con.execute(
                        "SELECT t.node_id,t.status,n.node_type,v.props FROM graph_task t "
                        "JOIN node n ON n.node_id=t.node_id "
                        "JOIN node_version v ON v.node_id=t.node_id AND v.tx_to=? "
                        "WHERE t.status_mode='sidecar'", (SENTINEL,)).fetchall()
                    cur = con.cursor()
                    for row in rows:
                        props = json.loads(row["props"])
                        if props.get("status") != row["status"]:
                            continue
                        try:
                            for status in ("unclaimed", "in_progress", "complete"):
                                _schemas.validate_props(cur, "node", row["node_type"],
                                                        {**props, "status": status})
                        except Invalid:
                            continue
                        cur.execute("UPDATE graph_task SET status_mode='versioned' WHERE node_id=?",
                                    (row["node_id"],))
                    cur.close()
                    con.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                                "ON CONFLICT(key) DO NOTHING", (marker, "1"))
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise

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
        """Open a write transaction. `agent_id` is a free-form LABEL; the author is the token.

        The identity is read from the contextvar rather than taken as an argument so that no call
        site can be told the wrong one — and so the 28 existing `db.write(...)` calls across the
        engine did not have to be edited (and one of them forgotten). identity is imported here,
        not at module scope, because identity.py imports Invalid from this module.
        """
        from .identity import current_identity
        who = current_identity()
        # An unresolvable identity is recorded, not rejected: a write refused for want of one would
        # take the live fleet down mid-migration, whose bootstrap credential is a legacy project
        # token, and would also break every startup/CLI path that has no request context at all.
        user = who.user if who is not None else LEGACY_USER
        device = (who.device if who is not None else "") or ""
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
                    "INSERT INTO tx(tx_time, agent_id, reason, meta, user_id, device) "
                    "VALUES(?,?,?,?,?,?)",
                    (tstamp, agent_id, reason, canonical_json(meta or {}), user, device),
                )
                tx = Tx(cur.lastrowid, cur, tstamp, user, device)
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

    # ── writes without provenance (the bus) ──────────────────────────────────────
    @contextmanager
    def write_light(self) -> Iterator[sqlite3.Cursor]:
        """Same locking/retry discipline as write(), but no `tx` row.

        For work where a provenance row would be wrong or useless. Its original caller — the v1
        polling bus, whose rows were TTL-reaped — no longer exists (the WebSocket bus stores
        nothing at all); what uses it today is `admin.backfill_authors`, which rewrites the author
        column on rows that predate it and must not stamp a tx of its own on each 5,000-row batch.
        Anything that belongs to the revision axis must use write() instead — provenance is not
        optional there.
        """
        con = self.conn()
        attempts = 0
        while True:
            attempts += 1
            self._write_lock.acquire()
            began = False
            try:
                con.execute("BEGIN IMMEDIATE")
                began = True
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if ("busy" in msg or "locked" in msg) and attempts <= 6:
                    self._write_lock.release()
                    time.sleep(min(0.05 * 2 ** attempts, 1.0) * (0.5 + random.random()))
                    continue
                self._write_lock.release()
                raise
            cur = con.cursor()
            try:
                yield cur
                con.execute("COMMIT")
                return
            except Exception:
                if began:
                    try:
                        con.execute("ROLLBACK")
                    except sqlite3.OperationalError:
                        pass
                raise
            finally:
                cur.close()
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
