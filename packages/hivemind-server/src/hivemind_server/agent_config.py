"""Per-project stable agent policy, versioned separately from capability advertisements."""
from __future__ import annotations

import math
import time

from . import capabilities
from .chat import StableAddress, _address
from .db import Conflict, Database, Invalid

DEFAULT_PARALLEL_TASKS = 20


def limit_in_transaction(cur, who: StableAddress) -> int:
    row = cur.execute("SELECT max_parallel_tasks FROM agent_config WHERE user=? AND "
                      "device=? AND client=?", _address(who)).fetchone()
    return row["max_parallel_tasks"] if row else DEFAULT_PARALLEL_TASKS


def get(db: Database, who: StableAddress) -> dict:
    address = _address(who)
    with db.read() as cur:
        row = cur.execute("SELECT * FROM agent_config WHERE user=? AND device=? AND client=?",
                          address).fetchone()
    caps = capabilities.get(db, address)
    return {"address": address,
            "max_parallel_tasks": row["max_parallel_tasks"] if row else DEFAULT_PARALLEL_TASKS,
            "auto_claim_enabled": bool(row["auto_claim_enabled"]) if row else True,
            "updated_at": row["updated_at"] if row else None,
            "human_managed": bool(row["human_managed"]) if row else False,
            "capabilities": caps["capabilities"],
            "capabilities_updated_at": caps["updated_at"],
            "capabilities_human_managed": caps["human_managed"]}


def update(db: Database, who: StableAddress, *, max_parallel_tasks: int,
           auto_claim_enabled: bool, capability_tags: list[str] | None = None,
           expected_updated_at: float | None = None,
           expected_capabilities_updated_at: float | None = None,
           managed_by_ui: bool = False, enforce_revision: bool = False) -> dict:
    address = _address(who)
    if type(max_parallel_tasks) is not int or not 1 <= max_parallel_tasks <= 20:
        raise Invalid("max_parallel_tasks must be an integer from 1 to 20")
    if type(auto_claim_enabled) is not bool:
        raise Invalid("auto_claim_enabled must be true or false")
    if expected_updated_at is not None and type(expected_updated_at) not in (int, float):
        raise Invalid("expected_updated_at must be the server's configuration timestamp")
    if capability_tags is not None:
        capabilities.normalize(capability_tags)
    with db.write("agent-config", "update project agent configuration") as tx:
        prior = tx.cur.execute("SELECT updated_at,human_managed FROM agent_config WHERE "
                               "user=? AND device=? AND client=?", address).fetchone()
        revision = prior["updated_at"] if prior else None
        if enforce_revision and revision != expected_updated_at:
            raise Conflict("agent config changed; refresh and retry")
        if expected_updated_at is not None and revision != expected_updated_at:
            raise Conflict("agent config changed; refresh and retry")
        if prior and prior["human_managed"] and not managed_by_ui and expected_updated_at is None:
            raise Conflict("human-managed config changed; read agent_config_get and pass "
                           "expected_updated_at")
        if capability_tags is not None:
            capabilities.replace_in_transaction(
                tx, address, capability_tags, managed_by_ui=managed_by_ui,
                expected_updated_at=expected_capabilities_updated_at,
                enforce_revision=managed_by_ui)
        t = time.time()
        if revision is not None and t <= revision:
            t = math.nextafter(revision, math.inf)
        tx.cur.execute("INSERT INTO agent_config(user,device,client,max_parallel_tasks,"
                       "auto_claim_enabled,updated_at,human_managed) VALUES(?,?,?,?,?,?,?) "
                       "ON CONFLICT(user,device,client) DO UPDATE SET "
                       "max_parallel_tasks=excluded.max_parallel_tasks,"
                       "auto_claim_enabled=excluded.auto_claim_enabled,"
                       "updated_at=excluded.updated_at,human_managed=excluded.human_managed",
                       (*address, max_parallel_tasks, int(auto_claim_enabled), t,
                        int(managed_by_ui or bool(prior and prior["human_managed"]))))
    return get(db, address)
