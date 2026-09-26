"""Authenticated MCP registration of project-local agent capabilities."""
from __future__ import annotations

from . import agent_config as _config, capabilities as _caps
from .chat import ChatStore, _address, stable_identity
from .db import Invalid
from .envelope import WRITE, current_project, envelope as _envelope
from .identity import Identity, current_identity
from .projects_meta import can_access


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
                               capabilities: list[str],
                               expected_updated_at: float | None = None) -> dict:
        project, who = _caller(client, session_id)
        return _caps.replace(project.db, who, capabilities,
                             expected_updated_at=expected_updated_at)

    @mcp.tool(annotations=WRITE,
              description="Read a stable agent's project-local self-reported capabilities.")
    @_envelope
    def agent_capabilities_get(user: str, device: str, agent_client: str,
                               client: str, session_id: str) -> dict:
        project, _ = _caller(client, session_id)
        return _caps.get(project.db, _address((user, device, agent_client)))

    @mcp.tool(annotations=WRITE, description="Page the project-wide server capability catalog, including human-defined descriptions. Legacy self-advertised tags have an empty description until curated.")
    @_envelope
    def agent_capability_catalog(client: str, session_id: str,
                                 limit: int = 100, after: str | None = None) -> dict:
        project, _ = _caller(client, session_id)
        return _caps.catalog(project.db, after=after, limit=limit)

    @mcp.tool(annotations=WRITE, description="Read your or another project agent's configuration: advertised capability tags, maximum parallel task claims and automatic task pickup policy. Supply all three target address components for a peer.")
    @_envelope
    def agent_config_get(client: str, session_id: str, user: str | None = None,
                         device: str | None = None, agent_client: str | None = None) -> dict:
        project, who = _caller(client, session_id)
        if any(value is not None for value in (user, device, agent_client)):
            if any(value is None for value in (user, device, agent_client)):
                raise Invalid("supply user, device and agent_client together")
            who = _address((user, device, agent_client))
            if not can_access(Identity(who[0], who[1]), project.meta):
                raise Invalid("target agent lacks project access")
        return _config.get(project.db, who)

    @mcp.tool(annotations=WRITE, description="Update your own project agent configuration; pass expected_updated_at after a human edits it. Optional capabilities update atomically with task limits and needs expected_capabilities_updated_at for human-managed tags.")
    @_envelope
    def agent_config_update(client: str, session_id: str, max_parallel_tasks: int,
                            auto_claim_enabled: bool, capabilities: list[str] | None = None,
                            expected_updated_at: float | None = None,
                            expected_capabilities_updated_at: float | None = None) -> dict:
        project, who = _caller(client, session_id)
        return _config.update(project.db, who, max_parallel_tasks=max_parallel_tasks,
                              auto_claim_enabled=auto_claim_enabled,
                              capability_tags=capabilities,
                              expected_updated_at=expected_updated_at,
                              expected_capabilities_updated_at=expected_capabilities_updated_at)
