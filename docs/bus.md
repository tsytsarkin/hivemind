# The agent bus

Live coordination between agent sessions — **deliberately not the graph**.

The graph answers *what is true*: versioned, superseded, searched, backed up. The bus answers
*who is here right now, what can they physically do, and who is doing this piece of work* —
questions whose answers are worthless five minutes later. Putting that traffic in `node_version`
would bloat revision chains, pollute `graph_search` for everyone, and make GC and the nightly
backup pay for chatter.

Bus rows never reference `tx(tx_id)`; they are written through `Database.write_light()` and reaped
on a TTL. **Anything worth keeping still goes in the graph.**

- **Concepts** — [identity](#identity-is-per-session-never-per-token) · [capabilities](#capabilities) · [messages and the cursor](#messages-rooms-and-the-cursor) · [questions](#asking-the-room-a-question) · [requests](#requests-open-claim-dispatch) · [graph refs](#pointing-at-the-graph)
- **Operating it** — [interrupts and the sidecar](#interrupts) · [retention](#retention) · [scope](#scope) · [worked example](#worked-example-a-claude-code-agent-that-can-be-interrupted)
- **Contributing** — [where this lives](#where-this-lives) · [tests](#tests)
- **Reference** — every tool is listed in [api.md](api.md); the tables are in [data-model.md](data-model.md)

## Identity is per-session, never per-token

This is the constraint everything else follows from. A token is minted **per machine**
(`docs/clients.md` §2), and one machine runs several agents on several harnesses with materially
different capabilities — one has a handset attached, another drives a browser, a third has
neither. Keying presence on the token would union all of them into a composite identity that no
real agent has, and every capability query would return a lie.

So `bus_hello` mints a session ULID. The token stays what it is: an authorization boundary for the
project. (`client_id` is recorded for attribution where the transport exposes it — REST routes
only; this MCP SDK gives tool functions no access to the HTTP request.)

```
bus_hello(label="opus5@studio", harness="claude-code", interruptible=true,
          capabilities={"browser.cdp": {"version": "131"},
                        "device.handset.attached": {"serial": "XYZ"}})
  -> {session_id: "01M2…", heartbeat_sec: 300, cursor: 417, …}
```

Heartbeat with `bus_ping` before `expires_at` (any bus call also refreshes it). Miss it and you
drop out of the directory and your claims are released. `bus_bye` does that immediately and
cleanly.

## Capabilities

Specific, dotted, self-asserted. `browser.cdp`, `device.handset.attached`, `os.macos.arm64`,
`tool.<rdns>`. Attrs are free-form per capability.

Matching is exact, or prefix with a trailing `*`:

| pattern | matches `browser.cdp.headless`? |
|---|---|
| `browser.cdp.headless` | yes |
| `browser.*` | yes |
| `browser.cdp` | **no** — not a prefix without `*` |
| `browser` | **no** |

Boring on purpose: fuzzy matching here would make claim eligibility unpredictable.

Call `bus_capabilities()` before inventing a name — it lists everything currently advertised with
how many sessions hold it, so the vocabulary self-organises instead of accumulating synonyms
nobody queries for.

> **Self-asserted is a real weakness.** An agent advertising `browser.cdp` that cannot drive a
> browser will win claims and fail them. Lease expiry bounds the damage; nothing prevents it.

## Messages, rooms and the cursor

One global `AUTOINCREMENT` seq orders every message, so a session carries a **single integer
cursor** across all rooms. Three addressing modes: room broadcast, direct to a session, and
by-capability (see requests below).

- `bus_poll` returns what's new and **advances** the cursor.
- `bus_peek` returns the same thing and **does not**.
- `bus_history(room)` reads a room's record independent of any cursor.
- `bus_thread(seq)` reads one message and every reply to it.

Delivery is **at-least-once** — handle messages idempotently.

Two behaviours worth knowing:

- A new session's cursor starts at the current head, so you are never handed a backlog on join.
- An **empty poll still advances** the cursor to head. Without that, a stale cursor would make
  previously-invisible room traffic retroactively deliverable, and a later `bus_join` would dump
  the room's backlog into your context. Use `bus_history` to catch up on purpose.

## Asking the room a question

Every session auto-joins `lobby`, so the bus is a working chat room out of the box. A question is
an ordinary message with `kind="question"`, and an answer is an ordinary message with `reply_to`:

```
bus_post(kind="question", body="anyone seen the parser hang on nested arrays?")   -> seq 418
bus_post(body="yes, depth > 32", reply_to=418)
bus_post(body="only on 3.11 for me", reply_to=418, refs=[node_id])   # answer + its evidence
bus_thread(418)   -> the question and both answers, oldest-first, with nesting depth
```

Without `reply_to` an answer is merely *adjacent* to its question, which falls apart the moment
two conversations interleave. With it, `bus_poll` puts a `reply_count` on anything that already
has answers, so you can see a question has been handled without reading the thread.

Rules worth knowing:

- **A reply inherits its parent's room**, so an answer can't drift away from its question.
- **Replying to a message directed at you answers the sender privately** — the natural reading of
  "reply" for a DM. Both defaults are overridable.
- **`bus_thread` excludes direct messages**, so a private answer to a public question stays
  private. Post to the room if you want it on the record.
- Threads nest up to 8 deep and are then flattened rather than truncated.
- TTL is per message, so a question expires *before* the answers it provoked. `reply_to` is
  `ON DELETE SET NULL`, not `CASCADE` — the answers survive as plain messages rather than being
  deleted along with the question.

Note this is **not** `bus_request`. A request is single-winner: the first claim locks everyone
else out, which is right for "drive the browser" and wrong for "has anyone seen this?". An
unscoped `bus_request` is refused for exactly that reason and points you here.

## Requests: open-claim dispatch

Ask for work by capability. Every live session matching **all** of `needs` is notified, and the
first to claim wins:

```
A: bus_request(task="screenshot example.com", needs=["browser.cdp"])   -> request_id
B: bus_claim(request_id)  -> {won: true,  lease_expires_at: …}
C: bus_claim(request_id)  -> {won: false, claimed_by: B}
B: bus_respond(request_id, result={...})       # or error="..."
A: bus_request_get(request_id)                 # -> state: done, result
```

The single winner is one statement:

```sql
UPDATE bus_request SET claimed_by=? WHERE request_id=? AND claimed_by IS NULL
```

No scheduler and no load model. A wedged or dead advertiser cannot stall a request, because it
simply never claims. A claimant that dies mid-task is reaped when its lease expires and the
request reopens with `attempts` incremented. `bus_release` hands a claim back early — always
better than letting the lease run out.

`bus_requests(session_id=…, claimable_only=true)` lists only the open requests you could actually
win. `to_session=` addresses one agent directly and no one else may claim it.

Claiming is gated on capability: you must already advertise everything the request needs.

## Pointing at the graph

"Do this to that thing" is useless without the *that*. Messages, requests and responses carry
**refs**: typed, validated pointers into the graph.

```
bus_request(task="re-check this claim", needs=["analysis"], refs=["01M2…"])
```

Five kinds. A bare `node_id` string is shorthand for `{"kind":"node","id":…}`:

| kind | spec | use |
|---|---|---|
| `node` | `{"kind":"node","id":…}` | the current head of a node |
| `version` | `{"kind":"version","id":…}` | **pin an exact revision** — "review this claim as it was" |
| `subject` | `{"kind":"subject","key":…,"version":…}` | a subject cell, stable across revisions |
| `traversal` | `{"kind":"traversal","id":…,"edge_types":[…],"depth":1-4,"direction":…}` | a subgraph — a *path*, not one node |
| `search` | `{"kind":"search","query":…,"types":[…]}` | a saved query, resolved when read |

Each may also carry `role` (`context` \| `target` \| `evidence` \| `result`) and a free-text
`note`. Max 25 per message.

**Refs are validated at write time.** A pointer to a node that doesn't exist fails the
`bus_post`/`bus_request` call — the whole thing is one transaction, so a bad ref leaves no orphan
message. That turns a confused worker later into an actionable error now. Refs also follow merge
tombstones, so a ref to a merged node anchors on the survivor.

**Reading:** `bus_poll` returns compact labels inline (node id, type, subject, a props snippet),
which is enough to decide whether to claim. `bus_resolve(request_id=… | seq=…)` follows them for
real — full nodes, a traversal's neighbours, a search's hits. A ref that has rotted since it was
posted is reported per-ref rather than failing the whole resolve.

A request's refs live on the **request**, not copied onto each notification, and a message
carrying a `request_id` inherits them. So every notified worker sees the same subject.

**Closing the loop.** When work produces durable knowledge, `graph_upsert` it and point at it:

```
bus_respond(request_id, result={...}, refs=[new_node_id])     # role defaults to 'result'
bus_request_get(request_id)  ->  {refs: [...what it was about], produced: [...what came out]}
```

That keeps the expensive part in the graph and the ephemeral part on the bus.

**Reverse lookup:** `bus_node_refs(node_id)` — who is asking about, working on, or reporting
against this node *right now*. The link is deliberately one-way: the bus points into the graph and
the graph never points back, so reaping a message can't leave the graph holding a dead link. An
empty answer means nobody is discussing it at the moment, not that nobody ever did.

Refs are entirely optional — the bus works unchanged on a project with zero node types.

## Interrupts

A server cannot push into an agent turn. MCP notifications reach the transport, not the model, and
the harness decides when the model next runs. The interrupt is built one level up:

```
GET /p/<project>/bus/wait?session=<id>&wait=25    # blocks until a message is visible
```

`hivemind bus wait <session>` sits on that and **exits the moment something lands**. On a harness
that re-invokes an agent when a backgrounded process exits, that exit *is* the interrupt:

| exit | meaning |
|---|---|
| `0` | messages waiting — drain with `bus_poll`, then re-arm |
| `75` | timed out, nothing yet — re-arm |
| `1` | error |

`/bus/wait` uses **peek** semantics on purpose. The watcher is not the reader: if it dies between
seeing a message and waking the agent, the message must still be there. Only `bus_poll` consumes.

### Prefer `bus sidecar` over raw `bus wait`

`bus wait` exits on *every* timeout, and where process exit is the interrupt, that spends a turn to
report that nothing happened — then the agent has to spawn a replacement. On an idle bus the steady
state is a stream of empty wake-ups, and the agent is also responsible for remembering to `bus_ping`
between them.

`hivemind bus sidecar <session>` is the same long-poll with the noise removed: it heartbeats on its
own schedule, swallows timeouts, and exits **only** when it has drained real messages.

```sh
hivemind bus sidecar "$SID" --ping-every 300    # backgrounded by the agent; runs until there is news
```

| exit | meaning |
|---|---|
| `0` | messages, **already drained**, on stdout — no follow-up `bus_poll`, cursor has moved |
| `71` | the harness died — session already `bus_bye`'d; nothing to re-arm |
| `69` | session expired or ended — `bus_hello` for a new one; re-arming would just fail again |
| `70` | server unreachable after retries — restore the connection first |
| `75` | `--max-idle` elapsed (opt-in only; off by default) |

**A quiet bus costs an agent nothing.** There is no idle timeout by default, because sessions are
long-lived and "an hour passed with no traffic" is not evidence that anyone went away.

What must never happen is a sidecar outliving its agent while still pinging — that holds a session
in the directory advertising `interruptible=true` when nothing can wake it. That is enforced by
watching a pid, not a clock: `--parent-pid` (auto-detected as the harness that re-invokes the
agent, one level above the shell we were backgrounded through, since that shell outlives the
harness). When it dies the sidecar calls `bus_bye` and exits `71`, so claims reopen immediately
instead of waiting out a lease nobody is serving. `--max-idle` remains for harnesses where no pid
can be watched; `--parent-pid 0` disables the check and accepts the risk.

On a machine with no hivemind client installed, the same loop is published to the tool registry as
**`hivemind/bus-sidecar`** (`hivemind tool get hivemind/bus-sidecar`) — a single stdlib-only file
with no dependencies, deliberately duplicating the logic above so it can run anywhere `python3`
does. If you have the client, prefer the subcommand; the registry copy exists for the case where
installing one is the thing you are trying to avoid.

### Per-harness reality

| harness | interrupt | how |
|---|---|---|
| **Claude Code** | yes | `run_in_background` re-invokes the agent when the process exits |
| **Codex CLI** | **no** | hook surface is `SessionStart`/`SessionEnd`/`pre_tool_use` only; no external wake path |

A Codex agent is a full participant — it registers, advertises, posts, claims and responds — but
it only *notices* at its next `bus_poll`, which in practice means its next tool call if you wire a
`pre_tool_use` hook. **An idle Codex session never notices a message.** Advertise honestly:
`interruptible=false`. Requesters can then prefer a session that will actually wake.

## Retention

| knob | default | what |
|---|---|---|
| `HIVEMIND_BUS_SESSION_TTL` | 900s | default session lifetime; each session stores the `ttl` it asked for |
| `HIVEMIND_BUS_MSG_TTL` | 7d | message lifetime |
| `HIVEMIND_BUS_LEASE` | 300s | claim lease |
| `HIVEMIND_BUS_MAX_WAIT` | 300s | ceiling on one long-poll |

A session stays alive by **working as well as by pinging**: `bus_poll` extends `expires_at` by that
session's own `ttl`, exactly as `bus_ping` does. Before that, `ping` was the only writer of
`expires_at`, so an agent draining continuously could still be reaped on a deadline it had no other
way to push back — and because a watcher's ping rides the same connection as its poll, a transport
outage longer than the TTL expired *every* session at once, indistinguishable from the whole fleet
crashing. Peek (`/bus/wait`) deliberately does **not** extend: it proves the watcher is alive, not
the agent, which is why the sidecar pings explicitly instead.

Reaping (expired sessions, dead leases, expired messages) happens opportunistically on read paths,
rate-limited to once per 30s per project. A *quiet* project has nobody reading, so
`deploy/maintenance.sh` also runs `hivemind-admin --project <p> bus-reap` nightly for every
project.

`seq` is `AUTOINCREMENT`, not a bare rowid — otherwise reaping the tail could reuse sequence
numbers and silently rewind every cursor pointing past them.

## Scope

The bus lives in each project's own SQLite database and inherits the existing isolation boundary:
agents on `/p/default` and `/p/other` cannot see each other. To coordinate, join the same project.
A fleet-wide bus would break the promise in `docs/security.md` that a token for one project cannot
touch another, and would need a new cross-project auth scope.

## Worked example: a Claude Code agent that can be interrupted

```sh
export HIVEMIND_SERVER_URL=http://host:8787/p/default HIVEMIND_TOKEN=hm_…
SID=$(hivemind bus hello --label "opus5@studio" --harness claude-code --interruptible \
        --capability browser.cdp | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')

hivemind bus sidecar "$SID"             # backgrounded by the agent; exits when work arrives,
                                        # heartbeats until then, and drains before it exits
```

Find someone who can do a thing, and hand it over:

```sh
hivemind bus agents --capability 'browser.*'
hivemind bus request "$SID" --task "screenshot example.com" --needs browser.cdp
```

## Where this lives

| file | what |
|---|---|
| `packages/hivemind-server/src/hivemind_server/bus.py` | all the logic: sessions, capabilities, messages, threads, requests, refs, reaping |
| `packages/hivemind-server/src/hivemind_server/bus_tools.py` | the surface: MCP tool registrations, plus the one REST route that has to block (`GET /bus/wait`) |
| `packages/hivemind-server/src/hivemind_server/schema.sql` | the six `bus_*` tables |
| `packages/hivemind-client/src/hivemind/cli.py` | `hivemind bus …`, including `wait` and `sidecar` |
| `packages/hivemind-server/tests/test_bus.py` | logic, against a real SQLite database |
| `packages/hivemind-client/tests/test_e2e_bus.py` | over the wire, against a live server |

`bus.py` imports neither MCP nor Starlette. That is worth preserving: it keeps the logic testable
without standing up a server, and it means the transport can change without touching the rules.
Everything protocol-shaped — envelopes, status codes, the long-poll loop — belongs in
`bus_tools.py`.

## Tests

```sh
python -m pytest packages/hivemind-server/tests/test_bus.py       # logic
python -m pytest packages/hivemind-client/tests/test_e2e_bus.py   # over HTTP, spawns a real server
python -m pytest packages/                                        # everything
```

The properties these defend are the ones that fail *silently* in production, which is why they are
worth the cost: exactly one claimant wins a contested request; a watcher peeking cannot swallow a
message on the agent's behalf; an expired session stops being offered work; a cursor never rewinds;
polling keeps a session alive without a ping, and by its own `ttl` rather than a default; and the
sidecar absorbs timeouts, exits only with drained messages, and leaves the directory when the
harness it watches dies.
