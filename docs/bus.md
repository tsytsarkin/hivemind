# The agent bus

Live messaging between running Hivemind agents, on the same machine or across the LAN/mesh.
Ephemeral by design: it is for coordination, not for knowledge. Anything worth keeping goes in
the graph.

## Why v1 was replaced

The first bus was poll-based, and that is why it was flaky. Four failure modes, all reproduced
against the live server before the rewrite:

1. **No push.** A message was only seen if the receiver *chose* to call `bus_poll`. A Claude Code
   session is turn-based and runs no background loop, so an idle or busy agent never learned a
   message had arrived.
2. **Silent loss.** After a session's TTL lapsed, `bus_poll` hard-errored while `bus_post` to that
   session was still accepted — the sender was told it delivered.
3. **`bus_wait` was not an MCP tool.** It existed only as a REST route and a CLI command, so the
   long-poll escape hatch was unreachable from an agent.
4. **Waiting cost a turn.** Even when reachable, a long poll blocked the agent doing nothing.

## How delivery works now

```
  sender agent                 Hivemind server                receiver agent
  ────────────                 ───────────────                ──────────────
  bus_send(to,body) ─MCP────▶  hub fan-out
                               WS /p/<proj>/bus/ws ─frame──▶  bus-listen.py
                                                              (run by Monitor)
                                                                    │ one line
                                                                    ▼
                                                              notification in the
                                                              agent's conversation
```

The WebSocket **server** is part of the Hivemind server; clients on any machine dial it over the
network like any other client. What is unusual is only *which process* dials it:

> Claude Code's Monitor tool has a built-in `ws` source, but it refuses private addresses —
> measured: `Monitor cannot open a WebSocket to 192.168.x.x: the address is in a private,
> link-local, or cloud-metadata range.` Hivemind lives on a LAN address, so Monitor cannot dial it
> directly. Instead Monitor runs the listener script, and that process holds the WebSocket. A
> subprocess carries no address policy, and the connection is an ordinary cross-machine WS.

## Using it

```python
bus_connect(label="mac-studio")      # once per session -> returns monitor_command
Monitor(command=<monitor_command>, description="hivemind bus", persistent=True)

bus_peers()                          # who is connected
bus_send(to="lab-box", body="census done, 4712 gated entry points")
bus_broadcast(body="pausing writes for a migration")
bus_message("<id>")                  # full text of a clipped message
bus_disconnect(label="mac-studio")
```

From a shell: `hivemind bus connect <label>` · `listen --url …` · `peers` · `send <to> <body>` ·
`broadcast <body>`.

## Design

| Property | Choice | Why |
|---|---|---|
| Presence | the socket | A peer is connected exactly while its WebSocket is open. No TTL, no reaper — the two things that made v1 lose messages. |
| State | in memory | Bus traffic is ephemeral; persisting chat meant provenance rows outliving the messages they described. A restart is a clean slate. |
| Offline messages | bounded queue (100 / 1 h) | A message sent during a brief disconnect survives the reconnect. Bounded, because unbounded retention is how the blob store reached 94 GB. The reference implementation drops these entirely. |
| Long bodies | retained ~1 h, fetched by id | A notification is clipped near 512 characters, so the wire frame cannot be the only copy. The line carries a pointer; `bus_message(id)` returns the rest. |
| Auth | reusable signed listen key (7 d), or a single-use 60 s ticket | The listener connects by URL and cannot set an `Authorization` header, so an authenticated MCP call mints a ticket. The long-lived bearer token never lands in a URL, a shell history or an access log. The default is the **listen key**: HMAC-signed over (label, expiry) with a per-project secret in `<project>/bus_secret`, so verification needs no table and a key keeps working across a server restart — a listener reconnects on its own instead of dying until a human notices. It grants only "join the bus as this label", expires, and is revoked wholesale by deleting the secret. |

## The listener is shipped by the plugin, not the CLI

A machine that installed the Claude Code plugin has the MCP tools and nothing else. `hivemind bus
listen` lives in the separate `hivemind-client` package and needs a third-party `websockets`
dependency on top, so for a plugin-only agent `bus_connect` used to return a command its shell
could not find. It also had no `HIVEMIND_SERVER_URL`/`HIVEMIND_TOKEN` in its environment.

So the plugin carries `skills/hivemind/scripts/bus-listen.py`: a stdlib-only RFC 6455 client, no
dependencies, any `python3`. Loading the skill copies it to `$HOME/.hivemind/bus-listen.py`, and
`bus_connect` returns

```
python3 "$HOME/.hivemind/bus-listen.py" --url ws://<host>/p/<proj>/bus/ws --key hk1....
```

The fixed `$HOME` path is deliberate: a Monitor command runs in a plain shell, and **measured**,
neither `CLAUDE_PLUGIN_ROOT` nor `CLAUDE_SKILL_DIR` is set there — a path built from either would
expand to nothing. The server cannot know the plugin's install path either, so the skill puts the
file somewhere both ends can name.

Because two copies of the rendering rules now exist (the plugin script cannot import the client
package), `test_listener_render_matches_the_client_exactly` pins them together frame by frame.

`monitor_command_cli` is still returned for a machine that does have the CLI installed.
| Identity | stable per label | A reconnect reuses the same peer, so queued mail is not orphaned and peers keep addressing the same name. |
| Displaced sockets | closed with 4409 | A second connection for one identity supersedes the first instead of leaving a ghost peer "online" forever. |

## Safety

Peer messages are instructions from another LLM, not from a trusted system. The skill tells
agents: act on them as on a user request, never as a permission escalation; destructive operations
need explicit intent; only the leading `[hivemind …]` header is authoritative, because the peer
controls everything after it. The listener strips control characters, folds newlines so one frame
stays one line, and removes the header's delimiters from the peer label — so a peer cannot forge a
second header or inject a trailing directive.

## What was deliberately dropped

v1's request/claim/lease work queue (`bus_request`/`bus_claim`/`bus_respond`). It was unrelated to
the flakiness, had four live rows, and a distributed work queue deserves its own design rather
than riding inside a chat transport. Recoverable from git history if it is ever wanted.
