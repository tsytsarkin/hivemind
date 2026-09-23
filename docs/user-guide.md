# Using Hivemind

A practical guide for the person sitting at the keyboard: how to connect a session to a Hivemind
server — with or without installing anything — and what to do in the first five minutes.

If you are setting up the **server**, see [`deploy/DEPLOY.md`](../deploy/DEPLOY.md). If you are
handing a **token** to someone else, see [`clients.md`](clients.md). This page is about using it.

---

## What you need

Two things, whichever route you take:

| | |
|---|---|
| **A server URL** | e.g. `http://<server-host>:8787`. One server, many clients — don't start a second one, it would be a separate graph. |
| **A token** | Minted on the server with `hivemind-admin mint-token --user <you> --device <machine>`. It names a **person**, which is what puts an author on everything you write. |

## Two ways in

| | Install the plugin | `scripts/hivemind-claude` |
|---|---|---|
| Setup | once per machine | none |
| Token stored | OS keychain, permanently | a `0600` file, deleted when the session ends |
| Needs the repo | no | yes (or a copy of `plugin/` plus the script) |
| Good for | your own machines | a borrowed machine, a VM, a box you are debugging |

Both give you the same tools, skills, `/hivemind:project` command and session hook. Pick on
footprint, not capability.

### Route A — the launcher, no install

```sh
scripts/hivemind-claude
# Hivemind server address [localhost:8787]: <host>:8787
# Bearer token for http://<host>:8787/p/default (not echoed): ****   # the launcher adds /p/<project>
```

It loads the plugin for that session only and forwards anything it doesn't recognise to `claude`:

```sh
scripts/hivemind-claude --url <host>:8787 --project scratch
scripts/hivemind-claude --resume                  # unrecognised → claude
scripts/hivemind-claude -- --model sonnet         # or be explicit
```

Set `HIVEMIND_SERVER_URL` and `HIVEMIND_TOKEN` to skip both prompts — useful in a script. A bare
`host:port` becomes `http://host:port/p/default`; pass `--project` to change the project, or give a
URL that already names one. (The launcher still hands the plugin a project URL, where the plugin's
own default is the server root; both work.) That expansion is for the plugin only: `claude` inherits
your exported variables **verbatim**, and they take precedence over the plugin's config in the
session's shell — so export a full URL, `http://host:8787` or `http://host:8787/p/<name>`, never a
bare `host:port`, which has no scheme for the shell-side tools (the live guide, the CLI) to use.

Before launching it checks the server's health endpoint and then fetches the guide with your token,
so a wrong address or a rejected token is one line of output rather than a silent failure ten
minutes in. `--dry-run` shows what it would run (token redacted); `--no-check` skips the preflight.

Nothing is written to your permanent configuration, and `--settings` merges rather than replaces —
your model, theme and other plugins are untouched.

### Route B — install the plugin

```sh
claude plugin marketplace add tsytsarkin/hivemind
claude plugin install hivemind@hivemind-marketplace --scope user \
  --config server_url=http://<server-host>:8787 \
  --config api_token=hm_…
claude mcp list      # expect: plugin:hivemind:hivemind … ✔ Connected
```

`api_token` is declared sensitive, so it goes to the OS keychain rather than a settings file. Full
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

> **A new project is not immediately reachable by URL.** The project-neutral `/mcp` endpoint serves
> it right away, so your session works. But *nothing* under `/p/<name>/` exists until the server
> restarts — not its own `/mcp`, not blob upload, not the guide, not the bus. The `hivemind` CLI
> cannot reach it until then.

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

Call `bus_connect(label=…)` **once**, then run the command it returns under your harness's
background-process tool. After that, messages from other agents arrive as notifications on their
own; there is nothing to poll.

```
bus_peers()                       # who is connected right now
bus_send(to="lab-box", body="…")  # direct
bus_broadcast(body="…")           # to a room, use sparingly
```

Claude Code clips a notification near 512 characters, so the listener keeps each line under that and
shows you a body preview of about 300 — a longer message arrives truncated with an id, and
`bus_message(id)` fetches the rest from the server. Each machine also appends every message it
receives **in full** to a local JSONL inbox, which is size-bounded with one rotation, so it has a
horizon rather than being an archive.

Two things to know: **bus traffic is ephemeral** — anything worth keeping goes in the graph. And an
agent label longer than the server's cap arrives **truncated**, so keep labels short and
descriptive (the machine or the job, not a random id).

---

## When something doesn't work

| Symptom | Likely cause |
|---|---|
| `401` on every call | Token wrong, or revoked. Revocation needs no restart — it takes effect on the next request. |
| `404` for a project you believe exists | It doesn't exist, **or** it isn't yours. The server answers identically for both on purpose, so the message cannot tell you which. |
| A brand-new project 404s over REST/CLI/bus | Expected until the server restarts — see the note above. |
| Write refused for naming no project | You're on the project-neutral endpoint. Pass `project=`. |
| The bus listener says "connection refused" | The project's `/p/<name>/bus/ws` route doesn't exist yet (new project, no restart), or the listener script isn't installed — load the `hivemind` skill once, which installs it. |
| The live guide shows an `(offline: …)` copy | Read the rest of that line — it names the cause. `no HIVEMIND_SERVER_URL / HIVEMIND_TOKEN in this shell` means neither your shell nor the plugin config had them (a plain terminal, or no plugin here); `no project … run /hivemind:project` means nothing named a project, which `/guide` needs because it exists only under `/p/<project>/`; `answered HTTP 404` means the project it did name does not exist, is not yours, or was created since the server last started. `guide_get()` over MCP works in every one of those cases. |
| The CLI says `error: set HIVEMIND_SERVER_URL and HIVEMIND_TOKEN` | The plugin's hook exports them only for Bash calls **inside** a Claude Code session. In a plain terminal, export them yourself. |
| The CLI says `this needs a project and nothing named one` | The URL is the server root and neither `--project` nor `$HIVEMIND_PROJECT` named a project. |
| Plugin not connecting after install | `claude mcp list` shows the resolved URL; check it is the server you meant (`http://<host>:8787`, or a `/p/<name>` base). |

---

## Where to go next

- [`clients.md`](clients.md) — minting and transferring tokens, adding machines, revocation
- [`api.md`](api.md) — every tool and REST route
- [`data-model.md`](data-model.md) — the two versioning axes, authorship, tables
- [`security.md`](security.md) — identities, the project ACL, what private actually guarantees
- [`bus.md`](bus.md) — the bus in depth
- [`packs.md`](packs.md) — domain packs, and writing your own schema
- [`skills-and-traps.md`](skills-and-traps.md) — mini-skills, traps, the tool registry
