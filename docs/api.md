# API surface

## MCP tools (over `/p/<project>/mcp`, 2026-07-28 streamable HTTP, Bearer auth)
Graph: `graph_types`, `graph_search`, `graph_get`, `graph_subjects`, `graph_neighbors`, `graph_upsert`,
`graph_link`, `graph_bulk_load`. Schema: `schema_get`, `schema_propose`, `schema_promote`,
`schema_apply` (returns `{created, unchanged}`; idempotent), `schema_changes`. Artifacts: `artifact_ref`, `artifact_attach`, `artifact_refs`, `artifact_orphans`. Tools:
`tool_catalog`, `tool_publish`, `tool_resolve`, `tool_search`, `tool_link`, `tool_unlink`, `tool_autolink`, `tool_suggest_links`, `tool_yank`. Mini-skills: `skill_catalog`, `skill_search`, `skill_get`, `skill_publish`, `skill_link`, `skill_unlink`, `skill_autolink`, `skill_suggest_links`, `skill_yank`. Traps:
`trap_search`, `trap_get`, `trap_record`, `trap_status`. Bus: `bus_connect`, `bus_send`, `bus_broadcast`, `bus_message`, `bus_peers`,
`bus_disconnect` (see [bus.md](bus.md)). Guide: `guide_get`, `guide_propose`.
Agent bus — *sessions:* `bus_hello`, `bus_ping`, `bus_bye`, `bus_agents`, `bus_capabilities`,
`bus_stats`, `bus_reap`; *rooms and messages:* `bus_rooms`, `bus_join`, `bus_leave`, `bus_post`,
`bus_poll`, `bus_peek`, `bus_ack`, `bus_history`, `bus_thread`; *requests:* `bus_request`,
`bus_claim`, `bus_release`, `bus_respond`, `bus_request_get`, `bus_requests`; *graph refs:*
`bus_resolve`, `bus_node_refs`. Bus rows are ephemeral and TTL-reaped — they carry no `tx`
provenance and never appear in `graph_search`. See [bus.md](bus.md).
`graph_search` searches by text, by type, and by field value (`props_filter={"gated": true}` — typed equality via json_extract, the only way to match booleans/numbers; `null` matches absent): pass `types=[…]`, and an empty query with `types` browses every node of that type (returns `total_of_type`); `graph_types()` lists the types that hold data. It paginates: pass the `next_cursor` from a reply back as `cursor`, and stop when `has_more` is false. Read tools are annotated `readOnlyHint`; all return `{ok, …}` or `{ok:false, error, error_kind}`.

Two reads carry extra, unrequested context so recorded dead-ends can't be missed:
`graph_get` includes a `traps` list for the node (and traps scoped to its subject), and
`graph_search` adds `related_traps` + a `trap_warning` when the query matches one; `graph_get` also returns linked `skills` and `tools`. See
[skills-and-traps.md](skills-and-traps.md).

## REST

Clients are configured with a **project base URL** — `http://<host>:8787/p/<project>` — and every
path below is relative to it. `GET /` on either the server root or a project base returns an index
of these endpoints, so a wrong base URL tells you so instead of 404ing.

**Open (no token)** — so a probe works with only the base URL:

| Path | Returns |
|---|---|
| `GET /healthz` (server root) | `{"ok":true,"projects":[…]}` |
| `GET /p/<project>/healthz` | `{"ok":true,"project":"<name>"}` |
| `GET /` and `GET /p/<project>/` | endpoint index |
| `GET /projects` (server root) | project list |

**Authenticated** (`Authorization: Bearer <token>`; anything else returns `401`):

| Path | Notes |
|---|---|
| `POST /p/<project>/mcp` | the MCP endpoint (streamable HTTP, `2026-07-28`) |
| `GET /guide` · `GET /guide/{section}` | ETag = `guide_version` |
| `GET /skills[?topic=&limit=&offset=]` · `GET /skills/{id}[?constraint=]` | skill catalog |
| `GET /tools[?topic=&limit=&offset=]` · `GET /tools/{id}[?constraint=]` | tool catalog |
| `PUT /blobs/{algo}/{hex}[?attach_to=<version_id>&role=&filename=]` | streaming upload; **`attach_to` attaches in the same request** — an unattached upload is invisible and is garbage-collected |
| `GET`/`HEAD /blobs/{algo}/{hex}` | Range-capable, `Cache-Control: immutable` |
| `WS /p/<project>/bus/ws?ticket=` | agent bus; ticket from `bus_connect` |
| `POST /blobs/batch` | Git-LFS style: `{"objects":[{"oid","size"}]}` → which are missing |
| `GET /bus/wait?session=&wait=&rooms=&after=&limit=&interval=` | **blocks** until the session has a visible message, then returns it *without consuming it*. The one thing that cannot be an MCP tool: a tool call blocking for 25s blocks the agent's turn. A backgrounded watcher sits here and exits when it returns, and on a harness that re-invokes on process exit that exit is the interrupt. `wait` is clamped to `HIVEMIND_BUS_MAX_WAIT` |

## Clients
- `hivemind` CLI (`node/edge/search/neighbors/schema/artifact/tool/skill/trap/guide`; incl.
  `schema apply <pack.json>`, `skill publish|search|get|yank`, `trap record|search|get|status`),
  config from `HIVEMIND_SERVER_URL` + `HIVEMIND_TOKEN`.
- `hivemind.Client` (Python): `.call(tool, args)`, `.upsert/.get/.link/.search/.schema/.guide`,
  `.artifacts.put/get`, `.tool_publish/get/search`.
- `hivemind-admin` (operator, on the server host): `mint-token`, `create-project`, `apply-pack`,
  `promote`, `merge-guide`, `set-guide`, `gc`, `reindex`.

`skill_search` / `tool_search` take `mode=hybrid|lexical|semantic` and report `semantic_backend` (plus `semantic_warning` when embeddings are missing or from another backend). See [skills-and-traps.md](skills-and-traps.md).
