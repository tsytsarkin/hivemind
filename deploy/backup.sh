#!/usr/bin/env bash
# Daily Hivemind backup to a second physical disk.
#
# A live SQLite database must never be copied with cp/rsync: WAL mode means the .db file alone is
# an inconsistent snapshot. This uses the online backup API, which is safe while the server keeps
# serving, then verifies the copy with integrity_check before it counts as a backup.
#
# Blobs are content-addressed and immutable, so they are mirrored incrementally and WITHOUT
# --delete: an artifact GC'd on the live side stays recoverable here.
#
# project.json and identities.json are backed up because they are ACL state, not convenience:
# project.json IS the per-project ACL, and a project restored without it reads as visibility=shared
# with no owner (Project.__init__ stamps that when the file is absent) — i.e. a restore would
# silently publish every private graph. identities.json holds every server-level token, so a
# restore without it revokes everyone.
set -euo pipefail

DATA_DIR="${HIVEMIND_DATA_DIR:-$HOME/hivemind-data}"
DEST="${HIVEMIND_BACKUP_DIR:-$HOME/hivemind-backup}"
KEEP="${HIVEMIND_BACKUP_KEEP:-7}"          # dated DB snapshots to retain per project
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$DEST/backup.log"

mkdir -p "$DEST"
exec >>"$LOG" 2>&1
echo "=== $(date -Is) backup start (keep=$KEEP) ==="

fail() { echo "!!! $*"; exit 1; }
[ -d "$DATA_DIR/projects" ] || fail "no projects dir at $DATA_DIR/projects"
mountpoint -q "$(df -P "$DEST" | tail -1 | awk '{print $6}')" || echo "note: $DEST is not its own mount"

total_start=$(date +%s)
for proj_dir in "$DATA_DIR"/projects/*/; do
  [ -d "$proj_dir" ] || continue
  proj="$(basename "$proj_dir")"
  out="$DEST/$proj"
  mkdir -p "$out/db" "$out/blobs"

  # ── database: online backup + verify ─────────────────────────────────────────
  src_db="$proj_dir/hivemind.db"
  [ -f "$src_db" ] || { echo "  [$proj] no database, skipping"; continue; }
  snap="$out/db/hivemind-$STAMP.db"
  python3 - "$src_db" "$snap" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)                      # online backup API: consistent while the server runs
s.close()
ok = d.execute("PRAGMA integrity_check").fetchone()[0]
n = d.execute("SELECT COUNT(*) FROM node").fetchone()[0]
t = d.execute("SELECT COUNT(*) FROM tx").fetchone()[0]
d.close()
if ok != "ok":
    raise SystemExit("integrity_check failed: %s" % ok)
print("  db ok: integrity=%s nodes=%d tx=%d" % (ok, n, t))
PY
  chmod 600 "$snap"
  echo "  [$proj] db snapshot $(du -h "$snap" | cut -f1) -> $(basename "$snap")"

  # ── blobs: incremental mirror, no deletes ────────────────────────────────────
  if [ -d "$proj_dir/blobs/sha256" ]; then
    rsync -a --stats --exclude 'tmp/' "$proj_dir/blobs/sha256/" "$out/blobs/sha256/" \
      | grep -E 'Number of regular files transferred|Total transferred file size' \
      | sed 's/^/  [blobs] /' || true
  fi

  # ── tokens (credentials: keep them 0600 here too) ────────────────────────────
  [ -f "$proj_dir/tokens.json" ] && install -m 600 "$proj_dir/tokens.json" "$out/tokens.json"

  # ── project.json: the ACL. Not optional — see the header. ────────────────────
  if [ -f "$proj_dir/project.json" ]; then
    install -m 600 "$proj_dir/project.json" "$out/project.json"
  else
    echo "  [$proj] !!! no project.json — this project will restore as SHARED"
  fi

  # ── rotation ─────────────────────────────────────────────────────────────────
  ls -1t "$out/db"/hivemind-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "  [$proj] pruning $(basename "$old")"
    rm -f "$old"
  done
done

# Server-level credentials, one per deployment rather than per project.
if [ -f "$DATA_DIR/identities.json" ]; then
  install -m 600 "$DATA_DIR/identities.json" "$DEST/identities.json"
  echo "  identities.json backed up ($(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$DATA_DIR/identities.json" 2>/dev/null || echo '?') tokens)"
else
  echo "  note: no identities.json at $DATA_DIR (no server-level identities minted yet)"
fi

echo "=== $(date -Is) backup done in $(( $(date +%s) - total_start ))s; dest usage: $(du -sh "$DEST" | cut -f1) ==="
