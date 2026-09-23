# Adding a machine (plugin + token)

Hivemind is **one server, many clients**. Don't run a second server per machine — each server has
its own SQLite database and blob store, so a second instance is a *separate graph*, not a shared
one. Point every machine at the same server URL.

## 1. Is the server reachable on your LAN?

The server binds **`127.0.0.1` by default (localhost only)**. To serve other machines set:

```sh
HIVEMIND_HOST=0.0.0.0        # listen on all private interfaces (LAN / Tailscale)
HIVEMIND_PORT=8787
```
(That's what `deploy/hivemind.env` does — the deployed copy the bootstrap and service installers
create from the tracked `deploy/hivemind.env.example`.) Verify from another machine:
```sh
curl http://<server-ip>:8787/healthz              # server root
curl http://<server-ip>:8787/p/default/healthz    # one project's own probe (shared projects)
curl http://<server-ip>:8787/p/default/           # index of every endpoint for that project
```
Both health paths and both indexes are **open** (no token) so a probe works with only a URL —
but the project one only for a **shared** project. A private project answers nothing at all without
authorisation, not even its health: every HTTP path under it returns the same `404` as a name that
does not exist. Everything else returns `401` without a bearer token.
**Being on the LAN is not authorization** — apart from those two probes on a shared project, every
`/p/<project>` request needs a bearer token; unauthenticated requests get `401`. That open list is
exactly two tails wide (`""` and `healthz`) and a test pins both halves of it:
`test_a_shared_projects_open_tails_are_exactly_two`, which asserts the two at 200 and `/guide`,
`/guide/core`, `/skills`, `/skills/{id}`, `/tools` and a blob `GET` at 401. `HIVEMIND_ALLOWED_HOSTS=*`
disables the DNS-rebinding host check (fine on a trusted private network); set explicit hostnames to
enable it. See [security.md](security.md).

## 2. Mint a token for the person using the new machine

A token names a **person**, not a machine — that is what puts an author on every write. Run **on the
server host**:

```sh
hivemind-admin mint-token --user nik --device mac-studio        # [--role member|admin]
# -> hm_…        (printed once; stored in <data-dir>/identities.json)
```

`--user` is the username authorship is recorded under: lowercase `[a-z0-9][a-z0-9_-]{0,31}`, **no
dots** (dots are reserved for the `<user>.<suffix>` project-ownership prefix). `--device` is a label kept
beside it, so one person can hold one token per machine and revoke them individually. `--role` is
accepted but carries no authority today.

This token reaches **every project that user may access**, and it is the only kind that works on the
project-neutral `/mcp` endpoint and on `GET /projects`.

<details><summary>The legacy per-project form</summary>

```sh
hivemind-admin --project default mint-token --client-id <machine-name>
# -> hm_…        (stored in that project's tokens.json)
```

Still supported for credentials already deployed. It is pinned to the one project whose file holds
it — it reaches no other project, not even another shared one — it is refused on `POST /mcp` and
`GET /projects` with a `401`, it cannot create or share a project, and its writes are attributed
`legacy:<client-id>` rather than to a person. Prefer `--user` for anything new.

</details>

**Transferring it:** the token is a bearer credential — move it over a channel you already trust:

```sh
# simplest: mint it over ssh and capture it directly on the client machine
ssh you@server 'hivemind-admin mint-token --user nik --device laptop' | tee ~/hm-token
```
or copy/paste from an SSH session into the machine's password manager / Keychain. **Don't** send it
over chat or email, and don't commit it.

**To revoke**, delete the token's entry from `<data-dir>/identities.json` (or, for a legacy token,
from `<data-dir>/projects/<project>/tokens.json`). **No restart is needed**: both files are re-read
whenever their mtime/size changes, so the revocation takes effect on the very next request — and
within one 30 s heartbeat for a bus socket that is already open.

## 3. Install just the plugin on the new machine

The plugin is self-contained — the machine needs **no server, no Python, no repo checkout**. It
ships a manifest, an `.mcp.json`, two skills (`hivemind` and `hivemind-schema`), the
`/hivemind:project` command and a `SessionStart` hook.

