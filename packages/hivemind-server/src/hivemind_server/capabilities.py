"""Self-reported, project-local capabilities of stable agent addresses."""
from __future__ import annotations

import json
import re
import time

from .chat import StableAddress, _address
from .db import Database, Invalid


_TAG = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
MAX_TAGS = 64


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
        row = cur.execute("SELECT tags_json, updated_at FROM agent_capability "
                          "WHERE user=? AND device=? AND client=?", stable).fetchone()
    return {"address": stable, "capabilities": json.loads(row["tags_json"]) if row else [],
            "updated_at": row["updated_at"] if row else None}


def replace(db: Database, who: StableAddress, tags: list[str]) -> dict:
    stable = _address(who)
    normalized = normalize(tags)
    t = time.time()
    with db.write_light() as cur:
        cur.execute("INSERT INTO agent_capability(user,device,client,tags_json,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(user,device,client) DO UPDATE SET "
                    "tags_json=excluded.tags_json,updated_at=excluded.updated_at",
                    (*stable, json.dumps(normalized, separators=(",", ":")), t))
    return {"address": stable, "capabilities": normalized, "updated_at": t}


def require_tags(required: list[str], advertised: list[str]) -> None:
    missing = sorted(set(required) - set(advertised))
    if missing:
        raise Invalid("missing required capabilities: " + ", ".join(missing))
