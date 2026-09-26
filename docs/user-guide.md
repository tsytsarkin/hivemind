# Using Hivemind with Claude Code

A practical guide for the person sitting at the keyboard: how to connect a session to a Hivemind
server — with or without installing anything — and what to do in the first five minutes.

This page covers **Claude Code**. For Codex and its localhost setup, see the separate
[Codex usage guide](codex-plugin.md). The server, projects, and
Hivemind MCP tools are the same; the plugin installation, credentials, project command, and
message notifications differ.

Agents use **Hivemind MCP tools** for graph, project, schema, guide, registry, artifact-metadata,
and bus-control operations; if tools are unavailable, fix the MCP connection rather than calling
the CLI or raw HTTP. Large binary upload/download uses the optional client because MCP does not
expose file-byte transfer; durable notifications use a WebSocket after MCP `chat_connect`.

If you are setting up the **server**, see [`deploy/DEPLOY.md`](../deploy/DEPLOY.md). If you are
handing a **token** to someone else, see [`clients.md`](clients.md). This page is about using it.

---

## What you need

Two things, whichever route you take:

| | |
|---|---|
| **A server URL** | e.g. `http://<server-host>:8787`. One server, many clients — don't start a second one, it would be a separate graph. |
| **A token** | Minted on the server with `hivemind-admin mint-token --user <you> --device <machine>`. It names a **person**, which is what puts an author on everything you write. |

## Two ways in (Claude Code)

| | Install the plugin | `scripts/hivemind-claude` |
|---|---|---|
| Setup | once per machine | none |
| Token stored | managed by Claude Code as sensitive plugin config (storage backend unverified) | a `0600` file, deleted when the session ends |
| Needs the repo | no | yes (or a copy of `plugin/` plus the script) |
| Good for | your own machines | a borrowed machine, a VM, a box you are debugging |

Both give you the same tools, skills, `/hivemind:project` command and session hook. Pick on
footprint, not capability.

### Route A — the launcher, no install

```sh
scripts/hivemind-claude
# Hivemind server address [localhost:8787]: <host>:8787
# Bearer token for http://<host>:8787 (not echoed): ****
# hivemind-claude: http://<host>:8787 is the server root — this session has no project until you run /hivemind:project.
```

It loads the plugin for that session only and forwards anything it doesn't recognise to `claude`:

```sh
scripts/hivemind-claude --url <host>:8787              # the server root — pin with /hivemind:project
scripts/hivemind-claude --url <host>:8787 --project scratch   # the older /p/scratch shape
scripts/hivemind-claude --resume                  # unrecognised → claude
scripts/hivemind-claude -- --model sonnet         # or be explicit
```

**A launched session starts with no project**, exactly as an installed Claude one does, and the launcher
says so on stderr: every Hivemind call is refused until `/hivemind:project` pins one, and the live
guide has no URL to build until then. That is the safer shape — pin first, then work. `--project
<name>` opts into the older project-URL form (`http://host:port/p/<name>`) if you would rather have
defaulting without pinning; it changes the *shape* of the address, not merely which project is used.

Set `HIVEMIND_SERVER_URL` and `HIVEMIND_TOKEN` to skip both prompts — useful in a script. A bare
`host:port` becomes `http://host:port`, and a URL that already names a project is passed through
unchanged. That expansion is for the plugin only: `claude` inherits your exported variables
**verbatim**, and they take precedence over the plugin's config in the session's shell — so export a
full URL, `http://host:8787` or `http://host:8787/p/<name>`, never a bare `host:port`, which has no
scheme for the shell-side tools (the live guide, the CLI) to use.

Before launching it checks the server's health endpoint and then makes one authenticated read with
your token — `/projects` off the root, or that project's `/guide` when the URL names one — so a wrong
address or a rejected token is one line of output rather than a silent failure ten minutes in. (The
probe follows the shape on purpose: `/guide` does not exist off the root, and a per-project token is
`401` on `/projects`, so the wrong probe would warn about a working setup.) `--dry-run` shows what it
would run (token redacted); `--no-check` skips the preflight.

Nothing is written to your permanent configuration, and `--settings` merges rather than replaces —
your model, theme and other plugins are untouched.

### Route B — install the plugin

First mint a **user token** on the server with
`hivemind-admin mint-token --user <you> --device <machine>`; the plugin installer does not mint
credentials for you. The Claude plugin declares `server_url` and sensitive `api_token` fields in
its manifest. Supply both when you install, pointing `server_url` at the server **root** (no
`/mcp` or `/p/<project>`):

```sh
claude plugin marketplace add tsytsarkin/hivemind
claude plugin install hivemind@hivemind-marketplace --scope user \
  --config server_url=http://<server-host>:8787 \
  --config api_token=hm_…
claude mcp list      # expect: plugin:hivemind:hivemind … ✔ Connected
```