```sh
# from the git repo (works anywhere the machine can reach the repo):
claude plugin marketplace add tsytsarkin/hivemind        # or: <git-url>
claude plugin install hivemind@hivemind-marketplace --scope user \
  --config server_url=http://<server-ip>:8787 \
  --config api_token=hm_…
```

If the repo is **private** and the machine has no GitHub credentials, use either:
```sh
# a) clone once with your own auth, then add the local checkout
git clone git@github.com:tsytsarkin/hivemind.git && claude plugin marketplace add ./hivemind

# b) copy just the plugin directory over ssh (it is a few KB)
scp -r you@thismachine:~/hivemind/plugin  ~/hivemind-plugin
claude plugin marketplace add ~/hivemind-plugin   # add a .claude-plugin/marketplace.json alongside,
                                                  # or point marketplace add at a repo root copy
```

Verify the connection:
```sh
claude mcp list
# plugin:hivemind:hivemind: http://<server-ip>:8787/mcp (HTTP) - ✔ Connected
```
`api_token` is declared `sensitive`, so Claude Code stores it in the OS keychain rather than in a
settings file.

### Where the URL and the token come from

The MCP connection is the easy half: `.mcp.json` interpolates `${user_config.server_url}` and
`${user_config.api_token}` straight into it, and reads no environment variable at all. But
`plugin/skills/hivemind/scripts/guide.sh` (the live guide), the `hivemind` CLI and `hivemind bus
listen` are *shell* consumers, and they read `HIVEMIND_SERVER_URL` / `HIVEMIND_TOKEN` —
and `HIVEMIND_PROJECT`, because the URL is the server and the project is no longer in it — from the
environment. There are three sources, in precedence order:

1. **An export already in the shell wins.** The environment is the override, so a machine
   deliberately pointed at a different server or token is never quietly redirected.
2. **Otherwise the plugin's own config supplies it.** Claude Code hands a `SessionStart` hook the
   user config as `CLAUDE_PLUGIN_OPTION_SERVER_URL` / `CLAUDE_PLUGIN_OPTION_API_TOKEN`, and
   `$CLAUDE_ENV_FILE` is a shell fragment that later **Bash tool calls in that session source**.
   `plugin/hooks/session-start` appends an `export` line for whichever is unset — including
   `HIVEMIND_PROJECT`, which comes not from the plugin config (which holds no project) but from the
   session pin the same hook reads. Each variable is decided on its own, so a shell that exports
   only the URL still gets the token.
   Neither `CLAUDE_PLUGIN_OPTION_*` nor `$CLAUDE_ENV_FILE` is visible to a plain Bash tool call —
   measured — which is why a script cannot read the plugin config itself and the hook has to be the
   bridge. That file holds a bearer token and Claude Code creates it `0644`, so the hook `chmod
   600`s it *before* writing. `packages/hivemind-server/tests/test_session_env_bridge.py` pins all
   of that, including the override direction and the mode.
3. **Nothing.** Then those tools degrade: `guide.sh` prints its cached or bundled copy (naming the
   reason), and the CLI exits with *"set HIVEMIND_SERVER_URL and HIVEMIND_TOKEN"*. A missing
   **project** is its own case, and both name it: `guide.sh` says *"no project … run
   /hivemind:project"* rather than blaming the network, and the CLI's tool calls are refused by the
   server while its REST paths refuse client-side, naming `--project`.

**Do not over-read the bridge.** It fires on SessionStart, so it covers Bash tool calls made later
in that session and nothing else. A plain terminal, a cron job, a machine where the plugin is not
installed, and a session that started before the plugin was updated all still need a manual
`export` — as does any use of the CLI outside Claude Code, which is most of them. MCP tool calls are
unaffected in every one of those cases.

