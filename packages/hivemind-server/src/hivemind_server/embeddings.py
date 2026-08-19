"""Semantic search over skills and tools.

Design constraint: this is a self-hosted service that must install with `pip install` and no
model download, but it also runs next to real GPUs. So the embedder is pluggable and degrades:

  1. `sentence-transformers` if it is importable (real neural embeddings, best quality);
  2. otherwise a dependency-free hashed TF-IDF vectoriser (classical vector-space semantics —
     it generalises over shared/rare terms but NOT over true paraphrase).

Whichever is active is reported in every response, so nobody mistakes the fallback for neural
retrieval. Vectors are L2-normalised float32 in SQLite, and cosine is a dot product; at a few
thousand items brute force is microseconds and needs no index (sqlite-vec is still pre-1.0).
"""
from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Iterable, Optional

from .db import Database

_TOKEN = re.compile(r"[a-z0-9_]+")
_HASH_DIM = 512


def _tokens(text: str) -> list:
    return _TOKEN.findall((text or "").lower())


class HashingEmbedder:
    """Hashed TF-IDF-ish vectors: no model, no downloads, deterministic. Honest about limits."""

    name = "hashing-tfidf-512"
    dim = _HASH_DIM

    def encode(self, texts: Iterable[str]) -> list:
        out = []
        for t in texts:
            vec = [0.0] * self.dim
            toks = _tokens(t)
            if not toks:
                out.append(vec); continue
            counts: dict = {}
            for tok in toks:
                counts[tok] = counts.get(tok, 0) + 1
            for tok, n in counts.items():
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big")
                idx = h % self.dim
                sign = 1.0 if (h >> 31) & 1 else -1.0
                # sublinear tf, and longer tokens carry more signal than stopword-ish short ones
                vec[idx] += sign * (1.0 + math.log(n)) * (1.0 + min(len(tok), 12) / 12.0)
            out.append(_normalise(vec))
        return out


class SentenceTransformerEmbedder:  # pragma: no cover - optional dependency
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer
        self._m = SentenceTransformer(model_name)
        self.name = f"st:{model_name}"
        self.dim = int(self._m.get_sentence_embedding_dimension())

    def encode(self, texts: Iterable[str]) -> list:
        vecs = self._m.encode(list(texts), normalize_embeddings=True)
        return [list(map(float, v)) for v in vecs]


_embedder = None


def embedder():
    global _embedder
    if _embedder is None:
        try:                                    # pragma: no cover - depends on environment
            _embedder = SentenceTransformerEmbedder()
        except Exception:
            _embedder = HashingEmbedder()
    return _embedder


def backend_name() -> str:
    return embedder().name


def is_neural() -> bool:
    return embedder().name.startswith("st:")


def _normalise(vec: list) -> list:
    n = math.sqrt(sum(v * v for v in vec))
    return [v / n for v in vec] if n else vec


def _pack(vec: list) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes, dim: int) -> list:
    return list(struct.unpack(f"<{dim}f", blob))


def upsert(db: Database, kind: str, item_id: str, text: str, agent_id: str = "embed") -> None:
    e = embedder()
    vec = e.encode([text])[0]
    with db.write(agent_id, f"embed {kind}:{item_id}") as tx:
        tx.cur.execute(
            "INSERT INTO embedding(kind,item_id,model,dim,vec,updated_tx) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(kind,item_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
            "vec=excluded.vec, updated_tx=excluded.updated_tx",
            (kind, item_id, e.name, e.dim, _pack(vec), tx.tx_id))


def query(db: Database, kind: str, text: str, limit: int = 20) -> list:
    """Brute-force cosine over one kind. Returns [(item_id, score)] best first."""
    e = embedder()
    qv = e.encode([text])[0]
    with db.read() as cur:
        rows = cur.execute(
            "SELECT item_id, dim, vec FROM embedding WHERE kind=? AND model=?",
            (kind, e.name)).fetchall()
    scored = []
    for r in rows:
        v = _unpack(r["vec"], r["dim"])
        if len(v) != len(qv):
            continue
        scored.append((r["item_id"], sum(a * b for a, b in zip(qv, v))))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:limit]


def backfill(db: Database, kind: str, items: Iterable[tuple]) -> dict:
    """items = [(item_id, text), …]. Re-embeds everything for the ACTIVE backend."""
    n = 0
    for item_id, text in items:
        upsert(db, kind, item_id, text)
        n += 1
    return {"kind": kind, "embedded": n, "backend": backend_name(), "neural": is_neural()}


def coverage(db: Database, kind: str) -> dict:
    """How many vectors exist for the ACTIVE backend vs other backends.

    query() filters by model name, so after switching backend (e.g. installing
    sentence-transformers) the old vectors stop matching and semantic search silently returns
    nothing while hybrid quietly degrades to lexical. Callers surface this rather than let a
    soft failure look like a thin library.
    """
    active = backend_name()
    with db.read() as cur:
        rows = cur.execute(
            "SELECT model, COUNT(*) n FROM embedding WHERE kind=? GROUP BY model",
            (kind,)).fetchall()
    by_model = {r["model"]: r["n"] for r in rows}
    mine = by_model.pop(active, 0)
    return {"backend": active, "vectors": mine, "stale_other_backends": by_model,
            "stale": bool(by_model) and mine == 0}


def warning_if_stale(db: Database, kind: str) -> Optional[str]:
    c = coverage(db, kind)
    if c["stale"]:
        others = ", ".join(f"{k} ({v})" for k, v in c["stale_other_backends"].items())
        return (f"semantic search is INACTIVE: 0 vectors for the active backend {c['backend']!r}, "
                f"but {others} exist from a previous backend. Results are lexical-only until you "
                f"run `hivemind-admin --project <p> embed` to re-embed.")
    if c["vectors"] == 0:
        return (f"semantic search is INACTIVE: no embeddings stored for {kind}s. Run "
                f"`hivemind-admin --project <p> embed` to enable it.")
    return None
