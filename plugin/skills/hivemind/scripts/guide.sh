#!/usr/bin/env bash
# Fetch a live Hivemind guide section with an ETag cache. NEVER fails the skill:
# on any error it prints the cached copy (or the bundled offline snapshot) and exits 0 — and says
# WHICH error, because "server unreachable" was wrong for most of them: a missing token, a 401 and
# a root-form URL (whose /guide is not a route) all reached this path and all blamed the network.
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

# $1 = why the live fetch produced nothing, in the reader's words. Every caller passes one: the
# reason is the difference between "check the network" and "check your URL", and the guide_get MCP
# tool is unaffected by any of them, so knowing which happened is what tells you to use it.
print_fallback() {
  WHY="${1:-live fetch failed}"
  if [ -f "$CACHE" ]; then
    echo "> (offline: $WHY; showing last cached copy of '$SECTION')"; echo
    cat "$CACHE"
  elif [ -f "$OFFLINE" ]; then
    echo "> (offline: $WHY; showing bundled framework guide)"; echo
    cat "$OFFLINE"
  else
    echo "> (offline: $WHY; no cached guide available — call the guide_get MCP tool instead)"
  fi
  exit 0
}

# Both come from the shell. The plugin's SessionStart hook exports them from the plugin's own
# config when the shell has not, so this is normally set even on a plugin-only machine; it is empty
# when the plugin holds no value either, or outside a Claude Code session.
if [ -z "${HIVEMIND_SERVER_URL:-}" ] || [ -z "${HIVEMIND_TOKEN:-}" ]; then
  print_fallback "no HIVEMIND_SERVER_URL / HIVEMIND_TOKEN in this shell"
fi
command -v curl >/dev/null 2>&1 || print_fallback "curl is not installed"

URL="${HIVEMIND_SERVER_URL%/}/guide/$SECTION"
INM=""
[ -f "$ETAG" ] && INM="$(cat "$ETAG" 2>/dev/null)"

TMP="$(mktemp 2>/dev/null)" || print_fallback "no writable temporary file"
CODE="$(curl -s -m 8 -o "$TMP" -w '%{http_code}' \
  -H "Authorization: Bearer $HIVEMIND_TOKEN" \
  ${INM:+-H "If-None-Match: $INM"} \
  -D "$CACHE_DIR/.hdr-$SECTION" "$URL" 2>/dev/null)" \
  || { rm -f "$TMP"; print_fallback "no answer from $URL"; }

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
    rm -f "$TMP"
    # The server answered, so this is not unreachability. A 404 off a URL with no /p/<project> in
    # it has one cause worth naming: only /mcp is project-neutral, and /guide is mounted under a
    # project prefix, so the server root can never serve it however valid the token is.
    WHY="$URL answered HTTP $CODE"
    case "$CODE:$HIVEMIND_SERVER_URL" in
      404:*/p/*) ;;
      404:*) WHY="$WHY — that is the server root; /guide is only mounted under /p/<project>/" ;;
    esac
    print_fallback "$WHY" ;;
esac
exit 0