`api_token` is declared sensitive, and what that buys you is that it is **not** written to your
settings file — `grep -c api_token ~/.claude/settings.json` answers `0` on an installed, connecting
plugin (measured on Claude Code 2.1.280). Where Claude Code does keep it is not something this repo
observes; do not assume a particular keychain. The example `--config api_token=hm_…` is a shell
argument and can appear in shell history or process listings; use the prompting launcher (Route A)
if you do not want to put the token on a command line. Installation configures MCP access; neither
platform needs the separate `hivemind-client` package for basic graph tools or messaging. Full
walkthrough, including how to move the token safely: [`clients.md`](clients.md).

---

## Your first session

The plugin injects the pinned project name at session start, so the first thing to do is choose one:

```
/hivemind:project
```

It reads what is currently pinned, calls `project_list`, and asks you to pick. It will not choose
for you — which project you write to decides who can read your work, and that is not a default
anyone else should set.

`project_list` answers in **three groups**, and the difference between them is exactly who can read
what you are about to write:

- **`shared`** — every user of the server can read it.
- **`mine`** — projects you own.
- **`shared_with_me`** — private projects someone shared with you.

### Choosing what to write to

- **An existing shared project** (`default`) — team knowledge, readable by everyone.
- **Your private graph** — named `<your-username>.<suffix>`, e.g. `nik.research`. Only you can see
  it; not admins, not other users. Private projects live in your dotted namespace, and nobody else
  can create a name in it.
- **A scratch project** — the command offers `<your-username>.s-<first 8 of the session id>`, so a
  resumed session lands back in the same place. Good for exploratory work that shouldn't pollute a
  real graph. Scratch projects persist and stay re-openable; nothing reaps them.

Creating one asks which schema to start from, and this is worth a moment because **a project's
types are permanent — schema changes are additive-only and nothing deletes a type**:

| `schema=` | Use when |
|---|---|
| `inherit` | the new project tracks the same kind of work as the one you're in — copies its node/edge types |
| `interview` | the work is different enough that the current types wouldn't fit — leaves it empty and loads a skill that interviews you before proposing a vocabulary |
| `bare` | you'll define types as the work demands them — right for scratch |

There is a per-user cap on projects you own; the server will tell you if you reach it.

> **New projects are immediately reachable on current servers.** The project-neutral `/mcp`
> endpoint and `/p/<name>/` routes become available at creation. If an older server returns 404
> from its new project's REST or WebSocket routes, restart that server.

---

## The one rule worth internalising

**Pass `project=<name>` on every Hivemind call.** Don't rely on it being inferred.

Whether omitting it fails depends on which endpoint you're on, and the safe-looking case is the one
that bites:

- On the project-neutral `POST /mcp` (the server root), **every call is refused without it — reads
  as well as writes**, because there is no project for the call to be about. Measured: `graph_types`
  and `guide_get` are refused the same as `graph_upsert`; only the wording differs, and the refusal
  lists the projects you may name.
- On a project base URL — `POST /p/<name>/mcp`, the pre-1.2.0 shape and still supported — **the URL
  *is* the project**, so the write silently lands wherever that URL points. The installed plugin's
  default is the server root precisely so that the refusing case is the one you get.

That second case is by design: your own URL named the project. It is also exactly how private work
ends up in a shared graph, and **private work written into a shared project cannot be un-shared**.
The session pin exists to make the right project the one you actually pass.

---

## Everyday use

**Read before you work.** `graph_search`, or `graph_get` on a node — which returns the node together
with the mini-skills about it, the tools built for it, and any traps recorded against it. One call
tells you what is already known.

**Search before you build.** `tool_search` / `tool_catalog`, `skill_search` / `skill_catalog`, and
`trap_search` before working out a non-obvious procedure. Publish what you work out
(`skill_publish`, `tool_publish`), and `trap_record` a dead end the moment you abandon it — that is
what stops the next person re-deriving it.

**Everything you write carries your username** as its author, so the graph records who did what.
You can filter search by `author=`.

