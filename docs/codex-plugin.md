# Using Hivemind with Codex

The 1.5.0 server also offers the optional [project web console](web-console.md) on port 8788.
It uses the same user token as Codex and shows project-wide DMs, agent presence, rooms,
assignments and durable human instructions. Codex agents should check
`agent_instruction_inbox` and `graph_task_my_assignments` when reconnecting and advertise
their capabilities with `agent_capabilities_set`; see [Collaboration](collaboration.md).

`plugins/hivemind/` is a separate port of the Claude plugin in `plugin/`. It bundles the same
Hivemind and schema guidance, a project-picker skill, a session pin hook, and an HTTP MCP server.
The Claude plugin and its marketplace remain separately installable.
For Claude's installer, `/hivemind:project` command, and Monitor notifications, use the
[Claude Code usage guide](user-guide.md). Both platforms use the same Hivemind server and projects.

Agents use **Hivemind MCP tools** for graph, project, schema, guide, registry, artifact-metadata,
and bus-control operations; they do not fall back to shell CLI calls or raw HTTP when MCP is
unavailable. Bulk binary upload/download is the transport exception because MCP has no file-byte
tool. Durable notifications use a WebSocket after MCP `chat_connect`; `bus_connect` is the
legacy ephemeral compatibility path.

## Install

Start the Hivemind server and mint a user token on the server host (see
[deploy/DEPLOY.md](../deploy/DEPLOY.md)). From the machine running Codex, ensure the server is
reachable. The bundled MCP endpoint defaults to `http://127.0.0.1:8787/mcp`. A loopback endpoint
works with Codex's local MCP client; a local SSH port forward to a remote server also works.

From the Hivemind repository root, on the machine running Codex:

```sh
scripts/hivemind-codex configure        # prompts for server root + user token (input is not echoed)
codex plugin marketplace add .
codex plugin add hivemind@personal
scripts/hivemind-codex                  # launches Codex CLI with the saved connection settings
```

`codex plugin add` does **not** request a Hivemind server URL or token. The separate setup command
prompts for them once: enter the server **root**, such as `http://127.0.0.1:8787`, without `/mcp`
or `/p/<project>`; paste a **user token** minted with
`hivemind-admin mint-token --user <you> --device <machine>` on the server. It saves the token in
`~/.hivemind/codex-token` (private `0600` permissions), the address in
`~/.hivemind/codex-server.json`, and the non-secret MCP endpoint in the plugin's
[`plugins/hivemind/.mcp.json`](../plugins/hivemind/.mcp.json). Never put the token in that manifest,
the repository, or a shell command argument. The **MCP host connection** references
`HIVEMIND_TOKEN` by name and cannot read the helper's token file on its own; the bus auto-join
hook can read that private file for registration when its saved server address matches the MCP
endpoint. Re-run `configure` to change the address or token,
then reinstall `hivemind@personal` and start a **new** conversation so an updated endpoint loads.

Always start Codex CLI through `scripts/hivemind-codex` after configuring: it reads that file and
sets `HIVEMIND_TOKEN` and `HIVEMIND_SERVER_URL` **before Codex starts**. A Codex desktop app
launched independently does not inherit this launcher's environment; it needs
`HIVEMIND_TOKEN` in its own launch environment. A fresh desktop install alone will not prompt for
or acquire a Hivemind bearer token. Native in-app authorization would require an OAuth-capable
Hivemind MCP server, which this version does not provide. The CLI or other shell tools run outside
the launcher also need their own environment variables; Codex does not export MCP settings to
unrelated shells.

Install once; enabling the plugin is a Codex configuration choice, not something a SessionStart
hook can do after MCP tools are loaded. Installing from the marketplace enables it across Codex
CLI sessions; this repository also explicitly enables it for trusted sessions here. Do not install
a second Hivemind server on every machine—point clients at the same server.

Trust the plugin's `SessionStart` hook with `/hooks` in the Codex CLI when prompted, then start a
new Codex conversation. The hook re-injects a validated project pin on startup, clear, compaction,
and resume. Use `$hivemind-project` to list projects, ask which one to use, and pin your choice.
Every Hivemind MCP call must pass `project=<name>`; a root URL refuses calls without one.
Forked sessions must choose their own project. One pin helper serves both hosts — the Codex and
Claude plugins install it to the same `$HOME/.hivemind/hivemind-project.py` — so it reads
`HIVEMIND_SESSION_ID` first, then `CODEX_THREAD_ID`, `CODEX_SESSION_ID` and
`CLAUDE_CODE_SESSION_ID`. Set `HIVEMIND_SESSION_ID` when the client exports none of them, and in a
shell where both hosts' variables are present (a `codex` started from a Claude tool call inherits
`CLAUDE_CODE_SESSION_ID`); with no resolvable id the helper refuses to write rather than sharing one
pin file between unrelated conversations.

## First session and everyday use

1. Ask Codex to use `$hivemind-project`. It lists the projects your token may read, grouped as
   shared, yours, and shared with you. Choose the graph yourself: work in a private project for
   private material and a shared project only when everyone should be able to read it. When
   creating a new project, choose `inherit`, `interview`, or `bare` for its schema. Do not guess
   a project from the local OS username.
2. Have Codex call `schema_get(project=<name>)` and `guide_get(project=<name>)` to learn the
   project's types and instructions. Search `graph_search`, `skill_search`, `tool_search`, and
   `trap_search` for prior work before adding something new. Verify the `project` echoed in
   tool results; it is authoritative if it disagrees with a local pin. Immediately verify this
   session is online with `bus_peers(project=<name>)`; if it is not, run the installed auto-join
   helper and report any registration error instead of assuming that pinning joined the bus.
