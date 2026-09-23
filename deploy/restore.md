# Restoring a Hivemind backup

Backups live on a second physical disk: per project under `$HIVEMIND_BACKUP_DIR/<project>/` —
dated `db/hivemind-<stamp>.db` snapshots, a `blobs/sha256/` mirror, `tokens.json` and
`project.json` — plus `$HIVEMIND_BACKUP_DIR/_server/identities.json` for the whole deployment.
(Server-level files live under `_server/` because the top level of the backup dir is one directory
per project, and `identities.json` is itself a legal project name.)

One file in a project directory is **not** in the backup, by decision: **`bus_secret`**. It
needs nothing done during a restore, but it does have a consequence every agent on the
project will notice — see [the section at the end](#bus_secret-is-not-restored-and-every-listener-dies-once).

> **Restore `project.json` before starting the server.** It is the per-project ACL, and a project
> whose `project.json` is absent is stamped `visibility=shared, owner=null` the first time the
> server constructs it — so skipping step 3 publishes every private graph to every user of the
> server. The file itself is easy to put back afterwards; what cannot be taken back is the reading
> that happened while it was missing.

```sh
sudo systemctl stop hivemind   ||  pkill -f hivemind-server      # stop writers first
P=$HIVEMIND_DATA_DIR/projects/default
B=$HIVEMIND_BACKUP_DIR/default

# 1. database — pick a snapshot and verify it BEFORE overwriting anything
python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" \
  $B/db/hivemind-<stamp>.db
mv $P/hivemind.db $P/hivemind.db.broken            # keep the old one until you are satisfied
rm -f $P/hivemind.db-wal $P/hivemind.db-shm        # stale WAL must not be applied to a restored db
cp $B/db/hivemind-<stamp>.db $P/hivemind.db

# 2. blobs — the mirror never deletes, so this only adds back what is missing
rsync -a $B/blobs/sha256/ $P/blobs/sha256/

# 3. project.json — the ACL. Do this BEFORE the server starts (see the warning above).
cp $B/project.json $P/project.json && chmod 600 $P/project.json

# 4. tokens (only if you lost them; existing clients keep working otherwise)
cp $B/tokens.json $P/tokens.json && chmod 600 $P/tokens.json

# 5. server-level identities — one file for the whole deployment, not per project.
#    Without it every `mint-token --user` credential is gone, i.e. everyone is revoked.
cp $HIVEMIND_BACKUP_DIR/_server/identities.json $HIVEMIND_DATA_DIR/identities.json \
  && chmod 600 $HIVEMIND_DATA_DIR/identities.json
```

Then start the server and check `/healthz`. If the database is newer than the blob mirror,
`hivemind-admin --project default gc` reports nothing to collect — a blob referenced by the DB but
missing on disk shows up as a 404 on download, not as corruption.

## `bus_secret` is not restored, and every listener dies once

`<project>/bus_secret` is the 32-byte HMAC key that signs that project's listen keys. It is
deliberately absent from `backup.sh` and from the steps above: the file **is** the wholesale
revocation lever — deleting it revokes every outstanding listen key for the project — so restoring
an old copy would resurrect keys a rotation had already revoked. Not restoring it is the safer
default, and the cost has to be written down because nobody would guess it:

- The server **recreates** the secret the first time it registers the project (`bus_ws.register_secret`
  at startup), so nothing is broken and nothing needs doing by hand.
- Every listen key minted before the restore then fails verification. The refusal is pre-accept, so
  it reaches the listener as a bare `403`, which it classifies as *refused* rather than as a blip:
  it prints `[hivemind bus] refused (…); run bus_connect for a fresh key` and **exits**.
- That is **terminal**. A listener does not retry a refusal — a listen key is reusable, so a refusal
  means revoked or expired, not a dropped link — so **every agent on that project must call
  `bus_connect` once and restart its `Monitor` task.** Unlike an HTTP caller, none of them recovers
  on its own.
- Nothing is lost. Bus traffic is ephemeral by design; anything durable is already in the graph.
  Queued offline messages are in memory and were gone with the process anyway.

There is no copy of it in `$B` to put back — `backup.sh` never writes one, so do not go looking. The
usual restore leaves the live `$P/bus_secret` untouched (only the database, blobs and the two ACL
files above are replaced), and in that case the keys stay valid and there is nothing to do. The
listeners die once only when the secret itself is gone: a lost or rebuilt data directory, or a
deliberate rotation. If you want to survive that with keys intact, you have to have taken your own
copy of `$P/bus_secret` beforehand and restore it *before* starting the server (`chmod 600` it) —
and understand that this also reinstates any key a rotation revoked.
