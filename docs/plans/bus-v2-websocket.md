# Plan: replace the polling bus with WebSocket push

> **Historical.** This plan is delivered — the WebSocket bus is what ships. The *Why* below
> describes the v1 polling bus it replaced, so read that section in the past tense; the
> shipped design is documented in [../bus.md](../bus.md).

## Why

The current bus is poll-based and agents report it as flaky. Four failure modes, all reproduced
against the live server on 2026-09-18:

1. **No push.** A message is only ever seen if the receiver *chooses* to call `bus_poll`. A
   Claude Code session is turn-based — it runs no background loop — so an agent that is idle
   (waiting on the user) or busy (mid-turn) never learns a message arrived. This is the root cause.
2. **Silent loss on session expiry.** Sessions expire after `ttl` (default 900 s). After expiry
   `bus_poll` hard-errors (`session … has ended`), while `bus_post` to that session is still
   **accepted** — the sender believes it delivered, the message is never read.
3. **`bus_wait` was not an MCP tool.** It existed only as a REST route and a CLI
   subcommand of `hivemind bus`. Calling it over MCP returned `Unknown tool: bus_wait`. The only long-poll escape hatch is
   unreachable from an agent, so the documented "watcher" pattern depends on a backgrounded CLI
   process and the agent noticing its exit.
4. **Long-poll burns the turn.** Even when reachable, `bus_wait` blocks the agent's turn doing
   nothing — it cannot both wait and work.

## The mechanism that fixes it

Claude Code's **`Monitor` tool has a native `ws` source**:

```js
Monitor({ ws: {url: "ws://…"}, description: "hivemind bus", persistent: true, timeout_ms: … })
```

> "open a WebSocket and stream each incoming text frame as an event. No shell, no polling: the
> server pushes, you get notified."

