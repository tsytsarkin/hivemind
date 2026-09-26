"""Restart-safe opaque browser sessions: only credential fingerprints, never tokens."""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from .identity import Identity, IdentityStore

IDLE_TTL = 8 * 3600
ABSOLUTE_TTL = 24 * 3600
MAX_SESSIONS = 10_000


class UISessions:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # sqlite3.connect creates 0644 under a permissive umask. Explicitly create the
        # secret-bearing file at mode 0600; preserve existing operator-owned permissions.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(sqlite3.connect(path, timeout=10)) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS ui_session (digest TEXT PRIMARY KEY, "
                        "user TEXT NOT NULL,device TEXT NOT NULL,credential TEXT NOT NULL, "
                        "csrf TEXT NOT NULL,created REAL NOT NULL,last REAL NOT NULL)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_ui_session_last ON ui_session(last)")

    def create(self, who: Identity) -> tuple[str, str]:
        secret = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.time()
        with closing(sqlite3.connect(self.path, timeout=10)) as con, con:
            con.execute("DELETE FROM ui_session WHERE created<? OR last<?",
                        (now - ABSOLUTE_TTL, now - IDLE_TTL))
            count = con.execute("SELECT COUNT(*) FROM ui_session").fetchone()[0]
            if count >= MAX_SESSIONS:
                con.execute("DELETE FROM ui_session WHERE digest IN (SELECT digest FROM "
                            "ui_session ORDER BY last ASC LIMIT 1)")
            con.execute("INSERT INTO ui_session VALUES(?,?,?,?,?,?,?)",
                        (hashlib.sha256(secret.encode()).hexdigest(), who.user, who.device,
                         who.credential_hash, csrf, now, now))
        return secret, csrf

    def lookup(self, secret: str | None, identities: IdentityStore):
        if not secret or len(secret) > 256:
            return None
        digest = hashlib.sha256(secret.encode()).hexdigest()
        now = time.time()
        with closing(sqlite3.connect(self.path, timeout=10)) as con, con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM ui_session WHERE digest=?", (digest,)).fetchone()
            if row is None:
                return None
            if (now - row["created"] > ABSOLUTE_TTL or now - row["last"] > IDLE_TTL or
                    not identities.active_credential(row["user"], row["device"],
                                                     row["credential"])):
                con.execute("DELETE FROM ui_session WHERE digest=?", (digest,))
                return None
            con.execute("UPDATE ui_session SET last=? WHERE digest=?", (now, digest))
            return Identity(row["user"], row["device"],
                            credential_hash=row["credential"]), row["csrf"]

    def revoke(self, secret: str | None) -> None:
        if secret and len(secret) <= 256:
            with closing(sqlite3.connect(self.path, timeout=10)) as con, con:
                con.execute("DELETE FROM ui_session WHERE digest=?",
                            (hashlib.sha256(secret.encode()).hexdigest(),))
