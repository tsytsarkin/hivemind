#!/usr/bin/env bash
# Restart the server without racing itself. Runs on Linux (the deploy host) and macOS (a dev box).
#
# Failure modes seen in practice, all of them silent:
#   * `pkill -f hivemind_server` (underscore) never matches the real process name (hyphen);
#   * killing and immediately re-launching loses the bind race — the new instance gets
#     "address already in use", exits, and a wrapper process lingers so `pgrep` still says
#     "running" while nothing is listening;
#   * on macOS none of the tooling was there: no `setsid` (so the server never launched at all),
#     no `ss` (so every port check reported 0 and the script cheerfully carried on), and BSD
#     `fuser` rejects both `-k` and the `8787/tcp` syntax.
# So: kill by pattern AND by port, wait for the port to actually free, then verify it is serving —
# at each step using whichever tool this host actually has.
set -uo pipefail

# ── where the checkout is ───────────────────────────────────────────────────────────────────────
# The deploy host keeps it at ~/hivemind; a dev box has it wherever it was cloned. Derive the root
# from this script's own location — on the server that still resolves to ~/hivemind, so nothing
# changes there — and let HIVEMIND_HOME override. The symlink walk is so a ~/bin symlink to this
# script still finds the repo; `readlink -f` would be shorter but does not exist on macOS.
if [ -n "${HIVEMIND_HOME:-}" ]; then
    ROOT=$HIVEMIND_HOME