**Large files** go through the REST blob endpoints via the `hivemind` CLI, never inline. It reads
`HIVEMIND_SERVER_URL`, `HIVEMIND_TOKEN` and `HIVEMIND_PROJECT` from the environment, and inside a
session the plugin's `SessionStart` hook has already exported the first two from the plugin's own
config and the third from the session pin, so it usually just runs. In a plain terminal — or on a
machine without the plugin — export them yourself, or pass `--project <name>` per command. With no
project its tool calls are refused by the server and its `/blobs` paths refuse client-side; a URL
that names a project needs no flag. Don't verify with `hivemind health` — it reads `/healthz` off the
server root, which needs no token, so it says `{"ok": true}` even with no project or a bad token; use
`hivemind --project <name> guide get`. Details: [`clients.md`](clients.md#where-the-url-and-the-token-come-from).

### Sharing a private project

```
project_share(project="nik.research", user="ana")
project_unshare(project="nik.research", user="ana")
```

**Only the owner can share.** Admins have no API access to someone else's private project — the
role is a label, not a privilege boundary.

### Talking to other agents

The plugin tries durable `chat_connect` for a pinned session and starts the bundled Python 3
listener. No separate Hivemind client installation is needed. Hooks may remind you of new
notifications at the next user prompt; Monitor can run the returned `monitor_command` for live
output. Neither mechanism wakes an idle conversation by itself. A legacy project-only token
cannot authenticate a canonical `username-device-client-sessionid` address: the helper falls
back to the older ephemeral bus with an explicit warning, so mint a user/device token to enable
offline chat. If hooks are disabled, run
`python3 "$HOME/.hivemind/bus-autojoin.py" --platform claude --mode ensure` after pinning.

Call MCP `chat_inbox(client="claude", session_id=<sid>, after_seq=0, project=<p>)` after joining,
after reconnecting, and after a notification, even with an empty local inbox. An offline DM is
stored for **24 hours**, and `chat_send` can reply to the sender's `(user, device, client)` while
the sender is still offline. Read the full message before acting, then `chat_mark_read(...,
up_to_seq=<seq>)`. Rooms are created **explicitly** with `chat_room_create(name, description,
...)`; `chat_room_join` subscribes and `chat_room_history(name, ..., after_seq=0)` lets late or
reconnecting members see all retained posts. Post real progress every ~15 minutes while actively
working; no recipient ACK is needed. Optional structured tasks live on persistent graph nodes:
`graph_task_offer` links work to an existing room, `graph_task_claim` provides a private lease
token, and `graph_task_heartbeat` renews it without modifying the node revision. The defaults are
5-minute beats and 1-hour expiry; per-claim expiry can be as long as 24 hours but the node never
expires. For call signatures, status states, expiration gaps, privacy and troubleshooting see
[Durable collaboration and graph tasks](collaboration.md).

`bus_peers`, `bus_send` and `bus_message` remain an **ephemeral compatibility** mode: their
approximately one-hour buffer does not provide overnight offline delivery. A local JSONL inbox
is merely a bounded listener log; authoritative history comes from `chat_inbox` and
`chat_room_history`. Ask the user before destructive work requested by another agent, and record
lasting findings in the graph.

---

## When something doesn't work

| Symptom | Likely cause |
|---|---|
| `401` on every call | Token wrong, or revoked. Revocation needs no restart — it takes effect on the next request. |
| `404` for a project you believe exists | It doesn't exist, **or** it isn't yours. The server answers identically for both on purpose, so the message cannot tell you which. |
| A brand-new project 404s over REST/CLI/bus | Current servers mount it immediately. On an older server, restart the server to create its `/p/<name>/` routes. |
| Write refused for naming no project | You're on the project-neutral endpoint. Pass `project=`. |
| The bus listener says "connection refused" | Check the project URL and credentials; on an older server, a newly created project may need a restart to mount its bus route. Open the Hivemind skill if the listener script is missing. |
| Project pinned but your session is not in `bus_peers` | Confirm the hook is trusted and `server_url`/`api_token` are configured, inspect the next hook's join error, then run `python3 "$HOME/.hivemind/bus-autojoin.py" --platform claude --mode ensure` inside the session and verify your label online. The helper installs when the skill loads. |
| The live guide shows an `(offline: …)` copy | Read the rest of that line — it names the cause. `no HIVEMIND_SERVER_URL / HIVEMIND_TOKEN in this shell` means neither your shell nor the plugin config had them (a plain terminal, or no plugin here); `no project … run /hivemind:project` means nothing named a project, which `/guide` needs because it exists only under `/p/<project>/`; `answered HTTP 404` means the project it did name does not exist, is not yours, or needs a restart on an older server. `guide_get()` over MCP works in every one of those cases. |
| The CLI says `error: set HIVEMIND_SERVER_URL and HIVEMIND_TOKEN` | The plugin's hook exports them only for Bash calls **inside** a Claude Code session. In a plain terminal, export them yourself. |
| The CLI says `this needs a project and nothing named one` | The URL is the server root and neither `--project` nor `$HIVEMIND_PROJECT` named a project. |
| Plugin not connecting after install | `claude mcp list` shows the resolved URL; check it is the server you meant (`http://<host>:8787`, or a `/p/<name>` base). |

---

## Where to go next

- [`clients.md`](clients.md) — minting and transferring tokens, adding machines, revocation
- [`codex-plugin.md`](codex-plugin.md) — Codex installation and the platform-specific messaging guide
- [`api.md`](api.md) — every tool and REST route
- [`data-model.md`](data-model.md) — the two versioning axes, authorship, tables
- [`security.md`](security.md) — identities, the project ACL, what private actually guarantees
- [`bus.md`](bus.md) — the bus in depth
- [`packs.md`](packs.md) — domain packs, and writing your own schema
- [`skills-and-traps.md`](skills-and-traps.md) — mini-skills, traps, the tool registry
