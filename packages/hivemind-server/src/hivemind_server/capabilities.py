"""Self-reported, project-local capabilities of stable agent addresses."""
from __future__ import annotations

import json
import math
import re
import time

from .chat import StableAddress, _address
from .db import Conflict, Database, Invalid


_TAG = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
MAX_TAGS = 64


def define(db: Database, name: str, description: str) -> dict:
    if not isinstance(name, str) or not _TAG.fullmatch(name):
        raise Invalid("capability name must be a lowercase ASCII slug")
    if (not isinstance(description, str) or not description.strip() or
            len(description) > 512 or len(description.encode("utf-8")) > 2048):
        raise Invalid("capability description must be 1–512 characters/2048 bytes")
    t = time.time()
    with db.write("project-capabilities", "define project-wide capability") as tx:
        tx.cur.execute("INSERT INTO project_capability(name,description,created_at,updated_at) "
                       "VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                       "description=excluded.description,updated_at=excluded.updated_at",
                       (name, description.strip(), t, t))
    return {"name": name, "description": description.strip(), "updated_at": t}


def catalog(db: Database, *, after: str | None = None, limit: int = 100) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    if after is not None and (not isinstance(after, str) or not _TAG.fullmatch(after)):
        raise Invalid("invalid capability cursor")
    with db.read() as cur:
        rows = cur.execute("SELECT name,description,updated_at FROM project_capability "
                           "WHERE name>? ORDER BY name LIMIT ?", (after or "", limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"capabilities": [dict(row) for row in rows],
            "next_cursor": rows[-1]["name"] if more else None}


def descriptions(db: Database, tags: list[str]) -> dict[str, str]:
    if not tags:
        return {}
    with db.read() as cur:
        rows = cur.execute("SELECT name,description FROM project_capability WHERE name IN (" +
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


def get_in_transaction(cur, who: StableAddress) -> list[str]:
    row = cur.execute("SELECT tags_json FROM agent_capability WHERE user=? AND device=? "
                      "AND client=?", _address(who)).fetchone()
    return json.loads(row["tags_json"]) if row else []


def get(db: Database, who: StableAddress) -> dict:
    stable = _address(who)
    with db.read() as cur:
        row = cur.execute("SELECT tags_json, updated_at,human_managed FROM agent_capability "
                          "WHERE user=? AND device=? AND client=?", stable).fetchone()
    return {"address": stable, "capabilities": json.loads(row["tags_json"]) if row else [],
            "updated_at": row["updated_at"] if row else None,
            "human_managed": bool(row["human_managed"]) if row else False}


def list_project(db: Database, *, limit: int = 100) -> list[dict]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise Invalid("limit must be 1–100")
    with db.read() as cur:
        rows = cur.execute("SELECT * FROM agent_capability ORDER BY updated_at DESC "
                           "LIMIT ?", (limit,)).fetchall()
    return [{"address": (r["user"], r["device"], r["client"]),
             "capabilities": json.loads(r["tags_json"]), "updated_at": r["updated_at"]}
            for r in rows]


def list_addresses(db: Database, addresses: set[StableAddress]) -> list[dict]:
    """Read only visible roster/room members, not every self-advertiser in a project."""
    if not addresses:
        return []
    result = []
    stable = sorted(_address(who) for who in addresses)
    with db.read() as cur:
        for offset in range(0, len(stable), 100):
            batch = stable[offset:offset + 100]
            keys = ",".join("(?,?,?)" for _ in batch)
            rows = cur.execute("SELECT * FROM agent_capability WHERE (user,device,client) "
                               f"IN ({keys})", tuple(part for who in batch for part in who))
            result.extend({"address": (r["user"], r["device"], r["client"]),
                           "capabilities": json.loads(r["tags_json"]),
                           "updated_at": r["updated_at"]} for r in rows)
    return result


def replace_in_transaction(tx, who: StableAddress, tags: list[str], *,
                           managed_by_ui: bool = False,
                           expected_updated_at: float | None = None,
                           enforce_revision: bool = False) -> dict:
    stable = _address(who)
    normalized = normalize(tags)
    if expected_updated_at is not None and type(expected_updated_at) not in (int, float):
        raise Invalid("expected_updated_at must be a server timestamp")
    t = time.time()
    previous = tx.cur.execute("SELECT tags_json,updated_at,human_managed FROM "
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
    register_in_transaction(tx.cur, normalized, t)
    tx.cur.execute("INSERT INTO agent_capability(user,device,client,tags_json,updated_at,"
                   "human_managed) VALUES(?,?,?,?,?,?) ON CONFLICT(user,device,client) "
                   "DO UPDATE SET tags_json=excluded.tags_json,updated_at=excluded.updated_at,"
                   "human_managed=excluded.human_managed",
                   (*stable, json.dumps(normalized, separators=(",", ":")), t,
                    int(managed_by_ui or bool(previous and previous["human_managed"]))))
    from . import assignments
    assignments.invalidate_for_agent(tx, stable)
    return {"address": stable, "capabilities": normalized, "updated_at": t,
            "human_managed": managed_by_ui or bool(previous and previous["human_managed"])}


def replace(db: Database, who: StableAddress, tags: list[str], *,
            managed_by_ui: bool = False, expected_updated_at: float | None = None,
            enforce_revision: bool = False) -> dict:
    with db.write("agent-capabilities", "replace self-advertised capabilities") as tx:
        return replace_in_transaction(tx, who, tags, managed_by_ui=managed_by_ui,
                                      expected_updated_at=expected_updated_at,
                                      enforce_revision=enforce_revision)


def require_tags(required: list[str], advertised: list[str]) -> None:
    missing = sorted(set(required) - set(advertised))
    if missing:
        raise Invalid("missing required capabilities: " + ", ".join(missing))