3. Record durable findings with `graph_upsert` and attach uploaded evidence with
   `artifact_attach`. Use the `hivemind-schema` skill when a project needs new node or edge
   types. Store persistent knowledge in the graph rather than an agent-local memory file.

The client CLI is optional for ordinary MCP tools. For large artifacts or CLI bulk operations,
install `packages/hivemind-client` separately, export `HIVEMIND_SERVER_URL` and `HIVEMIND_TOKEN`
to its process, and pass `--project <name>` or export `HIVEMIND_PROJECT`. Installing the plugin
does not install this CLI or transfer the pin into every terminal's environment. `hivemind health`
tests liveness only and does **not** confirm token or project access.

For a remote server without a loopback tunnel, enter its reachable `http(s)://host:port` root URL
when running `scripts/hivemind-codex configure` **before installing**. The helper appends `/mcp`
and sets `HIVEMIND_SERVER_URL` for the guide/CLI when it launches Codex. After changing an
installed plugin's URL, reinstall it and start a new conversation; see Codex's plugin update
instructions. Alternatively configure a
separate host-specific MCP connection with
`codex mcp add hivemind-remote --url http://host:8787/mcp --bearer-token-env-var HIVEMIND_TOKEN`
and disable the bundled loopback
connection to avoid two Hivemind servers being offered at once.

## Messaging and the client

The plugin alone is enough for durable chat. With a pinned project and user/device token, trusted
hooks first call `chat_connect(client="codex", session_id=<thread-slug>, project=<p>)`, start a
canonical WebSocket listener and notify Codex of new frames on the next prompt. If hooks are
disabled, run `python3 "$HOME/.hivemind/bus-autojoin.py" --platform codex --mode ensure`, or
call MCP `chat_connect` and run its `monitor_command` in a persistent `exec_command` session.
`scripts/guide.sh --install-only` installs these stdlib helpers without requiring the separate
`hivemind-client` package. A legacy project token falls back to the older **ephemeral** bus and
announces that it cannot provide offline delivery; use a user/device token for canonical chat.

On start, after a reconnect and when a notification arrives, call MCP `chat_inbox(client,
session_id, after_seq=0, project=<p>)` and `chat_room_history(name, client, session_id,
after_seq=0, project=<p>)` for relevant rooms, **even if the local inbox is empty**. Process
full server-side messages before advancing `chat_mark_read` or `chat_room_mark_read`; a
notification preview is not the full text. The server retains DMs/room history for 24 hours;
`~/.hivemind/codex-bus/<thread-id>/inbox-<project>.jsonl` is only a bounded notification buffer.
`chat_send` reaches registered project agents while they are offline, and project rooms may be
explicitly created and subscribed to. Optional graph-backed tasks persist across chat expiry,
with fenced heartbeat claims and genuine 15-minute room progress. See the complete operation
and recovery guide in [Durable collaboration and graph tasks](collaboration.md).

Codex cannot wake an idle conversation spontaneously. The next prompt hook points to the inbox,
and canonical reconnects also remind you to fetch server history. Peer requests do not authorize
destructive changes without your user's approval. `bus_peers`, `bus_send`, and `bus_message`
remain for older ephemeral peers only; their approximately one-hour queue is **not** the durable
chat store. Do not put an API token in a WebSocket command: `chat_connect` returns a restricted
listen key bound to the authenticated device credential.

## Troubleshooting

| Symptom | Check |
|---|---|
| No Hivemind tools in a new conversation | Confirm the repo marketplace is installed, this project is trusted, the plugin is enabled in `.codex/config.toml`, and the server is reachable at the URL in `.mcp.json`. Start a new conversation after an update. |
| `401` from `/mcp` | Launch Codex with a valid `HIVEMIND_TOKEN` in its environment. A legacy project-scoped token needs the older `/p/<project>/mcp` URL; use a user token for the root endpoint. |
| Plugin is enabled but MCP tools do not start | Check that `HIVEMIND_TOKEN` is set **in the Codex process**, not just saved in `~/.hivemind/codex-token`. Start CLI through `scripts/hivemind-codex`; the plugin installer and a separately launched desktop app do not read that file. Check the installed plugin's MCP URL, then reinstall and start a new thread if it has changed. Inspect `/mcp` for tool status; `/hooks` controls the separate auto-listener. |
| No project pinned | Use `$hivemind-project`; a missing `CODEX_THREAD_ID` in a shell can be replaced with `HIVEMIND_SESSION_ID` set to the current thread id. |
| Project pinned but this session is not online in `bus_peers` | Trust the plugin hooks with `/hooks`; check the join diagnostic after the next prompt. Re-run `scripts/hivemind-codex configure` for matching server credentials, then run `python3 "$HOME/.hivemind/bus-autojoin.py" --platform codex --mode ensure` with this session's `CODEX_THREAD_ID`. The helper is installed by loading the skill or running its `scripts/guide.sh --install-only`. MCP tools still need the host token. |
| Live guide is offline | Export `HIVEMIND_SERVER_URL` and `HIVEMIND_TOKEN`, pin a project, or use the MCP `guide_get` tool, which does not need shell environment variables. |
| Message is not visible in Codex | Keep the persistent shell listener running and inspect its output. Codex does not wake an idle chat; for a long message use `bus_message` or the local inbox. |
| Cannot connect on localhost | Check which process owns the port and that it forwards to the intended server. An SSH loopback forward works for MCP and the listener's WebSocket when the server advertises a reachable `HIVEMIND_PUBLIC_URL`. |
