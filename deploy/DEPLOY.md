# Deploying Hivemind

Dependencies are tracked three ways so any machine can reproduce the environment:

- **`uv.lock`** (repo root) — source of truth, a universal lock across Python 3.9–3.14. Used by
  the `uv` path for an exact, reproducible install.
- **`deploy/requirements-{server,client}.txt`** — fully-pinned exports of `uv.lock`, for an exact
  `pip` install **against the same package index uv used** (regenerate with `deploy/relock.sh`).
- The package metadata itself (`pyproject.toml`) — for a normal `pip install` that resolves
  compatible dependencies against whatever index the machine sees. **Most portable.**

Pick **uv** for an exact lockfile install, or **plain venv + pip** if uv isn't available.

> **Deploying to a server that already holds real data?** Follow
> [deploy-checklist.md](deploy-checklist.md) rather than this file alone. It is the operational
> half: get the branch to the remote *before* the server (or the `git clone` below reinstalls the
> old code), tighten the modes on files that already exist, verify the ACL from two identities, and
> run the one irreversible step — the author backfill — with a backup behind it. This file says how
> to install; that one says what to check.

## Server (lab box, Python ≥3.11)

### Option A — uv (recommended: exact, from uv.lock)
```sh
curl -LsSf https://astral.sh/uv/install.sh | sh          # one-time; or deploy/bootstrap-uv.sh
git clone <repo> hivemind && cd hivemind
uv sync --package hivemind-server
uv run hivemind-server                                    # or ./.venv/bin/hivemind-server
```
`deploy/bootstrap-labbox.sh` does all of this + mints a token + installs the systemd service.

### Option B — plain venv + pip
```sh
git clone <repo> hivemind && cd hivemind
python3 -m venv .venv && . .venv/bin/activate
pip install -U pip                                        # the bundled pip is often too old
pip install ./packages/hivemind-server                    # normal resolution (portable)
#   …or, for an exact pin against uv's index:
#   pip install -r deploy/requirements-server.txt && pip install --no-deps ./packages/hivemind-server
hivemind-server
```

## Client (any machine, Python ≥3.9 — incl. the Mac Studio's system 3.9.6)

### Option A — uv
```sh
uv tool install --from ./packages/hivemind-client hivemind      # puts `hivemind` on PATH
```

### Option B — plain venv + pip  (tested on stock Python 3.9.6)
```sh
python3 -m venv .venv-hm && . .venv-hm/bin/activate
pip install -U pip
pip install ./packages/hivemind-client                    # deps: httpx + websockets (pure wheels)
hivemind --help
```
Then point it at your project:
```sh
export HIVEMIND_SERVER_URL=http://<lan-or-tailscale-ip>:8787/p/default
export HIVEMIND_TOKEN=<token from `hivemind-admin mint-token`>
hivemind health
```

> Note: `pip install -U pip` first — the pip bundled with an old system Python can fail to
> resolve modern package metadata. The exact-pin `requirements-*.txt` files assume the same
> package index uv resolved against; if a pin is unavailable on your mirror, use the normal
> `pip install ./packages/<pkg>` path above.

