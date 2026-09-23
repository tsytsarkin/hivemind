#!/usr/bin/env bash
# Fetch a live Hivemind guide section with an ETag cache. NEVER fails the skill:
# on any error it prints the cached copy (or the bundled offline snapshot) and exits 0.
set -u
SECTION="core"
while [ $# -gt 0 ]; do
  case "$1" in
    --section) SECTION="${2:-core}"; shift 2 ;;
    --section=*) SECTION="${1#*=}"; shift ;;
    *) shift ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
OFFLINE="$HERE/../references/OFFLINE.md"

# Install the agent-runnable scripts at fixed, shell-expandable paths.
#
# bus_connect hands the agent a Monitor command, and a Monitor command runs in a plain shell where
# — measured — neither CLAUDE_PLUGIN_ROOT nor CLAUDE_SKILL_DIR is set. So the command cannot name
# the plugin directory, and the server cannot know it either. Copying the listener to a path built
# only from $HOME makes one fixed string work on every machine. The project-pin helper is here for
# the same reason: /hivemind:project and the agent itself invoke it from a plain shell. (The
# SessionStart hook can reach the plugin copy — hooks.json commands do get CLAUDE_PLUGIN_ROOT
# substituted — and falls back to it when the skill has never loaded on this machine.)
# Refreshed on each skill load, so both track the installed plugin version. Silent and
# best-effort: this must never fail the skill.
install_script() {           # $1 = file in this directory, $2 = destination path
  [ -f "$HERE/$1" ] || return 0
  cmp -s "$HERE/$1" "$2" 2>/dev/null && return 0
  mkdir -p "$(dirname "$2")" 2>/dev/null &&
    cp "$HERE/$1" "$2" 2>/dev/null &&
    chmod +x "$2" 2>/dev/null
  return 0
}
install_script bus-listen.py "${HIVEMIND_LISTENER:-$HOME/.hivemind/bus-listen.py}"
install_script hivemind-project.py "${HIVEMIND_PIN_HELPER:-$HOME/.hivemind/hivemind-project.py}"
CACHE_DIR="${HIVEMIND_CACHE_DIR:-$HOME/.cache/hivemind}"
CACHE="$CACHE_DIR/guide-$SECTION.md"
ETAG="$CACHE_DIR/guide-$SECTION.etag"
mkdir -p "$CACHE_DIR" 2>/dev/null

print_fallback() {
  if [ -f "$CACHE" ]; then
    echo "> (offline: showing last cached copy of '$SECTION')"; echo
    cat "$CACHE"
  elif [ -f "$OFFLINE" ]; then
    echo "> (offline: server unreachable; showing bundled framework guide)"; echo
    cat "$OFFLINE"
  else
    echo "> (offline: no cached guide available; call the guide_get MCP tool instead)"
  fi
  exit 0
}

if [ -z "${HIVEMIND_SERVER_URL:-}" ] || [ -z "${HIVEMIND_TOKEN:-}" ]; then
  print_fallback
fi
command -v curl >/dev/null 2>&1 || print_fallback

URL="${HIVEMIND_SERVER_URL%/}/guide/$SECTION"
INM=""
[ -f "$ETAG" ] && INM="$(cat "$ETAG" 2>/dev/null)"

TMP="$(mktemp 2>/dev/null)" || print_fallback
CODE="$(curl -s -m 8 -o "$TMP" -w '%{http_code}' \
  -H "Authorization: Bearer $HIVEMIND_TOKEN" \
  ${INM:+-H "If-None-Match: $INM"} \
  -D "$CACHE_DIR/.hdr-$SECTION" "$URL" 2>/dev/null)" || { rm -f "$TMP"; print_fallback; }

case "$CODE" in
  200)
    mv "$TMP" "$CACHE"
    NEW_ETAG="$(awk 'tolower($1)=="etag:"{print $2}' "$CACHE_DIR/.hdr-$SECTION" | tr -d "\r")"
    [ -n "$NEW_ETAG" ] && printf '%s' "$NEW_ETAG" > "$ETAG"
    GV="$(awk 'tolower($1)=="x-guide-version:"{print $2}' "$CACHE_DIR/.hdr-$SECTION" | tr -d "\r")"
    echo "> (live: guide '$SECTION' v${GV:-?}, fetched just now)"; echo
    cat "$CACHE" ;;
  304)
    rm -f "$TMP"
    echo "> (live: guide '$SECTION' unchanged; cached copy is current)"; echo
    cat "$CACHE" ;;
  *)
    rm -f "$TMP"; print_fallback ;;
esac
exit 0
