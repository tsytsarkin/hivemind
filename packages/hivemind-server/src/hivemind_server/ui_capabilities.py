"""Human-authored capability edits with immediate server enforcement and durable notices."""
from __future__ import annotations

import logging

from . import agent_config, capabilities, ui_chat
from .chat import ChatStore, StableAddress
from .db import Conflict, Invalid
from .ids import ulid
from .identity import Identity
from .projects_meta import can_access

log = logging.getLogger(__name__)


async def replace_and_notify(project, identities, author: Identity,
                             target: StableAddress, raw_tags: list[str],
                             expected_updated_at: float | None) -> dict:
    db = project.db
    tags = capabilities.normalize(raw_tags)
    known = capabilities.descriptions(db, tags)
    missing = set(tags) - set(known)
    if missing:
        raise Invalid("add capability to the project catalog first: " + ", ".join(sorted(missing)))
    original = capabilities.get(db, target)
    if original["updated_at"] != expected_updated_at:
        raise Conflict("agent capabilities changed; refresh and retry")
    previous = original["capabilities"]
    if previous == tags and not original["pending_capabilities"]:
        return {**original, "changed": False,
                "notified_rooms": 0, "notification_warnings": []}
    store = ChatStore.for_project(db, project.dir)
    updated = capabilities.replace(db, target, tags, managed_by_ui=True,
                                   expected_updated_at=expected_updated_at,
                                   enforce_revision=True)
    source = (author.user, author.device, "human")
    detailed = "; ".join(f"{tag} — {known[tag] or 'description not set'}" for tag in tags) or "none"
    body = (f"Capabilities updated by {author.user} in the web console: "
            f"{detailed} (previously: "
            f"{', '.join(previous) if previous else 'none'}). "
            "Refresh agent_capabilities_get for your address before advertising changes or claiming work.")
    key = "capabilities-" + ulid()
    warnings = []
    try:
        dm = store.send("dm", target, source, body, key, sender_origin="human_ui")
        await ui_chat.notify_human_dm(project.dir, target, dm)
    except Exception:
        log.exception("capabilities updated but agent DM could not be stored")
        warnings.append("agent DM could not be stored")
    try:
        with db.read() as cur:
            rooms = [r["name"] for r in cur.execute(
                "SELECT r.name FROM chat_subscription s JOIN chat_room r ON r.room_id=s.room_id "
                "WHERE s.user=? AND s.device=? AND s.client=? ORDER BY r.name", target)]
    except Exception:
        log.exception("capabilities updated but room member lookup failed")
        rooms = []
        warnings.append("room member lookup failed; capability update remains applied")
    posted = 0
    for room in rooms:
        try:
            message = store.send("room", room, source,
                f"{'.'.join(target)} capabilities updated: {detailed}.",
                key, sender_origin="human_ui")
            await ui_chat.notify_human_room(project.dir, db, project.meta, identities,
                                            room, message)
            posted += 1
        except Exception:
            log.exception("capabilities updated but announcement in room %r failed", room)
            warnings.append(f"room {room} announcement could not be stored")
    return {**updated, "changed": True, "notified_rooms": posted,
            "notification_warnings": warnings}


async def define_and_notify(project, identities, author: Identity,
                            name: str, description: str,
                            expected_updated_at: float | None) -> dict:
    db = project.db
    old = capabilities.descriptions(db, [name]).get(name)
    store = ChatStore.for_project(db, project.dir)
    defined = capabilities.define(db, name, description,
                                  expected_updated_at=expected_updated_at,
                                  enforce_revision=True)
    if old == defined["description"]:
        return {**defined, "changed": False, "notified_agents": 0,
                "notification_warnings": []}
    warnings = []
    try:
        with db.read() as cur:
            holders = [(r["user"], r["device"], r["client"]) for r in cur.execute(
                "SELECT user,device,client FROM agent_capability, json_each(agent_capability.tags_json) "
                "WHERE json_each.value=? ORDER BY user,device,client", (name,)).fetchall()]
            rooms = set()
            for holder in holders:
                for row in cur.execute("SELECT r.name FROM chat_subscription s JOIN chat_room r "
                                       "ON r.room_id=s.room_id WHERE s.user=? AND s.device=? AND "
                                       "s.client=?", holder).fetchall():
                    rooms.add(row["name"])
    except Exception:
        log.exception("capability description saved but subscriber lookup failed")
        holders, rooms = [], set()
        warnings.append("subscriber lookup failed; description remains saved")
    source = (author.user, author.device, "human")
    key = "capability-description-" + ulid()
    delivered = 0
    for holder in holders:
        if not identities.has_device(holder[0], holder[1]) or not can_access(
                Identity(holder[0], holder[1]), project.meta):
            continue
        try:
            message = store.send("dm", holder, source,
                f"Project capability {name} is defined as: {defined['description']}. "
                "Refresh agent_capability_catalog and your own agent_capabilities_get.",
                key, sender_origin="human_ui")
            await ui_chat.notify_human_dm(project.dir, holder, message)
            delivered += 1
        except Exception:
            log.exception("capability %s saved but description DM could not be stored", name)
            warnings.append("description DM could not be stored for " + ".".join(holder))
    for room in sorted(rooms):
        try:
            post = store.send("room", room, source,
                              f"Project capability {name} description updated: "
                              f"{defined['description']}.", key, sender_origin="human_ui")
            await ui_chat.notify_human_room(project.dir, db, project.meta, identities,
                                            room, post)
        except Exception:
            log.exception("capability description updated but room announcement failed: %s", room)
            warnings.append("description announcement failed in room " + room)
    return {**defined, "changed": True, "notified_agents": delivered,
            "notification_warnings": warnings}