Each **text frame becomes one notification in the agent's conversation**, `persistent: true` keeps
it armed for the session's lifetime, and socket close ends the watch with the close code surfaced.
This is the same architecture as the reference project
(<https://github.com/yilunzhang/claude-code-inter-session>), which uses WebSocket push + Monitor
notifications and explicitly cites "ms-level delivery latency, no active polling, and no token or
performance cost when there are no messages."

Difference from the reference worth stating: it binds `127.0.0.1` and trusts any local process as
the same Unix user. Ours is **multi-machine over the LAN/mesh**, so it keeps Hivemind's existing
bearer-token model — see Auth below.

## Constraints discovered (these shape the design)

| Constraint | Consequence |
|---|---|
| **`Monitor.ws` refuses private/RFC1918 addresses** — measured: `Monitor cannot open a WebSocket to <private-ip>: the address is in a private, link-local, or cloud-metadata range.` Loopback is allowed; `<server-ip>` is not. | **The ws source cannot reach our LAN server at all.** Agents connect instead through `Monitor(command: "hivemind bus listen")` — a subprocess carries no address policy. The server still speaks WebSocket; our CLI is the WS client and prints one line per frame. |
| `Monitor.ws` accepts only `url` and `protocols` — **no custom headers** | Bearer token cannot be sent as a header. Auth must ride the URL or the subprotocol. |
| **Notifications are clipped at ~512 chars** (empirical: reference-impl issue #2, measured on CC 2.1.126 — 511 delivered, 512 truncated; not in any Anthropic doc) | The printed line must fit a ~500-char budget: short prefix, body capped ~380, full text retrievable by id rather than inlined. |
| `ws` is documented for the Monitor **tool** only, never for plugin `monitors.json` | Do not put a ws monitor in `monitors.json`; the skill calls `Monitor(command: …)`. |
| Plugin monitors are **per-process**, so subagents/teammates each start one (reference impl issue #5: "one bus listener per worker" across 30 workers) | Connect from the skill on demand, not `when: "always"`. |
| Monitors are **rate-limited**; "a firehose will be suppressed and eventually stopped" | The server must filter server-side (per-session/room subscription), never stream everything. |
| Each text frame = one notification; **binary frames become a placeholder** | Frames must be UTF-8 **text** JSON, one event per frame. |
| `MCPServer.custom_route` is HTTP-methods only | The WS route mounts at the **Starlette app level** in `app.py`, not via `custom_route`. |
| `uvicorn[standard]` already ships `websockets 17.0.1` | **No new dependency.** |

## Design

### Server (`bus.py` replaced by `bus_ws.py`)

- **`WS /p/<project>/bus/ws?ticket=<t>`** — the only delivery path. On connect the server
  registers the live connection in memory and streams frames as they are published.
- **In-memory hub, not a table.** Connections are process-local; delivery is direct fan-out. The
  old design persisted every chat message and then reaped it — provenance rows outliving the
  message they described. Presence and delivery are now *connection state*, which is what they
  actually are.
- **Offline queue, bounded.** A short per-recipient ring buffer (default 100 messages / 1 h) so a
  message sent while a session is briefly disconnected is delivered on reconnect. Bounded, so it
  cannot grow into the 94 GB-style leak the blob store hit. Anything durable belongs in the graph,
  not here — that rule is unchanged and gets restated in the skill.
- **Frames are text JSON**, one event per frame:
  ```json
  {"v":1,"type":"message","id":"01M…","from":"mac-studio","to":"labbox",
   "room":null,"body":"…","ts":"2026-09-18T…Z"}
  ```
  `type` ∈ `message` | `broadcast` | `presence` | `hello` | `error`. A `hello` frame on connect
  carries the assigned session id and the current peer list.
- **Heartbeat.** Server-side WS ping every 30 s; a connection that misses two is dropped and its
  presence removed. This replaces the TTL/reaper entirely — liveness is the socket, not a clock.

### Auth (the header constraint)

`Monitor.ws` cannot set headers, so:
- MCP tool **`bus_connect()`** returns a **short-lived single-use ticket** (60 s TTL) plus the
  fully-formed `ws://…?ticket=…` URL to hand to `Monitor`.
- The ticket is minted from the caller's already-authenticated MCP session, so the long-lived
  bearer token never appears in a URL, a shell history, or the server access log.
- Ticket → session binding happens at the WS handshake; the ticket is burned on use.

### Agent-facing MCP tools (small surface — receiving is Monitor's job)

| Tool | Purpose |
|---|---|
| `bus_connect(label?)` | mint ticket + return the `Monitor({ws:…})` snippet to run |
| `bus_send(to, body)` | direct message to one peer |
| `bus_broadcast(body, room?)` | fan-out |
| `bus_peers()` | who is connected right now |
| `bus_disconnect()` | leave cleanly |

That is **5 tools, replacing 24**. Everything the old surface did for *receiving* (`poll`, `peek`,
`ack`, `wait`, cursors, history, reap) disappears, because push removes the need for it.

**Deliberately dropped:** the request/claim/lease work-queue (`bus_request`/`bus_claim`/
`bus_respond`/…). It has 4 live rows, is unrelated to the flakiness being fixed, and a
distributed work queue deserves to be designed on its own rather than smuggled into a chat
transport. Removing it is a real capability loss and is called out here so it is a decision, not
an accident.

### Client + skill

- `hivemind bus send|broadcast|peers` in the CLI for shell use.
- **SKILL.md**: connect once at session start via `Monitor`, then *just work* — messages arrive on
  their own. Plus the reaction policy borrowed from the reference: incoming messages are
  **instructions from a peer agent**, but destructive operations need explicit affirmative
  content and ambiguous requests get a clarifying question first.

### Removal

Delete `bus.py` (1016), `bus_tools.py` (332), `test_bus.py` (791), `test_e2e_bus.py` (327),
`docs/bus.md` (348), the 52 bus lines in `cli.py`, the 37 in `SKILL.md`, and **drop the 6
`bus_*` tables**. Live data discarded: 49 sessions / 97 messages / 4 requests — ephemeral by
design (7-day TTL), so nothing durable is lost. A `DROP TABLE` migration runs on startup, after a
pre-deploy backup.

## Verification

1. **Unit** — hub fan-out, offline queue bound, ticket single-use + expiry, heartbeat eviction.
2. **Two-machine live test** — Mac Studio session connects via `Monitor`, lab-box client sends;
   assert the frame arrives as a notification **while the receiving session is idle**, which is
   precisely what the old bus could not do.
3. **Reconnect** — kill the socket mid-flight, send, reconnect, assert the queued message lands.
4. **Regression** — the other 41 tools and the graph/skills/traps/artifacts surfaces unaffected.
5. **No-firehose** — broadcast storm stays under Monitor's rate limit via server-side filtering.

## Rollout

Backup → remove old bus + drop tables → deploy → restart → verify → bump plugin (**0.10.0**, or
clients keep the old skill and never learn to connect) → refresh plugin.
