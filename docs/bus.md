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
bus_message("<id>")                  # full text of a clipped message, from the server
bus_disconnect(label="mac-studio")
```

```bash
grep '<id>' ~/.hivemind/bus-inbox.jsonl*   # this machine's own copy — the `*` picks up the
                                           # rolled generation `.1` as well
```

From a shell: `hivemind bus connect <label>` · `listen --url …` · `peers` · `send <to> <body>` ·
`broadcast <body>`.

## Design

| Property | Choice | Why |
|---|---|---|
| Presence | the socket | A peer is connected exactly while its WebSocket is open. No TTL, no reaper — the two things that made v1 lose messages. |
| State | in memory | Bus traffic is ephemeral; persisting chat meant provenance rows outliving the messages they described. A restart is a clean slate. |
| Offline messages | bounded queue (100 / 1 h) | A message sent during a brief disconnect survives the reconnect. Bounded, because unbounded retention is how the blob store reached 94 GB. The reference implementation drops these entirely. |
| Long bodies | kept locally in full (4 MiB, one rotation); also retained ~1 h server-side | A notification is clipped near 512 characters, so the wire frame cannot be the only copy. The listener appends every frame to a local JSONL inbox and the line points at both routes; `bus_message(id)` returns the rest from the server. The local file is bounded for the same reason the offline queue is: an append-only file nobody prunes is how the blob store reached 94 GB. |
| Identity | stable per label | A reconnect reuses the same peer, so queued mail is not orphaned and peers keep addressing the same name. |
| Displaced sockets | closed with 4409 | A second connection for one identity supersedes the first instead of leaving a ghost peer "online" forever. |
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

## The listener keeps what it could only preview

The 512-character clip is applied **here**, by `render()`, with the whole frame in hand — the
server sent everything. So the remainder used to be thrown away on the receiving machine, and
`bus_message("<id>")` was its only route back. That route is conditional twice over: the reading
host has to expose the tool, and the server only retains the body for about an hour. When it did
not resolve, an agent answered a message having read a ~300-character preview.

Every `message` and `broadcast` frame is therefore appended verbatim, as one JSON line, to
`~/.hivemind/bus-inbox.jsonl` before the preview is printed — `--inbox PATH` overrides the
location, on the plugin listener and on `hivemind bus listen` alike. A clipped line then names both
routes, because they fail differently:

```
[hivemind msg=2ZABCDEF from="labbox" chars=9000] <first ~300 chars>… full text: grep 2ZABCDEF ~/.hivemind/bus-inbox.jsonl · or bus_message("2ZABCDEF")
```

Details that are load-bearing:

* **Short messages are recorded too.** Otherwise the local record has holes exactly where the
  conversation was cheap, and no tail is a transcript.
* **Presence, keepalives, the greeting and transport errors are not.** They carry nothing an agent
  would reread.
* **A failed append never costs the printed line**, and the pointer then names only
  `bus_message` — an agent sent to a file with no such line in it reads the preview and answers
  anyway, which is the bug this fixes.
* **One frame is one line by construction**: the JSON is written with `ensure_ascii`, because
  `str.splitlines()` breaks on U+2028/U+2029 and a peer chooses its own body.
* **It has a horizon.** `INBOX_MAX_BYTES` is 4 MiB and there is exactly one rotation: a record
  that finds the file at or over 4 MiB rolls `bus-inbox.jsonl` to `bus-inbox.jsonl.1` first, and
  whatever `.1` held is gone. That is ~9,100 typical messages per generation (~460 B each) or
  ~4,000 of the long ones that actually get clipped (~1 KB) — ~18,000 and ~8,000 across the two
  files. Bounded for the same reason the offline queue is bounded: an append-only file written on
  every message, on every agent machine, that nobody will ever prune is the shape that took the
  blob store to 94 GB.
* **The size on disk is 8 MiB plus at most one record per generation** — not "at most 8 MiB". The
  size is checked *before* the append, so every generation ends one whole record over the cap
  (measured: a `.1` of 4,194,648 B). With ordinary traffic that overshoot is ~1 KB. With
  server-legal maxima it is 3.00 MiB, for a **ceiling of 14.00 MiB** across the two files, because
  `MAX_BODY` is 256 Ki **code points** — so a body of astral characters (emoji) is legal, and
  `ensure_ascii` writes each one as a surrogate *pair*, twelve bytes rather than the six a BMP
  character costs. Such a body is only 1.00 MiB on the wire under UTF-8, well inside `MAX_FRAME`,
  so this is reachable traffic and not a construction.

  Do not take that number on trust, and do not re-derive it from an expansion factor: this file
  has carried a wrong quantified ceiling three times (once implicitly, then ×6, now ×12). It is
  *built and measured* by `test_the_worst_case_record_is_measured_not_assumed`, and
  `test_both_halves_agree_on_the_cap` asserts `INBOX_MAX_BYTES` against that measurement, so the
  floor moves on its own if `MAX_BODY` or the encoding does. The cap is deliberately above the
  worst-case record: below it, a maximal message could never be recorded at all.
* **A failed rotation costs nothing.** It is attempted before the append, inside the same
  best-effort discipline: if `os.replace` fails the message is still appended, to the oversized
  file, and still printed. The append uses `os.open(…, 0o600)` rather than `open()` so the
  generation opened by a rotation is owner-only like the one it replaced — peer traffic is not
  world-readable.
* **A torn write is never vouched for.** `os.write` is `write(2)` and may take less than the whole
  buffer; the loop insists on the rest and reports `False` the moment it cannot finish, so a
  truncated record is never sold to an agent as the full text. The fragment keeps its own line: the
  next writer checks the file and starts a new one, either within this process (`_TORN` marks the
  inbox) or at startup (for a tear an earlier process left). Sealing the line at the moment of the
  tear is *not* the mechanism, because it is unreliable — measured on a full filesystem, a 1-byte
  newline after a torn write landed in 3 of 5 trials and hit `ENOSPC` in the other 2. Verified on a
  2 MB filesystem driven to `ENOSPC` with 40 KB bodies: the torn record is the only unparseable
  line, is *not* named in its own notification, the notification still prints, every earlier line
  still parses, and the first message after space returned lands cleanly on a new line.
* **Two listeners must not share one inbox.** Nothing locks the file, and three things go wrong.
  `O_APPEND` is atomic per *write*, not per line, and after a short write this code finishes the
  line in further writes — so under a filesystem that splits writes, two interleaved appends can
  interleave *within* a line. Both processes can see the file over the cap and both `os.replace`
  it, the second roll overwriting a full `.1` with a near-empty one. And a tear left by one is
  invisible to the other's `_TORN`, so the other can append onto the fragment. One listener per
  inbox; a second on the same machine gets `--inbox <another path>`.
* The inbox is **not** a server archive and not durable knowledge. It is this machine's receipt log;
  anything worth keeping still goes in the graph.

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