async def retire_and_notify(project, identities, author: Identity,
                            name: str, expected_updated_at: float) -> dict:
    db = project.db
    retired = capabilities.retire(db, name, expected_updated_at=expected_updated_at)
    store = ChatStore.for_project(db, project.dir)
    source = (author.user, author.device, "human")
    key = "capability-retired-" + ulid()
    warnings = []
    rooms = set()
    try:
        with db.read() as cur:
            for holder in retired["affected_agents"]:
                rows = cur.execute("SELECT r.name FROM chat_subscription s JOIN chat_room r "
                                   "ON r.room_id=s.room_id WHERE s.user=? AND s.device=? "
                                   "AND s.client=?", holder).fetchall()
                rooms.update(row["name"] for row in rows)
            rows = cur.execute("SELECT DISTINCT r.name FROM graph_task t "
                               "JOIN chat_room r ON r.room_id=t.room_id, "
                               "json_each(t.required_capabilities_json) tag "
                               "WHERE tag.value=?", (name,)).fetchall()
            rooms.update(row["name"] for row in rows)
    except Exception:
        log.exception("capability retired but room lookup failed: %s", name)
        warnings.append("room lookup failed; retirement remains applied")
    delivered = 0
    for holder in retired["affected_agents"]:
        if not identities.has_device(holder[0], holder[1]) or not can_access(
                Identity(holder[0], holder[1]), project.meta):
            continue
        try:
            message = store.send("dm", holder, source,
                f"Project capability {name} was retired by {author.user}. "
                "It has been removed from your grants. Refresh agent_capabilities_get "
                "and agent_capability_catalog before taking new work.",
                key, sender_origin="human_ui")
            await ui_chat.notify_human_dm(project.dir, holder, message)
            delivered += 1
        except Exception:
            log.exception("capability retirement DM failed for %s", holder)
            warnings.append("agent DM could not be stored for " + ".".join(holder))
    posted = 0
    for room in sorted(rooms):
        try:
            message = store.send("room", room, source,
                f"Project capability {name} was retired by {author.user}. "
                "Agents should refresh their server-defined capabilities; completed task history "
                "is unchanged.", key, sender_origin="human_ui")
            await ui_chat.notify_human_room(project.dir, db, project.meta, identities,
                                            room, message)
            posted += 1
        except Exception:
            log.exception("capability retirement room notice failed: %s", room)
            warnings.append("room notice could not be stored for " + room)
    return {"name": retired["name"], "retired": True,
            "updated_at": retired["updated_at"],
            "affected_agent_count": len(retired["affected_agents"]),
            "notified_agents": delivered, "notified_rooms": posted,
            "notification_warnings": warnings}


async def update_agent_config(project, identities, author: Identity, target: StableAddress, *,
                              max_parallel_tasks: int, auto_claim_enabled: bool,
                              expected_updated_at: float | None,
                              capability_tags: list[str] | None = None,
                              expected_capabilities_updated_at: float | None = None) -> dict:
    store = ChatStore.for_project(project.db, project.dir)
    before = capabilities.get(project.db, target)["capabilities"]
    if capability_tags is not None:
        missing = set(capabilities.normalize(capability_tags)) - set(
            capabilities.descriptions(project.db, capability_tags))
        if missing:
            raise Invalid("add capability to the project catalog first: " + ", ".join(sorted(missing)))
    saved = agent_config.update(project.db, target, max_parallel_tasks=max_parallel_tasks,
                                auto_claim_enabled=auto_claim_enabled,
                                capability_tags=capability_tags,
                                expected_updated_at=expected_updated_at,
                                expected_capabilities_updated_at=expected_capabilities_updated_at,
                                managed_by_ui=True, enforce_revision=True)
    descriptions = capabilities.descriptions(project.db, saved["capabilities"])
    detailed = "; ".join(f"{tag} — {descriptions.get(tag) or 'description not set'}"
                         for tag in saved["capabilities"]) or "none"
    key = "agent-config-" + ulid()
    source = (author.user, author.device, "human")
    warnings = []
    try:
        dm = store.send("dm", target, source,
                        f"Your project agent config changed: maximum {saved['max_parallel_tasks']} "
                        f"parallel tasks, automatic task pickup "
                        f"{'enabled' if saved['auto_claim_enabled'] else 'disabled'}, "
                        f"capabilities: {detailed}. "
                        "Call agent_config_get and agent_capability_catalog before continuing.",
                        key, sender_origin="human_ui")
        await ui_chat.notify_human_dm(project.dir, target, dm)
    except Exception:
        log.exception("agent config updated but DM could not be stored")
        warnings.append("agent config DM could not be stored")
    if capability_tags is not None and before != saved["capabilities"]:
        try:
            with project.db.read() as cur:
                rooms = [row["name"] for row in cur.execute(
                    "SELECT r.name FROM chat_subscription s JOIN chat_room r ON r.room_id=s.room_id "
                    "WHERE s.user=? AND s.device=? AND s.client=? ORDER BY r.name", target)]
        except Exception:
            log.exception("agent config saved but room lookup failed")
            rooms = []
            warnings.append("room lookup failed; config remains saved")
        for room in rooms:
            try:
                post = store.send("room", room, source,
                    f"{'.'.join(target)} capabilities updated: {detailed}.",
                    key, sender_origin="human_ui")
                await ui_chat.notify_human_room(project.dir, project.db, project.meta,
                                                identities, room, post)
            except Exception:
                log.exception("agent config capability room post failed for %s", room)
                warnings.append("room " + room + " announcement could not be stored")
    return {**saved, "notification_warnings": warnings}