The **project** is published the same way for the same reason: `server_url` is the server, `/guide`
and `/blobs` exist only under `/p/<project>/`, so both shell consumers compose the prefix themselves
from `HIVEMIND_PROJECT`. `guide.sh` goes one better — when that variable is unset it reads the pin
file *at call time*, which is what follows a `/hivemind:project` switch made mid-session; an exported
value is frozen at the event that wrote it. The CLI takes `--project` instead, so a plain terminal
needs no pin. Either way a project nobody named is refused, not guessed.

### Without installing anything: `scripts/hivemind-claude`

Installing writes the plugin and its token into the machine's permanent configuration. On a
borrowed machine, a throwaway VM, or a box you are only debugging from, that is the wrong
footprint. The launcher loads the plugin for **one session** instead:

```sh
scripts/hivemind-claude                      # prompts for address (default localhost:8787) + token
scripts/hivemind-claude --url <host>:8787 --project scratch
scripts/hivemind-claude --resume             # anything it does not recognise goes to claude
scripts/hivemind-claude -- --model sonnet    # or be explicit with --
```

It needs the repo (or just a copy of `plugin/` plus the script) and nothing else — no install, no
marketplace, no change to your settings. `HIVEMIND_SERVER_URL` and `HIVEMIND_TOKEN` are used as the
defaults, so setting both makes it non-interactive. A bare `host:port` is expanded to
`http://host:port/p/<project>`; a URL that already names a project is left alone.

Before starting, it checks `/healthz` and then fetches `/guide` with your token, so a wrong address
or a rejected token is a line of output rather than a silent MCP failure ten minutes later. `--dry-run`
prints what it would run — the token redacted — and `--no-check` skips the preflight.

How the token reaches the plugin, since there is no install step to collect it: the script writes
`pluginConfigs.hivemind.options` to a temporary settings file and passes `--plugin-dir` and
`--settings`. Three things about that were established by measurement rather than assumed:

- the `pluginConfigs` key for a `--plugin-dir` plugin is the **bare** plugin name. The
  `name@marketplace` forms are accepted and then ignored, so a rename there would silently fall
  back to the `userConfig` defaults instead of erroring;
- the token is written only to a `0600` file inside a `0700` directory, removed when the session
  ends, and never placed on the `claude` command line — `ps` is world-readable;
- `--settings` **merges**, so your model, theme and other plugins are unaffected.

`packages/hivemind-server/tests/test_launcher.py` pins the first two (plus "must not `exec`", since
the trap that removes the token file cannot fire after one). The third is Claude Code's own
behaviour, which no test in this repo can hold — it was measured against Claude Code 2.1.280 and is
recorded in the script's header comment.

So the launcher's token arrives exactly the way an installed plugin's does, and from there the
`SessionStart` hook exports it to the session's shell like any other plugin config — the launcher
itself exports nothing. Two asymmetries to know. Its own `HIVEMIND_SERVER_URL` only pre-fills
the prompt, and `claude` inherits that variable from your shell **verbatim**, before the launcher's
normalisation: give it a bare `box.local:8787` and the plugin gets
`http://box.local:8787/p/default` while the session's Bash calls see `box.local:8787`, which carries
no scheme and is not a usable base for anything. Export a full URL — either shape — or let the prompt
collect it. And the launcher still hands the plugin a **project** URL where the plugin's own default
is now the server root, so a launched session keeps the older behaviour for a call that names no
project: it lands in the URL's project instead of being refused.

### `server_url`: the server root, or a project base

`server_url` is used as `${server_url}/mcp`, and **both forms work**. The root is the default and the
recommended one:

| Form | What a call with no `project=` does | REST (blobs, guide, catalogs) | The bus |
|---|---|---|---|
| `http://<ip>:8787` (default since 1.2.0) | is **refused**, reads and writes alike | works — the consumer adds `/p/<project>/` itself | works |
| `http://<ip>:8787/p/default` | acts in `default` — the URL named it | works, under that prefix | works |

The root form is the project-neutral endpoint: one connection reaches every project the token may
access, by passing `project=<name>` on each call, and a call that names none is refused rather than
defaulted. It needs a server-level identity token (a legacy per-project one gets `401`).

