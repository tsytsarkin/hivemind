"""Content-addressed blob store: SHA-256 files on disk, referenced by DB rows.

Layout: <blobs>/sha256/ab/cd/<64hex>  (mode 0444). Atomic write = stream→tmp (same mount),
verify digest, dedup, rename, fsync dir, chmod. Blob-on-disk-first, DB-row-second, so the only
failure mode is unreferenced garbage (recoverable), never a dangling reference.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import BinaryIO, Iterable, Optional

from .db import Database, Invalid, NotFound


class BlobStore:
    def __init__(self, root: Path, db: Database, *, max_bytes: int, grace_seconds: int):
        self.root = Path(root)
        self.db = db
        self.max_bytes = max_bytes
        self.grace_seconds = grace_seconds
        self.tmp = self.root / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)

    # ── addressing ────────────────────────────────────────────────────────────────
    @staticmethod
    def parse_digest(digest: str) -> tuple[str, str]:
        if ":" not in digest:
            raise Invalid("digest must be '<algo>:<hex>'")
        algo, hexd = digest.split(":", 1)
        if algo != "sha256" or len(hexd) != 64 or not all(c in "0123456789abcdef" for c in hexd):
            raise Invalid("only lowercase sha256:<64hex> digests are supported")
        return algo, hexd

    def path_for(self, digest: str) -> Path:
        _, hexd = self.parse_digest(digest)
        return self.root / "sha256" / hexd[:2] / hexd[2:4] / hexd

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    # ── write ─────────────────────────────────────────────────────────────────────
    def put_stream(self, chunks: Iterable[bytes], *, agent_id: str,
                   expected_digest: Optional[str] = None, media_type: Optional[str] = None,
                   declared_size: Optional[int] = None) -> dict:
        if declared_size is not None and declared_size > self.max_bytes:
            raise Invalid(f"declared size {declared_size} exceeds limit {self.max_bytes}")
        os.makedirs(self.tmp, exist_ok=True)
        fd, tmp_name = _mkstemp(self.tmp)
        h = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise Invalid(f"blob exceeds size limit {self.max_bytes}")
                    h.update(chunk)
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            digest = "sha256:" + h.hexdigest()
            if expected_digest is not None and expected_digest != digest:
                raise Invalid(f"digest mismatch: declared {expected_digest}, computed {digest}")
            final = self.path_for(digest)
            if final.exists():
                os.unlink(tmp_name)                       # dedup: identical bytes already stored
                self._ensure_row(digest, size, media_type, agent_id)
                return {"digest": digest, "size": size, "deduplicated": True}
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_name, final)                   # atomic on same mount
            _fsync_dir(final.parent)
            try:
                os.chmod(final, 0o444)
            except OSError:
                pass
            self._ensure_row(digest, size, media_type, agent_id)
            return {"digest": digest, "size": size, "deduplicated": False}
        except BaseException:
            with _suppress():
                os.unlink(tmp_name)
            raise

    def _ensure_row(self, digest: str, size: int, media_type: Optional[str], agent_id: str) -> None:
        with self.db.write(agent_id, "blob_put") as tx:
            tx.cur.execute(
                "INSERT INTO blob(digest,size,media_type,created_tx) VALUES(?,?,?,?) "
                "ON CONFLICT(digest) DO NOTHING", (digest, size, media_type, tx.tx_id))

    # ── streaming finalize (used by async REST handlers that hash while receiving) ──
    def new_tmp(self) -> str:
        fd, name = _mkstemp(self.tmp)
        os.close(fd)
        return name

    def finalize_written(self, tmp_path: str, digest: str, size: int,
                         media_type: Optional[str], agent_id: str, *,
                         expected_digest: Optional[str] = None) -> dict:
        """Move an already-written+hashed tmp file into its content-addressed slot."""
        try:
            if expected_digest is not None and expected_digest != digest:
                raise Invalid(f"digest mismatch: declared {expected_digest}, computed {digest}")
            if size > self.max_bytes:
                raise Invalid(f"blob exceeds size limit {self.max_bytes}")
            final = self.path_for(digest)
            if final.exists():
                with _suppress():
                    os.unlink(tmp_path)
                self._ensure_row(digest, size, media_type, agent_id)
                return {"digest": digest, "size": size, "deduplicated": True}
            final.parent.mkdir(parents=True, exist_ok=True)
            with _suppress():
                os.fsync(os.open(tmp_path, os.O_RDONLY))
            os.replace(tmp_path, final)
            _fsync_dir(final.parent)
            with _suppress():
                os.chmod(final, 0o444)
            self._ensure_row(digest, size, media_type, agent_id)
            return {"digest": digest, "size": size, "deduplicated": False}
        except BaseException:
            with _suppress():
                os.unlink(tmp_path)
            raise

    # ── read ──────────────────────────────────────────────────────────────────────
    def stat(self, digest: str) -> dict:
        with self.db.read() as cur:
            r = cur.execute("SELECT * FROM blob WHERE digest=?", (digest,)).fetchone()
        if r is None or not self.exists(digest):
            raise NotFound(f"blob {digest} not found")
        return dict(r)

    def open(self, digest: str) -> BinaryIO:
        p = self.path_for(digest)
        if not p.exists():
            raise NotFound(f"blob {digest} not found")
        return open(p, "rb")

    # ── references + attach ─────────────────────────────────────────────────────────
    def attach(self, agent_id: str, digest: str, from_version_id: str, *,
               role: str = "attachment", filename: Optional[str] = None) -> dict:
        self.stat(digest)                                 # ensure the blob exists
        with self.db.write(agent_id, "blob_attach") as tx:
            _require_version(tx.cur, from_version_id)
            tx.cur.execute(
                "INSERT INTO blob_ref(digest,from_version_id,role,filename) VALUES(?,?,?,?) "
                "ON CONFLICT(digest,from_version_id,role) DO UPDATE SET filename=excluded.filename",
                (digest, from_version_id, role, filename))
        return {"digest": digest, "from_version_id": from_version_id, "role": role}

    def refs(self, digest: str) -> list[dict]:
        with self.db.read() as cur:
            return [dict(r) for r in cur.execute(
                "SELECT from_version_id, role, filename FROM blob_ref WHERE digest=?", (digest,))]

    def pin(self, agent_id: str, digest: str, reason: str = "") -> dict:
        with self.db.write(agent_id, "blob_pin") as tx:
            tx.cur.execute("INSERT INTO blob_pin(digest,reason) VALUES(?,?) "
                           "ON CONFLICT(digest) DO UPDATE SET reason=excluded.reason",
                           (digest, reason))
        return {"digest": digest, "pinned": True}

    # ── garbage collection (mark-and-sweep with a grace window + pins) ──────────────
    def gc(self, *, dry_run: bool = True) -> dict:
        cutoff = time.time() - self.grace_seconds
        with self.db.read() as cur:
            rows = cur.execute(
                "SELECT b.digest, t.tx_time FROM blob b JOIN tx t ON t.tx_id=b.created_tx "
                "WHERE b.digest NOT IN (SELECT digest FROM blob_ref) "
                "AND b.digest NOT IN (SELECT digest FROM blob_pin)"
            ).fetchall()
        collected, kept_young = [], 0
        for r in rows:
            created = _iso_epoch(r["tx_time"])
            if created > cutoff:                          # inside the grace window → keep
                kept_young += 1
                continue
            collected.append(r["digest"])
        if not dry_run:
            for digest in collected:
                p = self.path_for(digest)
                if not self.refs(digest):                 # re-check under no lock
                    with _suppress():
                        if p.exists():
                            os.chmod(p, 0o644)
                            os.unlink(p)
                    with self.db.write("gc", "blob_gc") as tx:
                        tx.cur.execute("DELETE FROM blob WHERE digest=?", (digest,))
        return {"unreferenced": len(collected), "kept_within_grace": kept_young,
                "deleted": 0 if dry_run else len(collected), "dry_run": dry_run}


# ── small fs/util helpers ────────────────────────────────────────────────────────────
def _mkstemp(dirpath: Path) -> tuple[int, str]:
    import tempfile
    return tempfile.mkstemp(dir=str(dirpath), prefix="up-")


def _fsync_dir(dirpath: Path) -> None:
    with _suppress():
        dfd = os.open(str(dirpath), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _require_version(cur, version_id: str) -> None:
    r = cur.execute("SELECT 1 FROM node_version WHERE version_id=? "
                    "UNION ALL SELECT 1 FROM edge_version WHERE version_id=? LIMIT 1",
                    (version_id, version_id)).fetchone()
    if r is None:
        raise Invalid(f"from_version_id {version_id!r} is not a node/edge version")


def _iso_epoch(iso: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


class _suppress:
    def __enter__(self): return self
    def __exit__(self, *exc): return True
