"""Project-user-approved capability definitions and stable agent grants."""
from __future__ import annotations

import json
import math
import re
import time

from .chat import StableAddress, _address
from .db import Conflict, Database, Invalid, NotFound


_TAG = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
MAX_TAGS = 64


def define(db: Database, name: str, description: str, *,
           expected_updated_at: float | None = None,
           enforce_revision: bool = False) -> dict:
    if not isinstance(name, str) or not _TAG.fullmatch(name):
        raise Invalid("capability name must be a lowercase ASCII slug")
    if (not isinstance(description, str) or not description.strip() or
            len(description) > 512 or len(description.encode("utf-8")) > 2048):
        raise Invalid("capability description must be 1–512 characters/2048 bytes")
    if expected_updated_at is not None and type(expected_updated_at) not in (int, float):
        raise Invalid("expected_updated_at must be a server timestamp")
    t = time.time()
    with db.write("project-capabilities", "define project-wide capability") as tx:
        prior = tx.cur.execute("SELECT updated_at,deleted_at FROM project_capability WHERE name=?",
                               (name,)).fetchone()
        revision = prior["updated_at"] if prior and prior["deleted_at"] is None else None
        if enforce_revision and revision != expected_updated_at:
            raise Conflict("project capability changed; refresh and retry")
        last = tx.cur.execute("SELECT MAX(updated_at) FROM project_capability").fetchone()[0]
        if last is not None and t <= last:
            t = math.nextafter(last, math.inf)
        tx.cur.execute("INSERT INTO project_capability(name,description,created_at,updated_at,"
                       "approved,deleted_at) VALUES(?,?,?,?,1,NULL) ON CONFLICT(name) DO UPDATE SET "
                       "description=excluded.description,updated_at=excluded.updated_at,"
                       "approved=1,deleted_at=NULL",
                       (name, description.strip(), t, t))
    return {"name": name, "description": description.strip(), "updated_at": t,
            "approved": True}


def catalog(db: Database, *, after: str | None = None, limit: int = 100) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    if after is not None and (not isinstance(after, str) or not _TAG.fullmatch(after)):
        raise Invalid("invalid capability cursor")
    with db.read() as cur:
        rows = cur.execute("SELECT name,description,updated_at,approved FROM project_capability "
                           "WHERE name>? AND deleted_at IS NULL ORDER BY name LIMIT ?",
                           (after or "", limit + 1)).fetchall()
        revision = cur.execute("SELECT MAX(updated_at) FROM project_capability").fetchone()[0]
    more = len(rows) > limit
    rows = rows[:limit]
    return {"capabilities": [{**dict(row), "approved": bool(row["approved"])} for row in rows],
            "next_cursor": rows[-1]["name"] if more else None, "revision": revision or 0}


def retire(db: Database, name: str, *, expected_updated_at: float | None) -> dict:
    """Retire a definition without losing completed task history or resurrecting legacy tags."""
    if not isinstance(name, str) or not _TAG.fullmatch(name):
        raise Invalid("capability name must be a lowercase ASCII slug")
    if type(expected_updated_at) not in (int, float) or not math.isfinite(expected_updated_at):
        raise Invalid("expected_updated_at must be the current catalog revision")
    with db.write("project-capabilities", "retire project capability") as tx:
        cur = tx.cur
        row = cur.execute("SELECT updated_at FROM project_capability WHERE name=? AND "
                          "deleted_at IS NULL", (name,)).fetchone()
        if row is None:
            raise NotFound("project capability not found")
        if row["updated_at"] != expected_updated_at:
            raise Conflict("project capability changed; refresh and retry")
        task = cur.execute("SELECT t.node_id FROM graph_task t, "
                           "json_each(t.required_capabilities_json) tag WHERE "
                           "tag.value=? AND t.status!='complete' LIMIT 1", (name,)).fetchone()
        if task:
            raise Conflict("capability is required by an open task; update or complete the task first")
        holders = cur.execute("SELECT a.* FROM agent_capability a WHERE EXISTS "
                              "(SELECT 1 FROM json_each(a.tags_json) tag WHERE tag.value=?) "
                              "ORDER BY a.user,a.device,a.client", (name,)).fetchall()
        t = time.time()
        latest = cur.execute("SELECT MAX(updated_at) FROM project_capability").fetchone()[0]
        if t <= latest:
            t = math.nextafter(latest, math.inf)
        cur.execute("UPDATE project_capability SET approved=0,deleted_at=?,updated_at=? "
                    "WHERE name=?", (t, t, name))
        from . import assignments
        addresses = []
        for holder in holders:
            who = (holder["user"], holder["device"], holder["client"])
            tags = [tag for tag in json.loads(holder["tags_json"]) if tag != name]
            approved = [tag for tag in json.loads(holder["approved_tags_json"]) if tag != name]
            stamp = time.time()
            if stamp <= holder["updated_at"]:
                stamp = math.nextafter(holder["updated_at"], math.inf)
            cur.execute("UPDATE agent_capability SET tags_json=?,approved_tags_json=?,updated_at=? "
                        "WHERE user=? AND device=? AND client=?",
                        (json.dumps(tags, separators=(",", ":")),
                         json.dumps(approved, separators=(",", ":")), stamp, *who))
            assignments.invalidate_for_agent(tx, who)
            addresses.append(who)
    return {"name": name, "retired": True, "affected_agents": addresses,
            "updated_at": t}


