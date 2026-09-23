#!/usr/bin/env bash
# Daily Hivemind maintenance. Runs AFTER the backup, so anything collected is already captured
# in the backup mirror (which never deletes) and stays recoverable.
#
# Why this is scheduled rather than manual: unattached uploads are invisible garbage, and left
# alone they reached 94 GB (80% of the store) before anyone looked. Bounded, automatic collection
# is the structural fix; the grace window gives an agent time to attach what it uploaded.
set -euo pipefail
DEST="${HIVEMIND_BACKUP_DIR:-$HOME/hivemind-backup}"
LOG="$DEST/maintenance.log"
mkdir -p "$DEST"
exec >>"$LOG" 2>&1
cd "$(dirname "$0")/.."          # the repo this script lives in
export PATH="$HOME/.local/bin:$PATH"
set -a; . deploy/hivemind.env; set +a
echo "=== $(date -Is) maintenance ==="

# Every project, not just 'default' — a second project otherwise silently never gets collected.
PROJECTS=$(uv run --package hivemind-server hivemind-admin list-projects \
  | python3 -c 'import json,sys; print(" ".join(json.load(sys.stdin)["projects"]))')
echo "projects: $PROJECTS"

for proj in $PROJECTS; do
  echo "--- project $proj ---"
  # A failure on one project must not abort maintenance for the rest.
  echo "-- who is uploading without attaching --"
  uv run --package hivemind-server hivemind-admin --project "$proj" orphans --older-than-hours 24 \
    || echo "orphans failed for $proj"
  echo "-- garbage collection --"
  uv run --package hivemind-server hivemind-admin --project "$proj" gc --yes \
    || echo "gc failed for $proj"
  # There is deliberately NO bus step here. The v1 polling bus had sessions, claim leases and
  # stored messages to reap; the WebSocket bus has none of them — presence IS the socket, the
  # offline queue is bounded by count and bytes, and the recent buffer expires on its own. A
  # `bus-reap` call survived here after the rewrite deleted the subcommand and failed silently
  # behind `|| echo` every night. test_every_command_this_repo_invokes_exists is what stops the
  # next one.
done

echo "-- disk --"
df -h / | tail -1
