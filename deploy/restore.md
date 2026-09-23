# Restoring a Hivemind backup

Backups live on a second physical disk: per project under `$HIVEMIND_BACKUP_DIR/<project>/` —
dated `db/hivemind-<stamp>.db` snapshots, a `blobs/sha256/` mirror, `tokens.json` and
`project.json` — plus `$HIVEMIND_BACKUP_DIR/_server/identities.json` for the whole deployment.
(Server-level files live under `_server/` because the top level of the backup dir is one directory
per project, and `identities.json` is itself a legal project name.)

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
