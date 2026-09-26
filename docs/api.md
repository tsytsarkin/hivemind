# API surface

New in 1.4.0: `chat_*` for 24-hour persistent offline DMs and explicit project topic rooms,
and `graph_task_*` for optional graph-backed tasks with claim leases. All require an explicit
`project`; every call supplies `client` and `session_id` and authenticates sender user/device
from the token. See [Durable collaboration and graph tasks](collaboration.md) for the complete
tool signatures, retention, status transitions and recovery sequence. The older `bus_*` API is
ephemeral and remains supported only for compatibility.

## MCP tools (over `/mcp` or `/p/<project>/mcp`, 2026-07-28 streamable HTTP, Bearer auth)

One MCP server answers for every project, so **which project a call is for is decided per call,
not per connection** (`envelope.with_project`). Every tool below therefore carries an injected
`project` argument on top of its own:

- `project=<name>` names it explicitly. Works on either endpoint.
- Otherwise the project in the URL is used — the mount default, published by the auth middleware
  only *after* its ACL passed. On `/mcp` there is no such default.
- Otherwise the call is **refused**. There is deliberately no fall-back to a configured default
  project: a write that forgot the argument would land in the shared graph with nothing to notice.
  Writes and reads both refuse; only the wording differs (`envelope.resolve_project`).

So a write with no `project` argument is refused **on `/mcp`**, and on `/p/<name>/mcp` it lands in
`<name>` — the URL named it. Every reply from a tool carrying the injected argument echoes the
`project` it acted in, and that echo is the authoritative answer: compare it against what you
intended rather than against what you pinned.

Projects: `project_list` (grouped `shared` / `mine` / `shared_with_me`), `project_create`
(`visibility="private"|"shared"`, `schema="inherit"|"interview"|"bare"`, plus a per-user cap —
`HIVEMIND_MAX_PROJECTS_PER_USER`, default 50, counting every project you own at either visibility),
`project_info`, `project_share`, `project_unshare` (both **owner-only**). These five are *about*
projects rather than *in* one, so no per-call `project` is injected into them: `project_info`,
`project_share` and `project_unshare` take their own **required** `project`, `project_create` names
the new one with `name`, and `project_list` takes no arguments at all.

**A project created by `project_create` is usable immediately on current servers.** Its graph is
available on the neutral `/mcp`, and `build_app` inserts the new `/p/<name>/` REST and WebSocket
routes at creation time. Older deployments mounted only at startup and require a restart before
the new project's `/p/<name>/` routes answer. It appears in `GET /projects` immediately.

Graph: `graph_types`, `graph_search`, `graph_get`, `graph_subjects`, `graph_neighbors`, `graph_upsert`,
`graph_link`, `graph_bulk_load`. Schema: `schema_get`, `schema_propose`, `schema_promote`,
`schema_apply` (returns `{created, unchanged}`; idempotent), `schema_changes`. Artifacts: `artifact_ref`, `artifact_attach`, `artifact_refs`, `artifact_orphans`. Tools:
`tool_catalog`, `tool_publish`, `tool_resolve`, `tool_search`, `tool_link`, `tool_unlink`, `tool_autolink`, `tool_suggest_links`, `tool_yank`. Mini-skills: `skill_catalog`, `skill_search`, `skill_get`, `skill_publish`, `skill_link`, `skill_unlink`, `skill_autolink`, `skill_suggest_links`, `skill_yank`. Traps:
`trap_search`, `trap_get`, `trap_record`, `trap_status`. Bus: `bus_connect`, `bus_send`, `bus_broadcast`, `bus_message`, `bus_peers`,
`bus_disconnect` (see [bus.md](bus.md)). Guide: `guide_get`, `guide_propose`. That list is exactly
the 52 tools the MCP server registers — 28 in `mcp_tools.build_mcp`, 9 from `registry.attach_tools`,
4 more from `registry_tools.attach`, 5 from `project_tools.attach` and 6 from `bus_ws_tools.attach`.

The three digest-taking artifact tools (`artifact_ref`, `artifact_attach`, `artifact_refs`) accept a
full digest **or a unique hex prefix** — 8+ characters, which is what a listing gives you — and echo
the full digest they matched; an unknown, ambiguous or truncated one is an **error**, never an empty
result. And `artifact_refs` returning `[]` means "no `blob_ref` row", **not** "orphaned": a digest recorded
in a node's props or carried by `tool_version.artifact_digest` is a GC root with no `blob_ref` row at
all. `artifact_orphans` is the accounting that covers those roots; `artifact_refs` is not.

Those six `bus_*` tools are the whole bus. It holds **no tables**: presence is the open WebSocket
and messages live in memory, so nothing it carries writes a `tx` row or appears in `graph_search`,
and a restart is a clean slate. (The v1 polling bus — `bus_poll`, `bus_post`, `bus_request` and the
rest — was removed along with its six tables; `Database._DROPPED` drops those from any database that
still has them.) See [bus.md](bus.md).

