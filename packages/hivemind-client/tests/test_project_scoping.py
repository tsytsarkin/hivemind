"""Where a call goes, and which project it names, for both shapes of base URL.

Since plugin 1.2.0 the configured address is the SERVER ROOT, so the project can no longer come from
the URL. It comes from `Client(project=…)` (the CLI's `--project` / `$HIVEMIND_PROJECT`), and the two
surfaces need it in different places: an MCP call carries it as a tool ARGUMENT to `<root>/mcp`,
while the REST paths — blobs, guide, catalogs, the bus socket — only exist under `/p/<project>/`.

The pre-1.2.0 project base URL must keep behaving exactly as it did, which is what the four e2e
files exercise; the tests here pin the parts of that no e2e file can see, such as the absence of a
`project` argument on the wire.
"""
import httpx
import pytest

from hivemind import Client, HivemindError

ROOT = "http://stub.invalid"
PROJ = "http://stub.invalid/p/t"

# ── the MCP surface ──────────────────────────────────────────────────────────────────────────
def test_a_root_url_sends_the_project_as_an_argument_to_the_root_endpoint(fake_transport):
    c = fake_transport([{"ok": True}], base_url=ROOT, project="nik.private")
    c.call("graph_types")
    assert c.urls == ["http://stub.invalid/mcp"]
    assert c.sent[0]["arguments"]["project"] == "nik.private"


def test_a_project_url_sends_no_project_argument_at_all(fake_transport):
    """The old shape, unchanged: the server takes the project from the URL it was called on, and a
    client that started sending one anyway would silently change which project an omitted argument
    means for every existing caller."""
    c = fake_transport([{"ok": True}])
    c.call("graph_types")
    assert c.urls == ["http://stub.invalid/p/t/mcp"]
    assert "project" not in c.sent[0]["arguments"], c.sent[0]["arguments"]


def test_a_per_call_project_wins_over_the_clients_own(fake_transport):
    """What makes a cross-project batch possible from one client."""
    c = fake_transport([{"ok": True}], base_url=ROOT, project="nik.private")
    c.call("graph_types", {"project": "shared"})
    assert c.sent[0]["arguments"]["project"] == "shared"


def test_an_explicit_none_project_is_filled_in_rather_than_sent_as_null(fake_transport):
    """The CLI builds whole argument dicts with `None` for every flag nobody passed. `None` on the
    wire is "not given" to the server, so a client default that used `setdefault` would be dropped
    by exactly the caller that needs it most."""
    c = fake_transport([{"ok": True}], base_url=ROOT, project="nik.private")
    c.call("graph_types", {"project": None})
    assert c.sent[0]["arguments"]["project"] == "nik.private"


def test_an_empty_project_is_no_project_not_a_project_named_empty(fake_transport):
    """`os.environ.get("HIVEMIND_PROJECT")` is "" for a variable exported empty."""
    c = fake_transport([{"ok": True}], base_url=ROOT, project="")
    c.call("graph_types")
    assert "project" not in c.sent[0]["arguments"], c.sent[0]["arguments"]


# ── the REST surface ─────────────────────────────────────────────────────────────────────────
def test_blobs_go_under_the_project_prefix_on_a_root_url():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url)))
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(201, json={"size": 3})

    c = Client(ROOT, "tok", project="nik.private")
    c._http.close()
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    import tempfile, os
    fd, path = tempfile.mkstemp()
    os.write(fd, b"abc"); os.close(fd)
    try:
        c.artifacts.put(path)
    finally:
        os.unlink(path); c.close()
    import hashlib
    digest = hashlib.sha256(b"abc").hexdigest()
    assert seen[0] == ("HEAD", "http://stub.invalid/p/nik.private/blobs/sha256/" + digest)
    assert seen[1][0] == "PUT" and "/p/nik.private/blobs/sha256/" + digest in seen[1][1]


def test_blob_urls_are_unchanged_for_a_project_base_url():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200)

    c = Client(PROJ, "tok")
    c._http.close()
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        out = c.artifacts.put(__file__)
    finally:
        c.close()
    assert out["deduplicated"] is True            # the HEAD said 200, so no upload
    assert seen[0].startswith("http://stub.invalid/p/t/blobs/sha256/"), seen


def test_a_rest_path_with_no_project_anywhere_names_the_missing_project():
    """Off the root these paths do not exist, and the server's 404 would read as "no such blob"
    rather than "you never said which project". So the client refuses before the request."""
    c = Client(ROOT, "tok")
    try:
        with pytest.raises(HivemindError) as e:
            c.project_url("/blobs/sha256/x")
    finally:
        c.close()
    assert e.value.kind == "no_project"
    msg = str(e.value)
    assert "--project" in msg and "HIVEMIND_PROJECT" in msg, msg


def test_an_explicit_project_beats_one_in_the_base_url():
    """A caller who passed `--project` asked for it. Acting in the URL's project instead would make
    the flag a lie — and `hivemind --project other artifact put` would upload to the wrong graph."""
    c = Client(PROJ, "tok", project="other")
    try:
        assert c.project_url("/blobs/x") == "http://stub.invalid/p/other/blobs/x"
    finally:
        c.close()


def test_the_bus_socket_is_under_the_project_prefix_too():
    """`hivemind bus listen` builds its ws URL from the client, and /bus/ws is mounted per project —
    off the root there is no such route."""
    c = Client(ROOT, "tok", project="nik.private")
    try:
        assert c.project_url("/bus/ws") == "http://stub.invalid/p/nik.private/bus/ws"
    finally:
        c.close()


# ── health: the one path that answers off the root, and proves nothing ───────────────────────
@pytest.mark.parametrize("base", [ROOT, PROJ])
def test_health_reads_healthz_off_the_server_root_either_way(base):
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={"ok": True})

    c = Client(base, "tok", project="nik.private")
    c._http.close()
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert c.health() == {"ok": True}
    finally:
        c.close()
    # No project in the path and no token on the request: this is liveness only. Nothing may read
    # `hivemind health` as evidence that a tool call or an upload is configured to work.
    assert seen == [("http://stub.invalid/healthz", None)], seen