else
    src=$0
    while [ -L "$src" ]; do
        link=$(readlink "$src")
        case $link in /*) src=$link ;; *) src=$(dirname "$src")/$link ;; esac
    done
    ROOT=$(cd "$(dirname "$src")/.." && pwd)
fi
cd "$ROOT" || { echo "!!! cannot cd to $ROOT" >&2; exit 1; }

if [ ! -f deploy/hivemind.env ]; then
    echo "!!! no deploy/hivemind.env in $ROOT — copy deploy/hivemind.env.example and edit it" >&2
    exit 1
fi
export PATH="$HOME/.local/bin:$PATH"
set -a; . deploy/hivemind.env; set +a

# Take the port and the log path from the env instead of repeating 8787 here: hardcoded, this
# script killed and probed port 8787 no matter what hivemind.env actually told the server to bind.
PORT=${HIVEMIND_PORT:-8787}
LOG=${HIVEMIND_DATA_DIR:-$HOME/hivemind-data}/server.log
mkdir -p "$(dirname "$LOG")"
# How long to wait for the new instance to answer /healthz. A warm start answers in about a second;
# a cold `uv run` that has to resolve the environment first can take considerably longer than 30s,
# hence the knob.
START_TIMEOUT=${HIVEMIND_START_TIMEOUT:-30}

# ── port tools that exist on both platforms ─────────────────────────────────────────────────────
# `ss` is iproute2, absent on macOS. `lsof` ships with macOS but is frequently missing from a
# minimal Linux server. `netstat` is deprecated on Linux but still the last resort. Ask for each in
# turn so neither platform depends on a tool the other one owns.
listeners() {   # -> the listening addr:port, one per line; empty output means nothing holds it
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk -v p="[.:]$PORT\$" 'NR > 1 && $4 ~ p { print $4 }'
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 { print $9 }' | sort -u
    else
        netstat -an 2>/dev/null | awk -v p="[.:]$PORT\$" '$NF == "LISTEN" && $4 ~ p { print $4 }'
    fi
}
# Anchoring on the port ($4 ~ ":8787$") also fixes a quieter bug in the old `grep ':8787'`: it
# matched a peer address, or port 18787, and called that "listening".

port_pids() {   # -> PIDs holding the port. BSD fuser cannot answer this at all, so lsof goes first.
    if command -v lsof >/dev/null 2>&1; then
        lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null
    elif command -v fuser >/dev/null 2>&1; then
        fuser -n tcp "$PORT" 2>/dev/null
    fi
}

# -> every server PID, whether or not it still holds the port. A wedged shutdown holds none, which
# is the whole reason this exists; see the escalation below. `$$` and `$PPID` are excluded because
# `-f` matches a full command line and a caller's own — an ssh running a one-liner that mentions
# hivemind-server, for instance — must not be killed by its own pattern.
server_pids() {
    { pgrep -f "uv run --package hivemind-server" 2>/dev/null
      pgrep -f "hivemind-server"                  2>/dev/null
      port_pids
    } | sort -u | grep -v -x -e "$$" -e "${PPID:-0}"
}

# ── stop whatever is running ────────────────────────────────────────────────────────────────────
pkill -f "uv run --package hivemind-server" 2>/dev/null || true
pkill -f hivemind-server                    2>/dev/null || true

# Then by port. `fuser -k` did this with an immediate SIGKILL and only on Linux; signalling the PIDs
# ourselves works on both and gives the server the chance to close its database cleanly first.
pids=$(port_pids)
[ -n "$pids" ] && kill $pids 2>/dev/null

# Wait for the port to actually free rather than sleeping a flat 4s and hoping — that was too long
# on an idle box and, on a loaded one, sometimes not long enough, which is the bind race above.
#
# Waiting on the PROCESSES too, not just the port, is the other half — and assuming the port implied
# them leaked a server on every single restart. uvicorn closes the LISTENING SOCKET as soon as it
# takes SIGTERM and only then waits for open connections to drain; a bus WebSocket never closes on
# its own, so the process sits in graceful shutdown indefinitely. The port therefore came free, the
# escalation below was gated on the port alone and so never fired, and the outgoing instance stayed
# alive — still holding the database open, still serving its already-connected peers OLD CODE that
# no later restart would ever replace, while this script exited 0. Measured on the deploy host: two
# such servers, ~3 days old, holding 15 and 22 open handles on the live 8.4 GB database.
i=0
while [ "$i" -lt 20 ] && { [ -n "$(listeners)" ] || [ -n "$(server_pids)" ]; }; do
    sleep 0.5
    i=$((i + 1))
done
survivors=$(server_pids)
if [ -n "$(listeners)" ] || [ -n "$survivors" ]; then
    echo "still up after 10s (pids:$(echo " $survivors" | tr '\n' ' ')); escalating to SIGKILL"
    [ -n "$survivors" ] && kill -9 $survivors 2>/dev/null
    sleep 1
fi
# SIGKILL is not refusable, so anything still here is stuck in uninterruptible I/O and the launch
# below will lose the bind race for a reason this script cannot fix. Say so instead of reporting a
# clean stop — the point of the file is that no silent failure survives it.
survivors=$(server_pids)
[ -n "$survivors" ] && echo "!!! still alive after SIGKILL:$(echo " $survivors" | tr '\n' ' ')" >&2
echo "port $PORT in use before start: $(listeners | wc -l | tr -d ' ')"

# ── pick a launcher that exists ─────────────────────────────────────────────────────────────────
# DEPLOY.md documents two installs — uv (option A) and a plain venv (option B) — but this script
# only ever knew the uv one, so on a venv-only host every restart died with "uv: command not found"
# and left nothing listening, the reason buried in the log while the script still printed its
# healthz line. Prefer uv (it resolves from uv.lock).
if command -v uv >/dev/null 2>&1; then
    LAUNCH='exec uv run --package hivemind-server hivemind-server'
elif [ -x "$ROOT/.venv/bin/hivemind-server" ]; then
    LAUNCH='exec ./.venv/bin/hivemind-server'
else
    echo "!!! no launcher: neither uv nor ./.venv/bin/hivemind-server — see deploy/DEPLOY.md" >&2
    exit 1
fi

# setsid gives the server its own session on Linux. macOS has no setsid; nohup gets us the part
# that actually matters once this script exits — immunity to the SIGHUP of a closing ssh session.
if command -v setsid >/dev/null 2>&1; then
    DETACH=setsid
else
    DETACH=nohup
fi

echo "launching with: ${LAUNCH#exec } (via $DETACH)"
# ROOT travels in the environment rather than interpolated into the -c string, so a checkout path
# containing a space or a quote cannot break the child's `cd`.
# ROTATE, do not truncate. `>"$LOG"` on a file the OUTGOING server still holds open does not
# reset its file offset — it only sets the length to 0. That process then writes its shutdown line
# ("Waiting for connections to close") at its stale offset and the kernel zero-fills the gap, so the
# log becomes one sparse line of NULs. Measured on the deploy host: apparent size 1.3 MB, 8 KB on
# disk, one line of 1,340,225 characters — which `tail` below then faithfully printed in full,
# making every restart emit a megabyte of nothing. A rename leaves the old fd pointing at the old
# inode, so the dying process's last words land in .1 where they belong and the new log starts clean.
[ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"
HM_ROOT=$ROOT $DETACH bash -c '
    cd "$HM_ROOT" || exit 1
    set -a; . deploy/hivemind.env; set +a
    export PATH="$HOME/.local/bin:$PATH"
    '"$LAUNCH" >"$LOG" 2>&1 &

# ── verify it is actually serving ───────────────────────────────────────────────────────────────
# Poll instead of `sleep 10` then one curl: a healthy start answers in about a second, and a broken
# one should not cost ten seconds to find out about.
health=""
i=0
while [ "$i" -lt "$START_TIMEOUT" ]; do
    health=$(curl -s -m 2 "http://127.0.0.1:$PORT/healthz" 2>/dev/null) && [ -n "$health" ] && break
    health=""
    sleep 1
    i=$((i + 1))
done

echo "listening: $(listeners | tr '\n' ' ')"
if [ -n "$health" ]; then
    echo "healthz: $health"
else
    echo "healthz: FAILED (no answer in ${i}s)"
fi
echo "--- tail $LOG ---"
# Bounded in bytes as well as lines: "5 lines" is not a size when a single line can be a megabyte,
# and the caller is usually an ssh whose output someone is reading. cut per line, not head on the
# stream, so five short lines still all appear.
tail -5 "$LOG" | cut -c1-400

# Exit non-zero when the server did not come up, so a caller — or a human reading $? after an ssh
# one-liner — is not told a failed restart succeeded. This is the whole point of the file.
[ -n "$health" ]
