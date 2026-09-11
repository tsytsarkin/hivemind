"""The agent bus: coordination between live agent sessions, deliberately outside the graph.

The graph answers "what is true". The bus answers "who is here right now, what can they do, and
who is doing this piece of work" — questions whose answers are worthless five minutes later. Four
design choices follow from that and none of them are negotiable without re-reading docs/bus.md:

  1. Identity is a per-SESSION id minted here, never the bearer token. One token is reused across
     many agents on many harnesses; an agent with a handset attached and an agent with a headless
     browser share a token and are not the same actor. Keying on the token would union their
     capabilities into a composite that no real agent has, and every capability query would lie.

  2. Capabilities are specific, dotted and self-asserted (`device.handset.attached`, `browser.cdp`).
     Self-asserted is a real weakness: an agent that claims `browser.cdp` and cannot drive a
     browser will win claims and fail them. Lease expiry bounds the damage; nothing prevents it.

  3. Order is one global AUTOINCREMENT seq, so an agent carries a single integer cursor across
     every room. Delivery is at-least-once and the READER advances the cursor — never the server,
     and never the sidecar (see peek() vs poll()). A message seen by a watcher that then died must
     still be there for the agent it was trying to wake.

  4. Work is distributed by OPEN CLAIM: notify everyone who matches, let exactly one win a single
     `UPDATE ... WHERE claimed_by IS NULL`. No scheduler, no load model, and a wedged advertiser
     cannot stall a request because it simply never claims.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from .db import SENTINEL, Database, Invalid, NotFound, canonical_json, now_iso
from .ids import ulid

SYSTEM = "system"
MSG_KINDS = ("chat", "question", "request", "response", "claim", "system")
MAX_THREAD_DEPTH = 8
OPEN, CLAIMED, DONE, FAILED, EXPIRED, CANCELLED = (
    "open", "claimed", "done", "failed", "expired", "cancelled")

# Lazy reaping: leases and sessions expire on a clock, but nothing here runs on a timer. Rather
# than a background task (which would need a lifespan hook per project) the read paths reap at
# most once every _REAP_EVERY seconds. Keyed by db path so projects don't share a clock.
_REAP_EVERY = 30.0
_last_reap: dict[str, float] = {}


def _ts(seconds_from_now: float = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).isoformat()


def _jloads(s: Optional[str], default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def _cap_clause(pattern: str) -> tuple[str, list]:
    """Translate one capability pattern into SQL.

    Exact by default; a trailing '*' is a prefix match, so `browser.*` covers `browser.cdp` and
    `browser.cdp.headless` but a bare `browser` matches only itself. Deterministic and boring on
    purpose — a fuzzy match here would make claim eligibility unpredictable.
    """
    if pattern.endswith("*"):
        prefix = pattern[:-1].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return "name LIKE ? ESCAPE '\\'", [prefix + "%"]
    return "name = ?", [pattern]


def _matches_any(cur, session_id: str, needs: Iterable[str]) -> bool:
    for pattern in needs:
        clause, params = _cap_clause(pattern)
        row = cur.execute(
            f"SELECT 1 FROM bus_capability WHERE session_id=? AND {clause} LIMIT 1",
            [session_id, *params]).fetchone()
        if row is None:
            return False           # ALL needs must be met, not any
    return True


def _sessions_matching(cur, needs: list[str], now: str) -> list[str]:
    """Live sessions holding every capability in `needs` (empty needs = every live session)."""
    live = ("SELECT session_id FROM bus_session WHERE ended_at IS NULL AND expires_at > ?")
    ids = [r["session_id"] for r in cur.execute(live, (now,))]
    if not needs:
        return ids
    return [sid for sid in ids if _matches_any(cur, sid, needs)]


# ── graph references ──────────────────────────────────────────────────────────────
# The bus holds no knowledge, but "do this to that thing" needs the that. A ref is a typed,
# VALIDATED pointer into the graph, so a worker gets the subject of the work instead of a prose
# description of it, and a stale pointer is rejected at post time rather than at claim time.

REF_KINDS = ("node", "version", "subject", "traversal", "search")
REF_ROLES = ("context", "target", "evidence", "result")
MAX_REFS = 25


def _norm_ref(cur, ref: Any) -> dict:
    """Validate one reference and normalise it. Raises Invalid with an actionable message."""
    if isinstance(ref, str):                     # bare node id is the common case
        ref = {"kind": "node", "id": ref}
    if not isinstance(ref, dict):
        raise Invalid(f"a ref must be a node_id string or an object, got {type(ref).__name__}")
    kind = ref.get("kind", "node")
    if kind not in REF_KINDS:
        raise Invalid(f"ref kind must be one of {REF_KINDS}, got {kind!r}")
    role = ref.get("role", "context")
    if role not in REF_ROLES:
        raise Invalid(f"ref role must be one of {REF_ROLES}, got {role!r}")
    note = ref.get("note")
    spec: dict = {"kind": kind}
    anchor = None

    if kind in ("node", "traversal"):
        nid = ref.get("id") or ref.get("node_id")
        if not nid:
            raise Invalid(f"ref kind {kind!r} needs id=<node_id>")
        row = cur.execute("SELECT node_id, redirect_to FROM node WHERE node_id=?",
                          (nid,)).fetchone()
        if row is None:
            raise Invalid(f"no node {nid!r} in this project — graph_search for the right id, and "
                          f"remember the bus cannot point at another project's graph")
        anchor = row["redirect_to"] or row["node_id"]   # follow a merge tombstone
        spec["id"] = anchor
        if kind == "traversal":
            depth = int(ref.get("depth", 1))
            if not 1 <= depth <= 4:
                raise Invalid("traversal depth must be 1..4 (graph_neighbors' own limit)")
            direction = ref.get("direction", "out")
            if direction not in ("out", "in", "both"):
                raise Invalid("traversal direction must be out|in|both")
            spec.update(depth=depth, direction=direction,
                        edge_types=list(ref.get("edge_types") or []) or None)
    elif kind == "version":
        vid = ref.get("id") or ref.get("version_id")
        row = cur.execute("SELECT version_id, node_id FROM node_version WHERE version_id=?",
                          (vid,)).fetchone() if vid else None
        if row is None:
            raise Invalid(f"no node version {vid!r} — pass a version_id from graph_get/graph_upsert "
                          f"(use kind='node' if you meant the current head, not a pinned revision)")
        anchor = row["node_id"]
        spec.update(id=row["version_id"], node_id=row["node_id"])
    elif kind == "subject":
        key = ref.get("key") or ref.get("subject_key")
        if not key:
            raise Invalid("ref kind 'subject' needs key=<subject_key>")
        version = ref.get("version") or ref.get("subject_version")
        q = "SELECT node_id FROM node WHERE subject_key=?"
        params = [key]
        if version is not None:
            q += " AND subject_version=?"
            params.append(version)
        row = cur.execute(q + " ORDER BY subject_order, subject_version LIMIT 1", params).fetchone()
        if row is None:
            raise Invalid(f"no node for subject_key={key!r}"
                          + (f" at version {version!r}" if version is not None else "")
                          + " — check graph_subjects(subject_key)")
        anchor = row["node_id"]
        spec.update(key=key, version=version)
    else:                                          # search
        query = (ref.get("query") or "").strip()
        if not query:
            raise Invalid("ref kind 'search' needs a non-empty query")
        spec.update(query=query, types=list(ref.get("types") or []) or None)

    return {"kind": kind, "anchor": anchor, "spec": spec, "role": role, "note": note}


def _store_refs(cur, refs: Optional[list], *, message_seq: Optional[int] = None,
                request_id: Optional[str] = None) -> list[dict]:
    if not refs:
        return []
    if len(refs) > MAX_REFS:
        raise Invalid(f"at most {MAX_REFS} refs per message/request, got {len(refs)}")
    now = now_iso()
    out = []
    for raw in refs:
        r = _norm_ref(cur, raw)
        cur.execute(
            "INSERT INTO bus_ref(message_seq,request_id,kind,anchor,spec,role,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (message_seq, request_id, r["kind"], r["anchor"], canonical_json(r["spec"]),
             r["role"], r["note"], now))
        out.append(r)
    return out


def _label(cur, node_id: Optional[str]) -> Optional[dict]:
    """One-line summary of a node, shaped like a search hit so agents see a familiar thing."""
    if not node_id:
        return None
    row = cur.execute(
        "SELECT n.node_id, n.node_type, n.subject_key, n.subject_version, nv.version_id, nv.props "
        "FROM node n JOIN node_version nv ON nv.node_id=n.node_id AND nv.tx_to=? "
        "WHERE n.node_id=?", (SENTINEL, node_id)).fetchone()
    if row is None:
        return {"node_id": node_id, "gone": True}
    return {"node_id": row["node_id"], "node_type": row["node_type"],
            "subject_key": row["subject_key"], "subject_version": row["subject_version"],
            "version_id": row["version_id"], "snippet": (row["props"] or "")[:200]}


def _refs_bulk(cur, message_seqs: list, request_ids: list) -> tuple[dict, dict]:
    """Refs for many messages/requests in two queries, so poll() stays O(1) in round trips."""
    by_msg: dict = {}
    by_req: dict = {}
    for ids, column, sink in ((message_seqs, "message_seq", by_msg),
                              (request_ids, "request_id", by_req)):
        ids = [i for i in ids if i is not None]
        if not ids:
            continue
        marks = ",".join("?" * len(ids))
        for r in cur.execute(
                f"SELECT * FROM bus_ref WHERE {column} IN ({marks}) ORDER BY ref_id", ids):
            sink.setdefault(r[column], []).append(
                {"kind": r["kind"], "role": r["role"], "note": r["note"],
                 "spec": _jloads(r["spec"], {}), "target": _label(cur, r["anchor"])})
    return by_msg, by_req


def refs_for_node(db: Database, node_id: str, *, limit: int = 50) -> dict:
    """Which live bus traffic points at this node — the reverse of a ref.

    One-way by design: this reads the bus, and nothing writes a bus pointer into the graph, so
    reaping a message cannot leave the graph holding a dead link.
    """
    with db.read() as cur:
        rows = cur.execute(
            "SELECT r.*, m.room AS room, m.sender AS sender, m.body AS body, m.kind AS msg_kind "
            "FROM bus_ref r LEFT JOIN bus_message m ON m.seq=r.message_seq "
            "WHERE r.anchor=? ORDER BY r.ref_id DESC LIMIT ?",
            (node_id, max(1, min(limit, 200)))).fetchall()
        out = []
        for r in rows:
            item = {"kind": r["kind"], "role": r["role"], "note": r["note"],
                    "spec": _jloads(r["spec"], {}), "created_at": r["created_at"]}
            if r["message_seq"] is not None:
                item.update(seq=r["message_seq"], room=r["room"], sender=r["sender"],
                            body=r["body"], message_kind=r["msg_kind"])
            if r["request_id"] is not None:
                rq = cur.execute("SELECT task, state, requester, claimed_by FROM bus_request "
                                 "WHERE request_id=?", (r["request_id"],)).fetchone()
                item["request"] = ({"request_id": r["request_id"], **dict(rq)} if rq
                                   else {"request_id": r["request_id"]})
            out.append(item)
    return {"node_id": node_id, "refs": out, "count": len(out),
            "hint": "bus traffic is ephemeral — absence here means nobody is discussing it NOW"}


def resolve(db: Database, *, request_id: Optional[str] = None, seq: Optional[int] = None,
            limit: int = 25) -> dict:
    """Follow a message's or request's refs into the graph and return what they point at.

    Kept out of the poll path on purpose: a traversal can be large, and a worker deciding whether
    to claim needs the labels, not the whole subgraph.
    """
    if (request_id is None) == (seq is None):
        raise Invalid("give exactly one of request_id or seq")
    from . import graph
    column, value = ("request_id", request_id) if request_id is not None else ("message_seq", seq)
    with db.read() as cur:
        rows = cur.execute(f"SELECT * FROM bus_ref WHERE {column}=? ORDER BY ref_id",
                           (value,)).fetchall()
        specs = [(r["kind"], _jloads(r["spec"], {}), r["role"], r["note"]) for r in rows]
    out = []
    for kind, spec, role, note in specs:
        item: dict = {"kind": kind, "role": role, "note": note, "spec": spec}
        try:
            if kind == "node":
                item["node"] = graph.get_node(db, node_id=spec["id"])
            elif kind == "version":
                item["node"] = graph.get_node(db, node_id=spec["node_id"])
                item["pinned_version_id"] = spec["id"]
            elif kind == "subject":
                item["node"] = graph.get_node(db, subject_key=spec["key"],
                                              subject_version=spec.get("version")) \
                    if spec.get("version") else graph.list_subjects(db, spec["key"])
            elif kind == "traversal":
                item["neighbors"] = graph.neighbors(
                    db, spec["id"], edge_types=spec.get("edge_types"),
                    depth=spec.get("depth", 1), direction=spec.get("direction", "out"))
            else:
                item["results"] = graph.search_nodes(db, spec["query"], types=spec.get("types"),
                                                     limit=limit)
        except (NotFound, Invalid) as e:
            # A ref validated at post time can rot: the node may have been merged or retracted
            # since. Report it rather than failing the whole resolve.
            item["error"] = str(e)
        out.append(item)
    return {column: value, "refs": out, "count": len(out)}


def _live(cur, session_id: str, now: Optional[str] = None) -> dict:
    now = now or now_iso()
    r = cur.execute("SELECT * FROM bus_session WHERE session_id=?", (session_id,)).fetchone()
    if r is None:
        raise NotFound(f"no bus session {session_id!r} — call bus_hello first")
    if r["ended_at"] is not None:
        # Deliberately does not name bus_bye as the cause. A reap sets ended_at too, so an expired
        # session reports "ended" or "expired" depending only on whether a rate-limited
        # _maybe_reap happened to run first — and telling an agent it called bye when it did not
        # sends it looking for a bug in its own shutdown path.
        raise Invalid(f"session {session_id!r} has ended (bus_bye, or expired and was reaped); "
                      f"call bus_hello for a new one")
    if r["expires_at"] <= now:
        raise Invalid(
            f"session {session_id!r} expired at {r['expires_at']} (missed heartbeat). "
            f"Call bus_hello to register again; capabilities must be re-advertised.")
    return dict(r)


# ── sessions ──────────────────────────────────────────────────────────────────────

def hello(db: Database, *, label: str, capabilities: Optional[dict] = None,
          harness: Optional[str] = None, interruptible: bool = False,
          rooms: Optional[list[str]] = None, meta: Optional[dict] = None,
          client_id: Optional[str] = None, ttl: int = 900) -> dict:
    """Register a session and advertise what it can do. Returns the id everything else keys on.

    `capabilities` is {name: attrs} — attrs may be {} but the name must be specific enough that
    another agent can act on it. `interruptible` says whether a sidecar can actually wake this
    session; requesters use it to prefer a worker that will notice.
    """
    if not (label or "").strip():
        raise Invalid("label is required — it is what other agents see in the directory")
    caps = capabilities or {}
    for name, attrs in caps.items():
        if not name or " " in name:
            raise Invalid(f"capability name {name!r} must be non-empty and space-free "
                          f"(dotted form, e.g. 'device.handset.attached')")
        if not isinstance(attrs, dict):
            raise Invalid(f"capability {name!r} attrs must be an object, got {type(attrs).__name__}")
    sid = ulid()
    now = now_iso()
    deadline = _ts(ttl)
    rooms = list(dict.fromkeys(["lobby", *(rooms or [])]))
    with db.write_light() as cur:
        cur.execute(
            "INSERT INTO bus_session(session_id,label,harness,interruptible,client_id,meta,"
            "cursor,started_at,last_seen,expires_at,ttl) "
            "VALUES(?,?,?,?,?,?,(SELECT COALESCE(MAX(seq),0) FROM bus_message),?,?,?,?)",
            (sid, label, harness, 1 if interruptible else 0, client_id,
             canonical_json(meta or {}), now, now, deadline, ttl))
        for name, attrs in caps.items():
            cur.execute("INSERT INTO bus_capability(session_id,name,attrs) VALUES(?,?,?)",
                        (sid, name, canonical_json(attrs)))
        for room in rooms:
            cur.execute("INSERT INTO bus_membership(session_id,room,joined_at) VALUES(?,?,?)",
                        (sid, room, now))
        start = cur.execute("SELECT cursor FROM bus_session WHERE session_id=?",
                            (sid,)).fetchone()["cursor"]
    return {"session_id": sid, "label": label, "cursor": start, "rooms": rooms,
            "capabilities": sorted(caps), "interruptible": bool(interruptible),
            "heartbeat_sec": max(30, ttl // 3), "expires_at": deadline,
            "hint": ("Heartbeat with bus_ping before expires_at or you drop out of the directory. "
                     "Your cursor starts at the current head, so you will not be handed a backlog.")}


def ping(db: Database, session_id: str, *, ttl: int = 900,
         capabilities: Optional[dict] = None, status: Optional[str] = None) -> dict:
    """Heartbeat. Optionally re-advertise capabilities (replaces the whole set) or set a status."""
    now = now_iso()
    deadline = _ts(ttl)
    with db.write_light() as cur:
        s = _live(cur, session_id, now)
        meta = _jloads(s["meta"], {})
        if status is not None:
            meta["status"] = status
        cur.execute("UPDATE bus_session SET last_seen=?, expires_at=?, ttl=?, meta=? "
                    "WHERE session_id=?",
                    (now, deadline, ttl, canonical_json(meta), session_id))
        if capabilities is not None:
            cur.execute("DELETE FROM bus_capability WHERE session_id=?", (session_id,))
            for name, attrs in capabilities.items():
                cur.execute("INSERT INTO bus_capability(session_id,name,attrs) VALUES(?,?,?)",
                            (session_id, name, canonical_json(attrs or {})))
    return {"session_id": session_id, "expires_at": deadline, "status": meta.get("status")}


def bye(db: Database, session_id: str) -> dict:
    """Leave cleanly: drop out of the directory now and release anything still claimed."""
    now = now_iso()
    with db.write_light() as cur:
        released = _release_claims(cur, session_id, now, "session ended (bus_bye)")
        cur.execute("UPDATE bus_session SET ended_at=?, expires_at=? WHERE session_id=?",
                    (now, now, session_id))
        cur.execute("DELETE FROM bus_capability WHERE session_id=?", (session_id,))
        cur.execute("DELETE FROM bus_membership WHERE session_id=?", (session_id,))
    return {"session_id": session_id, "ended_at": now, "requests_released": released}


def sessions(db: Database, *, capability: Optional[str] = None,
             include_ended: bool = False) -> dict:
    """The live directory: who is here and what they say they can do."""
    _maybe_reap(db)
    now = now_iso()
    with db.read() as cur:
        q = "SELECT * FROM bus_session"
        params: list = []
        if not include_ended:
            q += " WHERE ended_at IS NULL AND expires_at > ?"
            params.append(now)
        rows = cur.execute(q + " ORDER BY started_at", params).fetchall()
        out = []
        for r in rows:
            caps = {c["name"]: _jloads(c["attrs"], {}) for c in cur.execute(
                "SELECT name, attrs FROM bus_capability WHERE session_id=?", (r["session_id"],))}
            if capability:
                clause, cparams = _cap_clause(capability)
                hit = cur.execute(
                    f"SELECT 1 FROM bus_capability WHERE session_id=? AND {clause} LIMIT 1",
                    [r["session_id"], *cparams]).fetchone()
                if hit is None:
                    continue
            rooms = [m["room"] for m in cur.execute(
                "SELECT room FROM bus_membership WHERE session_id=? ORDER BY room",
                (r["session_id"],))]
            out.append({"session_id": r["session_id"], "label": r["label"],
                        "harness": r["harness"], "interruptible": bool(r["interruptible"]),
                        "capabilities": caps, "rooms": rooms, "cursor": r["cursor"],
                        "last_seen": r["last_seen"], "expires_at": r["expires_at"],
                        "status": _jloads(r["meta"], {}).get("status"),
                        "ended_at": r["ended_at"]})
    return {"sessions": out, "count": len(out), "as_of": now,
            "hint": ("capabilities are SELF-ASSERTED — an agent advertising a capability it does "
                     "not have will win a claim and fail it. Prefer interruptible sessions for "
                     "work that should start promptly.")}


def capability_index(db: Database) -> dict:
    """Every capability currently advertised, with how many live sessions hold it.

    This is how the vocabulary self-organises: agents read what is already in use instead of
    inventing a near-synonym nobody queries for.
    """
    now = now_iso()
    with db.read() as cur:
        rows = cur.execute(
            "SELECT c.name AS name, COUNT(*) AS n FROM bus_capability c "
            "JOIN bus_session s ON s.session_id=c.session_id "
            "WHERE s.ended_at IS NULL AND s.expires_at > ? GROUP BY c.name ORDER BY n DESC, c.name",
            (now,)).fetchall()
    return {"capabilities": [{"name": r["name"], "sessions": r["n"]} for r in rows],
            "count": len(rows),
            "hint": "match with an exact name or a trailing '*' prefix, e.g. browser.*"}


# ── rooms and messages ────────────────────────────────────────────────────────────

def join(db: Database, session_id: str, room: str) -> dict:
    with db.write_light() as cur:
        _live(cur, session_id)
        cur.execute("INSERT OR IGNORE INTO bus_membership(session_id,room,joined_at) "
                    "VALUES(?,?,?)", (session_id, room, now_iso()))
    return {"session_id": session_id, "room": room, "joined": True}


def leave(db: Database, session_id: str, room: str) -> dict:
    with db.write_light() as cur:
        n = cur.execute("DELETE FROM bus_membership WHERE session_id=? AND room=?",
                        (session_id, room)).rowcount
    return {"session_id": session_id, "room": room, "left": bool(n)}


def rooms(db: Database) -> dict:
    now = now_iso()
    with db.read() as cur:
        rows = cur.execute(
            "SELECT m.room AS room, COUNT(*) AS members FROM bus_membership m "
            "JOIN bus_session s ON s.session_id=m.session_id "
            "WHERE s.ended_at IS NULL AND s.expires_at > ? GROUP BY m.room ORDER BY m.room",
            (now,)).fetchall()
        recent = {r["room"]: r["n"] for r in cur.execute(
            "SELECT room, COUNT(*) n FROM bus_message WHERE expires_at > ? GROUP BY room", (now,))}
    return {"rooms": [{"room": r["room"], "members": r["members"],
                       "messages": recent.get(r["room"], 0)} for r in rows]}


def post(db: Database, *, sender: str, body: str, room: Optional[str] = None,
         to_session: Optional[str] = None, kind: str = "chat",
         data: Optional[dict] = None, request_id: Optional[str] = None,
         refs: Optional[list] = None, reply_to: Optional[int] = None,
         ttl: int = 604800) -> dict:
    """Send a message, optionally as a reply.

    A reply inherits its parent's room, so an answer cannot drift into a different room from its
    question. Replying to a message that was directed at you defaults to answering the sender
    privately — the natural reading of "reply" in both cases. Both are overridable.
    """
    if kind not in MSG_KINDS:
        raise Invalid(f"kind must be one of {MSG_KINDS}")
    if not (body or "").strip():
        raise Invalid("body is required")
    with db.write_light() as cur:
        if sender != SYSTEM:
            _live(cur, sender)
        parent = None
        if reply_to is not None:
            parent = cur.execute("SELECT * FROM bus_message WHERE seq=?", (reply_to,)).fetchone()
            if parent is None:
                raise NotFound(f"no message {reply_to!r} to reply to — it may already have "
                               f"expired; post it as a new message instead")
            if room is None:
                room = parent["room"]
            if to_session is None and parent["to_session"] == sender:
                to_session = parent["sender"]
        if room is None:
            room = "lobby"
        if to_session is not None:
            t = cur.execute("SELECT ended_at, expires_at FROM bus_session WHERE session_id=?",
                            (to_session,)).fetchone()
            if t is None:
                raise NotFound(f"no bus session {to_session!r} to address")
        seq = _emit(cur, sender=sender, body=body, room=room, to_session=to_session,
                    kind=kind, data=data, request_id=request_id, ttl=ttl, reply_to=reply_to)
        stored = _store_refs(cur, refs, message_seq=seq)
    return {"seq": seq, "room": room, "to_session": to_session, "kind": kind,
            "reply_to": reply_to,
            "refs": [{"kind": r["kind"], "role": r["role"], "anchor": r["anchor"]}
                     for r in stored]}


def _emit(cur, *, sender: str, body: str, room: str, to_session: Optional[str],
          kind: str, data: Optional[dict], request_id: Optional[str], ttl: int,
          reply_to: Optional[int] = None) -> int:
    cur.execute(
        "INSERT INTO bus_message(room,sender,to_session,kind,body,data,request_id,reply_to,"
        "created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (room, sender, to_session, kind, body, canonical_json(data or {}), request_id,
         reply_to, now_iso(), _ts(ttl)))
    return int(cur.lastrowid)


_VISIBLE = ("(m.to_session = :sid OR (m.to_session IS NULL AND m.room IN "
            "(SELECT room FROM bus_membership WHERE session_id = :sid)))")


def _fetch(cur, session_id: str, after: int, limit: int, rooms_filter: Optional[list[str]],
           include_self: bool) -> list[dict]:
    q = ["SELECT m.* FROM bus_message m WHERE m.seq > :after AND", _VISIBLE]
    params: dict[str, Any] = {"sid": session_id, "after": after, "limit": limit}
    if not include_self:
        q.append("AND m.sender != :sid")
    if rooms_filter:
        marks = ",".join(f":r{i}" for i in range(len(rooms_filter)))
        q.append(f"AND m.room IN ({marks})")
        params.update({f"r{i}": r for i, r in enumerate(rooms_filter)})
    q.append("ORDER BY m.seq LIMIT :limit")
    rows = cur.execute(" ".join(q), params).fetchall()
    # A request's refs live on the request, not copied onto each notification, so a message
    # carrying a request_id inherits them — that is how a worker sees what it is being asked
    # to act ON, not just what it is being asked to do.
    by_msg, by_req = _refs_bulk(cur, [r["seq"] for r in rows],
                                [r["request_id"] for r in rows])
    counts = _reply_counts(cur, [r["seq"] for r in rows])
    out = []
    for r in rows:
        refs = by_msg.get(r["seq"], []) + by_req.get(r["request_id"], [])
        m = {"seq": r["seq"], "room": r["room"], "sender": r["sender"],
             "to_session": r["to_session"], "kind": r["kind"], "body": r["body"],
             "data": _jloads(r["data"], {}), "request_id": r["request_id"],
             "reply_to": r["reply_to"], "created_at": r["created_at"],
             "direct": r["to_session"] is not None}
        if counts.get(r["seq"]):
            m["reply_count"] = counts[r["seq"]]
            m["thread_hint"] = f"bus_thread({r['seq']}) for the answers already given"
        if refs:
            m["refs"] = refs
            m["refs_hint"] = ("these point into the graph; bus_resolve(...) follows them, "
                              "graph_get(node_id) reads one in full")
        out.append(m)
    return out


def _reply_counts(cur, seqs: list) -> dict:
    """Direct reply counts for a batch, so a poller can see a question already has answers."""
    seqs = [s for s in seqs if s is not None]
    if not seqs:
        return {}
    marks = ",".join("?" * len(seqs))
    return {r["reply_to"]: r["n"] for r in cur.execute(
        f"SELECT reply_to, COUNT(*) n FROM bus_message WHERE reply_to IN ({marks}) "
        f"GROUP BY reply_to", seqs)}


def thread(db: Database, seq: int, *, limit: int = 200) -> dict:
    """A message and everything posted in reply to it, oldest-first with nesting depth.

    Room-scoped like history(): direct messages are excluded, so a private answer to a public
    question stays private. Post to the room if you want your answer on the record.
    """
    lim = max(1, min(limit, 500))
    with db.read() as cur:
        root = cur.execute("SELECT * FROM bus_message WHERE seq=?", (seq,)).fetchone()
        if root is None:
            raise NotFound(f"no message {seq!r} — it may have expired")
        rows = cur.execute(
            """WITH RECURSIVE t(seq, depth) AS (
                   SELECT seq, 0 FROM bus_message WHERE seq = ?
                 UNION ALL
                   SELECT m.seq, t.depth + 1 FROM bus_message m JOIN t ON m.reply_to = t.seq
                   WHERE t.depth < ?
               )
               SELECT m.*, t.depth AS depth FROM t JOIN bus_message m ON m.seq = t.seq
               WHERE m.to_session IS NULL OR m.seq = ?
               ORDER BY m.seq LIMIT ?""",
            (seq, MAX_THREAD_DEPTH, seq, lim)).fetchall()
        by_msg, _ = _refs_bulk(cur, [r["seq"] for r in rows], [])
    msgs = []
    for r in rows:
        m = {"seq": r["seq"], "depth": r["depth"], "room": r["room"], "sender": r["sender"],
             "kind": r["kind"], "body": r["body"], "data": _jloads(r["data"], {}),
             "reply_to": r["reply_to"], "created_at": r["created_at"]}
        if by_msg.get(r["seq"]):
            m["refs"] = by_msg[r["seq"]]
        msgs.append(m)
    return {"seq": seq, "room": root["room"], "messages": msgs, "count": len(msgs),
            "replies": max(0, len(msgs) - 1), "truncated": len(msgs) == lim,
            "max_depth": MAX_THREAD_DEPTH}


def peek(db: Database, session_id: str, *, after: Optional[int] = None, limit: int = 50,
         rooms_filter: Optional[list[str]] = None, include_self: bool = False) -> dict:
    """Look without consuming. The cursor is NOT advanced.

    This is what the long-poll sidecar uses. A watcher that saw a message and then died must not
    have consumed it on behalf of the agent it was trying to wake — only a reader that actually
    read advances the cursor.
    """
    with db.read() as cur:
        s = _live(cur, session_id)
        at = s["cursor"] if after is None else after
        msgs = _fetch(cur, session_id, at, max(1, min(limit, 500)), rooms_filter, include_self)
    return {"session_id": session_id, "cursor": at, "messages": msgs, "count": len(msgs),
            "head": msgs[-1]["seq"] if msgs else at, "consumed": False}


def poll(db: Database, session_id: str, *, after: Optional[int] = None, limit: int = 50,
         rooms_filter: Optional[list[str]] = None, include_self: bool = False,
         advance: bool = True) -> dict:
    """Read new messages and advance the cursor. Delivery is at-least-once: handle idempotently."""
    _maybe_reap(db)
    lim = max(1, min(limit, 500))
    with db.write_light() as cur:
        s = _live(cur, session_id)
        at = s["cursor"] if after is None else after
        msgs = _fetch(cur, session_id, at, lim, rooms_filter, include_self)
        truncated = len(msgs) == lim
        if truncated:
            head = msgs[-1]["seq"]
        else:
            # Nothing visible is left, so record that we are caught up to the CURRENT HEAD, not
            # merely to the last message we happened to see. Otherwise a poll that returned
            # nothing leaves the cursor stale, and a later bus_join makes previously-invisible
            # room traffic retroactively deliverable — a join would dump the room's backlog.
            # Safe to read MAX(seq) here: write_light holds BEGIN IMMEDIATE, so no writer can
            # slip a message in between the SELECT above and this one.
            head = max(at, cur.execute(
                "SELECT COALESCE(MAX(seq),0) FROM bus_message").fetchone()[0])
        now = now_iso()
        # Draining IS a heartbeat. ping() used to be the only writer of expires_at, so a session
        # could talk to us continuously and still be reaped on a deadline it had no other way to
        # push back — and because a watcher's ping travels the same connection as its poll, one
        # transport outage longer than the TTL expired every session on the bus at once, which is
        # indistinguishable from every agent crashing simultaneously. Extend by the session's own
        # ttl so an agent that is demonstrably here stays in the directory.
        alive = _ts(s["ttl"])
        if advance:
            # Only ever move forward: an explicit `after` in the past must not rewind a cursor
            # another reader already advanced.
            cur.execute("UPDATE bus_session SET cursor=MAX(cursor,?), last_seen=?, expires_at=? "
                        "WHERE session_id=?", (head, now, alive, session_id))
        else:
            cur.execute("UPDATE bus_session SET last_seen=?, expires_at=? WHERE session_id=?",
                        (now, alive, session_id))
    return {"session_id": session_id, "messages": msgs, "count": len(msgs),
            "next_cursor": head, "consumed": bool(advance and msgs), "has_more": truncated}


def history(db: Database, room: str, *, limit: int = 50, before: Optional[int] = None) -> dict:
    """A room's recent broadcasts, oldest-first, independent of any cursor.

    Joining a room deliberately does NOT replay its backlog into your poll (that would dump a
    week of chatter into an agent's context on join), so this is how you catch up on purpose.
    Direct messages are never included — they are not part of a room's public record.
    """
    lim = max(1, min(limit, 200))
    with db.read() as cur:
        q = "SELECT * FROM bus_message WHERE room=? AND to_session IS NULL"
        params: list = [room]
        if before is not None:
            q += " AND seq < ?"
            params.append(int(before))
        rows = cur.execute(q + " ORDER BY seq DESC LIMIT ?", [*params, lim]).fetchall()
        by_msg, by_req = _refs_bulk(cur, [r["seq"] for r in rows],
                                    [r["request_id"] for r in rows])
        counts = _reply_counts(cur, [r["seq"] for r in rows])
    msgs = []
    for r in reversed(rows):
        m = {"seq": r["seq"], "room": r["room"], "sender": r["sender"], "kind": r["kind"],
             "body": r["body"], "data": _jloads(r["data"], {}), "request_id": r["request_id"],
             "reply_to": r["reply_to"], "created_at": r["created_at"]}
        if counts.get(r["seq"]):
            m["reply_count"] = counts[r["seq"]]
        refs = by_msg.get(r["seq"], []) + by_req.get(r["request_id"], [])
        if refs:
            m["refs"] = refs
        msgs.append(m)
    return {"room": room, "messages": msgs, "count": len(msgs),
            "oldest_seq": msgs[0]["seq"] if msgs else None,
            "has_more": len(rows) == lim}


def ack(db: Database, session_id: str, seq: int) -> dict:
    """Move the cursor by hand (after processing a peek, or to skip a backlog)."""
    with db.write_light() as cur:
        _live(cur, session_id)
        cur.execute("UPDATE bus_session SET cursor=MAX(cursor,?) WHERE session_id=?",
                    (int(seq), session_id))
        now = cur.execute("SELECT cursor FROM bus_session WHERE session_id=?",
                          (session_id,)).fetchone()["cursor"]
    return {"session_id": session_id, "cursor": now}


# ── requests: open-claim work distribution ────────────────────────────────────────

def request(db: Database, *, requester: str, task: str, needs: Optional[list[str]] = None,
            to_session: Optional[str] = None, payload: Optional[dict] = None,
            room: Optional[str] = None, refs: Optional[list] = None, lease_sec: int = 300,
            ttl: int = 3600, msg_ttl: int = 604800) -> dict:
    """Ask for work. Every live session matching `needs` is notified; the first to claim wins.

    `refs` points at what the work is ABOUT — graph nodes, a pinned revision, a subject, a
    traversal or a saved search. They are validated here, so a bad pointer fails at request time
    rather than surfacing as a confused worker later.
    """
    if not (task or "").strip():
        raise Invalid("task is required — say what you want done")
    needs = list(needs or [])
    if not needs and not to_session:
        # Not because nobody would be notified — an empty `needs` matches EVERY live session.
        # Because it would then be locked by whoever claimed first, which is almost never what
        # an unscoped ask means.
        raise Invalid("give needs=[capability,...] or to_session. An unscoped request would be "
                      "offered to every live session and then locked by whichever claimed it "
                      "first — for an open question that anyone may answer, bus_post to a room "
                      "instead")
    rid = ulid()
    now = now_iso()
    deadline = _ts(ttl)
    with db.write_light() as cur:
        _live(cur, requester, now)
        if to_session is not None:
            _live(cur, to_session, now)
            targets = [to_session]
        else:
            targets = [s for s in _sessions_matching(cur, needs, now) if s != requester]
        cur.execute(
            "INSERT INTO bus_request(request_id,requester,needs,to_session,task,payload,room,"
            "state,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (rid, requester, canonical_json(needs), to_session, task,
             canonical_json(payload or {}), room, OPEN, now, now, deadline))
        stored = _store_refs(cur, refs, request_id=rid)
        for sid in targets:
            _emit(cur, sender=requester, body=task, room=room or "lobby", to_session=sid,
                  kind="request", request_id=rid, ttl=msg_ttl,
                  data={"needs": needs, "payload": payload or {}, "lease_sec": lease_sec,
                        "claim_with": f"bus_claim(request_id='{rid}')"})
    return {"request_id": rid, "state": OPEN, "needs": needs, "to_session": to_session,
            "notified": len(targets), "notified_sessions": targets,
            "refs": [{"kind": r["kind"], "role": r["role"], "anchor": r["anchor"]}
                     for r in stored],
            "lease_sec": lease_sec, "expires_at": deadline,
            "hint": ("nobody matched — check bus_agents(capability=...) and bus_capabilities()"
                     if not targets else
                     "poll bus_request_get(request_id) for the response")}


def claim(db: Database, request_id: str, session_id: str, *, lease_sec: int = 300) -> dict:
    """Take a request. Exactly one caller wins; everyone else gets won=False and who beat them."""
    now = now_iso()
    lease_until = _ts(lease_sec)
    with db.write_light() as cur:
        _live(cur, session_id, now)
        r = cur.execute("SELECT * FROM bus_request WHERE request_id=?", (request_id,)).fetchone()
        if r is None:
            raise NotFound(f"no bus request {request_id!r}")
        if r["to_session"] is not None and r["to_session"] != session_id:
            raise Invalid(f"request {request_id!r} is addressed to {r['to_session']!r}")
        needs = _jloads(r["needs"], [])
        if needs and not _matches_any(cur, session_id, needs):
            raise Invalid(
                f"session {session_id!r} does not advertise every capability this request needs "
                f"({needs}). Advertise them with bus_ping(capabilities=...) or leave it to "
                f"someone who can.")
        n = cur.execute(
            "UPDATE bus_request SET claimed_by=?, claimed_at=?, lease_expires_at=?, state=?, "
            "updated_at=? WHERE request_id=? AND claimed_by IS NULL AND state=?",
            (session_id, now, lease_until, CLAIMED, now, request_id, OPEN)).rowcount
        cur2 = cur.execute("SELECT claimed_by, state FROM bus_request WHERE request_id=?",
                           (request_id,)).fetchone()
        if n:
            _emit(cur, sender=session_id, body=f"claimed: {r['task'][:120]}",
                  room=r["room"] or "lobby", to_session=r["requester"], kind="claim",
                  request_id=request_id, ttl=604800,
                  data={"claimed_by": session_id, "lease_expires_at": lease_until})
    return {"request_id": request_id, "won": bool(n), "claimed_by": cur2["claimed_by"],
            "state": cur2["state"],
            "lease_expires_at": lease_until if n else None,
            "hint": ("do the work, then bus_respond(request_id, result=...). If you cannot finish "
                     "before the lease expires, re-claim by calling bus_claim again after "
                     "bus_release, or the request reopens for someone else."
                     if n else "someone else got there first — nothing to do")}


def release(db: Database, request_id: str, session_id: str, reason: str = "") -> dict:
    """Give a claim back without failing the request, so another agent can take it."""
    now = now_iso()
    with db.write_light() as cur:
        n = cur.execute(
            "UPDATE bus_request SET claimed_by=NULL, claimed_at=NULL, lease_expires_at=NULL, "
            "state=?, attempts=attempts+1, updated_at=? WHERE request_id=? AND claimed_by=?",
            (OPEN, now, request_id, session_id)).rowcount
        if not n:
            raise Invalid(f"you do not hold the claim on {request_id!r}")
        r = cur.execute("SELECT * FROM bus_request WHERE request_id=?", (request_id,)).fetchone()
        _emit(cur, sender=session_id, body=f"released: {reason or 'no reason given'}",
              room=r["room"] or "lobby", to_session=r["requester"], kind="system",
              request_id=request_id, ttl=604800, data={"released_by": session_id})
    return {"request_id": request_id, "state": OPEN, "released_by": session_id}


def respond(db: Database, request_id: str, session_id: str, *, result: Any = None,
            error: Optional[str] = None, refs: Optional[list] = None,
            msg_ttl: int = 604800) -> dict:
    """Answer a claimed request. Only the claimant may answer.

    Pass `refs` (role='result') to point at what you WROTE rather than inlining it: if the work
    produced durable knowledge it belongs in the graph, and the requester should get its node_id,
    not a copy of it in a message that expires in a week.
    """
    now = now_iso()
    state = FAILED if error else DONE
    with db.write_light() as cur:
        r = cur.execute("SELECT * FROM bus_request WHERE request_id=?", (request_id,)).fetchone()
        if r is None:
            raise NotFound(f"no bus request {request_id!r}")
        if r["claimed_by"] != session_id:
            raise Invalid(f"request {request_id!r} is claimed by {r['claimed_by']!r}, not you — "
                          f"claim it first (bus_claim) or leave it alone")
        if r["state"] not in (CLAIMED, OPEN):
            raise Invalid(f"request {request_id!r} is already {r['state']}")
        cur.execute("UPDATE bus_request SET state=?, result=?, error=?, updated_at=? "
                    "WHERE request_id=?",
                    (state, None if result is None else canonical_json(result), error, now,
                     request_id))
        seq = _emit(cur, sender=session_id,
                    body=(error or "done"), room=r["room"] or "lobby",
                    to_session=r["requester"], kind="response", request_id=request_id,
                    ttl=msg_ttl, data={"state": state, "result": result, "error": error})
        # Default result refs to role='result' so the requester can tell what was PRODUCED from
        # what was merely context on the original request.
        stored = _store_refs(cur, [_with_role(x, "result") for x in (refs or [])],
                             message_seq=seq)
    return {"request_id": request_id, "state": state, "responder": session_id, "seq": seq,
            "refs": [{"kind": x["kind"], "role": x["role"], "anchor": x["anchor"]}
                     for x in stored]}


def _with_role(ref: Any, role: str) -> dict:
    if isinstance(ref, str):
        return {"kind": "node", "id": ref, "role": role}
    return {**ref, "role": ref.get("role", role)}


def request_get(db: Database, request_id: str) -> dict:
    with db.read() as cur:
        r = cur.execute("SELECT * FROM bus_request WHERE request_id=?", (request_id,)).fetchone()
        if r is None:
            raise NotFound(f"no bus request {request_id!r}")
        d = dict(r)
        _, by_req = _refs_bulk(cur, [], [request_id])
        # Result refs live on the response MESSAGE, so surface them here too — otherwise a
        # requester polling request_get would never see what the worker actually produced.
        result_refs, _ = _refs_bulk(cur, [m["seq"] for m in cur.execute(
            "SELECT seq FROM bus_message WHERE request_id=? AND kind='response'",
            (request_id,)).fetchall()], [])
    d["needs"] = _jloads(d["needs"], [])
    d["payload"] = _jloads(d["payload"], {})
    d["result"] = _jloads(d["result"], None) if d["result"] else None
    d["refs"] = by_req.get(request_id, [])
    produced = [ref for refs in result_refs.values() for ref in refs]
    if produced:
        d["produced"] = produced
    return d


def requests(db: Database, *, session_id: Optional[str] = None, state: Optional[str] = None,
             claimable_only: bool = False, limit: int = 50) -> dict:
    """List requests. With claimable_only, just the open ones this session could actually win."""
    _maybe_reap(db)
    now = now_iso()
    with db.read() as cur:
        q = "SELECT * FROM bus_request WHERE 1=1"
        params: list = []
        if state:
            q += " AND state=?"
            params.append(state)
        if claimable_only:
            q += " AND state=? AND expires_at > ? AND (to_session IS NULL OR to_session=?)"
            params += [OPEN, now, session_id]
        rows = cur.execute(q + " ORDER BY created_at DESC LIMIT ?",
                           [*params, max(1, min(limit, 200))]).fetchall()
        out = []
        for r in rows:
            needs = _jloads(r["needs"], [])
            if claimable_only and session_id and needs and not _matches_any(cur, session_id, needs):
                continue
            out.append({"request_id": r["request_id"], "requester": r["requester"],
                        "task": r["task"], "needs": needs, "state": r["state"],
                        "to_session": r["to_session"], "claimed_by": r["claimed_by"],
                        "attempts": r["attempts"], "created_at": r["created_at"],
                        "expires_at": r["expires_at"]})
    return {"requests": out, "count": len(out)}


# ── reaping ───────────────────────────────────────────────────────────────────────

def _release_claims(cur, session_id: str, now: str, why: str) -> int:
    rows = cur.execute("SELECT request_id, requester, room FROM bus_request "
                       "WHERE claimed_by=? AND state=?", (session_id, CLAIMED)).fetchall()
    for r in rows:
        cur.execute(
            "UPDATE bus_request SET claimed_by=NULL, claimed_at=NULL, lease_expires_at=NULL, "
            "state=?, attempts=attempts+1, updated_at=? WHERE request_id=?",
            (OPEN, now, r["request_id"]))
        _emit(cur, sender=SYSTEM, body=f"claim released: {why}", room=r["room"] or "lobby",
              to_session=r["requester"], kind="system", request_id=r["request_id"], ttl=604800,
              data={"reason": why, "was_claimed_by": session_id})
    return len(rows)


def reap(db: Database) -> dict:
    """Expire sessions past their heartbeat, reopen dead leases, delete expired messages.

    Idempotent and safe to run from anywhere. The read paths call it on a timer (see
    _maybe_reap); deploy/maintenance.sh calls it explicitly so a quiet server still cleans up.
    """
    now = now_iso()
    out = {"sessions_expired": 0, "leases_reopened": 0, "requests_expired": 0,
           "messages_deleted": 0}
    with db.write_light() as cur:
        dead = [r["session_id"] for r in cur.execute(
            "SELECT session_id FROM bus_session WHERE ended_at IS NULL AND expires_at <= ?",
            (now,))]
        for sid in dead:
            out["leases_reopened"] += _release_claims(cur, sid, now, "worker session expired")
            cur.execute("UPDATE bus_session SET ended_at=? WHERE session_id=?", (now, sid))
            cur.execute("DELETE FROM bus_capability WHERE session_id=?", (sid,))
            cur.execute("DELETE FROM bus_membership WHERE session_id=?", (sid,))
        out["sessions_expired"] = len(dead)

        stale = cur.execute("SELECT request_id, requester, room, claimed_by FROM bus_request "
                            "WHERE state=? AND lease_expires_at IS NOT NULL AND "
                            "lease_expires_at <= ?", (CLAIMED, now)).fetchall()
        for r in stale:
            cur.execute(
                "UPDATE bus_request SET claimed_by=NULL, claimed_at=NULL, lease_expires_at=NULL, "
                "state=?, attempts=attempts+1, updated_at=? WHERE request_id=?",
                (OPEN, now, r["request_id"]))
            _emit(cur, sender=SYSTEM, body="claim lease expired; request reopened",
                  room=r["room"] or "lobby", to_session=r["requester"], kind="system",
                  request_id=r["request_id"], ttl=604800,
                  data={"was_claimed_by": r["claimed_by"]})
        out["leases_reopened"] += len(stale)

        out["requests_expired"] = cur.execute(
            "UPDATE bus_request SET state=?, updated_at=? WHERE state IN (?,?) AND expires_at <= ?",
            (EXPIRED, now, OPEN, CLAIMED, now)).rowcount
        out["messages_deleted"] = cur.execute(
            "DELETE FROM bus_message WHERE expires_at <= ?", (now,)).rowcount
    return out


def _maybe_reap(db: Database) -> None:
    key = db.path
    last = _last_reap.get(key, 0.0)
    nowm = time.monotonic()
    if nowm - last < _REAP_EVERY:
        return
    _last_reap[key] = nowm
    try:
        reap(db)
    except Exception:            # reaping is opportunistic; never fail a read because of it
        pass


def stats(db: Database) -> dict:
    now = now_iso()
    with db.read() as cur:
        def one(sql, params=()):
            return cur.execute(sql, params).fetchone()[0]
        return {
            "live_sessions": one("SELECT COUNT(*) FROM bus_session WHERE ended_at IS NULL "
                                 "AND expires_at > ?", (now,)),
            "messages": one("SELECT COUNT(*) FROM bus_message"),
            "head_seq": one("SELECT COALESCE(MAX(seq),0) FROM bus_message"),
            "open_requests": one("SELECT COUNT(*) FROM bus_request WHERE state='open'"),
            "claimed_requests": one("SELECT COUNT(*) FROM bus_request WHERE state='claimed'"),
        }
