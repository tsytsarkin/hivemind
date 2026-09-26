"""`deploy/restart.sh` — stop the server, wait for the port, start it again, prove it is serving.

The script exists because every one of its failure modes is silent: a launcher that was never
installed, a port that has not freed yet, a server that died three lines into startup. So what is
worth pinning is not the happy path but that each of those fails *loudly* — non-zero, with the
reason on stdout — and that it does so identically on Linux and on macOS, which share almost none
of the relevant tooling. `ss`, `setsid` and `fuser -k` are Linux-only; `lsof` is the macOS answer;
the script is supposed to ask for whichever one the host actually has.

Two safety rules shape every test here:

  * `pkill` and `pgrep` are both stubbed on PATH — `pkill` to a no-op, `pgrep` to "no matches". The
    real script finds servers by process name with one and signals them with the other, and a suite
    that did either for real would take down a live server the moment someone ran pytest on the
    deploy host. `pgrep` matters as much as `pkill` now that discovery drives a SIGKILL escalation:
    a test that wants a process found overrides the stub with one that reports its own fixture's
    PID and nothing else. The kill that *is* platform-specific — finding and signalling whoever
    holds the port — is exercised for real, against an ephemeral port nothing else is using.
  * HOME and PATH point away from the developer's machine, because the script prefers `uv` when it
    is installed. Left alone, these tests would exercise a different branch on your laptop than in
    CI, which is the kind of coverage that is worse than none.
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
RESTART = REPO / "deploy" / "restart.sh"

# The script's whole verification step is a curl to /healthz; without curl there is nothing to test.
pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="restart.sh verifies via curl")

# Binds the configured port, answers anything with 200, and drops a pidfile so a test can tell one
# instance from the next without depending on lsof being installed.
FAKE_SERVER = """#!@PYTHON@
import os, http.server
port = int(os.environ["HIVEMIND_PORT"])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", port), H)
open(os.path.join(os.environ["HIVEMIND_DATA_DIR"], "server.pid"), "w").write(str(os.getpid()))
print("fake server on %d" % port, flush=True)
srv.serve_forever()
"""

DIES_ON_STARTUP = "#!/bin/sh\necho \"ImportError: no module named 'whatever'\" >&2\nexit 1\n"

# A server wedged in graceful shutdown: it has already released the listening socket and now ignores
# SIGTERM forever, which is what uvicorn does while it waits for a bus WebSocket that never closes.
# It holds no port, so only process-level discovery can find it.
#
# The fork is so the suite is NOT its parent: a SIGKILLed child of the test process lingers as a
# zombie, and both `_gone_within` and the stub `pgrep` below read a zombie as alive. Orphaned, it is
# reaped by init the moment it dies.
WEDGED_SHUTDOWN = """#!@PYTHON@
import os, signal, sys, time
if os.fork():
    sys.exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open(os.environ["WEDGE_PIDFILE"], "w") as fh:
    fh.write(str(os.getpid()))
time.sleep(600)
"""

# Reports one PID, and only while it is genuinely alive — a static `echo` would keep naming it after
# the script had killed it, and the script would then correctly complain that SIGKILL had not worked.
PGREP_REPORTING = "#!/bin/sh\nkill -0 @PID@ 2>/dev/null && echo @PID@\nexit 0\n"


def _free_port():
    # Never hand back something like 58787: one test asserts the default port never appears in the
    # output, and a substring match would fail it for the wrong reason roughly one run in a hundred.
    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if "8787" not in str(port):
            return port
    raise RuntimeError("could not find a free port unlike the default")


def _gone_within(pid, seconds):
    """True once `pid` no longer exists. A dead-but-unreaped process still answers signal 0, so
    this is a poll for the reap, not a check of whether the kill landed."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


def _path_without_uv():
    """Drop every PATH entry that provides `uv`, so the launcher takes its venv branch here whether
    or not the machine running the suite happens to have uv installed."""
    kept = [d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d and not (Path(d) / "uv").exists()]
    return os.pathsep.join(kept)


