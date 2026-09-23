"""The blob store's contract with its callers, pinned by the three ways it broke in the field.

Each test names the cost the bug imposed, because the cost is what decides the behaviour: a
false "orphaned" answer gets a 20 GB artifact re-uploaded, an advertised-but-ignored Range gets a
chunked download silently assembled out of repeated file heads, and a size cap that fires after
the body is drained burns the whole transfer before refusing it.
"""
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest
from hivemind_server import graph
from hivemind_server.db import Invalid, NotFound

from conftest import Lifespan


# ── helpers ───────────────────────────────────────────────────────────────────────────
def _put(store, body: bytes) -> str:
    """Store bytes through the same finalize path the REST upload uses."""
    d = "sha256:" + hashlib.sha256(body).hexdigest()
    tmp = store.new_tmp()                       # returns a str, not a Path
    Path(tmp).write_bytes(body)
    store.finalize_written(tmp, d, len(body), "application/octet-stream", "test")
    return d


def _a_version(db) -> str:
    """A real node_version id: attach() validates from_version_id, so 'v1' is not attachable."""
    return graph.upsert_node(db, "test", "finding", {"title": "carrier"})["version_id"]


def _seed_blob_rows(store, digests) -> None:
    """Rows only, no bytes. resolve_digest reads the `blob` table, so this is enough to build
    the ambiguity that two real sha256es essentially never produce."""
    with store.db.write("test", "seed blob rows") as tx:
        for d in digests:
            tx.cur.execute(
                "INSERT INTO blob(digest,size,media_type,created_tx) VALUES(?,?,?,?)",
                (d, 0, None, tx.tx_id))


# ── fixtures ──────────────────────────────────────────────────────────────────────────
@pytest.fixture()
def store(tmp_path, db):
    from hivemind_server.blobs import BlobStore
    return BlobStore(tmp_path / "blobs", db, max_bytes=1 << 20, grace_seconds=0)


def _build(monkeypatch, tmp_path, **env):
    """Boot the real app against a fresh data dir. Env is read at Config() time, so anything a
    test needs to change about the server (the blob cap) has to be set before this runs."""
    monkeypatch.setenv("HIVEMIND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HIVEMIND_PROJECTS_DIR", str(tmp_path / "data" / "projects"))
    monkeypatch.setenv("HIVEMIND_ALLOWED_HOSTS", "*")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from hivemind_server import app as appmod
    from hivemind_server.config import Config
    cfg = Config()
    application = appmod.build_app(cfg)
    proj = application.state.registry.all()[0]
    tok = next(iter(json.loads((proj.dir / "tokens.json").read_text())))
    return application, cfg, proj, tok


@pytest.fixture()
async def client_for_blobs(tmp_path, monkeypatch):
    """A live client plus one stored blob LARGER than 64 KiB.

    The size is not decoration: the field failure split exactly at 64 KiB, where the caller's
    harness switched to ranged reads, so a fixture body smaller than that would make the
    reassembly test nominal.
    """
    application, _cfg, proj, tok = _build(monkeypatch, tmp_path)
    body = os.urandom(200_000)
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(
            transport=transport, base_url=f"http://t/p/{proj.name}", timeout=30,
            headers={"Authorization": f"Bearer {tok}"}) as c:
        r = await c.put(f"/blobs/sha256/{digest.split(':')[1]}", content=body)
        assert r.status_code == 201, r.text
        yield c, digest, body


@pytest.fixture()
async def env_for_blobs(tmp_path, monkeypatch):
    """A live client against a server whose blob cap is small enough to actually exceed.

    The real cap is 2 GiB; a test that sent max_blob_bytes+1 bytes at that setting would be the
    very 4 GB transfer this task exists to avoid burning.
    """
    application, cfg, proj, tok = _build(monkeypatch, tmp_path, HIVEMIND_MAX_BLOB="65536")
    assert cfg.max_blob_bytes == 65536
    over = b"x" * (cfg.max_blob_bytes + 1)
    digest = hashlib.sha256(over).hexdigest()            # bare hex: it goes in the URL
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(
            transport=transport, base_url=f"http://t/p/{proj.name}", timeout=30,
            headers={"Authorization": f"Bearer {tok}"}) as c:
        yield c, cfg, digest


# ── 15a. a truncated digest must not look like an orphan ──────────────────────────────
def test_a_truncated_digest_of_an_attached_blob_reports_its_refs(store, db):
    """The field failure: 8 ATTACHED PoCs all reported orphaned on 17-char digests."""
    d = _put(store, b"hello")
    store.attach("test", d, _a_version(db), role="poc", filename="poc.py")
    assert store.refs(d[:17]) == store.refs(d) != []