> **Prefer a newer Python for the venv.** `requires-python >=3.9` is a *floor* (so the client also
> runs on the Mac Studio's stock 3.9.6 with nothing installed) — it is not a cap. If a newer
> interpreter is available, use it and the old-pip friction disappears; the exact pins then install
> cleanly. Get one with zero system changes via uv: `uv python install 3.13` then
> `uv venv --python 3.13 .venv-hm` (or `python3.13 -m venv .venv-hm` if you have it). Verified: the
> pinned `requirements-client.txt` installs cleanly on Python 3.13.

## Network exposure (who can reach it)

The server binds **`127.0.0.1` by default — localhost only**, so a plain `hivemind-server`
run is *not* reachable from other machines. To serve a LAN/Tailscale network set
`HIVEMIND_HOST=0.0.0.0` (this is what `deploy/hivemind.env` does) and confirm with
`curl http://<server-ip>:8787/healthz` from another host.

Being on the LAN is not authorization. Every `/p/<project>` request needs a bearer token with
exactly two exceptions, both on a **shared** project and neither exposing project data: the endpoint
index `/p/<name>/` and the health probe `/p/<name>/healthz` answer `200` without one, because clients
hold only a project base URL and a healthy server must not look dead to them. Everything else there
is `401`. A **private** project answers `404` to an unauthenticated caller on every path, its own
health included — indistinguishable from a project that does not exist. The allowlist is exactly two
entries and `test_a_shared_projects_open_tails_are_exactly_two` pins both halves of that; widening it
is an ACL bypass. Never bind a public interface.

To add client machines (token minting, secure transfer, installing just the plugin), see
[../docs/clients.md](../docs/clients.md).

## After upgrading

Indexes are maintained on write, so no routine maintenance is needed. Two exceptions:

```sh
hivemind-admin --project default embed      # after adding the embedding index, or changing backend
hivemind-admin --project default reindex    # rebuild node search indexes
hivemind-admin --project default autolink   # link skills/tools that have no links yet
```
Semantic search is optional and degrades safely: without `sentence-transformers` installed the
server uses a built-in hashed TF-IDF vectoriser, and every search reports which backend answered.
Installing a neural backend later requires re-running `embed` — until then searches say so
explicitly rather than quietly returning lexical-only results.

## Backups

`deploy/backup.sh` writes a daily backup to a **second physical disk** (so it survives a root-disk
failure, not just an accidental delete). Installed on the lab box as:

```
30 3 * * * /bin/bash $HOME/hivemind/deploy/backup.sh
```

Unlike `maintenance.sh`, `backup.sh` does **not** source `deploy/hivemind.env` — it reads the
environment and otherwise uses its own defaults. If this deployment moved `HIVEMIND_DATA_DIR` or
`HIVEMIND_BACKUP_DIR` in that file, set them in the crontab too, or the nightly run reads the
default paths instead (loudly: `!!! no projects dir at …`, then a non-zero exit).

What it does, per project:

- **Database** — SQLite's **online backup API**, not a file copy. A live WAL-mode database cannot
  be safely `cp`/`rsync`ed: the `.db` file alone is an inconsistent snapshot. Each copy is then
  verified with `PRAGMA integrity_check` and only counts as a backup if it passes.
- **Blobs** — incremental `rsync`, **without `--delete`**: artifacts are content-addressed and
  immutable, so anything GC'd on the live side stays recoverable in the backup.
- **Tokens** — `tokens.json`, copied at mode 0600.
- **`project.json`** — the per-project **ACL**: it names the owner and every member. Not optional.
  A project restored without it is stamped `visibility=shared, owner=null` the first time the
  server constructs it, so a restore that skipped it would **publish every private graph**. The
  script prints `!!! no project.json — this project will restore as SHARED` if one is missing.
- **Rotation** — keeps `HIVEMIND_BACKUP_KEEP` (default 7) dated database snapshots; blobs are a
  single mirror.

And once per deployment, not per project:

- **`_server/identities.json`** — every server-level token. A restore without it **revokes
  everyone**. It lives under `_server/` because the top level of the backup dir is one directory
  per project and `identities.json` is itself a legal project name (`projects_meta.NAME_RE` accepts
  it verbatim); the script refuses loudly if a real project ever collides with a name it owns.

**Deliberately NOT backed up: `<project>/bus_secret`.** It is the per-project HMAC key that signs
every listen key, and restoring an old one would resurrect keys that a rotation had revoked — the
file *is* the revocation lever (deleting it revokes every outstanding listen key for that project).
The cost of not having it is bounded and self-announcing: the server recreates the secret at
startup, every pre-existing listen key then fails verification, and each listener prints
`[hivemind bus] refused (…); run bus_connect for a fresh key` and **exits** — so every agent on
that project must re-run `bus_connect` once, and none of them recovers on its own the way an HTTP
caller does. No data is lost: bus traffic is ephemeral by definition and anything durable is in the
graph. See [restore.md](restore.md).

Tunables, with the defaults the script itself applies: `HIVEMIND_BACKUP_DIR`
(`$HOME/hivemind-backup`), `HIVEMIND_BACKUP_KEEP` (`7`), `HIVEMIND_DATA_DIR`
(`$HOME/hivemind-data`). Log: `<backup dir>/backup.log`.

Measured on the live project (1.7 GB database, 9,475 blobs / 9.7 GB): **23 s** for the first run,
**17 s** incrementally with zero blobs transferred. Restore procedure: [restore.md](restore.md).

## Rolling out a change to a live server

[deploy-checklist.md](deploy-checklist.md) is the step-by-step, and the one thing worth repeating
here: **push to the remote before pushing to the server.** Measured on this branch, `main` and
`origin/main` were both at the merge base while the work sat on a feature branch, so
`git push origin main` succeeded and sent nothing — and the server, which is pushed to directly
because it cannot fetch from the private repo, would then have run every commit of that branch while
GitHub held none of them (`git rev-list --left-right --count main...HEAD` is what says how many).
The `git clone <repo>` recovery path at the top of this file would have reinstalled the **old**
server. The checklist fast-forwards `main` first and then verifies that the remote actually moved.

## Restarting

Use `deploy/restart.sh`. Killing and immediately relaunching loses the bind race: the new
instance exits with *address already in use* while a wrapper process lingers, so `pgrep` reports
"running" while nothing is listening. The script kills by pattern **and** by port, waits for the
port to free, then verifies the server is actually serving.

## Maintenance (daily, automatic)

`deploy/maintenance.sh` runs at 03:45, **after** the 03:30 backup — so anything it collects is
already captured in the backup mirror (which never deletes) and stays recoverable.

```
30 3 * * * backup.sh        # verified DB snapshot + incremental blob mirror
45 3 * * * maintenance.sh   # orphan report, then garbage collection
```

It reports which agents uploaded blobs they never attached, then collects garbage. Roots are
`blob_ref` rows, digests written into node/edge props, tool artifacts (`tool_version`), and pins;
`HIVEMIND_BLOB_GRACE` (default **72h**) gives an agent time to attach what it uploaded before its
bytes are eligible.

Left unattended this leak reached **94 GB — 80% of the blob store**. Scheduling collection is the
structural fix; the one-step `?attach_to=` upload is what stops it being created.