`graph_search` searches by text, by type, and by field value (`props_filter={"gated": true}` — typed equality via json_extract, the only way to match booleans/numbers; `null` matches absent): pass `types=[…]`, and an empty query with `types` browses every node of that type (returns `total_of_type`); `graph_types()` lists the types that hold data. It paginates: pass the `next_cursor` from a reply back as `cursor`, and stop when `has_more` is false. Read tools are annotated `readOnlyHint`; all return `{ok, …}` or `{ok:false, error, error_kind}`.

What each hit carries is selectable, and **bounded**: by default a 200-character `snippet`;
`fields=["title","status"]` returns just those keys as real `props` plus that hit's `author`;
`props=true` returns every key the same way. Both of those modes clamp — a single node's props over
4,000 characters become a `_prefix` marker naming the real size, a page stops once 40,000 characters
of props have shipped, and `props=true` additionally caps the page at 10 hits. A clamped reply says
`props_clamped` and names which bound fired in `props_clamped_by`, with `has_more`/`next_cursor`
carrying the rest — so a short page is not the end of the results.

`author='<user>'` filters `graph_search`, `skill_search`, `trap_search` and `tool_search` by who
wrote a row — the identity the token named, never the free-form `agent` label. On `graph_search`
and the two registry searches it means *the author of the CURRENT/LATEST version*, not "ever
touched"; for everyone who ever revised a node, read `contributors` from `graph_get`. `author` is
also what `graph_get` reports for the version it returns, alongside `agent_label` (the job label)
and `created_by` (the node's first author). Rows written before authorship existed match
`author='legacy:unknown'`, which also matches a NULL column.

Two reads carry extra, unrequested context so recorded dead-ends can't be missed:
`graph_get` includes a `traps` list for the node (and traps scoped to its subject), and
`graph_search` adds `related_traps` + a `trap_warning` when the query matches one; `graph_get` also returns linked `skills` and `tools`. See
[skills-and-traps.md](skills-and-traps.md).

## REST

Every path below is relative to a **project base** — `http://<host>:8787/p/<project>`. A client
configured with the server root (the plugin's default) composes that prefix itself from the project
it was given: `HIVEMIND_PROJECT` / `--project` for the CLI, the session pin for the live guide. `GET /p/<project>/` returns an index of that project's endpoints, so
a wrong base URL tells you so instead of 404ing — except for a private project, which tells an
unauthorised caller nothing at all (see below). `GET /` on the **server root** is a different, much
shorter index: four links and an explicit note that project names are not listed there.

**The server root carries only four paths**: `GET /`, `GET /healthz`, `GET /projects` and
`POST /mcp`. The MCP app is mounted at the root as well as under every project prefix, so the blob,
guide and skill routes are *registered* there too — but each of them needs a project the root URL
does not name, so the router refuses anything else outside `/p/` with
`404 {"error": "no such endpoint; only /mcp is project-neutral. …"}` rather than trusting each
handler to fail closed (`app.ProjectAuthMiddleware._neutral`). Bytes therefore always move over a
project base URL, never over the root.

**Open (no token)** — so a probe works with only the base URL. None of these names a project the
caller did not already name:

| Path | Returns |
|---|---|
| `GET /healthz` (server root) | `{"ok":true}` — liveness only; it used to list every project |
| `GET /p/<project>/healthz` | `{"ok":true,"project":"<name>"}` — **shared projects only** |
| `GET /` and `GET /p/<project>/` | endpoint index — the project one for **shared projects only** |

A **private** project answers nothing without authorisation, not even its health, because a 200
there would confirm it exists to a caller with no credential: every **HTTP** path under
`/p/<name>/` returns the same `404 {"error":"unknown project or not accessible with this token"}`
as a name that does not exist. Status, body *and* headers are identical by design — see
`app.PROJECT_DENIED`, and `test_a_private_project_is_indistinguishable_from_a_missing_one`, which
compares all three. (The bus WebSocket is not an HTTP path and is not part of that comparison; it
carries its own credential and enforces the same ACL itself — see below.)

**Authenticated** (`Authorization: Bearer <token>`; anything else returns `401`):

| Path | Notes |
|---|---|
| `GET /projects` (server root) | only the projects **you** can reach; needs a server-level identity token (a legacy per-project token gets `401` here and uses its own project base URL instead) |
| `POST /mcp` (server root) | the **project-neutral** MCP endpoint; every call names its own `project`, and one with no project is refused rather than defaulted. Needs a server-level identity — a legacy per-project token gets `401` here, because it is pinned to a project this URL has not named |
| `POST /p/<project>/mcp` | the same MCP server under one project's prefix; the URL is the default for a call that passes no `project` (the pre-1.2.0 plugin shape, still supported) |
| `GET /guide` · `GET /guide/{section}` | ETag = `guide_version` |
| `GET /skills[?topic=&limit=&offset=]` · `GET /skills/{id}[?constraint=]` | skill catalog |
| `GET /tools[?topic=&limit=&offset=]` · `GET /tools/{id}[?constraint=]` | tool catalog |
| `PUT /blobs/{algo}/{hex}[?attach_to=<version_id>&role=&filename=]` | streaming upload; **`attach_to` attaches in the same request** — an unattached upload is invisible and is garbage-collected. `413` as soon as the cap is crossed: on `Content-Length` before a byte is read, and again on the stream (a chunked PUT declares none) |
| `GET`/`HEAD /blobs/{algo}/{hex}` | `Cache-Control: immutable`; honours one `bytes=` `Range` (`206` + `Content-Range`, or `416` carrying the real size). A multi-range or unparseable header is ignored and answered `200` with the whole body — never a `206` holding only the first part |
| `WS /p/<project>/bus/ws?ticket=` or `?key=` | agent bus. Credential is in the query string, not the header (the listener is launched by Monitor, which cannot set one): a single-use `ticket` or a reconnectable `key`, both from `bus_connect`. This is the one path the auth middleware skips, so the endpoint runs the project ACL itself — at the handshake and again on the open socket |
| `POST /blobs/batch` | Git-LFS style: `{"objects":[{"oid","size"}]}` → which are missing |

## Clients
- `hivemind` CLI (`health/node/edge/search/neighbors/schema/artifact/tool/skill/trap/guide/bus`; incl.
  `schema apply <pack.json>`, `skill publish|search|get|yank`, `trap record|search|get|status`),
  config from four environment variables — `HIVEMIND_SERVER_URL`, `HIVEMIND_TOKEN`,
  `HIVEMIND_PROJECT` and `HIVEMIND_AGENT` (`cli._client`). In a Claude Code session the plugin's
  `SessionStart` hook exports the first three for in-session Bash calls: the URL and token from the
  plugin's own config, the project from the session pin, which is where it lives — the plugin config
  holds no project. In Codex and in plain terminals, export them yourself for CLI calls; see the
  [Codex usage guide](codex-plugin.md) and [Claude Code usage guide](user-guide.md).
  The URL may be the server root: `--project <name>` (default `$HIVEMIND_PROJECT`) then names the
  project, riding as a tool argument on `<root>/mcp` and as the `/p/<name>/` prefix on every REST
  path. With no project at all a tool call is refused by the server and a REST path by the client,
  which names the missing flag rather than letting a `404` read as "no such blob". A URL that names
  a project still works and needs no flag; `--project` overrides it. `hivemind health` is the one
  command that needs neither, and it is not reassuring: it resolves `/healthz` from the root either
  way, and that path needs no token, so it prints `{"ok": true}` for a bogus token alike.
- `hivemind.Client` (Python): `.call(tool, args)`, `.upsert/.get/.link/.search/.schema/.guide`,
  `.artifacts.put/get`, `.tool_publish/get/search`.
  `.call` raises `HivemindError` when the tool refuses (`ok:false`), carrying `error_kind` as
  `.kind`; `raise_on_error=False` returns that envelope as data instead (auth/HTTP/JSON-RPC
  failures still raise — there is no reply to return). **Batches go through
  `.call_many([(tool, args), …])`**, which returns one reply per call, in order, so a single
  refused row cannot abandon the rest of the batch. A refusal is the server's own envelope,
  untouched; a call that raised instead is recorded in its place as `{ok:false, error_kind,
  error}`, where `error_kind` is `HivemindError.kind` when it has one, `"error"` when it has none
  (an HTTP-status failure carries no kind), and `"transport"` when no usable reply came back at
  all. The caller's own bug is *not* recorded but raised — an item that is not a `(tool, args)`
  pair, `args` that is not a mapping, an argument that will not serialise — since a batch row
  blaming the network for it is worse than a traceback. `stop_on_error=True` stops after the first
  non-ok reply, however it failed, and still returns it, so the caller can see where the batch
  stopped.
- `hivemind-admin` (operator, on the server host, direct file/DB access — no network):
  `mint-token` (`--user` for a server-level identity, `--client-id` for the legacy per-project
  form), `list-tokens`, `list-projects`, `create-project`, `project-share`/`project-unshare`
  (which act **as the project's owner**, the recovery path for an owner who lost their token, since
  the MCP tools are owner-only), `apply-pack`, `promote`, `list-proposals`, `merge-guide`,
  `set-guide`, `retire-guide`, `orphans`, `gc`, `backfill-authors`, `reindex`, `embed`, `autolink`.
  `gc` and `backfill-authors` report unless given `--yes`. Most act on the global `--project`
  (default `default`), **one project per invocation**, so a sweep over every project is
  `list-projects` and a loop. Three do not: `mint-token --user` writes a server-level identity and
  touches no project, `list-projects` is global by definition, and `project-share`/`project-unshare`
  take the project as a positional argument (the `--project` flag is ignored for them).

`skill_search` / `tool_search` take `mode=hybrid|lexical|semantic` and report `semantic_backend` (plus `semantic_warning` when embeddings are missing or from another backend). See [skills-and-traps.md](skills-and-traps.md).