What the root does not *carry* is the REST surface: `PUT`/`GET /blobs/…`, the guide and the catalogs
live only under `/p/<project>/`, and the router 404s them off the root. That is no longer a cost,
because the project comes from somewhere better than the URL — the session pin:

- the **live guide** (`guide.sh`) builds `${HIVEMIND_SERVER_URL}/p/<project>/guide/<section>`, taking
  the project from `HIVEMIND_PROJECT` (exported by the `SessionStart` hook from the pin) or, when
  that is unset, from the pin file read at call time — which is what follows a `/hivemind:project`
  switch made mid-session;
- the **CLI** takes `--project <name>`, defaulting to `$HIVEMIND_PROJECT`, and builds the same prefix
  for `/blobs/…` and for the bus socket. With no project at all its tool calls are refused by the
  server and its REST paths refuse client-side, naming the flag.

A URL that already names a project is used **verbatim** by both, `HIVEMIND_PROJECT` notwithstanding:
a URL whose own path names a project must not be silently redirected by a variable.

The bus works on both forms, and the reason is worth knowing: `bus_connect` is an MCP call, and what
it hands back is a **project-scoped** `ws://…/p/<name>/bus/ws` URL that the listener connects to
directly. The socket never goes through the root. So an MCP-only client on the root URL can still
join the bus — measured, not inferred.

**A brand-new project has no `/p/<name>/` prefix yet.** Those mounts are built once at startup, so a
project you just created with `project_create` answers `404` on every path under its own prefix —
`/mcp`, `/blobs/…`, `/guide`, `/healthz` alike — until the server restarts. Until then it is reachable
only on the neutral `/mcp` with `project=<name>`: its graph is live immediately, and it is the
byte-moving surfaces that wait — `hivemind artifact put/get`, `hivemind bus listen`, and the live
guide, which prints its cached copy naming that `404` as the reason.

Naming a project is never a restriction either way: `project=<name>` on an individual call overrides
both the URL and `--project`, so `/p/default/mcp` still reaches `nik.private` if the token may.

`hivemind health` proves none of this. It resolves `/healthz` from the server root whatever the base
URL is, and that path takes no token, so it answers `{"ok": true}` for a root URL with no project and
for a rejected token both. Check the CLI with something that authenticates and names a project —
`hivemind --project <name> guide get`.

### Picking the project for a session

The plugin ships `/hivemind:project`, which lists what the token can reach (grouped shared / yours /
shared-with-you), offers a private graph or a per-session scratch project, creates it if new, and
**pins** the choice in local state keyed by the session id. A `SessionStart` hook re-injects the
pinned name on every event that rebuilds context — the matcher is `startup|clear|compact|resume|fork`
— because a compaction drops the choice from context, and a dropped choice plus a defaulted write is
how private work reaches a shared graph. (`fork` is in that list because a fork does not replay the
transcript, so without it a forked session would get nothing while the pin file sat unread.) The pin
is only an aide-memoire: the `project` echoed in each tool result is the authoritative answer.

## 4. (Optional) the CLI, for large artifacts and tool publishing

The plugin covers in-conversation use. For big uploads/downloads and publishing tools, install the
client too (Python ≥3.9; two deps, `httpx` for the graph/artifact/tool calls and `websockets` for
`hivemind bus listen`) — see [DEPLOY.md](../deploy/DEPLOY.md):
```sh
pip install ./packages/hivemind-client
export HIVEMIND_SERVER_URL=http://<server-ip>:8787   # the server
export HIVEMIND_PROJECT=default                      # or pass --project <name> per command
export HIVEMIND_TOKEN=hm_…
hivemind health                                      # liveness only — see above
hivemind --project default guide get                 # the check that actually authenticates
```
Bytes move over `/p/<project>/blobs/…`, and the CLI composes that from the URL plus the project. The
three exports are what a **plain terminal** needs; inside a Claude Code session with the plugin
configured, the `SessionStart` hook has already set the URL and the token from the plugin's config
and the project from the session pin (see [Where the URL and the token come from](#where-the-url-and-the-token-come-from))
— export them there only to override.
