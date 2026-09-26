"""Authenticated MCP registration of project-local agent capabilities."""
from __future__ import annotations

from . import capabilities as _caps
from .chat import ChatStore, _address, stable_identity
from .envelope import WRITE, current_project, envelope as _envelope
from .identity import current_identity


def attach(mcp) -> None:
    def _caller(client: str, session_id: str):
        user, device, name, session = stable_identity(current_identity(), client, session_id)
        project = current_project()
        stable = (user, device, name)
        ChatStore(project.db).touch(stable, session)
        return project, stable

    @mcp.tool(annotations=WRITE,
              description="Replace your own self-reported capabilities in this project. "
                          "Task claims and assignments require every tag named by a task.")
    @_envelope
    def agent_capabilities_set(client: str, session_id: str,
                               capabilities: list[str]) -> dict:
        project, who = _caller(client, session_id)
        return _caps.replace(project.db, who, capabilities)

    @mcp.tool(annotations=WRITE,
              description="Read a stable agent's project-local self-reported capabilities.")
    @_envelope
    def agent_capabilities_get(user: str, device: str, agent_client: str,
                               client: str, session_id: str) -> dict:
        project, _ = _caller(client, session_id)
        return _caps.get(project.db, _address((user, device, agent_client)))
