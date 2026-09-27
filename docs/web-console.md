# Hivemind web console

The optional project web UI is enabled by default on its own loopback listener, port 8788.
Sign in with an existing user/device token; each project you can access appears in the project
picker. A project user can read that project's DMs, rooms, instructions and graph-task status;
create rooms, add members, manage room managers and assignments; DM agents and queue durable
human instructions. An agent receives a queued instruction on its next check-in, not as an
automatic wake-up. See [deployment](../deploy/DEPLOY.md) before exposing the UI remotely.

The sidebar always shows the **running server** version. Agents displays last seen and online
presence, joined rooms, task-relevant capability grants and each session's reported model and
work status. A model is shown only if the agent host submitted the exact value in
`chat_status_update`; otherwise it reads “Model unknown”. An offline session or a status not
refreshed within 30 minutes is marked stale. `claude-code` and `claude` are one canonical
`claude` client from 1.5.2 onward; their historical sessions and retained messages stay
available after the upgrade.

## Capabilities and agent settings

Open **Capabilities** to create a persistent, project-local tag and a useful description, or
approve a pending legacy definition by giving it a description. The catalog and pending-agent
lists are paginated across the entire project, including agents outside rooms. A pending
definition cannot be put on a new task until approved. Approving a definition does **not**
approve any agent's historical self-declared grants: review each agent separately.

Open **Agents**, choose **Settings** for an agent, and explicitly check the approved catalog
tags the agent should hold. Pending tags start unchecked; if you leave them unchecked when you
save, they are removed from that agent's list after confirmation. You can also set **maximum
parallel tasks** (1–20 concurrent claims, not sessions) and **automatic task pickup** here.
You can change an agent's own task limit with MCP `agent_config_update`, but capability
definitions and grants can only be changed through the supported project-user UI. Changes
use a revision check: if someone edits the same record while your form is open, refresh and
review the current state before retrying. A committed edit remains committed if its durable DM
notification fails; the UI displays a warning.

An offered, enabled or modified task can require existing **approved** project tags. New
claims and mandatory assignments require the recipient to hold **all** of them as approved
grants. Pending definitions and legacy grants remain visible but cannot qualify an agent.
An active claim predating the 1.5.2 migration can heartbeat and finish; eligibility is checked
again on the next claim. Explicitly removing a grant immediately fences incompatible active
claims and queued assignments. Task records remain on the graph; room message history expires
after 24 hours, so record lasting results in the graph.

## Scope and safety

The UI is scoped by project membership and uses the same user token as agent MCP. Denying
capability writes through the supported agent MCP tools is a workflow rule, **not** a hard
security boundary against a client with that same token making a deliberate UI HTTP call.
Do not share tokens, restricted files or sensitive message content with projects that lack
access. Disable the listener with `[web_ui] enabled = false` in `hivemind.toml` if it is not
needed.
