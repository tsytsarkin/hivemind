# Deploying Hivemind

Dependencies are tracked three ways so any machine can reproduce the environment:

- **`uv.lock`** (repo root) — source of truth, a universal lock across Python 3.9–3.14. Used by
  the `uv` path for an exact, reproducible install.
- **`deploy/requirements-{server,client}.txt`** — fully-pinned exports of `uv.lock`, for an exact
  `pip` install **against the same package index uv used** (regenerate with `deploy/relock.sh`).
- The package metadata itself (`pyproject.toml`) — for a normal `pip install` that resolves
  compatible dependencies against whatever index the machine sees. **Most portable.**

Pick **uv** for an exact lockfile install, or **plain venv + pip** if uv isn't available.

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
pip install ./packages/hivemind-client                    # only real dep is httpx
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

Bearer auth gates every `/p/<project>` request regardless — being on the LAN is not
authorization (unauthenticated requests get `401`). Never bind a public interface.

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

What it does, per project:

- **Database** — SQLite's **online backup API**, not a file copy. A live WAL-mode database cannot
  be safely `cp`/`rsync`ed: the `.db` file alone is an inconsistent snapshot. Each copy is then
  verified with `PRAGMA integrity_check` and only counts as a backup if it passes.
- **Blobs** — incremental `rsync`, **without `--delete`**: artifacts are content-addressed and
  immutable, so anything GC'd on the live side stays recoverable in the backup.
- **Tokens** — copied at mode 0600.
- **Rotation** — keeps `HIVEMIND_BACKUP_KEEP` (default 7) dated database snapshots; blobs are a
  single mirror.

Tunables: `HIVEMIND_BACKUP_DIR` (default `/mnt/fuzz/hivemind-backup`), `HIVEMIND_BACKUP_KEEP`,
`HIVEMIND_DATA_DIR`. Log: `<backup dir>/backup.log`.

Measured on the live project (1.7 GB database, 9,475 blobs / 9.7 GB): **23 s** for the first run,
**17 s** incrementally with zero blobs transferred. Restore procedure: [restore.md](restore.md).

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
