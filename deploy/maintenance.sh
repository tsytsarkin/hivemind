#!/usr/bin/env bash
# Daily Hivemind maintenance. Runs AFTER the backup, so anything collected is already captured
# in the backup mirror (which never deletes) and stays recoverable.
#
# Why this is scheduled rather than manual: unattached uploads are invisible garbage, and left
# alone they reached 94 GB (80% of the store) before anyone looked. Bounded, automatic collection
# is the structural fix; the grace window gives an agent time to attach what it uploaded.
set -euo pipefail
DEST="${HIVEMIND_BACKUP_DIR:-/mnt/fuzz/hivemind-backup}"
LOG="$DEST/maintenance.log"
mkdir -p "$DEST"
exec >>"$LOG" 2>&1
cd "$HOME/hivemind"
export PATH="$HOME/.local/bin:$PATH"
set -a; . deploy/hivemind.env; set +a
echo "=== $(date -Is) maintenance ==="
echo "-- who is uploading without attaching --"
uv run --package hivemind-server hivemind-admin --project default orphans --older-than-hours 24
echo "-- garbage collection --"
uv run --package hivemind-server hivemind-admin --project default gc --yes
echo "-- disk --"
df -h / | tail -1
