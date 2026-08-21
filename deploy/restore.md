# Restoring a Hivemind backup

Backups live on a second physical disk (default `/mnt/fuzz/hivemind-backup/<project>/`):
dated `db/hivemind-<stamp>.db` snapshots, a `blobs/sha256/` mirror, and `tokens.json`.

```sh
sudo systemctl stop hivemind   ||  pkill -f hivemind-server      # stop writers first
P=/home/nik/hivemind-data/projects/default
B=/mnt/fuzz/hivemind-backup/default

# 1. database — pick a snapshot and verify it BEFORE overwriting anything
python3 -c "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check').fetchone()[0])" \
  $B/db/hivemind-<stamp>.db
mv $P/hivemind.db $P/hivemind.db.broken            # keep the old one until you are satisfied
rm -f $P/hivemind.db-wal $P/hivemind.db-shm        # stale WAL must not be applied to a restored db
cp $B/db/hivemind-<stamp>.db $P/hivemind.db

# 2. blobs — the mirror never deletes, so this only adds back what is missing
rsync -a $B/blobs/sha256/ $P/blobs/sha256/

# 3. tokens (only if you lost them; existing clients keep working otherwise)
cp $B/tokens.json $P/tokens.json && chmod 600 $P/tokens.json
```

Then start the server and check `/healthz`. If the database is newer than the blob mirror,
`hivemind-admin --project default gc` reports nothing to collect — a blob referenced by the DB but
missing on disk shows up as a 404 on download, not as corruption.