class Checkout:
    """A throwaway ~/hivemind: the real script, a config, and a stubbed pkill."""

    def __init__(self, root):
        self.root = root
        self.port = _free_port()
        self.data = root / "data"

        (root / "deploy").mkdir(parents=True)
        shutil.copy(RESTART, root / "deploy" / "restart.sh")
        (root / "deploy" / "hivemind.env").write_text(
            f"HIVEMIND_DATA_DIR={self.data}\nHIVEMIND_HOST=127.0.0.1\nHIVEMIND_PORT={self.port}\n"
        )

        self.stub_bin = root / "stub-bin"
        self.stub_bin.mkdir()
        self._stub("pkill", "#!/bin/sh\nexit 0\n")  # see the module docstring
        self._stub("pgrep", "#!/bin/sh\nexit 1\n")  # "no matches", like the real one

    def _stub(self, name, body):
        p = self.stub_bin / name
        p.write_text(body)
        p.chmod(0o755)
        return p

    def install_launcher(self, body=FAKE_SERVER):
        exe = self.root / ".venv" / "bin" / "hivemind-server"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text(body.replace("@PYTHON@", sys.executable))
        exe.chmod(0o755)
        return exe

    def run(self, start_timeout="20"):
        # 20s is a ceiling, not a cost: a healthy start breaks out of the script's poll loop on the
        # first or second second. Only the deliberately-broken fixtures below wait it out, and they
        # pass a short timeout of their own. A tighter bound here was intermittently flaky — each
        # `lsof` the script runs costs ~0.2s, and that adds up on a loaded machine.
        env = dict(os.environ)
        env.pop("HIVEMIND_PORT", None)
        env.update(
            HOME=str(self.root),          # so $HOME/.local/bin cannot smuggle a uv onto PATH
            HIVEMIND_HOME=str(self.root),
            PATH=f"{self.stub_bin}{os.pathsep}{_path_without_uv()}",
            HIVEMIND_START_TIMEOUT=start_timeout,
        )
        return subprocess.run(["bash", str(self.root / "deploy" / "restart.sh")],
                              capture_output=True, text=True, env=env, timeout=180)

    def pid(self):
        return int((self.data / "server.pid").read_text().strip())

    def stop(self):
        try:
            os.kill(self.pid(), signal.SIGKILL)
        except (OSError, ValueError, FileNotFoundError):
            pass
        # The pidfile alone is not enough to clean up after a *failing* test: a server that came up
        # after the script stopped waiting writes its pidfile too late to be read here, and then
        # outlives the suite holding a port. The checkout path is unique per test, so matching on it
        # cannot touch anything but this fixture's own process.
        subprocess.run(["pkill", "-f", str(self.root)], capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    c = Checkout(tmp_path)
    yield c
    c.stop()


def test_it_parses_under_the_system_shell():
    """macOS ships bash 3.2, so a bashism from a newer release is a real portability failure."""
    r = subprocess.run(["bash", "-n", str(RESTART)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_a_missing_config_stops_before_touching_anything(checkout):
    """Without hivemind.env the script cannot know the port, so proceeding would kill and probe
    whatever happens to be on the default 8787 — someone else's server, on a shared box."""
    (checkout.root / "deploy" / "hivemind.env").unlink()
    r = checkout.run()
    assert r.returncode == 1, r.stdout
    assert "hivemind.env.example" in r.stderr, r.stderr


def test_no_launcher_is_refused_instead_of_starting_nothing(checkout):
    """DEPLOY.md documents a uv install and a plain-venv install. When neither is present the old
    script ran `uv` anyway, and the "command not found" went to the log while it printed healthz."""
    r = checkout.run()  # no install_launcher() call: neither uv nor .venv/bin exists
    assert r.returncode == 1, r.stdout
    assert "no launcher" in r.stderr and "DEPLOY.md" in r.stderr, r.stderr


def test_it_starts_the_server_and_confirms_it_is_serving(checkout):
    checkout.install_launcher()
    r = checkout.run()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert '"status":"ok"' in r.stdout, r.stdout
    assert str(checkout.port) in r.stdout, r.stdout


def test_restarting_over_a_live_instance_replaces_it(checkout):
    """The bind race this script was written for: the old instance still holds the port when the
    new one tries to bind, so the new one exits and a wrapper lingers looking healthy."""
    checkout.install_launcher()
    assert checkout.run().returncode == 0
    first = checkout.pid()

    r = checkout.run()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    second = checkout.pid()
    assert second != first, f"pid {second} unchanged — it never actually restarted"
    # And the one that was holding the port is gone, rather than lingering as a wrapper process
    # that makes pgrep report "running" while nothing is listening. Poll rather than sleep a fixed
    # interval: the old process is reaped by init once its shell exits, and that timing is not ours.
    assert _gone_within(first, 10), f"pid {first} still around; it was supposed to be killed"


def test_a_shutdown_that_wedges_is_killed_even_though_it_freed_the_port(checkout):
    """The leak this script existed to prevent and then caused for months. uvicorn closes the
    listening socket the instant it takes SIGTERM and only afterwards drains open connections, so a
    bus WebSocket — which never closes on its own — parks the process in graceful shutdown forever.
    The port comes free, so an escalation gated on the port never fires: measured on the deploy host,
    two servers ~3 days old, holding 15 and 22 handles on the live 8.4 GB database and still serving
    their already-connected peers code that no restart would ever replace, while every run of this
    script reported success. Discovery has to be by process, and the SIGKILL has to reach a process
    that holds no port at all."""
    pidfile = checkout.root / "wedge.pid"
    wedged = checkout.root / "wedged.py"
    wedged.write_text(WEDGED_SHUTDOWN.replace("@PYTHON@", sys.executable))
    wedged.chmod(0o755)
    # Returns as soon as the forking parent exits; the orphan writes the pidfile.
    subprocess.run([sys.executable, str(wedged)], check=True, timeout=30,
                   env=dict(os.environ, WEDGE_PIDFILE=str(pidfile)))
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.1)
    pid = int(pidfile.read_text().strip())
    assert not _gone_within(pid, 0.5), "the wedged fixture died before the test began"

    checkout._stub("pgrep", PGREP_REPORTING.replace("@PID@", str(pid)))
    checkout.install_launcher()
    try:
        r = checkout.run()
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert "escalating to SIGKILL" in r.stdout, r.stdout
        assert _gone_within(pid, 10), (
            f"pid {pid} survived: it freed the port, so nothing escalated to SIGKILL")
        assert "still alive after SIGKILL" not in r.stderr, r.stderr
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_a_server_that_dies_on_startup_exits_nonzero_with_the_reason(checkout):
    """The failure this script is supposed to make loud: it used to print `healthz: FAILED` and
    then exit 0, so `ssh box restart.sh && echo deployed` reported a successful deploy."""
    checkout.install_launcher(DIES_ON_STARTUP)
    r = checkout.run(start_timeout="3")
    assert r.returncode != 0, r.stdout
    assert "FAILED" in r.stdout, r.stdout
    # The reason has to reach the operator, not just the log file.
    assert "ImportError" in r.stdout, r.stdout


def test_it_uses_the_port_from_the_config_not_a_hardcoded_8787(checkout):
    """With 8787 baked in, changing HIVEMIND_PORT left the script killing and probing a port the
    server was never going to bind — reporting a dead server as healthy, or vice versa."""
    checkout.install_launcher()
    r = checkout.run()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert f"port {checkout.port}" in r.stdout, r.stdout
    assert "8787" not in r.stdout, r.stdout


# Emulates the two Linux tools this suite would otherwise never reach. `ss` reports the real state
# of the port in Linux's output format; the decoy rows are the ones the old `grep ':8787'` counted
# as a listener — a peer address that happens to use the port, and a longer port ending in it.
SS_STUB = """#!/bin/bash
echo "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process"
if (exec 3<>/dev/tcp/127.0.0.1/@PORT@) 2>/dev/null; then
    echo "LISTEN 0      128            0.0.0.0:@PORT@        0.0.0.0:*"
fi
echo "LISTEN 0      128            0.0.0.0:22    203.0.113.5:@PORT@"
echo "LISTEN 0      128          127.0.0.1:1@PORT@       0.0.0.0:*"
"""

SETSID_STUB = '#!/bin/sh\nexec "$@"\n'


def test_the_linux_only_branches_are_driven_correctly(checkout):
    """macOS never reaches these, and the deploy host runs nothing else. With `ss` and `setsid`
    stubbed, the script has to detach via setsid and read Linux-format output — picking our
    listener out of rows that the old substring match would have miscounted."""
    checkout.install_launcher()
    checkout._stub("ss", SS_STUB.replace("@PORT@", str(checkout.port)))
    checkout._stub("setsid", SETSID_STUB)

    r = checkout.run()
    assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
    assert "(via setsid)" in r.stdout, r.stdout
    assert f"listening: 0.0.0.0:{checkout.port}" in r.stdout, r.stdout
    # Exactly one listener: the peer-address row and the 1<port> row are not ours.
    assert "port %d in use before start: 0" % checkout.port in r.stdout, r.stdout


def test_uv_is_preferred_over_the_venv_when_both_are_available(checkout):
    """Option A resolves from uv.lock, so it wins when it is installed. The stub uv refuses to run,
    which is enough: what is pinned here is the choice, not the outcome."""
    checkout.install_launcher()
    checkout._stub("uv", "#!/bin/sh\necho 'stub uv declines' >&2\nexit 1\n")
    r = checkout.run(start_timeout="2")
    assert "launching with: uv run" in r.stdout, r.stdout
    assert r.returncode != 0, r.stdout


def test_the_log_is_rotated_not_truncated(checkout):
    """`>"$LOG"` on a file the OUTGOING server still holds open does not move its file offset — it
    only sets the length to 0. The dying process then writes at its stale offset and the kernel
    zero-fills the gap, so the log becomes one enormous sparse line of NULs.

    Measured on the deploy host before this fix: apparent size 1.3 MB against 8 KB on disk, a single
    line of 1,340,225 characters ending in "Waiting for connections to close" — the outgoing
    server's last words, landed a megabyte into the file the incoming one had just emptied.

    A rename leaves the old descriptor pointing at the old inode, so those last words go to .1 and
    the new log starts at offset 0.
    """
    body = (checkout.root / "deploy" / "restart.sh").read_text()
    assert 'mv -f "$LOG" "$LOG.1"' in body, \
        "the log must be renamed before launch; truncating one a live process holds open leaves a hole"
    launch = body[body.index("$DETACH bash -c"):]
    assert launch.index('>"$LOG"') > 0, "the new server should still write to a fresh $LOG"
    assert 'mv -f "$LOG"' in body[:body.index("$DETACH bash -c")], \
        "the rotation must happen BEFORE the launch, or the new server's own output is moved aside"


def test_the_log_tail_is_bounded_in_bytes_not_only_lines(checkout):
    """"Five lines" is not a size. One pathological line — exactly what the bug above produced —
    made every restart print a megabyte through whatever ssh the operator was reading."""
    body = (checkout.root / "deploy" / "restart.sh").read_text()
    tail = [l for l in body.splitlines() if l.strip().startswith("tail -5")]
    assert tail, "no tail of the log found"
    assert any("cut -c" in l or "head -c" in l or "tail -c" in l for l in tail), \
        f"the log tail is unbounded in bytes: {tail}"
