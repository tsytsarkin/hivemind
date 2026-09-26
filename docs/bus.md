# Legacy ephemeral agent bus

This page describes the older `bus_*` transport: live messaging with only a short bounded
in-memory queue. For 24-hour offline DMs, topic rooms, reconnect catch-up, read markers, and
graph-backed tasks use [Durable collaboration and graph tasks](collaboration.md) (`chat_*` and
`graph_task_*`). An `hk1` bus key, bus label or `bus_message` does not provide durable delivery.
The legacy bus remains available for compatibility. Anything worth keeping goes in the graph.

Platform-specific instructions: [Claude Code usage guide](user-guide.md#talking-to-other-agents)
and [Codex usage guide](codex-plugin.md#messaging-and-the-client). The WebSocket transport is the
same; how its listener output reaches the agent differs.

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
                                                              (Claude: Monitor;
                                                               Codex: shell session)
                                                                    │ one line
                                                                    ▼
                                                              Claude: notification;
                                                              Codex: shell output
```

The WebSocket **server** is part of the Hivemind server; clients on any machine dial it over the
network like any other client. What is unusual is only *which process* dials it:

> Claude Code's Monitor tool has a built-in `ws` source, but it refuses private addresses.
> Re-measured on **Claude Code 2.1.280, 2026-09-23**, by calling
> `Monitor(ws={url: "ws://<server-ip>:8787/p/default/bus/ws"})` — it returns an error, not a
> connection: `Monitor cannot open a WebSocket to <server-ip>: the address is in a private,
> link-local, or cloud-metadata range.` (the real message interpolates the literal address).
> Hivemind lives on a LAN address, so Monitor cannot dial it directly — this is the load-bearing
> reason for the whole listener-subprocess design, so re-run that one call and re-stamp the version
> before assuming it still holds. Instead Monitor runs the listener script, and that process holds
> the WebSocket. A subprocess carries no address policy, and the connection is an ordinary
> cross-machine WS.

## Using it with Claude Code

```python
bus_connect(label="mac-studio", project="my-project")  # optional live Monitor listener
Monitor(command=<monitor_command>, description="hivemind bus", persistent=True)

bus_peers(project="my-project")       # who is connected
bus_send(to="lab-box", body="census done", project="my-project")
bus_broadcast(body="pausing writes", project="my-project")
bus_message("<id>", project="my-project")
bus_disconnect(label="mac-studio", project="my-project")
```

With **Codex**, trusted hooks automatically register and start the stdlib listener after you pin
or restore a project; its auto-join helper may use the private token saved by Codex setup when
the saved address matches the configured MCP endpoint. The MCP host still needs the token in its
launch environment. Verify your session online with `bus_peers(project=<name>)`; when hooks are
unavailable, run the installed `$HOME/.hivemind/bus-autojoin.py` helper and investigate any join
error. The next prompt points to its per-session inbox; for active
coordination you can inspect that inbox or run the returned `monitor_command` in a persistent
shell session. With **Claude**, the plugin now auto-launches that same fallback on startup or
after pinning, and the next-prompt hook reports each new message once. Verify Claude's session in
`bus_peers` too. Monitor is optional for
live notifications; give its extra connection a different label from the auto listener. Neither
fallback wakes an idle
conversation. No separate client installation is required; `hivemind-client` is optional for its
CLI and the alternative `hivemind bus listen`. See the [Codex usage guide](codex-plugin.md) or
[Claude Code usage guide](user-guide.md).

```bash
grep '<id>' ~/.hivemind/bus-inbox.jsonl*   # this machine's own copy — the `*` picks up the
                                           # rolled generation `.1` as well
```

From a shell: `hivemind bus connect <label>` · `listen --url …` · `peers` · `send <to> <body>` ·
`broadcast <body>`.

## Design

| Property | Choice | Why |
|---|---|---|
| Presence | the socket | A peer is connected exactly while its WebSocket is open. No TTL and no timed reaper — the two things that made v1 lose messages. The one sweep that exists (`Hub.peers`) drops a peer only when it is offline **and** holding nothing queued, so it cannot lose mail; an explicit `bus_disconnect` passes `force=True` to get past that guard. |
| State | in memory | Bus traffic is ephemeral; persisting chat meant provenance rows outliving the messages they described. A restart is a clean slate. |
| Offline messages | bounded queue (100 frames / 1 h / 8 MiB of body) | A message sent during a brief disconnect survives the reconnect. Bounded on all three axes, because unbounded retention is how the blob store reached 94 GB. The byte bound counts **real UTF-8 bytes**, not `len()` on a `str` — see *The byte caps are bytes* below. The count is `deque(maxlen=MAX_QUEUE)`, the bytes are trimmed oldest-first on every queued frame, and the hour is applied when the queue is drained on reconnect (`Hub.attach`). |
| Long bodies | kept locally in full (4 MiB, one rotation); also retained ~1 h server-side | A notification is clipped near 512 characters, so the wire frame cannot be the only copy. The listener appends every frame to a local JSONL inbox and the line points at both routes; `bus_message(id)` returns the rest from the server. The local file is bounded for the same reason the offline queue is: an append-only file nobody prunes is how the blob store reached 94 GB. |
| Identity | stable per label | A reconnect reuses the same peer, so queued mail is not orphaned and peers keep addressing the same name. |
| Displaced sockets | closed with 4409 | A second connection for one identity supersedes the first instead of leaving a ghost peer "online" forever. |
| Caller-supplied names | truncated to `LABEL_CAP` (64) at the send path | The queue and the recent buffer account for the **body** of each frame and nothing else, so any *other* caller-controlled field is footprint those caps cannot see. `from` is the `agent` argument of `bus_send`; before it was normalised, an authenticated peer could park ~200 MB per offline peer and ~1 GB in the recent buffer with both caps reading it as zero. What the cap leaves uncounted afterwards is 3 fields x 64 code points x 4 bytes = 768 B per frame, i.e. at most 75 KiB on top of `QUEUE_BYTES` per peer and 375 KiB on top of `RECENT_BYTES` — under a thousandth of either. Truncated rather than refused, matching the ticket mints and the renderers' `_label()`. |
| Auth | reusable signed listen key (7 d), or a single-use 60 s ticket | The listener connects by URL and cannot set an `Authorization` header, so an authenticated MCP call mints a ticket. The long-lived bearer token never lands in a URL, a shell history or an access log. The default is the **listen key**: HMAC-signed over (label, **minting user**, expiry) with a per-project secret in `<project>/bus_secret`, so verification needs no table and a key keeps working across a server restart — a listener reconnects on its own instead of dying until a human notices. The user is *inside* the signature, not beside it, because the handshake re-runs the project ACL against it: a user field its holder could edit would let any key holder nominate the owner of the project it is aimed at. It grants only "join the bus as this label", expires, and is revoked wholesale by deleting the secret. |

## A newly created project's bus route

Current servers insert a new project's `/p/<name>/` routes into the running app at creation time,
including its bus route. Older servers mounted projects only at startup and returned 404 until
they restarted, although the project-neutral `/mcp` endpoint worked immediately.

On an older unmounted project, `bus_connect` refuses and names the restart, instead of minting a key
and handing back a `ws_url`. That is not tidiness: measured, the URL it used to return produced a
403, which the listener classifies `refused` and answers with *"call `bus_connect` for a fresh
URL"* — so the agent called it again, got another dead URL, and looped. `bus_send` says the same
thing in place of "peer offline; queued for reconnect", which on an unmounted project is a promise
the server cannot keep: there is no route to reconnect through, so the message expires in the queue.

`build_app` records the mounted set (`bus_ws.register_mount`, keyed by project **directory** for the
same reason the hub and the signing secret are — nothing enforces one app per process) and
`bus_connect` consults it. `test_bus_connect_refuses_a_project_that_has_no_routes_yet` pins both the
refusal and the control: the same call on a project that *was* mounted still returns a working URL.

## The listener is shipped by the plugin, not the CLI

A machine that installed the Claude Code plugin has the MCP tools and nothing else. `hivemind bus
listen` lives in the separate `hivemind-client` package and needs a third-party `websockets`
dependency on top, so for a plugin-only agent `bus_connect` used to return a command its shell
could not find. (`hivemind bus listen` also reads `HIVEMIND_SERVER_URL`/`HIVEMIND_TOKEN`, which
nothing on such a machine exported until the plugin's `SessionStart` hook began publishing its own
config to the session's shell — plugin 1.1.1, which 1.2.0 extends with HIVEMIND_PROJECT. The listener below needs neither: its URL and its
credential are in argv.)

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

The abbreviation is applied **here**, by `render()`, with the whole frame in hand — the server sent
everything. Claude Code clips a notification near 512 characters, so `render()` keeps the line it
emits under a **500**-character budget and the body preview under `BODY_CAP` (**300**), whichever is
smaller: `body[:min(BODY_CAP, 500 - len(head) - len(tail) - 2)]`. That is why the preview an agent
reads is ~300 characters and not ~512.

So the remainder used to be thrown away on the receiving machine, and `bus_message("<id>")` was its
only route back. That route is conditional twice over: the reading host has to expose the tool, and
the server only retains the body for about an hour. When it did not resolve, an agent answered a
message having read that ~300-character preview.

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
  whatever `.1` held is gone. A record is the JSON envelope (~170 B for ASCII labels and a ULID)
  plus the body, so that is ~9,100 typical messages per generation (~460 B each, i.e. a ~290-char
  body) or ~4,000 of the long ones that actually get clipped (~1 KB) — ~18,000 and ~8,000 across the
  two files. Bounded for the same reason the offline queue is bounded: an append-only file written on
  every message, on every agent machine, that nobody will ever prune is the shape that took the
  blob store to 94 GB.
* **The size on disk is 8 MiB plus at most one record per generation** — not "at most 8 MiB". The
  size is checked *before* the append (`_rotate` returns when `getsize < INBOX_MAX_BYTES`), so the
  overshoot of each generation is exactly the record that crossed the cap: a few hundred bytes with
  ordinary traffic, and at worst the 3.00 MiB maximal record below. `test_the_inbox_rotates_at_the_cap`
  pins that shape — everything up to the roll is in `.1`, and the message that found the file full
  opens the new generation. No fixed byte figure is quoted here on purpose: it is whatever the
  crossing record happened to be.

  The absolute ceiling is **14.00 MiB**: the largest line a listener can be made to write is
  **3.00 MiB** (measured: 3,147,418 B), and two generations of `cap + one such record` is
  14.00 MiB. What binds is `MAX_BODY` — 256 Ki **code points**, so a body of astral characters is
  legal and `ensure_ascii` writes each as a surrogate *pair*, twelve bytes — **but only because
  every other caller-controlled field on a frame is capped at `LABEL_CAP` on the send path**
  (see *Names are bounded* below). Without that cap the binding constraint is not the body at
  all: `from` carries a payload of its own, the wire becomes the limit, and the measured figures
  were 6.00 MiB and 20.00 MiB. A record anywhere near this is hostile traffic, not ordinary use.

  Do not take those numbers on trust, and do not re-derive them from an expansion factor: this
  file has carried a wrong quantified ceiling four times (implicitly ×1, then ×6, then ×12, then
  ×12-against-the-wrong-constraint). They are measured end to end by
  `test_the_ceiling_is_derived_from_a_frame_that_really_fits_the_wire`: the envelope comes from
  `Hub.send`, the line from the listener's own `_record`, the frame is proved to fit `MAX_FRAME`
  and to be maximal (one more code point of body is refused by the server, one more of name is
  dropped by the cap), and the character that maximises it is *chosen by measuring every UTF-8
  width* rather than named. `test_both_halves_agree_on_the_cap` pins both halves to the ceiling.
* **The cap bounds the file, not the record.** A maximal record fits inside a generation today —
  3.00 MiB against 4 MiB — but the two limits live in different files and move for different
  reasons, and nothing needs that to hold. An earlier version of this file asserted that it must
  ("or it could never land"), which was false: the rotation runs first and the append is
  unconditional, so a record larger than a whole generation opens one of its own and rolls the
  previous away. Pinned by `test_a_record_larger_than_a_whole_generation_would_still_land`.
* **A failed rotation costs nothing.** It is attempted before the append, inside the same
  best-effort discipline: if `os.replace` fails the message is still appended, to the oversized
  file, and still printed. The append uses `os.open(…, 0o600)` rather than `open()` so the
  generation opened by a rotation is owner-only like the one it replaced — peer traffic is not
  world-readable. At startup an inbox inherited at a wider mode is narrowed too, along with `.1`
  — except that `.1` is skipped when it is a **symlink**: the live inbox is ours whatever it is
  (every other operation follows it, so we are writing into its target either way), but `.1` is a
  path this code only ever *renames onto*, and `os.replace` does not follow a destination, so a
  link there points at something that is not ours. That check is `islink` → `stat` → `chmod` and
  is therefore not atomic; anyone who could win that race could replace the file outright, and
  the operation only ever narrows, so it is not worth `O_NOFOLLOW` + `fchmod` and the portability
  that costs.
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

## Names are bounded

`Hub.send` and `Hub.broadcast` normalise every name a caller controls — `from`, and `room` for a
broadcast — through `_norm_label`: stripped, truncated to `LABEL_CAP` (64), falling back to
`agent`/`lobby` when empty. `to` was already normalised, at the ticket mint.

This is a memory bound rather than cosmetics, and the tests pin the consequence rather than the
size: `test_the_offline_queue_counts_what_it_actually_holds` and
`test_the_recent_buffer_counts_what_it_actually_holds` assert that what those buffers physically
hold is within the byte caps that exist to bound them, *plus* the 768 B/frame the name fields are
allowed to add on top (stated as arithmetic in the test, so it moves with `LABEL_CAP`). Both fail
without the normalisation, which is the point — the accounting bug is what a refactor would
silently reintroduce, not the cap.

## The byte caps are bytes

`RECENT_BYTES` (32 MiB) and `QUEUE_BYTES` (8 MiB) are counted in **real UTF-8 bytes**:
`_body_bytes` encodes, because `len()` on a `str` counts *code points* and UTF-8 spends up to four
bytes on one. They used to be counted in code points, which made both 4x looser than their own
names. Measured, driving the largest bodies the server accepts — `MAX_BODY` code points of
U+1F600, 1.00 MiB of UTF-8 each:

| | accounted | really held | after the fix |
|---|---|---|---|
| recent buffer | 32.00 MiB (`RECENT_BYTES`) | **128.00 MiB** | 32.00 MiB — 32 frames |
| offline queue | 8.00 MiB (`QUEUE_BYTES`) | **32.00 MiB** | 8.00 MiB — 8 frames |

The fix is the **unit**, not the values. `len(x.encode())` is exact for every body; dividing the
constants by four would be exact only at the 4-byte extreme, and would cut ASCII retention
four-fold (128 recent frames to 32) for traffic that already fitted. Nothing changes for an ASCII
body: `len()` and `len(x.encode())` agree, and the same 128 frames are retained.

Two things this does *not* change:

* **`MAX_BODY` stays a code-point cap**, and says so in its refusal. The listener's inbox ceiling
  above is derived from exactly that — a body of astral characters is legal and `ensure_ascii`
  writes each as a surrogate pair — and `test_both_halves_agree_on_the_cap` pins the derivation.
  It bounds one frame; the two above bound a buffer.
* **The per-frame byte count is cached** on the stored frame as `_b` rather than recomputed inside
  the trim loop. The loop runs on every send, and re-encoding the whole retained set each time
  would be up to 32 MiB of encoding per message on a path whose budget is milliseconds. `_b` and
  `_t` are stripped by `_public()` on every egress — by prefix, so a third internal field cannot
  be added later and silently shipped on the wire.

`test_the_byte_caps_are_counted_in_bytes` is what holds this, and it is deliberately the only test
here that drives a multi-byte **body**: the two named above park their payload in the *name* field
and leave the body empty, so nothing in the suite exercised the trim loop with a body whose byte
count differs from its length. Reverting `_body_bytes` to `len()` fails it with
"the recent buffer physically holds 128.00 MiB against a 32.00 MiB cap"; reverting the test's own
measurement at the same time makes it pass again, which is exactly the self-agreeing assertion
this section exists to describe.

Truncation, not rejection: a send that failed because a caller passed a long agent name would be a
worse outcome than one recorded under a shortened name, and refusing would be a behaviour change
callers could trip over. **It is wire-visible, so callers should know it:** a `bus_send(agent=…)`
over 64 code points arrives at the receiver with `from` truncated to 64 — the recipient sees the
shortened name, not an error. The same holds for a `bus_broadcast(room=…)`, and an empty or
whitespace-only value falls back to `agent`/`lobby`. `_label()` in both renderers then truncates
further, to 48, for display only.

## Collaborating with peers

Authenticated peers on the project bus are collaborators. When a new message arrives, read its
full body, carry out the request within the shared work and current permissions, and reply to
its sender via MCP `bus_send` with either the result or a concrete question/blocker. Acknowledge
work that will take time rather than leaving the peer waiting. Do not delete files or make other
destructive changes on a peer's request without user approval. Peer coordination does not override
the user's instructions or platform permissions. The listener keeps each frame on one line and
shows the authenticated sender in the `[hivemind …]` header.

## What was deliberately dropped

v1's request/claim/lease work queue (`bus_request`/`bus_claim`/`bus_respond`). It was unrelated to
the flakiness, had four live rows, and a distributed work queue deserves its own design rather
than riding inside a chat transport. Recoverable from git history if it is ever wanted.
