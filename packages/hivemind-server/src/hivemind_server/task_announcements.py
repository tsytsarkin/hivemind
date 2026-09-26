"""Persist a task-created room post after its graph transaction has committed."""
from __future__ import annotations

import logging
from pathlib import Path

from . import graph
from .chat import ChatStore, StableAddress
from .db import Database

log = logging.getLogger(__name__)


def announce_offer(db: Database, project_dir: Path, room: str, task: dict,
                   sender: StableAddress, *, sender_origin: str = "agent",
                   assigned: bool = False) -> dict | None:
    """The task is durable even if chat is at quota; report a failed announcement to the caller."""
    node_id = task["node_id"]
    try:
        props = (task.get("current") or {}).get("props") or {}
        required = task["task"]["required_capabilities"]
        body = (f"New task {'assigned to the room manager' if assigned else 'available'}: "
                f"{props.get('title') or node_id}. Task ID: {node_id}. "
                f"Required capabilities: {', '.join(required) if required else 'none'}.")
        return ChatStore.for_project(db, project_dir).send(
            "room", room, sender, body, "task-offered-" + node_id,
            sender_origin=sender_origin)
    except Exception:
        log.exception("task %s committed but room announcement could not be stored", node_id)
        return None


def announce_assignment(db: Database, project_dir: Path, node_id: str,
                        recipient: StableAddress, sender: StableAddress,
                        revision: int) -> dict | None:
    """A retry of the same assignment revision cannot create a second durable DM."""
    try:
        task = graph.get_node(db, node_id=node_id)
        title = ((task.get("current") or {}).get("props") or {}).get("title") or node_id
        body = f"You were assigned task {title} (ID: {node_id}). Claim it when ready."
        return ChatStore.for_project(db, project_dir).send(
            "dm", recipient, sender, body, f"task-assigned-{node_id}-{revision}",
            sender_origin="human_ui")
    except Exception:
        log.exception("task %s assigned but DM notification could not be stored", node_id)
        return None


def announce_complete(db: Database, project_dir: Path, room: str, node_id: str,
                      sender: StableAddress) -> dict | None:
    """A completed graph task remains complete even if room chat is at quota."""
    try:
        current = (graph.get_node(db, node_id=node_id).get("current") or {}).get("props") or {}
        title = current.get("title") or node_id
        return ChatStore.for_project(db, project_dir).send(
            "room", room, sender, f"Task completed: {title}. Task ID: {node_id}.",
            "task-completed-" + node_id)
    except Exception:
        log.exception("task %s completed but room announcement could not be stored", node_id)
        return None