def test_a_digest_that_matches_nothing_raises_instead_of_reporting_orphaned(store):
    """'Orphaned' means stored and unreferenced. Never absent, and never malformed."""
    with pytest.raises((Invalid, NotFound)):
        store.refs("sha256:" + "ab" * 32)      # well-formed, 64 chars, absent
    with pytest.raises(Invalid):
        store.refs("nonsense")


def test_a_real_orphan_still_answers_empty(store):
    d = _put(store, b"hello")
    assert store.refs(d) == []


def test_a_unique_prefix_resolves(store):
    d = _put(store, b"hello")
    assert store.resolve_digest(d[7:19]) == d      # bare hex prefix
    assert store.resolve_digest(d[:19]) == d       # sha256:-qualified prefix
    assert store.resolve_digest(d) == d            # a full digest is its own answer


def test_an_ambiguous_prefix_is_refused_rather_than_guessed(store):
    a, b = "sha256:" + "ab" * 31 + "c1", "sha256:" + "ab" * 31 + "c2"
    _seed_blob_rows(store, [a, b])
    with pytest.raises(Invalid) as e:
        store.resolve_digest("ab" * 10)
    assert "ambiguous" in str(e.value).lower()
    assert store.resolve_digest(a) == a            # each is still reachable in full


def test_a_prefix_matching_nothing_is_not_found(store):
    with pytest.raises(NotFound):
        store.resolve_digest("beefbeefbeef")


def test_a_prefix_below_the_floor_is_refused(store):
    """A 4-character prefix would collide constantly; refuse rather than resolve by luck."""
    with pytest.raises(Invalid):
        store.resolve_digest("beef")


def test_pin_refuses_a_digest_it_cannot_resolve_and_accepts_a_prefix(store):
    d = _put(store, b"hello")
    with pytest.raises((Invalid, NotFound)):
        store.pin("test", "sha256:" + "ab" * 32)   # absent: an unpinnable pin is a silent no-op
    assert store.pin("test", d[:19])["digest"] == d


def test_the_full_length_fast_path_still_proves_existence(store):
    """A 64-character digest resolves by primary key rather than by scanning with LIKE — but it
    must still QUERY. Returning f"sha256:{raw}" unchecked is the short-circuit that let a digest
    nobody ever uploaded answer exactly like a stored orphan. This pins the fast path against
    being "optimised" back into that bug."""
    d = _put(store, b"hello")
    assert store.resolve_digest(d) == d
    for absent in ("sha256:" + "ab" * 32, "ab" * 32):        # qualified and bare, both absent
        with pytest.raises(NotFound):
            store.resolve_digest(absent)


def test_a_non_sha256_algorithm_is_refused(store):
    """Dropping the algorithm and keeping the hex would let md5:<hex> resolve against a sha256
    blob — validation that exists one method away in parse_digest and was not being called, which
    is the same shape as the bug this whole task is about."""
    _put(store, b"hello")
    with pytest.raises(Invalid):
        store.resolve_digest("md5:" + "ab" * 16)
    with pytest.raises(Invalid):
        store.refs("md5:" + "ab" * 32)


def test_a_digest_longer_than_a_sha256_is_refused(store):
    """66 hex characters is not a prefix of anything; NotFound would misdescribe the input."""
    with pytest.raises(Invalid):
        store.resolve_digest("ab" * 33)


def test_an_empty_refs_list_does_not_mean_orphaned(store, db):
    """refs() == [] is strictly NARROWER than orphaned, and the docs must not conflate them.

    A digest recorded only in a node's props has no blob_ref row at all, yet it is a GC root —
    282 blobs (1.8 GB) on the live graph are reachable only that way. An agent told that an empty
    refs list means "orphaned, about to be collected" re-uploads or re-attaches a blob that was
    never at risk: the same false-orphan cost this task exists to kill, one level up.
    """
    d = _put(store, b"hello")
    graph.upsert_node(db, "test", "finding", {"title": "evidence", "artifact": d})
    assert store.refs(d) == []                              # nothing ATTACHED it, but ...
    assert store.orphans()["unattached_blobs"] == 0         # ... it is not orphaned, and
    assert store.gc(dry_run=True)["kept_referenced_in_props"] == 1     # ... GC keeps it


