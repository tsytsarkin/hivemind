"""Authenticated agent-only MCP view of durable human instructions."""
from __future__ import annotations

from typing import Optional

from . import instructions
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
              description="Check your durable project-local human instruction queue, even after a disconnect; cursor paginated and not a chat mailbox.")
    @_envelope
    def agent_instruction_inbox(client: str, session_id: str,
                                after_id: Optional[str] = None, limit: int = 100) -> dict:
        project, who = _caller(client, session_id)
        return instructions.inbox(project.db, who, after_id=after_id, limit=limit)

    @mcp.tool(annotations=WRITE,
              description="Acknowledge, start, complete or fail an instruction addressed to your stable agent address. State changes are compare-and-swap; results are agent-reported.")
    @_envelope
    def agent_instruction_update(id: str, expected_state: str, new_state: str,
                                 client: str, session_id: str,
                                 result: Optional[str] = None) -> dict:
        project, who = _caller(client, session_id)
        return instructions.transition(project.db, who, id, expected_state, new_state, result)
