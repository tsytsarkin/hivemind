"""Every command this repository INVOKES must be a command that exists.

`deploy/maintenance.sh` ran `hivemind-admin --project "$proj" bus-reap` from cron at 03:45 on every
project for as long as the WebSocket rewrite had been merged. `bus-reap` was a v1 subcommand and was
deleted with the rest of the polling bus, so the step failed every night with
`argument cmd: invalid choice: 'bus-reap'` — invisibly, because the line ends in `|| echo`.

The sweep that was supposed to catch it built the CLI inventory from argparse and diffed it against
*documentation only*, so the one phantom that is actually executed was the one it could not see.
This test diffs the same inventory against the files that RUN: every tracked executable, shell
script, systemd unit and config file. Docs are included too, so nothing the earlier sweep covered is
dropped.

The inventory is read from the live argparse parsers rather than from a hand-written list, so
deleting a subcommand is what makes this fail — exactly as `bus-reap` should have.
"""
import argparse
import re
import subprocess
from pathlib import Path

from hivemind import cli as client_cli
from hivemind_server import admin as admin_mod

REPO = Path(__file__).resolve().parents[3]

# Extensions that are read as "a file that runs or configures something". Anything tracked and
# executable is included whatever its name.
RUNNABLE_EXT = (".sh", ".service", ".yml", ".yaml", ".json", ".toml", ".env", ".example",
                ".cfg", ".ini", ".conf", ".md")

# Options of the binary ITSELF, which take a value and sit before the subcommand. Anything else
# starting with `-` is assumed to be a flag without a value, which is safe: over-consuming an
# argument could hide a phantom, under-consuming one can only produce a token that fails the
# `_SUBCOMMAND` shape check below and is skipped.
GLOBAL_OPTS = {
    "hivemind-admin": {"--project"},
    "hivemind": {"--url", "--token", "--agent"},
    "hivemind-server": set(),
}

# A real subcommand looks like this and nothing else. Prose that merely mentions a binary —
# "`hivemind-admin` in another process", "puts `hivemind` on PATH" — yields a token that either
# fails this or is skipped as a word; it is the reason this test is not a grep.
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9-]*$")

# Words that follow a binary in an English sentence and would otherwise read as a subcommand.
# Kept explicit and short: a real phantom is never one of these, and a growing list here would be
# the test losing its teeth.
_PROSE = {"in", "on", "and", "is", "was", "works", "server", "client", "from", "to", "with",
          "has", "at", "for", "of", "as"}


def _inventory(build):
    """{subcommand: [sub-subcommands]} for one argparse CLI, harvested from the live parser."""
    seen = []
    real = argparse.ArgumentParser.add_subparsers

    def spy(self, *a, **k):
        sp = real(self, *a, **k)
        seen.append(sp)
        return sp

    argparse.ArgumentParser.add_subparsers = spy
    try:
        build()
    except SystemExit:                      # --help exits; the parser is built by then
        pass
    finally:
        argparse.ArgumentParser.add_subparsers = real
    if not seen:
        return {}
    out = {}
    for name, parser in seen[0].choices.items():     # seen[0] is the TOP-level subparser
        nested = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        out[name] = set(nested[0].choices) if nested else set()
    return out


def _inventories():
    return {
        "hivemind-admin": _inventory(lambda: admin_mod._run(["--help"])),
        "hivemind": _inventory(lambda: client_cli.main(["--help"])),
        "hivemind-server": {},                        # an entrypoint, no subcommands
    }


def _runnable_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout
    for rel in out.splitlines():
        p = REPO / rel
        if not p.is_file():
            continue
        if rel.endswith(RUNNABLE_EXT) or (p.stat().st_mode & 0o111):
            yield rel, p


def _invocations(line, binary, global_opts):
    """Every (subcommand, rest) this line invokes on `binary`. Empty for a prose mention."""
    for m in re.finditer(r"(?<![\w./-])" + re.escape(binary) + r"(?![\w.-])", line):
        # A shell separator or a comment ends the invocation; everything after belongs to
        # another command or to prose.
        rest = re.split(r"[|;&)#]|\\\s*$", line[m.end():])[0]
        # Quotes and backticks are stripped because a doc writes `hivemind bus send`; a COMMA is
        # deliberately not, because no shell invocation glues one to its subcommand while English
        # does — that is what separates `hivemind bus send` from "stream hivemind bus messages,
        # one line per frame" without a growing list of English words.
        toks = [t.strip("\"'`") for t in rest.split()]
        toks = [t for t in toks if t]
        i = 0
        while i < len(toks) and toks[i].startswith("-"):
            i += 2 if toks[i] in global_opts else 1
        if i >= len(toks):
            continue
        yield toks[i], toks[i + 1:]


def test_every_command_this_repo_invokes_exists():
    """The regression guard for the `bus-reap` class: a deleted subcommand that a script still runs.

    Fails loudly rather than at 03:45 behind an `|| echo`.
    """
    inv = _inventories()
    phantoms = []
    for rel, path in _runnable_files():
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for binary, subs in inv.items():
                if binary not in line:
                    continue
                for sub, tail in _invocations(line, binary, GLOBAL_OPTS[binary]):
                    if not _SUBCOMMAND.match(sub) or sub in _PROSE:
                        continue                      # prose, a shell variable, or a path
                    if not subs:
                        continue                      # the binary takes no subcommand
                    if sub not in subs:
                        phantoms.append(f"{rel}:{lineno}: `{binary} {sub}` — no such subcommand "
                                        f"(have: {', '.join(sorted(subs))})")
                    elif subs[sub]:
                        nxt = next((t for t in tail if not t.startswith("-")), None)
                        if (nxt and _SUBCOMMAND.match(nxt) and nxt not in _PROSE
                                and nxt not in subs[sub]):
                            phantoms.append(
                                f"{rel}:{lineno}: `{binary} {sub} {nxt}` — no such subcommand "
                                f"(have: {', '.join(sorted(subs[sub]))})")
    assert not phantoms, ("commands invoked that do not exist:\n  "
                          + "\n  ".join(sorted(set(phantoms))))


def test_the_sweep_can_actually_see_a_phantom():
    """The control the previous sweep did not have.

    A test that only ever reports "no phantoms" is indistinguishable from one whose scanner matches
    nothing at all — and that is precisely how `bus-reap` survived a sweep. So: feed the scanner the
    real deleted subcommand in the real line that ran it, and require that it comes back.
    """
    inv = _inventories()
    line = ('  uv run --package hivemind-server hivemind-admin --project "$proj" bus-reap \\')
    found = list(_invocations(line, "hivemind-admin", GLOBAL_OPTS["hivemind-admin"]))
    assert found and found[0][0] == "bus-reap", f"the scanner did not find the subcommand: {found}"
    assert "bus-reap" not in inv["hivemind-admin"], "bus-reap is back; this test is now wrong"

    # And a real one must NOT be flagged, or the test above would pass by rejecting everything.
    ok = list(_invocations('hivemind-admin --project "$proj" gc --yes', "hivemind-admin",
                           GLOBAL_OPTS["hivemind-admin"]))
    assert ok and ok[0][0] == "gc" and ok[0][0] in inv["hivemind-admin"]