def test_gc_survives_a_blob_row_whose_digest_is_not_well_formed(tmp_path, db):
    """refs() now RAISES where it used to return []. One junk row must not abort the sweep.

    A negative grace puts the cutoff in the future so every row is eligible, which makes the
    sweep certainly reach the junk row instead of keeping it as 'young'.
    """
    from hivemind_server.blobs import BlobStore
    s = BlobStore(tmp_path / "blobs-gc", db, max_bytes=1 << 20, grace_seconds=-86400)
    d = _put(s, b"hello")
    _seed_blob_rows(s, ["sha256:not-a-digest"])
    out = s.gc(dry_run=False)                   # Invalid on the junk row: must not propagate
    assert out["unreferenced"] == 2             # both rows were considered ...
    assert out["deleted"] == 1                  # ... and the real one was still collected
    assert not s.path_for(d).exists()


# ── 15b. Range is honoured, or it stops being advertised ──────────────────────────────
@pytest.mark.anyio
async def test_a_ranged_get_returns_206_and_only_that_range(client_for_blobs):
    c, digest, body = client_for_blobs
    r = await c.get(f"/blobs/sha256/{digest.split(':')[1]}", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert r.content == body[:10]
    assert r.headers["content-range"] == f"bytes 0-9/{len(body)}"
    assert r.headers["content-length"] == "10"


@pytest.mark.anyio
async def test_an_open_ended_and_a_suffix_range_both_work(client_for_blobs):
    c, digest, body = client_for_blobs
    hexd = digest.split(":")[1]
    r = await c.get(f"/blobs/sha256/{hexd}", headers={"Range": "bytes=10-"})
    assert r.status_code == 206 and r.content == body[10:]
    r = await c.get(f"/blobs/sha256/{hexd}", headers={"Range": "bytes=-5"})
    assert r.status_code == 206 and r.content == body[-5:]


@pytest.mark.anyio
async def test_an_unsatisfiable_range_is_416_with_the_real_size(client_for_blobs):
    c, digest, body = client_for_blobs
    r = await c.get(f"/blobs/sha256/{digest.split(':')[1]}",
                    headers={"Range": f"bytes={len(body) + 10}-"})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{len(body)}"
    # The blob is still immutable and still the same entity, so a 416 keeps the cache identity
    # the 200 and the 206 carry — the brief asks for these on every path.
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["etag"] == f'"{digest}"'


@pytest.mark.anyio
async def test_reassembling_ranges_reproduces_the_body_across_the_64k_boundary(client_for_blobs):
    """The field failure split exactly at 64 KiB, so cross that boundary deliberately."""
    c, digest, body = client_for_blobs
    hexd, out, step = digest.split(":")[1], b"", 65536
    for start in range(0, len(body), step):
        r = await c.get(f"/blobs/sha256/{hexd}",
                        headers={"Range": f"bytes={start}-{min(start + step, len(body)) - 1}"})
        assert r.status_code == 206, r.status_code
        out += r.content
    assert out == body, "assembled ranges must equal the original, not repeat its head"


@pytest.mark.anyio
async def test_an_unranged_get_is_still_200_and_whole(client_for_blobs):
    c, digest, body = client_for_blobs
    r = await c.get(f"/blobs/sha256/{digest.split(':')[1]}")
    assert r.status_code == 200 and r.content == body


@pytest.mark.anyio
async def test_a_partial_response_keeps_the_caching_identity_of_the_whole(client_for_blobs):
    """A resumed download validates its chunks against the ETag, so 206 must carry the same one
    as 200 — and must keep advertising that ranges work."""
    c, digest, body = client_for_blobs
    hexd = digest.split(":")[1]
    whole = await c.get(f"/blobs/sha256/{hexd}")
    part = await c.get(f"/blobs/sha256/{hexd}", headers={"Range": "bytes=0-9"})
    assert part.headers["etag"] == whole.headers["etag"] == f'"{digest}"'
    assert part.headers["cache-control"] == whole.headers["cache-control"]
    assert "immutable" in part.headers["cache-control"]
    assert part.headers["accept-ranges"] == "bytes"


@pytest.mark.anyio
async def test_an_unparseable_range_falls_back_to_the_whole_body(client_for_blobs):
    """A header we cannot read is not a client error — RFC 9110 says ignore it."""
    c, digest, body = client_for_blobs
    hexd = digest.split(":")[1]
    for bad in ("items=0-9", "bytes=abc", "bytes=", "nonsense"):
        r = await c.get(f"/blobs/sha256/{hexd}", headers={"Range": bad})
        assert (r.status_code, r.content) == (200, body), bad


@pytest.mark.anyio
async def test_a_multi_range_request_is_answered_with_the_whole_body(client_for_blobs):
    """Documented choice: multipart/byteranges is not implemented, so a multi-range request is
    answered by ignoring Range (a 200 with everything), which is always a correct answer."""
    c, digest, body = client_for_blobs
    r = await c.get(f"/blobs/sha256/{digest.split(':')[1]}",
                    headers={"Range": "bytes=0-9,20-29"})
    assert r.status_code == 200 and r.content == body


# ── 15c. the size cap precedes the body ───────────────────────────────────────────────
@pytest.mark.anyio
async def test_an_oversized_declared_length_is_refused_before_the_body_is_read(env_for_blobs):
    """413 must arrive without the body being drained: the field cost is a burned 4 GB upload.

    `sent` is counted inside the request-body generator, which httpx's ASGI transport pulls
    lazily — one chunk per `receive()` the app makes. So a non-zero count means the server
    asked for bytes. The control below proves the counter is not simply inert.
    """
    c, cfg, digest = env_for_blobs
    sent = 0

    async def body():
        nonlocal sent
        for _ in range(4):
            sent += 1024
            yield b"x" * 1024

    r = await c.put(f"/blobs/sha256/{digest}", content=body(),
                    headers={"Content-Length": str(cfg.max_blob_bytes + 1)})
    # Asserted FIRST: it is the headline claim, and a status-first order would report a
    # regression as a plain status mismatch and never show whether the body was drained.
    assert sent == 0, f"the body must not have been consumed; {sent} bytes were pulled"
    assert r.status_code == 413, r.text
    assert "max_blob_bytes" in r.text or "too large" in r.text.lower()


@pytest.mark.anyio
async def test_the_same_generator_body_IS_consumed_when_it_is_accepted(env_for_blobs):
    """Positive control for the assertion above. Without it, `sent == 0` could be passing
    because httpx never uses the generator at all, and would keep passing with the fix removed.
    """
    c, cfg, _digest = env_for_blobs
    sent = 0
    payload = b"x" * 1024

    async def body():
        nonlocal sent
        for _ in range(4):
            sent += 1024
            yield payload

    d = hashlib.sha256(payload * 4).hexdigest()
    r = await c.put(f"/blobs/sha256/{d}", content=body(),
                    headers={"Content-Length": str(4096)})
    assert r.status_code == 201, r.text
    assert sent == 4096, "the counter must register a body the server does read"


@pytest.mark.anyio
async def test_a_stream_that_exceeds_the_cap_without_declaring_it_still_fails(env_for_blobs):
    """Content-Length is a hint, not a guarantee — the streaming guard must remain.

    Sent as an async generator with NO Content-Length, so httpx uses chunked encoding and the
    declared-length check cannot be what produces the 413.
    """
    c, cfg, digest = env_for_blobs
    chunk = b"x" * 4096
    n = cfg.max_blob_bytes // len(chunk) + 2

    async def body():
        for _ in range(n):
            yield chunk

    r = await c.put(f"/blobs/sha256/{digest}", content=body())
    assert r.status_code == 413, r.text
    assert "max_blob_bytes" in r.text or "too large" in r.text.lower()


@pytest.mark.anyio
async def test_the_streaming_guard_stops_early_and_leaves_no_temp_file(env_for_blobs):
    """The abort must happen at the crossing, not at the end: a cap enforced after the last byte
    is a cap that still burned the transfer. Measured by how much of the body was pulled.
    """
    c, cfg, digest = env_for_blobs
    pulled = 0
    chunk = b"x" * 4096

    async def body():
        nonlocal pulled
        for _ in range(cfg.max_blob_bytes // len(chunk) * 8):     # 8x the cap on offer
            pulled += len(chunk)
            yield chunk

    r = await c.put(f"/blobs/sha256/{digest}", content=body())
    assert r.status_code == 413, r.text
    assert pulled <= cfg.max_blob_bytes + 2 * len(chunk), (
        f"streamed {pulled} bytes for a {cfg.max_blob_bytes}-byte cap: the guard fires at the end")
    tmp = Path(os.environ["HIVEMIND_PROJECTS_DIR"])
    leftovers = [p for p in tmp.rglob("blobs/tmp/*") if p.is_file()]
    assert leftovers == [], f"aborted upload left {leftovers}"


@pytest.mark.anyio
async def test_a_body_within_the_cap_is_still_accepted(env_for_blobs):
    """The guard must not have moved the cap: the largest legal upload still works."""
    c, cfg, _digest = env_for_blobs
    ok = b"y" * cfg.max_blob_bytes
    r = await c.put(f"/blobs/sha256/{hashlib.sha256(ok).hexdigest()}", content=ok)
    assert r.status_code == 201, r.text
    assert r.json()["size"] == cfg.max_blob_bytes
