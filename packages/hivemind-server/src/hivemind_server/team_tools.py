"""Project-aware MCP tools for room membership and self-directed management."""
from __future__ import annotations

from . import teams
from .chat import ChatStore, stable_identity
from .envelope import WRITE, current_project, envelope as _envelope
from .identity import current_identity


def attach(mcp) -> None:
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
              description="Become your own room's manager; atomically replace its prior manager. "
                          "Use the revision from team_room_get to detect competing promotions.")
    @_envelope
    def team_manager_self_promote(room: str, client: str, session_id: str,
                                  expected_revision: int) -> dict:
        project, who = _caller(client, session_id)
        return teams.promote(project.db, room, who, who,
                             expected_revision=expected_revision)
