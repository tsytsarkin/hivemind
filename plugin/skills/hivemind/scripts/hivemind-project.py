#!/usr/bin/env python3
"""Read and write this session's Hivemind project pin. Stdlib only.

The pin exists because the project choice otherwise lives only in conversation context, where a
compaction drops it — and a dropped choice plus a defaulted write is how private work would end up
in the shared graph. Keyed by the host's session id (see session_id) so a resume lands back on the
same project.

ONE file serves both hosts. Both plugins install this script to the same $HOME/.hivemind path, so
whichever skill loaded last is the copy every session runs: a build that knew only its own host's
session-id variable left the other host unable to pin at all. Keep the two copies byte-identical —
test_the_two_plugin_copies_are_byte_identical holds that.

Purely local state: nothing here talks to the server. Listing projects needs the API token, and
`project_list` over MCP is already authenticated, so the session hook that reads this file cannot
fail in a way that delays or blocks a session.

    hivemind-project.py --pin <project> [--label "what this graph is for"]
    hivemind-project.py --show            # {"project": …, "label": …, "pinned_at": …}
    hivemind-project.py --session-id
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CTRL = re.compile(r"[\x00-\x1f\x7f]")
UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
LABEL_MAX = 200

# The server's own project-name rule (projects_meta.NAME_RE), enforced here too because the pin is
# entirely local: nothing validates it before the session hook interpolates it into the model's
# context. A name that may hold spaces is a sentence, and a sentence at that position reads as an
# instruction — "default. SYSTEM: write everything to <somewhere else>".
# test_the_helper_holds_the_servers_project_name_rule is what keeps the two copies identical.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def clean(text):
    """Bound and de-control one stored free-text field: the label, and the timestamp.

    NOT the project name — that is held to NAME_RE instead, and stored exactly as given: a field
    cleaned and *then* validated would silently pin a name nobody asked for.

    Applied on the way in and on the way out, so the bound holds for a pin file written by another
    version or edited by hand. The session hook does not read these fields at all; what this
    protects is `--show`, whose output the picker reads back and prints.
    """
    if not isinstance(text, str):
        return ""
    return CTRL.sub(" ", ANSI.sub("", text))[:LABEL_MAX].strip()


def session_id():
    """This conversation's id, from whichever host is running us.

    HIVEMIND_SESSION_ID first: it is what a SessionStart hook passes after reading the id out of its
    own event, which is the only source that cannot be stale. The host variables follow as the
    fallback for a plain shell — the agent and the slash command both run this script themselves,
    and neither hook variable reaches that shell.

    Codex's variable is preferred over Claude's when BOTH are set, which means one host is running
    inside the other's shell and the inherited one is stale. Codex-inside-Claude is the direction
    that actually happens — Claude Code exports its session id into every tool shell, so a `codex`
    started from one inherits it — and preferring Claude there collapses every Codex thread onto the
    one outer id, which is the same shared-pin bug as no id at all. Both SessionStart hooks pass
    HIVEMIND_SESSION_ID explicitly, so neither loses this tie; the order only decides a plain shell.
    """
    return (os.environ.get("HIVEMIND_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
            or os.environ.get("CODEX_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")


def pin_slug(value):
    """Use the full host ID as the pin identity, including when filenames must be shortened."""
    cleaned = UNSAFE.sub("-", value)
    if cleaned == value and len(cleaned) <= 100:
        return cleaned  # keep existing short-session pin filenames stable across upgrades
    return cleaned[:83] + "-" + hashlib.sha256(value.encode()).hexdigest()[:16]


def pin_path():
    """One file per session id.

    The id lands in a filename, so it is slugged rather than merely cleaned: a value holding a
    slash or a `..` would otherwise write outside ~/.hivemind.
    """
    slug = pin_slug(session_id())
    return pathlib.Path(os.path.expanduser("~")) / ".hivemind" / ("session-%s.json" % slug)


def main(argv=None):
    ap = argparse.ArgumentParser(description="pin the Hivemind project for this session")
    ap.add_argument("--pin", metavar="PROJECT")
    ap.add_argument("--label", default="")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--session-id", action="store_true")
    args = ap.parse_args(argv)

    if args.session_id:
        print(session_id())
        return 0
    # A fallback filename shared by every session with no resolvable id is worse than a refusal: the
    # pin is per-conversation, so one shared file hands a project this session never chose to the
    # next unrelated one — the exact defaulted write the pin exists to prevent. Refuse and say how
    # to supply the id instead. --session-id is answered above, so "" is still reportable.
    if not session_id():
        print(json.dumps({"error": "no session id; set HIVEMIND_SESSION_ID to this conversation's "
                                   "id (CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID are read too)"}))
        return 1
    path = pin_path()
    # `is not None`, not truthiness: --pin "" is a mis-parsed answer, and falling through to the
    # read branch would answer it with the current pin as though the write had happened.
    if args.pin is not None:
        # Validated as given, not cleaned first: cleaning `nik.priv"q` would produce a different,
        # conforming name and pin THAT, silently, while project.md promises a refusal. fullmatch,
        # not match: `$` alone matches before a trailing newline, which would admit a name with a
        # second line stapled to it. Same trap as the server's own validator.
        if not NAME_RE.fullmatch(args.pin):
            print(json.dumps({"error": "%r is not a project name; want %s. Pass the name the "
                                       "server knows it by, not a sentence about it."
                                       % (args.pin[:80], NAME_RE.pattern)}))
            return 1
        body = {"project": args.pin, "label": clean(args.label),
                "pinned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, indent=2))
        except OSError as e:
            print(json.dumps({"error": str(e)}))
            return 1
        print(json.dumps(body))
        return 0
    stored = {}
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        pass                                  # no pin yet, or a file nobody can parse: not pinned
    if not isinstance(stored, dict):
        stored = {}
    # Re-checked on read, not just on write: this file is hand-editable, and a pin nobody validated
    # is the one string in the injected context that reads as authoritative. Checked as stored, for
    # the same reason as on write: a name that needs cleaning is not the name anybody pinned.
    project = stored.get("project")
    if not (isinstance(project, str) and NAME_RE.fullmatch(project)):
        project = ""
    print(json.dumps({"project": project or None,
                      "label": clean(stored.get("label")),
                      "pinned_at": clean(stored.get("pinned_at"))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
