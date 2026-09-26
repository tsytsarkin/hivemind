"""Project-aware MCP tools for room membership and self-directed management."""
from __future__ import annotations

from . import teams
from .chat import ChatStore, _address, stable_identity
from .db import Invalid
from .envelope import WRITE, current_project, envelope as _envelope
from .identity import Identity, current_identity
from .projects_meta import can_access


def attach(mcp, identities) -> None:
    def _caller(client: str, session_id: str):
        user, device, name, session = stable_identity(current_identity(), client, session_id)
        project = current_project()
        who = (user, device, name)
        ChatStore(project.db).touch(who, session)
        return project, who

    @mcp.tool(annotations=WRITE,
              description="List members and the current manager/revision of a project room.")
    @_envelope
    def team_room_get(room: str, client: str, session_id: str) -> dict:
        project, _ = _caller(client, session_id)
        return {**teams.manager(project.db, room),
                "members": ChatStore(project.db).subscribers(room)}

    @mcp.tool(annotations=WRITE,
              description="Add a known user/device/client agent to an explicitly created project room.")
    @_envelope
    def team_room_member_add(room: str, to_user: str, to_device: str, to_client: str,
                             client: str, session_id: str) -> dict:
        project, actor = _caller(client, session_id)
        target = _address((to_user, to_device, to_client))
        if not identities.has_device(to_user, to_device) or not can_access(
                Identity(to_user, to_device), project.meta):
            raise Invalid("agent user/device does not exist or lacks project access")
        return teams.add_member(project.db, room, target, actor)

    @mcp.tool(annotations=WRITE,
              description="Become your own room's manager; atomically replace its prior manager. "
                          "Use the revision from team_room_get to detect competing promotions.")
    @_envelope
    def team_manager_self_promote(room: str, client: str, session_id: str,
                                  expected_revision: int) -> dict:
        project, who = _caller(client, session_id)
        return teams.promote(project.db, room, who, who,
                             expected_revision=expected_revision)
