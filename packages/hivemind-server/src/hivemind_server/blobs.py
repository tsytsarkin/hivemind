"""Content-addressed blob store: SHA-256 files on disk, referenced by DB rows.

Layout: <blobs>/sha256/ab/cd/<64hex>  (mode 0444). Atomic write = stream→tmp (same mount),
verify digest, dedup, rename, fsync dir, chmod. Blob-on-disk-first, DB-row-second, so the only
failure mode is unreferenced garbage (recoverable), never a dangling reference.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
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

    # ── orphan accounting: the upstream cause of a bloated blob store ────────────────
    def orphans(self, *, by_agent: bool = True, older_than_hours: int = 0,
                limit: int = 20) -> dict:
        """Blobs that were uploaded and never attached to anything.

        Uploading is not recording: bytes with no blob_ref, no digest in props and no tool
        pointing at them are invisible to every other agent and are what the GC eventually
        reclaims. 94 GB (80% of the store) accumulated this way before anyone noticed, so this
        makes the leak visible — and attributable — while it is small.
        """
        mentioned = self._digests_mentioned_in_graph()
        cutoff = time.time() - older_than_hours * 3600
        with self.db.read() as cur:
            rows = cur.execute(
                "SELECT b.digest, b.size, t.tx_time, t.agent_id FROM blob b "
                "JOIN tx t ON t.tx_id = b.created_tx "
                "WHERE b.digest NOT IN (SELECT digest FROM blob_ref)").fetchall()
        per_agent: dict = {}
        total_n = total_b = 0
        for r in rows:
            if r["digest"] in mentioned:
                continue
            if older_than_hours and _iso_epoch(r["tx_time"]) > cutoff:
                continue
            a = per_agent.setdefault(r["agent_id"], {"agent": r["agent_id"], "blobs": 0,
                                                     "bytes": 0})
            a["blobs"] += 1
            a["bytes"] += r["size"] or 0
            total_n += 1
            total_b += r["size"] or 0
        ranked = sorted(per_agent.values(), key=lambda d: d["bytes"], reverse=True)[:limit]
        for a in ranked:
            a["gb"] = round(a["bytes"] / 1073741824, 2)
        return {"unattached_blobs": total_n, "bytes": total_b,
                "gb": round(total_b / 1073741824, 2),
                "by_agent": ranked if by_agent else [],
                "hint": ("attach uploads with artifact_attach(digest, version_id, role) or record "
                         "the digest in the node's props; unattached bytes are garbage-collected")}

    # ── garbage collection (mark-and-sweep with a grace window + pins) ──────────────
    _DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

    def _digests_mentioned_in_graph(self) -> set:
        """Every digest something still points at, other than via blob_ref.

        blob_ref is the intended way to attach an artifact, but nothing stops an agent recording
        a digest inside props instead — and 282 blobs (1.8 GB) on the live graph were reachable
        only that way. Those bytes are referenced in practice, so they are GC roots too; treating
        only blob_ref as a root silently deletes live data. All versions are scanned, not just
        heads, because superseded versions keep their evidence.
        """
        found: set = set()
        with self.db.read() as cur:
            for table in ("node_version", "edge_version"):
                for (props,) in cur.execute(
                        f"SELECT props FROM {table} WHERE props LIKE '%sha256:%'"):
                    if props:
                        found.update(self._DIGEST_RE.findall(props))
            # published tools point at their bytes via tool_version.artifact_digest, which is a
            # foreign key and NOT a blob_ref row. Missing this deleted a live tool artifact.
            for (d,) in cur.execute("SELECT DISTINCT artifact_digest FROM tool_version "
                                    "WHERE artifact_digest IS NOT NULL"):
                found.add(d)
        return found

    def gc(self, *, dry_run: bool = True) -> dict:
        cutoff = time.time() - self.grace_seconds
        mentioned = self._digests_mentioned_in_graph()
        with self.db.read() as cur:
            rows = cur.execute(
                "SELECT b.digest, t.tx_time FROM blob b JOIN tx t ON t.tx_id=b.created_tx "
                "WHERE b.digest NOT IN (SELECT digest FROM blob_ref) "
                "AND b.digest NOT IN (SELECT digest FROM blob_pin)"
            ).fetchall()
        collected, kept_young, kept_mentioned, kept_referenced = [], 0, 0, 0
        for r in rows:
            if r["digest"] in mentioned:                  # referenced by digest in props → root
                kept_mentioned += 1
                continue
            created = _iso_epoch(r["tx_time"])
            if created > cutoff:                          # inside the grace window → keep
                kept_young += 1
                continue
            collected.append(r["digest"])
        freed_planned = 0
        if collected:
            with self.db.read() as cur:
                for i in range(0, len(collected), 900):
                    chunk = collected[i:i + 900]
                    freed_planned += cur.execute(
                        f"SELECT COALESCE(SUM(size),0) FROM blob WHERE digest IN "
                        f"({','.join('?' * len(chunk))})", chunk).fetchone()[0]
        deleted = 0
        if not dry_run:
            for digest in collected:
                if self.refs(digest) or digest in mentioned:   # re-check without holding a lock
                    continue
                # Delete the row FIRST. If anything still references the blob, the foreign key
                # rejects it and the bytes stay on disk; unlinking first would orphan a live file
                # (it did: one tool artifact, recovered from backup).
                try:
                    with self.db.write("gc", "blob_gc") as tx:
                        tx.cur.execute("DELETE FROM blob WHERE digest=?", (digest,))
                except sqlite3.IntegrityError:
                    kept_referenced += 1
                    continue
                p = self.path_for(digest)
                with _suppress():
                    if p.exists():
                        os.chmod(p, 0o644)
                        os.unlink(p)
                deleted += 1
        # stale upload temporaries: interrupted PUTs leave files in blobs/tmp forever
        stale_tmp = 0
        if self.tmp.exists():
            for f in self.tmp.iterdir():
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        stale_tmp += 1
                        if not dry_run:
                            f.unlink()
                except OSError:
                    pass
        freed = freed_planned
        return {"unreferenced": len(collected), "kept_within_grace": kept_young,
                "kept_referenced_in_props": kept_mentioned,
                "kept_fk_referenced": kept_referenced, "stale_tmp_files": stale_tmp,
                "bytes": freed, "gb": round(freed / 1073741824, 2),
                "deleted": 0 if dry_run else deleted, "dry_run": dry_run}


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