def descriptions(db: Database, tags: list[str]) -> dict[str, str]:
    if not tags:
        return {}
    with db.read() as cur:
        rows = cur.execute("SELECT name,description FROM project_capability "
                           "WHERE deleted_at IS NULL AND name IN (" +
                           ",".join("?" for _ in tags) + ")", tags).fetchall()
    return {row["name"]: row["description"] for row in rows}


def register_in_transaction(cur, tags: list[str], now: float) -> None:
    for tag in tags:
        cur.execute("INSERT OR IGNORE INTO project_capability(name,description,created_at,updated_at) "
                    "VALUES(?,?,?,?)", (tag, "", now, now))


def normalize(tags: list[str], *, limit: int = MAX_TAGS) -> list[str]:
    if not isinstance(tags, list) or len(tags) > limit:
        raise Invalid(f"capabilities must be a list of at most {limit} tags")
    if any(not isinstance(tag, str) or not _TAG.fullmatch(tag) for tag in tags):
        raise Invalid("capability tags must be lowercase ASCII slugs of at most 64 characters")
    return sorted(set(tags))


def require_existing_approved(cur, tags: list[str]) -> None:
    if not tags:
        return
    rows = cur.execute("SELECT name FROM project_capability WHERE approved=1 AND "
                       "deleted_at IS NULL AND name IN (" +
                       ",".join("?" for _ in tags) + ")", tags).fetchall()
    missing = sorted(set(tags) - {row["name"] for row in rows})
    if missing:
        raise Invalid("add or approve project capability first: " + ", ".join(missing))


def _split_tags(cur, row) -> tuple[list[str], list[str]]:
    if row is None:
        return [], []
    tags = json.loads(row["tags_json"])
    confirmed = set(json.loads(row["approved_tags_json"]))
    if not tags or not confirmed:
        return [], tags
    allowed = {r["name"] for r in cur.execute(
        "SELECT name FROM project_capability WHERE approved=1 AND deleted_at IS NULL "
        "AND name IN (" +
        ",".join("?" for _ in tags) + ")", tags)}
    effective = [tag for tag in tags if tag in confirmed and tag in allowed]
    return effective, [tag for tag in tags if tag not in effective]


def approved_tags_in_transaction(cur, who: StableAddress) -> list[str]:
    row = cur.execute("SELECT tags_json,approved_tags_json FROM agent_capability WHERE user=? "
                      "AND device=? AND client=?", _address(who)).fetchone()
    return _split_tags(cur, row)[0]


def get_in_transaction(cur, who: StableAddress) -> list[str]:
    return approved_tags_in_transaction(cur, who)


def get(db: Database, who: StableAddress) -> dict:
    stable = _address(who)
    with db.read() as cur:
        row = cur.execute("SELECT tags_json,approved_tags_json,updated_at,human_managed FROM agent_capability "
                          "WHERE user=? AND device=? AND client=?", stable).fetchone()
        effective, pending = _split_tags(cur, row)
    return {"address": stable, "capabilities": effective,
            "pending_capabilities": pending,
            "updated_at": row["updated_at"] if row else None,
            "human_managed": bool(row["human_managed"]) if row else False}


