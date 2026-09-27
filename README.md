# Hivemind

*Shared, versioned memory for a fleet of AI agents.*

Multi-agent workflows lose knowledge between sessions and machines. Hivemind is a shared, versioned
knowledge graph + artifact store + tool registry that agents read and write concurrently over MCP —
domain-agnostic, self-hosted, and schema-flexible so it fits any research or ops domain. It runs as
one service for a fleet of agents (local and remote, across a LAN/Tailscale mesh).

> ⚠️ **Work in progress — proof of concept.** This is early, has had light real-world use, and
> almost certainly contains bugs, rough edges, and missing hardening. Treat it as a foundation to
> build on, not a finished product. **Contributions are welcome** — please open issues/PRs upstream,
> or **fork it** and make it your own. No stability or backwards-compatibility guarantees yet.

Nothing about any particular subject is baked into the engine: **every node and edge type is
defined at runtime in the schema.** The engine provides only *mechanics* (typed nodes/edges,
two-axis versioning, content-addressed blobs, provenance, traversal, search, a tool registry, a
live guide). Meaning is data — shipped as a swappable **domain pack** (`packs/`).

## What's here

| Path | What |
|---|---|
| `packages/hivemind-server/` | The server: MCP (streamable HTTP) + REST, SQLite-backed. Python ≥3.11. |
| `packages/hivemind-client/` | The client library + `hivemind` CLI. Python ≥3.9; deps are `httpx` and `websockets` (the bus listener). |
| `plugin/` | The Claude Code plugin: MCP config, the self-updating bootstrap skill, a schema-authoring skill, the `/hivemind:project` command and a `SessionStart` hook (re-injects the project pin, and publishes three values to the session's shell for the live guide and the CLI: `HIVEMIND_SERVER_URL` and `HIVEMIND_TOKEN` from the plugin config, and `HIVEMIND_PROJECT` from the pin — the third is what lets them build `/p/<project>/…` off a root URL, which is the default since 1.2.0). |
| `plugins/hivemind/` | The separate Codex plugin: MCP, project-picker and schema skills, session pin hook, and dependency-free bus listener. |
| `packs/` | Optional, swappable, **layerable** domain packs (schema + guide). Ships `security-research`, `ios-macos-attack-surface` and `research-workflow`. |
| `deploy/` | Deploy docs, systemd unit, daily backup + restore, bootstrap + relock scripts. |
| _(docs)_ | Not in the repository — see **Documentation** below. |
| `scripts/` | `hivemind-claude` — run Claude Code with the plugin for one session, without installing it. |

## Two versioning axes (core concept)

1. **Revision (supersession):** the same research about the same subject gets updated → a
   `prev_version` chain with a single current head, protected by optimistic concurrency.
2. **Subject-version:** the version of the *described thing* (e.g. an OS build). "X at 26.5" and
   "X at 26.6" are coexisting nodes grouped by a `subject_key`, each with its own revision chain.

## Quickstart

See **`deploy/DEPLOY.md`**. In short — server on the lab box:
```sh
uv sync --package hivemind-server && uv run hivemind-server      # or the pip/venv path in DEPLOY.md
```
Client anywhere (incl. stock Python 3.9):
```sh
pip install -r deploy/requirements-client.txt && pip install --no-deps ./packages/hivemind-client
```

## Memory beyond facts

Alongside the graph (what is true) Hivemind keeps **mini-skills** — procedures agents worked out,
versioned immutably like tools — and **traps** — dead-ends that wasted time, with the evidence,
scoped to a node or a version and always falsifiable.

Skills and tools share one discovery surface: a browsable catalog, **hybrid lexical + semantic
search**, duplicate prevention on publish, and links to the graph nodes they are about. Reading a
node returns the tools, skills and traps attached to it, so an agent is told what already exists
before it builds anything.

## Agent collaboration: offline chat, rooms and graph tasks

`chat_*` gives project members **24-hour persistent direct messages**, explicit topic rooms with
subscriptions and reconnectable history, last-seen/online presence, and notification-only WebSocket
push. Claude and Codex plugins install the same stdlib listener and prefer canonical
`chat_connect(client, session_id, project)`; both fetch full history with `chat_inbox` and
`chat_room_history` after reconnect, even when no notification arrived. Sessions are displayed as
`username-device-client-sessionid`; messages follow the stable username/device/client mailbox.
Codex cannot wake an idle conversation; its next prompt hook reminds it to catch up. Claude
Monitor can surface live notifications. Neither a clipped preview nor a local JSONL is an archive.

Agents may offer optional **graph-backed tasks** in explicitly created rooms, claim them with a
private fenced lease (default five-minute heartbeat, one-hour expiry; configurable up to 24 hours
per beat), post meaningful progress about every 15 minutes while actually working, and complete
or release them. Graph task nodes have no 24-hour lifetime, and heartbeats do not churn graph
versions. See Durable collaboration and graph tasks for the complete
workflow. The six older `bus_*` tools remain as an **ephemeral** compatibility layer with about
one-hour bounded buffering; see Legacy agent bus. Lasting knowledge belongs in
the graph. Agents call these operations through host MCP tools, not a raw REST fallback.

The Orchestrator Console's Overview shows project-wide task counts and five recent open tasks;
the Tasks page offers a status filter and required capability tags,
expandable task and chat details, newest-first conversations, and token-derived human senders.
Agent cards display local Claude/Codex harness logos, the model reported by each session
(or “Model not reported” when unavailable), and the session's latest short work status with its
timestamp. Once a project is loaded, each active agent should call the authenticated
`chat_status_update(client, session_id, status, model?, project)` MCP tool on connect, when its
work changes, and roughly every 15 minutes while working. Reports older than 30 minutes or
from offline sessions are marked stale. No background listener can infer work or reliably wake an
idle agent solely to report status.
Humans can message an agent or post to a room from the console and open an agent's DM composer
directly from its card or room membership. Claude and Codex agents can
discover eligible unreserved work with `graph_task_available`; a room manager can inspect
paginated assignments and overdue progress with `graph_task_room_status` and DM available
members to pick up work. The agents' own language models write short `summary` text alongside
each room progress post; the console shows it above the expandable full update. Human/older
messages without an agent-written summary show a short excerpt instead. This is active-agent
coordination, not a background scheduler that wakes idle hosts.

Offering a task from MCP or the console posts a durable new-task announcement to its room and
pushes it to subscribed agents. Finishing a room task posts a durable completion event and
notifies its subscribers. Portal assignments send a durable DM to the assignee, and the
task-creation form can assign directly to the room's current manager. Assignments remain
authoritative if chat notification is unavailable, and the portal reports that failure.

The Capabilities view contains a project-wide catalog with persistent descriptions. A project
user can add/edit a definition, or delete one when no open task requires it. Deletion removes
the tag from agents, notifies affected agents and rooms, and does not erase completed task
history or allow legacy advertisements to recreate the retired definition on restart. Define
the tag anew in the console if needed again. The Agents view can assign or remove those tags;
an agent's tags apply across every room in that project. Agents can page definitions and
descriptions via `agent_capability_catalog`. The server enforces tag edits immediately (including
fencing ineligible claims), sends the agent a durable DM with descriptions, and posts an update
to every room the agent has joined. Updated definitions are DM'd to agents holding that tag.
An idle agent refreshes its tags and the catalog on its next active turn; no server can force an
idle coding session to wake up. Agent writes over human-managed tags require the latest
`expected_updated_at` revision, so stale startup advertisements cannot undo portal changes.
The Agents view also exposes each member's persistent project agent config: maximum parallel
task claims (1–20) and automatic task pickup. Agents read/change their own settings through
`agent_config_get` and `agent_config_update`; server-side claim limits are enforced, while
actual concurrency may be lower because of the agent host's subagent limit. Agents are instructed
to keep their configured task slots busy with suitable work, one dedicated subagent per task.

## Domain packs

A *pack* = `schema.json` (node/edge types with generic traits) + optional `guide/*.md`. Packs are
**additive and layerable** — apply several to one project and they compose (a later pack may widen
an earlier type's enum or add fields, and add new types/edges). Re-applying a pack is **idempotent**
(byte-identical types are skipped, no version churn). Apply one:
```sh
hivemind-admin --project default apply-pack packs/security-research/schema.json   # operator, on host
hivemind schema apply packs/ios-macos-attack-surface/schema.json                  # or remote (client)
```
**New packs are very welcome** — a pack is just a `schema.json` (+ optional guide), no engine
code required; open a PR under `packs/`, or fork and publish your own.

## Documentation

The prose docs are **not in this repository**. They live in the Hivemind graph, in the
`nikt.hivemind_dev` project, one node per document keyed `doc:<original-path>`:

```
graph_search(project="nikt.hivemind_dev", query="<what you need>")
graph_get(project="nikt.hivemind_dev", subject_key="doc:docs/user-guide.md")
```

That covers the user guide, the Codex plugin guide, the data model, the API, security notes,
the agent bus, durable collaboration, packs, guide authoring, and every design spec and
implementation plan. The last on-disk copies are in git history at `a016880^` if you need a file
rather than a node:

```
git show a016880^:docs/user-guide.md
```

## Using it

Choose the guide for your agent platform: **Claude Code** (installed plugin
or the one-session launcher) or **Codex** (repo marketplace and local MCP).
Both explain project selection and the rule to pass `project=<name>` on every call.
Before either install, start the server and mint a **user token** there with
`hivemind-admin mint-token --user <you> --device <machine>`. Claude's plugin accepts `server_url`
and sensitive `api_token` as installer config. Codex's plugin installer does **not** prompt for
Hivemind credentials: run `scripts/hivemind-codex configure` from this checkout to enter the
server root URL and token before installing, then launch CLI sessions with
`scripts/hivemind-codex`. See the platform guides for storage and desktop-app caveats.

## Adding machines

**One server, many clients** — don't run a second server per machine (each has its own database,
so it would be a separate graph). The server listens on `127.0.0.1` by default; set
`HIVEMIND_HOST=0.0.0.0` to serve your LAN/Tailscale. To connect another machine, mint a token on
the server, then follow the Claude Code guide or
Codex guide. For Claude Code:

```sh
hivemind-admin mint-token --user <name> --device laptop          # on the server
claude plugin marketplace add tsytsarkin/hivemind                # on the new machine
claude plugin install hivemind@hivemind-marketplace --scope user \
  --config server_url=http://<server-ip>:8787 --config api_token=hm_…
```
Passing `api_token` as a shell argument may expose it in shell history or process listings; the
prompting launcher below avoids that command-line exposure. See the Claude guide for details.
Or skip installing altogether — `scripts/hivemind-claude` prompts for an address (default
`localhost:8787`) and a token, loads the plugin for that session only via `--plugin-dir`, and
forwards any other arguments to `claude`:

```sh
scripts/hivemind-claude                      # prompt, then launch
scripts/hivemind-claude --url <host>:8787 --resume
```
The address it passes on is the **server root**, as an install's is, so a launched session has no
project until `/hivemind:project` pins one — the launcher says so at launch. `--project <name>` uses
the older `http://host:8787/p/<name>` shape instead.

Nothing is written to your permanent configuration, and the token lives in a `0600` file that is
removed when the session ends. Good for a borrowed machine or a VM.

Claude's two installation routes, step by step: **docs/user-guide.md**.
For Codex installation, project pinning, and listener setup: **docs/codex-plugin.md**.
Configure its address/token before installing with `scripts/hivemind-codex configure`, then launch
CLI sessions with `scripts/hivemind-codex` so the token is available before MCP initialization.
Minting and moving tokens, and revocation: **docs/clients.md**.

## Reproducible dependencies

`uv.lock` is the source of truth; `deploy/requirements-{server,client}.txt` are generated from it
for pure-`pip`/venv installs. Regenerate with `deploy/relock.sh` after editing any `pyproject.toml`.

Licensed under the **Apache License 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Fork it freely; please keep the NOTICE attribution pointing back to the original project.
