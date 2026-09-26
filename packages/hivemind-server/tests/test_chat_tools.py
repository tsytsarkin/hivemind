"""Durable MCP chat must work without an online receiver or the legacy recent cache."""

import json

import httpx
import pytest
from starlette.testclient import TestClient, WebSocketDisconnect

from conftest import Lifespan, _call, _headers, _post, _rpc
from hivemind_server import bus_ws
from hivemind_server.chat import ChatStore
from hivemind_server.identity import IdentityStore


def _token(application, user, device):
    return IdentityStore(application.state.cfg.identities_path).mint(user, device)


async def _tool(http_client, token, project, tool_name, **args):
    result = await _post(http_client, "", token, "tools/call",
                         {"name": tool_name, "arguments": {"project": project, **args}})
    return _call(result)


@pytest.mark.anyio
async def test_create_room_and_offline_dm_catchup_over_mcp(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ana = _token(app, "ana", "laptop")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        room = await _tool(client, nik, project.name, "chat_room_create", name="parser-work",
                           description="Debug parser crashes", client="codex", session_id="sid-1")
        assert room["ok"] is True
        listed = await _tool(client, ana, project.name, "chat_room_list",
                             client="claude", session_id="sid-2")
        assert [r["name"] for r in listed["rooms"]] == ["parser-work"]
        sent = await _tool(client, nik, project.name, "chat_send", to_user="ana",
                           to_device="laptop", to_client="claude", client="codex",
                           session_id="sid-1", body="please check parser", idempotency_key="dm-1")
        assert sent["ok"] is True and sent["notified_live"] is False
        inbox = await _tool(client, ana, project.name, "chat_inbox", client="claude",
                            session_id="sid-99", after_seq=0)
        assert [m["body"] for m in inbox["messages"]] == ["please check parser"]
        assert inbox["messages"][0]["id"] == sent["id"]
        acknowledged = await _tool(client, ana, project.name, "chat_mark_read",
                                   client="claude", session_id="sid-99",
                                   up_to_seq=inbox["messages"][0]["seq"])
        assert acknowledged["ok"] is True
        resumed = await _tool(client, ana, project.name, "chat_inbox",
                              client="claude", session_id="sid-100", after_seq=0)
        assert resumed["last_read_seq"] == sent["seq"]


@pytest.mark.anyio
async def test_sender_cannot_create_mailbox_for_unregistered_device(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    _token(app, "ana", "laptop")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        out = await _tool(client, nik, project.name, "chat_send", to_user="ana",
                          to_device="someone-elses-device", to_client="claude", client="codex",
                          session_id="sid-1", body="secret", idempotency_key="dm-1")
    assert out["ok"] is False


@pytest.mark.anyio
async def test_accepted_room_post_survives_subscriber_lookup_failure(env, monkeypatch):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    store = ChatStore(project.db)
    store.create_room("parser-work", "Debug parser crashes", ("nik", "mac", "codex"))

    def unavailable(*args):
        raise OSError("notification lookup unavailable")

    monkeypatch.setattr(ChatStore, "subscribers", unavailable)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        out = await _tool(client, nik, project.name, "chat_room_post", name="parser-work",
                          client="codex", session_id="sid-1", body="persist this",
                          idempotency_key="post-1")
        history = await _tool(client, nik, project.name, "chat_room_history",
                              name="parser-work", client="codex", session_id="sid-1")
    assert out["ok"] is True and out["notified_live"] is False
    assert [message["id"] for message in history["messages"]] == [out["id"]]


@pytest.mark.anyio
async def test_accepted_dm_does_not_require_second_presence_write(env, monkeypatch):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ana = _token(app, "ana", "laptop")

    def unavailable(*args):
        raise OSError("second presence write unavailable")

    monkeypatch.setattr(ChatStore, "touch", unavailable)
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        out = await _tool(client, nik, project.name, "chat_send", to_user="ana",
                          to_device="laptop", to_client="claude", client="codex",
                          session_id="sid-1", body="persist this", idempotency_key="dm-1")
    assert out["ok"] is True
    assert [m["id"] for m in ChatStore(project.db).inbox(("ana", "laptop", "claude"))["messages"]] == [out["id"]]


@pytest.mark.anyio
async def test_project_chat_limits_reject_over_quota_without_discarding_unexpired_mail(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    _token(app, "ana", "laptop")
    (project.dir / "chat_limits.json").write_text(json.dumps({"max_bytes": 520, "max_messages": 1}))
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        first = await _tool(client, nik, project.name, "chat_send", to_user="ana",
                            to_device="laptop", to_client="claude", client="codex",
                            session_id="sid-1", body="hello", idempotency_key="dm-1")
        second = await _tool(client, nik, project.name, "chat_send", to_user="ana",
                             to_device="laptop", to_client="claude", client="codex",
                             session_id="sid-1", body="world", idempotency_key="dm-2")
    assert first["ok"] is True and second["ok"] is False and "quota" in second["error"]
    assert [m["id"] for m in ChatStore(project.db).inbox(("ana", "laptop", "claude"))["messages"]] == [first["id"]]


@pytest.mark.anyio
async def test_durable_private_dm_is_not_exposed_by_legacy_message_tool(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ana = _token(app, "ana", "laptop")
    eve = _token(app, "eve", "desktop")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        sent = await _tool(client, nik, project.name, "chat_send", to_user="ana",
                           to_device="laptop", to_client="claude", client="codex",
                           session_id="sid-1", body="private details", idempotency_key="dm-2")
        denied = await _tool(client, eve, project.name, "chat_message_get", id=sent["id"],
                             client="codex", session_id="sid-3")
        legacy = await _tool(client, eve, project.name, "bus_message", message_id=sent["id"])
        assert denied["ok"] is False
        assert legacy["ok"] is False
        allowed = await _tool(client, ana, project.name, "chat_message_get", id=sent["id"],
                              client="claude", session_id="sid-2")
        assert allowed["body"] == "private details"


@pytest.mark.anyio
async def test_canonical_notification_never_populates_legacy_recent_lookup():
    from hivemind_server import chat_ws
    hub = chat_ws.CanonicalHub()
    delivered = await hub.notify(("nik", "mac", "codex"), {"type": "chat", "id": "m1"})
    assert delivered == 0
    with pytest.raises(bus_ws.BusError):
        bus_ws.Hub().message("m1")


@pytest.mark.anyio
async def test_canonical_sessions_with_identical_display_labels_do_not_cross_deliver():
    from hivemind_server import chat_ws

    class Socket:
        def __init__(self):
            self.frames = []

        async def send_text(self, frame):
            self.frames.append(frame)

    hub = chat_ws.CanonicalHub()
    first, second = Socket(), Socket()
    await hub.attach(("a-b", "c", "d", "s1"), first)
    await hub.attach(("a", "b-c", "d", "s1"), second)
    assert hub.online(("a-b", "c", "d")) is True
    assert hub.online(("a-b", "c", "d"), "s2") is False
    assert await hub.notify(("a-b", "c", "d"), {"type": "chat", "id": "private"}) == 1
    assert len(first.frames) == 1 and second.frames == []


@pytest.mark.anyio
async def test_signed_canonical_connection_binds_all_identity_parts(env):
    from hivemind_server import chat_ws
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    async with Lifespan(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                               base_url="http://t", timeout=30) as client:
        joined = await _tool(client, nik, project.name, "chat_connect",
                             client="codex", session_id="sid-1")
    assert joined["ok"] is True
    assert joined["peer"] == "nik-mac-codex-sid-1"
    assert joined["listen_key"].startswith("hk2.")
    assert chat_ws.hub_for(project.dir).verify_key(joined["listen_key"])[0] == (
        "nik", "mac", "codex", "sid-1")


@pytest.mark.anyio
async def test_revoked_token_cannot_reconnect_with_its_unexpired_chat_key(env):
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    identities = IdentityStore(app.state.cfg.identities_path)
    _token(app, "nik", "mac")  # user and device still have access; only original token is revoked
    with TestClient(app) as client:
        result = client.post("/mcp", json=_rpc("tools/call", {
            "name": "chat_connect", "arguments": {"project": project.name,
                                                   "client": "codex", "session_id": "sid-1"}}),
            headers=_headers(nik, "tools/call", "chat_connect"))
        joined = _call(result)
        identities.refresh_if_changed()
        identities._tokens.pop(nik)
        identities.save()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f'/p/{project.name}/chat/ws?key={joined["listen_key"]}'):
                pass


@pytest.mark.anyio
async def test_revoked_socket_gets_no_further_live_notifications(env):
    from hivemind_server import chat_ws
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    identities = IdentityStore(app.state.cfg.identities_path)
    fingerprint = identities.verify(nik).credential_hash
    hub = chat_ws.hub_for(project.dir)

    class Socket:
        def __init__(self):
            self.frames = []
            self.closed = False

        async def send_text(self, frame):
            self.frames.append(frame)

        async def close(self, *, code):
            self.closed = True

    socket = Socket()
    await hub.attach(("nik", "mac", "codex", "sid-1"), socket,
                     credential_hash=fingerprint, identities=identities)
    identities._tokens.pop(nik)
    identities.save()
    assert await hub.notify(("nik", "mac", "codex"), {"type": "chat", "id": "m1"}) == 0
    assert socket.frames == [] and socket.closed and not hub.online(("nik", "mac", "codex"))


def test_signed_socket_receives_persisted_room_post_from_subscription(env):
    from hivemind_server import chat_ws
    app, project, _ = env
    nik = _token(app, "nik", "mac")
    ana = _token(app, "ana", "laptop")
    store = ChatStore(project.db)
    store.create_room("parser-work", "Debug parser crashes", ("nik", "mac", "codex"))
    store.join("parser-work", ("ana", "laptop", "claude"))
    key = chat_ws.hub_for(project.dir).mint_key(("ana", "laptop", "claude", "sid-2"),
                                                 IdentityStore(app.state.cfg.identities_path).verify(ana).credential_hash)

    with TestClient(app) as client:
        with client.websocket_connect(f"/p/{project.name}/chat/ws?key={key}") as socket:
            assert socket.receive_json()["peer"] == "ana-laptop-claude-sid-2"
            result = client.post("/mcp", json=_rpc("tools/call", {
                "name": "chat_room_post",
                "arguments": {"project": project.name, "name": "parser-work", "client": "codex",
                              "session_id": "sid-1", "body": "working on parser",
                              "idempotency_key": "progress-1", "kind": "progress"}}),
                headers=_headers(nik, "tools/call", "chat_room_post"))
            sent = _call(result)
            frame = socket.receive_json()
            assert sent["ok"] is True and sent["notified_live"] is True
            assert frame["type"] == "chat" and frame["id"] == sent["id"]
            assert frame["channel"] == "room" and frame["room"] == "parser-work"
            assert "body" not in frame


@pytest.mark.anyio
async def test_server_startup_purges_expired_chat_even_without_new_sends(env):
    app, project, _ = env
    store = ChatStore(project.db)
    store.send("dm", ("ana", "laptop", "claude"), ("nik", "mac", "codex"),
               "aged-out", "retry-old", now=1_700_000_000.0)
    with project.db.read() as cur:
        assert cur.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0] == 1
    async with Lifespan(app):
        with project.db.read() as cur:
            assert cur.execute("SELECT COUNT(*) FROM chat_message").fetchone()[0] == 0