def list_project(db: Database, *, limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    with db.read() as cur:
        rows = cur.execute("SELECT * FROM agent_capability ORDER BY updated_at DESC "
                           "LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            approved, pending = _split_tags(cur, row)
            result.append({"address": (row["user"], row["device"], row["client"]),
                           "capabilities": approved, "pending_capabilities": pending,
                           "updated_at": row["updated_at"]})
        return result


def list_addresses(db: Database, addresses: set[StableAddress]) -> list[dict]:
    """Read only visible roster/room members, not every legacy pending grant."""
    if not addresses:
        return []
    result = []
    stable = sorted(_address(who) for who in addresses)
    with db.read() as cur:
        for offset in range(0, len(stable), 100):
            batch = stable[offset:offset + 100]
            keys = ",".join("(?,?,?)" for _ in batch)
            rows = cur.execute("SELECT * FROM agent_capability WHERE (user,device,client) "
                               f"IN ({keys})", tuple(part for who in batch for part in who)).fetchall()
            for row in rows:
                approved, pending = _split_tags(cur, row)
                result.append({"address": (row["user"], row["device"], row["client"]),
                               "capabilities": approved, "pending_capabilities": pending,
                               "updated_at": row["updated_at"]})
    return result


def pending_assignments(db: Database, *, after: str | None = None,
                        limit: int = 100) -> dict:
    """Page legacy grants awaiting project-user confirmation, independent of rooms."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    try:
        start = _address(json.loads(after)) if after is not None else ("", "", "")
    except (TypeError, ValueError, Invalid) as exc:
        raise Invalid("invalid pending capability cursor") from exc
    with db.read() as cur:
        rows = cur.execute(
            "SELECT a.* FROM agent_capability a WHERE (a.user,a.device,a.client)>(?,?,?) "
            "AND EXISTS (SELECT 1 FROM json_each(a.tags_json) AS tag LEFT JOIN "
            "project_capability d ON d.name=tag.value WHERE NOT EXISTS "
            "(SELECT 1 FROM json_each(a.approved_tags_json) approved "
            "WHERE approved.value=tag.value) OR COALESCE(d.approved,0)=0) "
            "ORDER BY a.user,a.device,a.client LIMIT ?", (*start, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        agents = []
        for row in rows:
            effective, pending = _split_tags(cur, row)
            agents.append({"address": (row["user"], row["device"], row["client"]),
                           "capabilities": effective, "pending_capabilities": pending,
                           "updated_at": row["updated_at"]})
    return {"agents": agents,
            "next_cursor": json.dumps(agents[-1]["address"], separators=(",", ":"))
            if more else None}


def replace_in_transaction(tx, who: StableAddress, tags: list[str], *,
                           managed_by_ui: bool = False,
                           expected_updated_at: float | None = None,
                           enforce_revision: bool = False) -> dict:
    stable = _address(who)
    normalized = normalize(tags)
    if expected_updated_at is not None and type(expected_updated_at) not in (int, float):
        raise Invalid("expected_updated_at must be a server timestamp")
    t = time.time()
    previous = tx.cur.execute("SELECT tags_json,approved_tags_json,updated_at,human_managed FROM "
                              "agent_capability WHERE user=? AND device=? AND client=?",
                              stable).fetchone()
    if enforce_revision and (previous["updated_at"] if previous else None) != expected_updated_at:
        raise Conflict("agent capabilities changed; refresh and retry")
    if previous is not None:
        if t <= previous["updated_at"]:
            t = math.nextafter(previous["updated_at"], math.inf)
        if expected_updated_at is not None and expected_updated_at != previous["updated_at"]:
            raise Conflict("capabilities changed; fetch current updated_at")
        if previous["human_managed"] and not managed_by_ui and expected_updated_at is None:
            raise Conflict("human-managed capabilities changed; read "
                           "agent_capabilities_get and pass expected_updated_at")
    elif expected_updated_at is not None:
        raise Conflict("capabilities were not previously advertised; refetch before setting")
    if managed_by_ui:
        require_existing_approved(tx.cur, normalized)
    else:
        register_in_transaction(tx.cur, normalized, t)
    approved = (normalized if managed_by_ui else
                sorted(set(normalized) & set(json.loads(previous["approved_tags_json"])))
                if previous else [])
    tx.cur.execute("INSERT INTO agent_capability(user,device,client,tags_json,updated_at,"
                   "human_managed,approved_tags_json) VALUES(?,?,?,?,?,?,?) "
                   "ON CONFLICT(user,device,client) "
                   "DO UPDATE SET tags_json=excluded.tags_json,updated_at=excluded.updated_at,"
                   "human_managed=excluded.human_managed,"
                   "approved_tags_json=excluded.approved_tags_json",
                   (*stable, json.dumps(normalized, separators=(",", ":")), t,
                    int(managed_by_ui or bool(previous and previous["human_managed"])),
                    json.dumps(approved, separators=(",", ":"))))
    from . import assignments
    assignments.invalidate_for_agent(tx, stable)
    effective, pending = _split_tags(tx.cur, tx.cur.execute(
        "SELECT tags_json,approved_tags_json FROM agent_capability WHERE user=? AND device=? "
        "AND client=?", stable).fetchone())
    return {"address": stable, "capabilities": effective,
            "pending_capabilities": pending, "updated_at": t,
            "human_managed": managed_by_ui or bool(previous and previous["human_managed"])}


def replace(db: Database, who: StableAddress, tags: list[str], *,
            managed_by_ui: bool = False, expected_updated_at: float | None = None,
            enforce_revision: bool = False) -> dict:
    with db.write("agent-capabilities", "replace project agent capabilities") as tx:
        return replace_in_transaction(tx, who, tags, managed_by_ui=managed_by_ui,
                                      expected_updated_at=expected_updated_at,
                                      enforce_revision=enforce_revision)


def require_tags(required: list[str], advertised: list[str]) -> None:
    missing = sorted(set(required) - set(advertised))
    if missing:
        raise Invalid("missing required capabilities: " + ", ".join(missing))
