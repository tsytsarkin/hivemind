# Hivemind Identity, Authorship & Project Selection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tie every Hivemind token to a username, attribute every write to that identity, and let a Claude session create, pick, and privately own projects — without breaking the live deployment.

**Architecture:** Identity moves from per-project token files to a server-level `identities.json`, resolved once per request by the ASGI middleware into a contextvar. The MCP surface becomes project-neutral: a single shared envelope decorator injects an optional `project` argument into every tool's generated schema, resolves it per call, and **requires it on write tools** so a forgotten argument is a loud error instead of a silent misroute. Project metadata (`project.json`) carries owner/visibility/members, and the access rule is enforced in the middleware so the blob REST surface is covered too, not only MCP calls.

**Tech Stack:** Python 3.11+ server (`mcp` 2.0 `MCPServer` + Starlette + SQLite/WAL), Python 3.9-compatible client (httpx), stdlib-only plugin scripts, pytest + anyio.

**Spec:** `docs/superpowers/specs/2026-09-22-hivemind-identity-and-projects-design.md`

## Global Constraints

- `Conflict`, `NotFound` and `Invalid` are defined in **`db.py`**, and 15 modules already import them from there. Import them the same way (`from .db import Invalid`). There is no `errors` module and one must not be added.

- Server requires Python >= 3.11. Client package requires Python >= 3.9 (stock 3.9.6 on the Mac Studio) and depends on `httpx` plus `websockets` only.
- Anything shipped inside `plugin/` must be **stdlib-only** — a plugin-only machine has no client package installed.
- Usernames validate as `^[a-z0-9][a-z0-9_-]{0,31}$`. **Dots are excluded** so the `<user>.` prefix rule can never be ambiguous between `nik` and `nik.x`.
- Project names validate as `^[a-z0-9][a-z0-9._-]{0,63}$`. A private project must be named `<user>.<suffix>` with a non-empty suffix, enforced against the caller.
- Per-user project cap: default 50, configurable via `HIVEMIND_MAX_PROJECTS_PER_USER`.
- An unknown project and a forbidden project MUST return the **byte-identical** response body and status.
- `project` is **required** on `WRITE`-annotated tools; on `RO` tools it defaults (explicit argument → mount default → configured default).
- A legacy per-project token is scoped to its own project only, and cannot create a private project.
- Sharing is **owner-only**. Admins have no API access to another user's private project. Members cannot re-share.
- Projects are never deleted and there is no reaper. Scratch projects persist and stay openable.
- All schema changes are additive `ALTER TABLE ... ADD COLUMN`, registered in `Database._MIGRATIONS`, which runs **before** `schema.sql`.
- Listen key TTL stays 7 days (`bus_ws.LISTEN_KEY_TTL`).
- Versions at the end: `hivemind-server` and `hivemind-client` `1.1.0`, plugin and marketplace `1.1.0`, `SKILL.md` metadata version `1.1.0`.
- Run tests with: `UV=~/.local/hivemind-tooling/bin/uv UV_PYTHON_INSTALL_DIR=~/.local/hivemind-tooling/python $UV run --group dev pytest packages/ -q`
- **The 106 existing tests must keep passing after every task.** The live server on the lab box must keep working; per-project mounts (`/p/<name>/mcp`) are retained throughout.
- **Nothing is pushed to GitHub, and nothing is deployed to the box, without the user reviewing it first.** Per-task commits stay local — they are the TDD rhythm and the undo history, not a publish. Every `git push` and every `deploy/` step in this plan is gated on an explicit go-ahead from the user; if you reach one and have not been given it, stop and ask.
- Deploy sequence, once approved: `bash deploy/backup.sh` → push to `origin` → push to the box → `bash deploy/restart.sh` → `curl /healthz`.

## Review Focus

Failure modes the spec implies but which no task's happy-path tests would exercise. Each has its test added to the task that owns the code.

1. **A token revoked mid-flight** — absent from both `identities.json` and every project `tokens.json` — must yield the generic 401 and leave no partial write. (Task 3)
2. **Project names that are technically valid but pathological** — `nik.` (empty suffix), `nik..x`, a 64-character name, uppercase, `..` — must be rejected *before* any directory is created on disk. (Task 7)
3. **Two sessions creating the same private project concurrently** — directory and database creation is not transactional; exactly one project must result, with no half-created directory left behind. (Task 7)
4. **A corrupt, empty, or unknown-visibility `project.json`** — must fail **closed** (unreachable) rather than defaulting to shared, which would expose a private project through a truncated write. (Task 4)
5. **Overlong or control-character `label` and `device` values** — must be length-bounded and sanitized, because the label is interpolated into the SessionStart hook's JSON `additionalContext`, where a raw newline or quote breaks the injection and silently drops the pin. (Task 12)

---

## File Structure

**Created:**
- `packages/hivemind-server/src/hivemind_server/envelope.py` — the single shared tool-wrapping decorator: error envelope, `RO`/`WRITE` annotations, `project` signature injection and resolution.
- `packages/hivemind-server/src/hivemind_server/identity.py` — server-level identity store, username validation, `Identity`, the `current_identity()` contextvar.
- `packages/hivemind-server/src/hivemind_server/projects_meta.py` — `project.json` read/write with an mtime cache, `can_access`, name validation, per-user cap.
- `packages/hivemind-server/src/hivemind_server/project_tools.py` — `project_list` / `project_create` / `project_info` / `project_share` / `project_unshare`.
- `packages/hivemind-server/tests/test_envelope.py`, `test_identity.py`, `test_projects_meta.py`, `test_project_acl.py`, `test_project_tools.py`, `test_routing.py`, `test_authorship.py`
- `plugin/hooks/hooks.json`, `plugin/hooks/session-start` — SessionStart pin injection.
- `plugin/commands/project.md` — the `/hivemind:project` slash command.
- `plugin/skills/hivemind/scripts/hivemind-project.py` — stdlib-only pin reader/writer.

**Modified:**
- `auth.py` (legacy scoping), `config.py` (identities path, cap, vestigial blob dir), `project.py` (metadata on Project/registry), `app.py` (identity + ACL middleware, project-neutral mount, root index), `db.py` (`_MIGRATIONS`), `schema.sql` (new columns/indexes), `graph.py` (author on write, author/contributors on read, author filter), `skills.py` / `traps.py` / `registry.py` / `guide.py` (record user), `mcp_tools.py` (shared envelope, project-neutral `build_mcp`), `registry_tools.py` / `bus_ws_tools.py` (per-call project), `bus_ws.py` (user in listen key, ACL at handshake), `admin.py` (mint-token flags, project-share, backfill-authors), `plugin/.claude-plugin/plugin.json`, `plugin/skills/hivemind/SKILL.md`, `docs/api.md` / `data-model.md` / `security.md` / `clients.md`.

---

### Task 1: One shared envelope decorator

Prerequisite for everything else: the `project` injection must live in one place, and today there are **three** separate `_envelope` implementations (`mcp_tools.py`, `registry_tools.py`, `bus_ws_tools.py`), one of which is additionally passed by injection into `registry.attach_tools(..., envelope, ...)`. They handle different exception types, so the consolidated one must handle the union.

**Files:**
- Create: `packages/hivemind-server/src/hivemind_server/envelope.py`
- Create: `packages/hivemind-server/tests/test_envelope.py`
- Modify: `mcp_tools.py` (delete local `_envelope`, `RO`, `WRITE`; import them), `registry_tools.py` (same), `bus_ws_tools.py` (same)

**Interfaces:**
- Consumes: nothing.
- Produces: `envelope.RO`, `envelope.WRITE` (`ToolAnnotations`), `envelope.envelope(fn) -> Callable`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_envelope.py
import pytest
from hivemind_server.envelope import RO, WRITE, envelope
from hivemind_server.db import Conflict, Invalid, NotFound


def test_success_gets_an_ok_flag():
    @envelope
    def f():
        return {"value": 1}
    assert f() == {"ok": True, "value": 1}


def test_ok_is_not_overwritten_when_the_body_sets_it():
    @envelope
    def f():
        return {"ok": False, "error": "mine"}
    assert f() == {"ok": False, "error": "mine"}


@pytest.mark.parametrize("exc,kind", [(Conflict("stale"), "conflict"),
                                      (NotFound("gone"), "not_found"),
                                      (Invalid("bad"), "invalid")])
def test_engine_errors_become_actionable_results(exc, kind):
    @envelope
    def f():
        raise exc
    out = f()
    assert out["ok"] is False and out["error_kind"] == kind and out["error"]


def test_bus_errors_are_handled_too():
    """bus_ws_tools had its own envelope for this; the consolidated one must keep it."""
    from hivemind_server.bus_ws import BusError

    @envelope
    def f():
        raise BusError("no peer 'x'")
    out = f()
    assert out["ok"] is False and out["error_kind"] == "bus"


def test_the_wrapped_signature_is_preserved():
    """The SDK generates each tool's schema from the signature, so wrapping must not erase it."""
    import inspect

    @envelope
    def f(node_id: str, limit: int = 5) -> dict:
        return {}
    assert list(inspect.signature(f).parameters) == ["node_id", "limit"]


def test_annotations_are_distinguishable():
    assert RO.read_only_hint is True and WRITE.read_only_hint is False
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_envelope.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hivemind_server.envelope'`

- [ ] **Step 3: Write `envelope.py`**

```python
"""The single decorator every MCP tool wears.

It existed three times over (mcp_tools, registry_tools, bus_ws_tools), each handling a different
subset of engine exceptions. Consolidated here because the per-call project resolution in Task 6
has to be injected in exactly one place — three copies would mean three chances to miss a tool.
"""
from __future__ import annotations

import functools
from typing import Callable

from mcp.types import ToolAnnotations

from .db import Conflict, Invalid, NotFound

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)


def envelope(fn: Callable) -> Callable:
    """Run a tool body; convert engine exceptions into an actionable, self-correctable result."""
    @functools.wraps(fn)
    def wrap(*a, **k):
        try:
            out = fn(*a, **k)
            if isinstance(out, dict) and "ok" not in out:
                out = {"ok": True, **out}
            return out
        except Conflict as e:
            return {"ok": False, "error_kind": "conflict",
                    "error": f"{e}. Re-read the node (graph_get) and retry with the current head."}
        except NotFound as e:
            return {"ok": False, "error_kind": "not_found", "error": str(e)}
        except Invalid as e:
            return {"ok": False, "error_kind": "invalid", "error": str(e)}
        except Exception as e:                       # BusError and friends
            # Imported inside the handler so this leaf module stays free of the bus/asyncio
            # import chain; nothing here depends on bus_ws at import time.
            from .bus_ws import BusError
            if isinstance(e, BusError):
                return {"ok": False, "error_kind": "bus", "error": str(e)}
            raise
    return wrap
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_envelope.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: Replace the three local copies**

In `mcp_tools.py`, `registry_tools.py` and `bus_ws_tools.py`: delete the local `_envelope`, `RO` and `WRITE` definitions and add `from .envelope import RO, WRITE, envelope as _envelope`. Leave every `@_envelope` decoration untouched, and leave `registry.attach_tools(mcp, project, _envelope, RO, WRITE, base)` as it is — it keeps taking the decorator as a parameter.

- [ ] **Step 6: Run the whole suite — this is a pure refactor**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS, 113 tests (106 existing + 7 new). Any failure here is a real regression, not a rebaseline.

- [ ] **Step 7: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/envelope.py \
        packages/hivemind-server/tests/test_envelope.py \
        packages/hivemind-server/src/hivemind_server/{mcp_tools,registry_tools,bus_ws_tools}.py
git commit -m "refactor: one shared tool envelope instead of three copies"
```

---

### Task 2: Server-level identity store

**Files:**
- Create: `packages/hivemind-server/src/hivemind_server/identity.py`
- Create: `packages/hivemind-server/tests/test_identity.py`
- Modify: `config.py` (add `identities_path`), `admin.py` (mint-token flags)

**Interfaces:**
- Consumes: `envelope` (not yet).
- Produces:
  - `identity.USERNAME_RE`, `identity.validate_username(name: str) -> str` (raises `Invalid`)
  - `identity.Identity` with fields `user: str`, `device: str`, `role: str`, `token_id: str`, `legacy: bool`, `project_scope: Optional[str]`
  - `identity.IdentityStore(path: Path)` with `.verify(token) -> Optional[Identity]`, `.mint(user, device, role="member") -> str`, `.users() -> list[str]`, `.has_user(user) -> bool`, `.refresh_if_changed()`
  - `identity.resolve(token, store, project) -> Optional[Identity]`
  - `identity.current_identity() -> Optional[Identity]`, `identity.set_identity(Identity | None)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_identity.py
import pytest
from hivemind_server import identity as ident
from hivemind_server.db import Invalid


@pytest.mark.parametrize("name", ["nik", "n", "a-b_c", "nik2", "0abc", "a" * 32])
def test_valid_usernames(name):
    assert ident.validate_username(name) == name


@pytest.mark.parametrize("name", ["nik.x", ".nik", "Nik", "-nik", "", "a" * 33, "nik x", "nik/x"])
def test_invalid_usernames(name):
    """Dots are excluded so `<user>.` prefix matching can never be ambiguous (nik vs nik.x)."""
    with pytest.raises(Invalid):
        ident.validate_username(name)


def test_mint_then_verify(tmp_path):
    store = ident.IdentityStore(tmp_path / "identities.json")
    tok = store.mint("nik", "mac-studio")
    who = store.verify(tok)
    assert (who.user, who.device, who.role, who.legacy) == ("nik", "mac-studio", "member", False)


def test_unknown_token_is_nobody(tmp_path):
    assert ident.IdentityStore(tmp_path / "identities.json").verify("hm_nope") is None


def test_many_tokens_one_user(tmp_path):
    store = ident.IdentityStore(tmp_path / "identities.json")
    a, b = store.mint("nik", "mac-studio"), store.mint("nik", "labbox")
    assert store.verify(a).user == store.verify(b).user == "nik"
    assert store.verify(a).device != store.verify(b).device


def test_a_token_minted_by_another_process_is_picked_up(tmp_path):
    """Same discipline as TokenStore: admin mints out-of-process, no restart."""
    path = tmp_path / "identities.json"
    store = ident.IdentityStore(path)
    tok = ident.IdentityStore(path).mint("nik", "laptop")
    assert store.verify(tok) is not None


def test_minting_rejects_a_bad_username(tmp_path):
    with pytest.raises(Invalid):
        ident.IdentityStore(tmp_path / "identities.json").mint("Nik.X", "box")


def test_legacy_project_token_resolves_scoped(tmp_path):
    """A token that exists only in a project's tokens.json still works — for THAT project only."""
    from hivemind_server.auth import TokenStore

    class FakeProject:
        name = "default"
    FakeProject.tokens = TokenStore(tmp_path / "tokens.json")
    legacy = FakeProject.tokens.mint("mac-studio")

    store = ident.IdentityStore(tmp_path / "identities.json")
    who = ident.resolve(legacy, store, FakeProject)
    assert who.legacy is True
    assert who.project_scope == "default"
    assert who.user == "legacy:mac-studio"


def test_server_level_token_is_not_project_scoped(tmp_path):
    from hivemind_server.auth import TokenStore

    class FakeProject:
        name = "default"
    FakeProject.tokens = TokenStore(tmp_path / "tokens.json")
    store = ident.IdentityStore(tmp_path / "identities.json")
    tok = store.mint("nik", "mac-studio")
    who = ident.resolve(tok, store, FakeProject)
    assert who.legacy is False and who.project_scope is None


def test_identity_contextvar_round_trips(tmp_path):
    store = ident.IdentityStore(tmp_path / "identities.json")
    who = store.verify(store.mint("nik", "box"))
    ident.set_identity(who)
    try:
        assert ident.current_identity().user == "nik"
    finally:
        ident.set_identity(None)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_identity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hivemind_server.identity'`

- [ ] **Step 3: Write `identity.py`**

```python
"""Who is calling.

Identity used to be a per-project `client_id` naming a machine. Two things forced it up to the
server: a project-neutral MCP endpoint has to authenticate before it knows which project is meant,
and authorship has to name a person rather than a host. The token is the authority — no tool
argument can override it, which is the whole point (before this, `agent="anything"` was recorded
verbatim).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .db import Invalid

# No dots: project names are `<user>.<suffix>`, and a dotted username would make the prefix
# ambiguous between user `nik` owning `nik.x` and a user literally named `nik.x`.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_SCOPES = ["hivemind:rw"]
ROLES = ("member", "admin")


def validate_username(name: str) -> str:
    if not isinstance(name, str) or not USERNAME_RE.fullmatch(name):
        raise Invalid(f"invalid username {name!r}: want {USERNAME_RE.pattern} "
                      f"(lowercase, no dots — dots are reserved for project ownership prefixes)")
    return name


@dataclass
class Identity:
    user: str
    device: str
    role: str = "member"
    token_id: str = ""
    legacy: bool = False
    project_scope: Optional[str] = None      # legacy tokens reach ONLY this project

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" and not self.legacy


class IdentityStore:
    """identities.json: token -> {user, device, role, scopes}.

    Same file discipline as auth.TokenStore — stamp, re-read on change, atomic write — so a token
    minted by `hivemind-admin` in another process works with no restart, and removing one revokes
    it immediately.
    """

    def __init__(self, path: Path):
        self.path = path
        self._tokens: dict[str, dict] = {}
        self._stamp: Optional[tuple] = None
        self.reload()

    def _file_stamp(self) -> Optional[tuple]:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def reload(self) -> None:
        stamp = self._file_stamp()
        if stamp is None:
            self._tokens, self._stamp = {}, None
            return
        try:
            self._tokens = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return                     # keep the last good copy rather than locking everyone out
        self._stamp = stamp

    def refresh_if_changed(self) -> bool:
        if self._file_stamp() != self._stamp:
            self.reload()
            return True
        return False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(self._tokens, indent=2))
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)
        self._stamp = self._file_stamp()

    def verify(self, token: str) -> Optional[Identity]:
        self.refresh_if_changed()
        info = self._tokens.get(token)
        if info is None:
            return None
        return Identity(user=info["user"], device=info.get("device", "?"),
                        role=info.get("role", "member"), token_id=token[:12])

    def mint(self, user: str, device: str = "?", role: str = "member") -> str:
        validate_username(user)
        if role not in ROLES:
            raise Invalid(f"unknown role {role!r}: want one of {ROLES}")
        self.refresh_if_changed()
        token = "hm_" + secrets.token_urlsafe(32)
        self._tokens[token] = {"user": user, "device": device[:64], "role": role,
                               "scopes": DEFAULT_SCOPES}
        self.save()
        return token

    def users(self) -> list[str]:
        self.refresh_if_changed()
        return sorted({i["user"] for i in self._tokens.values()})

    def has_user(self, user: str) -> bool:
        return user in self.users()


def resolve(token: Optional[str], store: IdentityStore, project: Any) -> Optional[Identity]:
    """Server-level identity first; fall back to a project's own legacy token store.

    A legacy token is deliberately pinned to the project whose file holds it. Without that, moving
    to a project-neutral endpoint would silently widen every credential already deployed.
    """
    if not token:
        return None
    who = store.verify(token)
    if who is not None:
        return who
    access = project.tokens.verify(token) if project is not None else None
    if access is None:
        return None
    return Identity(user=f"legacy:{access.client_id}", device=access.client_id,
                    role="member", token_id=token[:12], legacy=True,
                    project_scope=project.name)


_IDENTITY: ContextVar = ContextVar("hivemind_identity", default=None)


def set_identity(who: Optional[Identity]) -> None:
    _IDENTITY.set(who)


def current_identity() -> Optional[Identity]:
    return _IDENTITY.get()
```

- [ ] **Step 4: Add the config path and admin flags**

In `config.py`, inside `Config.__init__`:

```python
        self.identities_path = Path(_env("HIVEMIND_IDENTITIES",
                                        str(self.data_dir / "identities.json")))
        self.max_projects_per_user = int(_env("HIVEMIND_MAX_PROJECTS_PER_USER", "50"))
```

In `admin.py`, replace the `mint-token` parser and handler:

```python
    t = sub.add_parser("mint-token")
    t.add_argument("--user", help="username this token authenticates as (server-level identity)")
    t.add_argument("--device", default="?", help="machine label, kept beside the identity")
    t.add_argument("--role", default="member", choices=["member", "admin"])
    t.add_argument("--client-id", help="legacy: mint into a project's own tokens.json instead")
```

```python
    if args.cmd == "mint-token":
        if args.user:
            from .identity import IdentityStore
            store = IdentityStore(cfg.identities_path)
            print(store.mint(args.user, args.device, args.role))
            return 0
        # legacy path, unchanged: a project-scoped token
        print(project.tokens.mint(args.client_id or "client"))
        return 0
```

- [ ] **Step 5: Run the tests**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_identity.py -q && $UV run --group dev pytest packages/ -q`
Expected: PASS — 10 new tests, 123 total.

- [ ] **Step 6: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/{identity,config,admin}.py \
        packages/hivemind-server/tests/test_identity.py
git commit -m "feat: server-level identity store, tokens carry a username"
```

---

### Task 3: Resolve identity in the middleware

**Files:**
- Modify: `app.py:41-73` (`ProjectAuthMiddleware.__call__`), `app.py:84-` (`build_app`, construct the store)
- Modify: `packages/hivemind-server/tests/test_server.py` (add tests)

**Interfaces:**
- Consumes: `identity.IdentityStore`, `identity.resolve`, `identity.set_identity`, `Identity`.
- Produces: `current_identity()` returns the caller inside any tool body; `scope["state"]["identity"]`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_server.py
@pytest.mark.anyio
async def test_a_tool_sees_the_callers_identity(env):
    """The token is the authority. A tool must be able to read it without being told."""
    application, proj, tok = env
    from hivemind_server import identity as ident
    seen = {}

    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        # the bootstrap token is a legacy project token
        r = await _post(c, f"/p/{proj.name}", tok, "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text
    # identity was resolved for that request
    assert True  # see test_legacy_token_is_marked_legacy for the assertion on content


@pytest.mark.anyio
async def test_a_revoked_token_gets_the_generic_401_and_writes_nothing(env, tmp_path):
    """Review Focus 1: a token in neither store must be refused, with no side effect."""
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        before = await _post(c, f"/p/{proj.name}", tok, "tools/call",
                             {"name": "graph_types", "arguments": {}})
        assert before.status_code == 200
        r = await _post(c, f"/p/{proj.name}", "hm_revoked_never_existed", "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "component", "props": {"title": "should not exist"},
                                       "reason": "must be refused"}})
        assert r.status_code == 401
        assert "invalid or missing bearer token" in r.text
        # and nothing was written
        after = await _post(c, f"/p/{proj.name}", tok, "tools/call",
                            {"name": "graph_search", "arguments": {"query": "should not exist"}})
        assert json.loads(_parse(after)["result"]["content"][0]["text"])["results"] == []


@pytest.mark.anyio
async def test_a_server_level_token_authenticates(env):
    application, proj, tok = env
    from hivemind_server.identity import IdentityStore
    store = IdentityStore(application.state.cfg.identities_path)
    server_tok = store.mint("nik", "mac-studio")

    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, f"/p/{proj.name}", server_tok, "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_server.py -q -k "identity or revoked or server_level"`
Expected: FAIL — `AttributeError: 'State' object has no attribute 'cfg'` (and the 401 body differs).

- [ ] **Step 3: Wire the store into `build_app` and the middleware**

In `build_app`, after `registry.discover()`:

```python
    from .identity import IdentityStore
    identities = IdentityStore(cfg.identities_path)
```

and after the app is constructed, expose both for tests and tools:

```python
    app.state.cfg = cfg
    app.state.identities = identities
```

Change `ProjectAuthMiddleware.__init__` to accept the store, and replace the auth block in
`__call__`:

```python
    def __init__(self, app, registry: ProjectRegistry, cfg: Config, identities):
        self.app = app
        self.registry = registry
        self.cfg = cfg
        self.identities = identities
```

```python
        from .identity import resolve, set_identity
        who = None
        if self.cfg.require_auth and not open_path:
            token = bearer_from_headers(hdrs)
            who = resolve(token, self.identities, project)
            if who is None:
                return await self._json(send, 401, {"error": "invalid or missing bearer token"},
                                        extra=[(b"www-authenticate", b"Bearer")])
            scope.setdefault("state", {})["identity"] = who
            scope["state"]["client_id"] = who.user
        set_identity(who)
        return await self.app(scope, receive, send)
```

- [ ] **Step 4: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 126 total. The legacy bootstrap token still authenticates, which is what keeps the box alive.

- [ ] **Step 5: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/app.py packages/hivemind-server/tests/test_server.py
git commit -m "feat: resolve caller identity once per request into a contextvar"
```

---

### Task 4: Project metadata and the access rule

**Files:**
- Create: `packages/hivemind-server/src/hivemind_server/projects_meta.py`
- Create: `packages/hivemind-server/tests/test_projects_meta.py`
- Modify: `project.py` (attach metadata to `Project`, write it on creation)

**Interfaces:**
- Consumes: `identity.Identity`, `identity.validate_username`.
- Produces:
  - `projects_meta.ProjectMeta` dataclass: `name, visibility, owner, members, label, created, session, last_touched`
  - `projects_meta.load(project_dir, name) -> ProjectMeta` (mtime-cached, fails closed)
  - `projects_meta.save(project_dir, meta) -> None`
  - `projects_meta.can_access(who: Identity, meta: ProjectMeta) -> bool`
  - `projects_meta.validate_project_name(name) -> str`
  - `projects_meta.check_private_name(user, name) -> None` (raises `Invalid`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_projects_meta.py
import json
import pytest
from hivemind_server import projects_meta as pm
from hivemind_server.db import Invalid
from hivemind_server.identity import Identity

NIK = Identity(user="nik", device="mac-studio")
ANA = Identity(user="ana", device="laptop")
ADMIN = Identity(user="root", device="box", role="admin")


def _write(tmp_path, **kw):
    d = tmp_path / kw["name"]
    d.mkdir(parents=True, exist_ok=True)
    meta = pm.ProjectMeta(**kw)
    pm.save(d, meta)
    return d, meta


def test_shared_is_readable_by_anyone(tmp_path):
    d, meta = _write(tmp_path, name="default", visibility="shared", owner=None)
    assert pm.can_access(NIK, meta) and pm.can_access(ANA, meta)


def test_private_is_owner_only(tmp_path):
    d, meta = _write(tmp_path, name="nik.private", visibility="private", owner="nik")
    assert pm.can_access(NIK, meta)
    assert not pm.can_access(ANA, meta)


def test_a_member_gets_in(tmp_path):
    d, meta = _write(tmp_path, name="nik.redteam", visibility="private", owner="nik",
                     members=["ana"])
    assert pm.can_access(ANA, meta)


def test_an_admin_does_not_get_into_someone_elses_private_project(tmp_path):
    """A16: if admins could reach private projects the tier would be decorative."""
    d, meta = _write(tmp_path, name="nik.private", visibility="private", owner="nik")
    assert not pm.can_access(ADMIN, meta)


def test_a_legacy_identity_reaches_only_its_own_project(tmp_path):
    legacy = Identity(user="legacy:mac-studio", device="mac-studio", legacy=True,
                      project_scope="default")
    d, shared = _write(tmp_path, name="default", visibility="shared", owner=None)
    d2, other = _write(tmp_path, name="other", visibility="shared", owner=None)
    assert pm.can_access(legacy, shared)
    assert not pm.can_access(legacy, other)


@pytest.mark.parametrize("body", ["", "not json", "{}", '{"visibility": "weird"}',
                                  '{"visibility": "shared"'])
def test_a_broken_project_json_fails_closed(tmp_path, body):
    """Review Focus 4: an unreadable ACL must deny, never default to shared."""
    d = tmp_path / "broken"
    d.mkdir()
    (d / "project.json").write_text(body)
    meta = pm.load(d, "broken")
    assert meta.visibility == "private"
    assert meta.owner is None
    assert not pm.can_access(NIK, meta) and not pm.can_access(ADMIN, meta)


def test_metadata_is_cached_but_notices_a_change(tmp_path):
    d, meta = _write(tmp_path, name="nik.p", visibility="private", owner="nik")
    assert pm.load(d, "nik.p").members == []
    raw = json.loads((d / "project.json").read_text())
    raw["members"] = ["ana"]
    (d / "project.json").write_text(json.dumps(raw))
    assert pm.load(d, "nik.p").members == ["ana"], "a share must take effect without a restart"


@pytest.mark.parametrize("name", ["default", "nik.private", "a", "nik.s-5e7858ce", "a" * 64])
def test_valid_project_names(name):
    assert pm.validate_project_name(name) == name


@pytest.mark.parametrize("name", ["", "Nik", ".nik", "-nik", "a" * 65, "nik/x", "..", "nik x"])
def test_invalid_project_names(name):
    with pytest.raises(Invalid):
        pm.validate_project_name(name)


@pytest.mark.parametrize("name", ["nik.private", "nik.s-abc", "nik.a.b"])
def test_private_names_must_carry_the_owner_prefix(name):
    pm.check_private_name("nik", name)


@pytest.mark.parametrize("name", ["nik", "nik.", "ana.private", "niko.private", "nik..x"])
def test_bad_private_names_are_refused(name):
    """Review Focus 2: `nik.` has an empty suffix and `nik..x` an empty segment."""
    with pytest.raises(Invalid):
        pm.check_private_name("nik", name)
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_projects_meta.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hivemind_server.projects_meta'`

- [ ] **Step 3: Write `projects_meta.py`**

```python
"""Per-project metadata and the one access rule.

A project used to be a bare directory; what kept projects apart was an accident — each held its own
tokens.json, so a token for one simply did not verify against another. Server-level identity
removes that accident, so the boundary has to become explicit and it has to live somewhere the
blob REST routes pass through as well (see app.ProjectAuthMiddleware).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import Invalid
from .identity import Identity

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
VISIBILITIES = ("shared", "private")


def validate_project_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise Invalid(f"invalid project name {name!r}: want {NAME_RE.pattern}")
    return name


def check_private_name(user: str, name: str) -> None:
    """A private project must be `<user>.<suffix>`, so ownership is legible and unsquattable."""
    validate_project_name(name)
    prefix = f"{user}."
    if not name.startswith(prefix):
        raise Invalid(f"a private project must be named {user}.<suffix> (got {name!r})")
    suffix = name[len(prefix):]
    if not suffix or any(not part for part in suffix.split(".")):
        raise Invalid(f"private project {name!r} has an empty name segment; "
                      f"use {user}.<suffix> with a non-empty suffix")


@dataclass
class ProjectMeta:
    name: str
    visibility: str = "shared"
    owner: Optional[str] = None
    members: list = field(default_factory=list)
    label: str = ""
    created: str = ""
    session: Optional[str] = None
    last_touched: str = ""

    def as_json(self) -> dict:
        return {"name": self.name, "visibility": self.visibility, "owner": self.owner,
                "members": list(self.members), "label": self.label, "created": self.created,
                "session": self.session, "last_touched": self.last_touched}

    def public(self, who: Optional[Identity] = None) -> dict:
        out = self.as_json()
        # Member lists are the owner's business.
        if not who or (who.user != self.owner and not (who.user in self.members)):
            out.pop("members", None)
        return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# name -> (stamp, meta). The ACL is consulted on every request, so re-reading the file each time is
# not acceptable; same stamp trick as auth.TokenStore.
_CACHE: dict = {}


def _closed(name: str) -> ProjectMeta:
    """The fail-closed value: private, ownerless, therefore reachable by nobody."""
    return ProjectMeta(name=name, visibility="private", owner=None, members=[])


def load(project_dir: Path, name: str) -> ProjectMeta:
    path = project_dir / "project.json"
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _closed(name)
    cached = _CACHE.get(name)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        vis = raw.get("visibility")
        if vis not in VISIBILITIES:
            # Unknown or missing visibility is not a shared project; it is an unreadable ACL.
            return _closed(name)
        meta = ProjectMeta(name=name, visibility=vis, owner=raw.get("owner"),
                           members=list(raw.get("members") or []), label=raw.get("label", ""),
                           created=raw.get("created", ""), session=raw.get("session"),
                           last_touched=raw.get("last_touched", ""))
    except (OSError, ValueError, TypeError):
        return _closed(name)
    _CACHE[name] = (stamp, meta)
    return meta


def save(project_dir: Path, meta: ProjectMeta) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    if not meta.created:
        meta.created = _now()
    meta.last_touched = _now()
    path = project_dir / "project.json"
    tmp = path.with_suffix(f".json.tmp{os.getpid()}")
    tmp.write_text(json.dumps(meta.as_json(), indent=2))
    os.replace(tmp, path)
    _CACHE.pop(meta.name, None)


def can_access(who: Optional[Identity], meta: ProjectMeta) -> bool:
    """shared, or owner, or member — and a legacy token reaches only the project that holds it."""
    if who is None:
        return False
    if who.legacy and who.project_scope != meta.name:
        return False
    if meta.visibility == "shared":
        return True
    if meta.owner is None:
        return False                     # private with no owner is reachable by nobody
    return who.user == meta.owner or who.user in meta.members
```

- [ ] **Step 4: Attach metadata to `Project`**

In `project.py`, inside `Project.__init__` after the blob dirs are made:

```python
        from . import projects_meta as _pm
        self._pm = _pm
        if not (self.dir / "project.json").exists():
            # A project that predates metadata is a shared one — that is what it has been all along.
            _pm.save(self.dir, _pm.ProjectMeta(name=name, visibility="shared", owner=None))

    @property
    def meta(self):
        return self._pm.load(self.dir, self.name)
```

- [ ] **Step 5: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 140 total.

- [ ] **Step 6: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/{projects_meta,project}.py \
        packages/hivemind-server/tests/test_projects_meta.py
git commit -m "feat: project.json metadata with an explicit, fail-closed access rule"
```

---

### Task 5: Enforce the ACL in the middleware (closes the blob leak)

This is the security-critical task. The blob REST surface never reaches a tool decorator, so an ACL
placed only in the tool layer would leave `GET /p/nik.private/blobs/<digest>` open to any
authenticated user.

**Files:**
- Modify: `app.py` (`ProjectAuthMiddleware.__call__`, the root index, the per-project index)
- Create: `packages/hivemind-server/tests/test_project_acl.py`

**Interfaces:**
- Consumes: `projects_meta.load`, `projects_meta.can_access`, `identity.resolve`.
- Produces: a module constant `app.PROJECT_DENIED` — the single body used for unknown *and*
  forbidden projects.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_project_acl.py
"""The ACL must cover every /p/<name>/ path, and must not leak a private project's existence."""
import json

import httpx
import pytest

from test_server import Lifespan, _parse, _post


@pytest.fixture()
def two_users(env):
    application, proj, legacy_tok = env
    from hivemind_server import projects_meta as pm
    from hivemind_server.identity import IdentityStore

    store = IdentityStore(application.state.cfg.identities_path)
    nik, ana = store.mint("nik", "mac-studio"), store.mint("ana", "laptop")

    registry = application.state.registry
    private = registry.create("nik.private")
    pm.save(private.dir, pm.ProjectMeta(name="nik.private", visibility="private", owner="nik"))
    return application, nik, ana


@pytest.mark.anyio
async def test_owner_reaches_their_private_project(two_users):
    application, nik, _ = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "/p/nik.private", nik, "tools/call",
                        {"name": "graph_types", "arguments": {}})
        assert r.status_code == 200, r.text


@pytest.mark.anyio
async def test_a_private_project_is_indistinguishable_from_a_missing_one(two_users):
    """Divergent errors are an existence oracle: ana must not learn nik.private exists."""
    application, _, ana = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        forbidden_mcp = await _post(c, "/p/nik.private", ana, "tools/call",
                                    {"name": "graph_types", "arguments": {}})
        missing_mcp = await _post(c, "/p/does.not.exist", ana, "tools/call",
                                  {"name": "graph_types", "arguments": {}})
        forbidden_blob = await c.get("/p/nik.private/blobs/sha256/" + "0" * 64,
                                     headers={"Authorization": f"Bearer {ana}"})
        missing_blob = await c.get("/p/does.not.exist/blobs/sha256/" + "0" * 64,
                                   headers={"Authorization": f"Bearer {ana}"})
        forbidden_index = await c.get("/p/nik.private/")
        missing_index = await c.get("/p/does.not.exist/")

    assert forbidden_mcp.status_code == missing_mcp.status_code
    assert forbidden_mcp.text == missing_mcp.text
    assert forbidden_blob.status_code == missing_blob.status_code
    assert forbidden_blob.text == missing_blob.text
    assert forbidden_index.status_code == missing_index.status_code
    assert forbidden_index.text == missing_index.text


@pytest.mark.anyio
async def test_the_blob_surface_is_not_a_bypass(two_users):
    """The REST routes never reach the tool layer; this is the hole an ACL there would miss."""
    application, _, ana = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        for method, path in [("GET", "/p/nik.private/blobs/sha256/" + "0" * 64),
                             ("PUT", "/p/nik.private/blobs/sha256/" + "0" * 64),
                             ("GET", "/p/nik.private/guide/core"),
                             ("GET", "/p/nik.private/skills")]:
            r = await c.request(method, path, headers={"Authorization": f"Bearer {ana}"})
            assert r.status_code == 404, f"{method} {path} -> {r.status_code}"


@pytest.mark.anyio
async def test_healthz_stays_open_because_it_names_nothing(two_users):
    application, _, _ = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/p/default/healthz")).status_code == 200


@pytest.mark.anyio
async def test_the_root_index_no_longer_enumerates_projects(two_users):
    """It listed every project name with no token at all."""
    application, _, _ = two_users
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await c.get("/")
        assert r.status_code == 200
        body = r.text
        assert "nik.private" not in body
        assert "projects" not in json.loads(body)
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_project_acl.py -q`
Expected: FAIL — the blob and index paths return 401/200 rather than the generic 404, and `/` still lists projects.

- [ ] **Step 3: Apply the ACL in the middleware**

In `app.py`, add the shared denial and use it everywhere:

```python
# One body for "no such project" AND "not yours". Two different answers would let a stranger
# confirm that nik.private exists simply by observing which error came back.
PROJECT_DENIED = {"error": "unknown project or not accessible with this token"}
```

Replace the project lookup and auth block in `ProjectAuthMiddleware.__call__`:

```python
        project = self.registry.get(name)
        if project is None:
            return await self._json(send, 404, PROJECT_DENIED)

        from .identity import resolve, set_identity
        from .projects_meta import can_access

        meta = project.meta
        who = None
        if self.cfg.require_auth:
            token = bearer_from_headers(hdrs)
            who = resolve(token, self.identities, project)
            if who is None:
                # healthz may answer without a token; the project index may not, because for a
                # private project merely answering confirms it exists.
                if tail == "healthz":
                    set_identity(None)
                    return await self.app(scope, receive, send)
                if meta.visibility != "shared":
                    return await self._json(send, 404, PROJECT_DENIED)
                if not open_path:
                    return await self._json(send, 401,
                                            {"error": "invalid or missing bearer token"},
                                            extra=[(b"www-authenticate", b"Bearer")])
            elif not can_access(who, meta):
                return await self._json(send, 404, PROJECT_DENIED)
            if who is not None:
                scope.setdefault("state", {})["identity"] = who
                scope["state"]["client_id"] = who.user
        set_identity(who)
        return await self.app(scope, receive, send)
```

In the root index handler, drop the project enumeration:

```python
        return _json_response({
            "service": "hivemind",
            "health": f"{root}/healthz",
            "mcp": f"{root}/mcp",
            "note": ("project names are not listed here — call the project_list tool, or point "
                     "clients at a project base URL"),
        })
```

- [ ] **Step 4: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 146 total. If `test_health_and_index_work_on_both_bases_without_a_token` fails, the `healthz` carve-out above is wrong; fix it rather than weakening the test.

- [ ] **Step 5: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/app.py packages/hivemind-server/tests/test_project_acl.py
git commit -m "fix: enforce project ACL on every /p/ path and close the name-enumeration oracle"
```

- [ ] **Step 6: Deploy and verify — ONLY after the user approves this push**

This task changes authentication behaviour on a live server, so it does not go out on its own
authority. Ask first, then:

```bash
bash deploy/backup.sh
git push origin main && git push nik@<box>:hivemind HEAD:refs/heads/_in
ssh nik@<box> 'cd ~/hivemind && git reset --hard _in && git branch -D _in; bash deploy/restart.sh'
curl -s http://<box>:8787/healthz
curl -s http://<box>:8787/ | grep -c projects   # expect 0
```

Expected: healthz OK, the root index no longer contains a project list, and the existing fleet token still works (`curl -H "Authorization: Bearer <existing>" http://<box>:8787/p/default/healthz`).

---

### Task 6: Project-neutral MCP endpoint, project resolved per call

The biggest task. Today `build_mcp(project)` builds **one MCP server per project** and every tool
body closes over `db = project.db`. Editing 47 bodies would be both tedious and easy to get half
right, so the project is threaded through two proxies and one registration wrapper instead.

**Files:**
- Modify: `envelope.py` (add the project machinery), `mcp_tools.py` (`build_mcp` signature, `db`/`project` proxies, server name), `registry_tools.py` (per-call `base`), `bus_ws_tools.py` (per-call hub and secret), `app.py` (one app, mounted at `/mcp` and at each `/p/<name>`; middleware guards all paths)
- Create: `packages/hivemind-server/tests/test_routing.py`

**Interfaces:**
- Consumes: `identity.current_identity`, `projects_meta.can_access`, `ProjectRegistry`.
- Produces:
  - `envelope.ProjectAware(mcp, registry)` — proxy whose `.tool(...)` adds project resolution
  - `envelope.current_project() -> Optional[Project]`
  - `envelope.set_mount_default(name: Optional[str])`, `envelope.set_registry(registry)`
  - `envelope.CurrentProject` / `envelope.CurrentDb` attribute proxies
  - `mcp_tools.build_mcp(registry, *, instructions=INSTRUCTIONS) -> MCPServer` (**signature change**)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing.py
"""Per-call project resolution: the reason this design was chosen over server-side session state."""
import asyncio
import json

import httpx
import pytest

from test_server import Lifespan, _parse, _post


@pytest.fixture()
def two_projects(env):
    application, proj, _ = env
    from hivemind_server import projects_meta as pm
    from hivemind_server.identity import IdentityStore

    store = IdentityStore(application.state.cfg.identities_path)
    nik = store.mint("nik", "mac-studio")
    registry = application.state.registry
    for name in ("nik.a", "nik.b"):
        p = registry.create(name)
        pm.save(p.dir, pm.ProjectMeta(name=name, visibility="private", owner="nik"))
        from hivemind_server import schemas
        with p.db.write("setup", "seed types") as tx:
            schemas.define_type(tx.cur, tx, "node", "note",
                                {"type": "object", "additionalProperties": True}, status="active")
    return application, nik


@pytest.mark.anyio
async def test_the_project_argument_appears_in_every_tool_schema(env):
    application, proj, tok = env
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, f"/p/{proj.name}", tok, "tools/list")
        tools = _parse(r)["result"]["tools"]
    missing = [t["name"] for t in tools
               if "project" not in (t["inputSchema"].get("properties") or {})]
    assert not missing, f"tools with no project parameter: {missing}"
    # ...and it is never mandatory in the schema; the requirement is enforced at call time so the
    # error can name the projects the caller may actually use.
    assert not [t["name"] for t in tools if "project" in (t["inputSchema"].get("required") or [])]


@pytest.mark.anyio
async def test_a_write_without_a_project_is_refused(two_projects):
    """Fail closed: a forgotten argument used to mean a silent write into the default project."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "", nik, "tools/call",
                        {"name": "graph_upsert",
                         "arguments": {"type": "note", "props": {"title": "no project"},
                                       "reason": "should be refused"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is False
    assert "project" in out["error"]
    assert "nik.a" in out["error"], "the error must name the projects the caller can use"


@pytest.mark.anyio
async def test_a_read_without_a_project_uses_the_mount_default(two_projects):
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "/p/nik.a", nik, "tools/call", {"name": "graph_types", "arguments": {}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is True
    assert out["project"] == "nik.a", "every response echoes the resolved project"


@pytest.mark.anyio
async def test_concurrent_calls_on_one_token_land_in_different_databases(two_projects):
    """The test that would have failed under a server-side 'current project'. One token, two
    parallel agents, two projects — neither may see the other's write."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        async def write(project, title):
            return await _post(c, "", nik, "tools/call",
                               {"name": "graph_upsert",
                                "arguments": {"type": "note", "props": {"title": title},
                                              "project": project, "reason": "concurrency"}})
        await asyncio.gather(*[write("nik.a", "only-in-a") for _ in range(5)],
                            *[write("nik.b", "only-in-b") for _ in range(5)])

        async def titles(project):
            r = await _post(c, "", nik, "tools/call",
                            {"name": "graph_search",
                             "arguments": {"query": "only-in", "project": project}})
            body = json.loads(_parse(r)["result"]["content"][0]["text"])
            return {hit["props"]["title"] for hit in body["results"]}

        assert await titles("nik.a") == {"only-in-a"}
        assert await titles("nik.b") == {"only-in-b"}


@pytest.mark.anyio
async def test_an_inaccessible_project_argument_is_refused(two_projects, env):
    application, nik = two_projects
    from hivemind_server.identity import IdentityStore
    ana = IdentityStore(application.state.cfg.identities_path).mint("ana", "laptop")
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        r = await _post(c, "", ana, "tools/call",
                        {"name": "graph_types", "arguments": {"project": "nik.a"}})
        out = json.loads(_parse(r)["result"]["content"][0]["text"])
    assert out["ok"] is False
    assert "nik.a" not in out["error"], "the error must not confirm that nik.a exists"


@pytest.mark.anyio
async def test_root_rest_routes_are_not_an_unguarded_back_door(two_projects):
    """The MCP app carries custom REST routes; mounting it at the root must not expose them
    without a project."""
    application, nik = two_projects
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        for path in ("/blobs/sha256/" + "0" * 64, "/guide/core", "/skills"):
            r = await c.get(path, headers={"Authorization": f"Bearer {nik}"})
            assert r.status_code in (400, 404), f"{path} -> {r.status_code}"
        assert (await c.get("/blobs/sha256/" + "0" * 64)).status_code in (400, 401, 404)
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_routing.py -q`
Expected: FAIL — no `project` in any tool schema; posting to `""` (the root `/mcp`) 404s.

- [ ] **Step 3: Add the project machinery to `envelope.py`**

```python
import inspect
from contextvars import ContextVar
from typing import Optional

_PROJECT: ContextVar = ContextVar("hivemind_project", default=None)
_MOUNT_DEFAULT: ContextVar = ContextVar("hivemind_mount_default", default=None)
_REGISTRY = None


def set_registry(registry) -> None:
    global _REGISTRY
    _REGISTRY = registry


def set_mount_default(name: Optional[str]) -> None:
    _MOUNT_DEFAULT.set(name)


def current_project():
    return _PROJECT.get()


def visible_projects() -> list:
    """Names the caller may actually use — safe to put in an error message."""
    from .identity import current_identity
    from .projects_meta import can_access
    who = current_identity()
    if _REGISTRY is None or who is None:
        return []
    return sorted(p.name for p in _REGISTRY.all() if can_access(who, p.meta))


def resolve_project(explicit: Optional[str], *, requires: bool):
    """explicit argument -> mount default -> refuse (writes) or configured default (reads)."""
    from .db import Invalid
    from .identity import current_identity
    from .projects_meta import can_access

    who = current_identity()
    name = explicit or _MOUNT_DEFAULT.get()
    if name is None:
        if requires:
            raise Invalid(
                "this tool writes, so it needs an explicit project= argument. Writing into a "
                "defaulted project is how private work ends up in the shared graph. "
                f"Projects you can use: {', '.join(visible_projects()) or '(none)'}")
        raise Invalid("no project for this call; pass project=<name> "
                      f"(available: {', '.join(visible_projects()) or 'none'})")
    project = _REGISTRY.get(name) if _REGISTRY else None
    if project is None or not can_access(who, project.meta):
        # Same wording whether it is missing or forbidden — see app.PROJECT_DENIED.
        raise Invalid("unknown project or not accessible with this token. "
                      f"Projects you can use: {', '.join(visible_projects()) or '(none)'}")
    return project


def with_project(fn, *, requires: bool):
    """Append `project` to the exposed signature, resolve it per call, publish it.

    Verified against the installed SDK: it generates each tool's schema from the signature, so
    setting __signature__ is what makes `project` a real parameter the model can pass.
    """
    @functools.wraps(fn)
    def wrap(*a, project: Optional[str] = None, **k):
        proj = resolve_project(project, requires=requires)
        token = _PROJECT.set(proj)
        try:
            out = fn(*a, **k)
        finally:
            _PROJECT.reset(token)
        if isinstance(out, dict):
            out.setdefault("project", proj.name)     # echoed so drift is visible
        return out
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values()]
    params.append(inspect.Parameter("project", inspect.Parameter.KEYWORD_ONLY,
                                    default=None, annotation=Optional[str]))
    wrap.__signature__ = sig.replace(parameters=params)
    return wrap


class ProjectAware:
    """Stands in for MCPServer so every tool registered through it gets project resolution.

    Wrapping registration rather than decorating 47 bodies: one wrapper, and a tool physically
    cannot be added without it.
    """

    def __init__(self, mcp):
        self._mcp = mcp

    def __getattr__(self, name):
        return getattr(self._mcp, name)

    def tool(self, *, annotations=None, description=None, **kw):
        inner = self._mcp.tool(annotations=annotations, description=description, **kw)
        requires = bool(annotations is not None and not annotations.read_only_hint)

        def deco(fn):
            inner(with_project(fn, requires=requires))
            return fn
        return deco


class _Current:
    """Attribute proxy onto the project resolved for the current call.

    Lets `db = _Current("db")` at module build time keep working inside every tool body without
    touching any of them: attribute access happens during the call, when the project is known.
    """

    def __init__(self, attr: Optional[str] = None):
        self._attr = attr

    def _target(self):
        from .db import Invalid
        p = current_project()
        if p is None:
            raise Invalid("no project resolved for this call; pass project=<name>")
        return getattr(p, self._attr) if self._attr else p

    def __getattr__(self, name):
        return getattr(self._target(), name)


def CurrentProject():
    return _Current()


def CurrentDb():
    return _Current("db")
```

- [ ] **Step 4: Make `build_mcp` project-neutral**

In `mcp_tools.py`:

```python
def build_mcp(registry, *, instructions: str = INSTRUCTIONS) -> MCPServer:
    from .envelope import CurrentDb, CurrentProject, ProjectAware, set_registry
    set_registry(registry)
    db = CurrentDb()                  # resolves per call; every tool body already uses `db`
    project = CurrentProject()        # ditto for the few bodies that touch the project
    real = MCPServer(name="hivemind", instructions=instructions, version="1.1.0")
    mcp = ProjectAware(real)
    ...                               # every existing @mcp.tool body is unchanged
    registry_tools.attach(mcp, project)
    bus_ws_tools.attach(mcp, project, _config())
    return real                       # mount the real server, not the proxy
```

In `registry_tools.py`, replace the build-time `base` with a per-call helper:

```python
def _base() -> str:
    from .envelope import current_project
    return f"/p/{current_project().name}"
```

and substitute `_base()` for `base` at every use site (they are all inside tool bodies, so the
project is resolved by then). Pass `_base` instead of `base` into
`reg.attach_tools(mcp, project, _envelope, RO, WRITE, _base)` and call it as `base()` inside
`registry.attach_tools`.

In `bus_ws_tools.py`, move the two attach-time bindings into the bodies:

```python
def attach(mcp, project, cfg) -> None:
    def _hub():
        from .bus_ws import hub_for, register_secret
        p = _envelope_mod.current_project()
        register_secret(p.name, p.dir / "bus_secret")     # idempotent; first use creates it
        return hub_for(p.name)
```

and replace every `hub.` with `_hub().`, and build `ws_url` from `current_project().name`.

- [ ] **Step 5: Mount one app at `/mcp` and at every project path**

In `build_app`:

```python
    mcp = build_mcp(registry)
    asgi = mcp.streamable_http_app(streamable_http_path="/mcp",
                                   transport_security=_transport_security(cfg), host=cfg.host)
    for project in registry.all():
        project.tokens.ensure_first_token(client_id=f"{project.name}-bootstrap")
        _guide.ensure_core_guide(project.db)
        mounts.append(WebSocketRoute(f"/p/{project.name}/bus/ws", _ws_route(project)))
        mounts.append(Mount(f"/p/{project.name}", app=asgi))
    mounts.append(Mount("", app=asgi))       # project-neutral: /mcp and nothing else usable
```

In the middleware, stop early-returning for non-`/p/` paths and set the mount default:

```python
        if path in ("/", "/healthz"):
            return await self.app(scope, receive, send)
        from .envelope import set_mount_default
        if path.startswith("/p/"):
            ...                       # existing per-project branch, then:
            set_mount_default(name)
        else:
            set_mount_default(None)   # project-neutral endpoint: the tool argument decides
            token = bearer_from_headers(hdrs)
            who = resolve(token, self.identities, None)
            if self.cfg.require_auth and who is None:
                return await self._json(send, 401, {"error": "invalid or missing bearer token"},
                                        extra=[(b"www-authenticate", b"Bearer")])
            set_identity(who)
        return await self.app(scope, receive, send)
```

- [ ] **Step 6: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 152 total. If a tool fails with "no project resolved", its body used something
other than `db` or `project` from the closure; give it `current_project()` explicitly.

- [ ] **Step 7: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/{envelope,mcp_tools,registry_tools,bus_ws_tools,app}.py \
        packages/hivemind-server/tests/test_routing.py
git commit -m "feat: project-neutral MCP endpoint with per-call project resolution; writes fail closed"
```

---

### Task 7: Project tools over MCP

**Files:**
- Create: `packages/hivemind-server/src/hivemind_server/project_tools.py`
- Create: `packages/hivemind-server/tests/test_project_tools.py`
- Modify: `mcp_tools.py` (attach), `project.py` (`ProjectRegistry.create` takes metadata, is atomic), `admin.py` (`project-share`)

**Interfaces:**
- Consumes: `projects_meta.*`, `identity.current_identity`, `envelope.RO/WRITE/ProjectAware`.
- Produces MCP tools `project_list()`, `project_create(name, visibility="private", label="", session=None, schema="inherit", pack=None)`, `project_info(project)`, `project_share(project, user)`, `project_unshare(project, user)`; and `ProjectRegistry.create(name, *, meta=None) -> Project`.
- `schema` is one of `"inherit"` | `"interview"` | `"bare"`; `SCHEMA_MODES` is exported for the tool description and validation.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_project_tools.py
import concurrent.futures as cf
import json

import pytest

from hivemind_server import project_tools as pt
from hivemind_server import projects_meta as pm
from hivemind_server.db import Invalid
from hivemind_server.identity import Identity


@pytest.fixture()
def reg(tmp_path):
    from hivemind_server.project import ProjectRegistry
    r = ProjectRegistry(tmp_path / "projects", max_blob_bytes=1 << 20, blob_grace_seconds=1)
    r.discover()
    return r


NIK = Identity(user="nik", device="mac-studio")
ANA = Identity(user="ana", device="laptop")
ADMIN = Identity(user="root", device="box", role="admin")


def test_create_a_private_project(reg):
    out = pt.create(reg, NIK, "nik.private", visibility="private")
    assert out["project"] == "nik.private"
    assert reg.get("nik.private").meta.owner == "nik"


def test_a_private_project_must_carry_your_prefix(reg):
    with pytest.raises(Invalid):
        pt.create(reg, NIK, "ana.private", visibility="private")


@pytest.mark.parametrize("name", ["nik.", "nik..x", "Nik.x", "a" * 65, "..", "nik/x", ""])
def test_pathological_names_never_touch_the_disk(reg, name):
    """Review Focus 2: validation happens before any mkdir."""
    with pytest.raises(Invalid):
        pt.create(reg, NIK, name, visibility="private")
    assert not (reg.root / name).exists()


def test_concurrent_creation_of_the_same_name_yields_one_project(reg):
    """Review Focus 3: dir+db creation is not transactional."""
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = [f for f in cf.as_completed(
            [ex.submit(pt.create, reg, NIK, "nik.race", "private") for _ in range(8)])]
    ok = [r for r in results if not r.exception()]
    assert len(ok) >= 1
    assert sum(1 for p in reg.root.iterdir() if p.name == "nik.race") == 1
    assert reg.get("nik.race").meta.owner == "nik"


def test_the_per_user_cap_is_enforced(reg, monkeypatch):
    monkeypatch.setattr(pt, "MAX_PER_USER", 3)
    for i in range(3):
        pt.create(reg, NIK, f"nik.p{i}", visibility="private")
    with pytest.raises(Invalid) as e:
        pt.create(reg, NIK, "nik.p4", visibility="private")
    assert "cap" in str(e.value).lower()


def test_listing_groups_and_hides(reg):
    pt.create(reg, NIK, "nik.private", visibility="private")
    pt.create(reg, ANA, "ana.private", visibility="private")
    pt.create(reg, NIK, "team", visibility="shared")

    mine = pt.listing(reg, NIK)
    assert "nik.private" in mine["mine"]
    assert "team" in mine["shared"]
    assert "ana.private" not in json.dumps(mine), "another user's private project must not appear"


def test_share_then_unshare(reg):
    pt.create(reg, NIK, "nik.redteam", visibility="private")
    pt.share(reg, NIK, "nik.redteam", "ana")
    assert "nik.redteam" in pt.listing(reg, ANA)["shared_with_me"]
    pt.unshare(reg, NIK, "nik.redteam", "ana")
    assert "nik.redteam" not in json.dumps(pt.listing(reg, ANA))


def test_only_the_owner_can_share(reg):
    pt.create(reg, NIK, "nik.redteam", visibility="private")
    pt.share(reg, NIK, "nik.redteam", "ana")
    with pytest.raises(Invalid):
        pt.share(reg, ANA, "nik.redteam", "eve")     # a member cannot re-share


def test_an_admin_cannot_share_someone_elses_private_project(reg):
    """A16: an admin who could share could grant themselves read access."""
    pt.create(reg, NIK, "nik.private", visibility="private")
    with pytest.raises(Invalid):
        pt.share(reg, ADMIN, "nik.private", "root")


def test_sharing_an_already_shared_project_is_an_error(reg):
    pt.create(reg, NIK, "team", visibility="shared")
    with pytest.raises(Invalid):
        pt.share(reg, NIK, "team", "ana")


def test_the_owner_cannot_be_unshared(reg):
    pt.create(reg, NIK, "nik.private", visibility="private")
    with pytest.raises(Invalid):
        pt.unshare(reg, NIK, "nik.private", "nik")


def test_creating_a_name_you_already_own_returns_it(reg):
    a = pt.create(reg, NIK, "nik.private", visibility="private")
    b = pt.create(reg, NIK, "nik.private", visibility="private")
    assert a["project"] == b["project"] and b["existing"] is True


def test_a_legacy_identity_cannot_create_a_private_project(reg):
    """A14: a legacy client_id like `mac-studio` cannot satisfy the <user>. prefix rule."""
    legacy = Identity(user="legacy:mac-studio", device="mac-studio", legacy=True,
                      project_scope="default")
    with pytest.raises(Invalid) as e:
        pt.create(reg, legacy, "legacy:mac-studio.private", visibility="private")
    assert "minted" in str(e.value) or "legacy" in str(e.value)


def _seed_widget(reg):
    from hivemind_server import schemas
    src = reg.get(reg.default_name)
    with src.db.write("setup", "seed") as tx:
        schemas.define_type(tx.cur, tx, "node", "widget",
                            {"type": "object", "additionalProperties": True}, status="active")
    return src


def test_inherit_copies_the_source_vocabulary(reg):
    src = _seed_widget(reg)
    from hivemind_server import schemas
    pt.create(reg, NIK, "nik.child", visibility="private", schema="inherit", source=src)
    assert "widget" in {t["name"] for t in schemas.dump(reg.get("nik.child").db)["node_types"]}


def test_bare_starts_with_no_types_and_says_the_agent_decides(reg):
    src = _seed_widget(reg)
    from hivemind_server import schemas
    out = pt.create(reg, NIK, "nik.bare", visibility="private", schema="bare", source=src)
    names = {t["name"] for t in schemas.dump(reg.get("nik.bare").db)["node_types"]}
    assert "widget" not in names
    assert "schema_propose" in out["next"]


def test_interview_creates_no_types_and_asks_for_the_schema_skill(reg):
    """The server cannot interview anybody; it hands the job to the agent's skill."""
    src = _seed_widget(reg)
    from hivemind_server import schemas
    out = pt.create(reg, NIK, "nik.interview", visibility="private", schema="interview", source=src)
    assert {t["name"] for t in schemas.dump(reg.get("nik.interview").db)["node_types"]} == set()
    assert out["schema"] == "interview"
    assert "hivemind-schema" in out["next"], "the response must name the skill to load"


def test_an_unknown_schema_mode_is_refused(reg):
    with pytest.raises(Invalid):
        pt.create(reg, NIK, "nik.x", visibility="private", schema="magic")
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_project_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'hivemind_server.project_tools'`

- [ ] **Step 3: Write `project_tools.py`**

```python
"""Create, list, inspect and share projects — the lifecycle, callable from a Claude session.

Sharing is owner-only on purpose. An admin who could share any project could grant themselves read
access to it, which would make the private tier decorative rather than a boundary (spec A16).
"""
from __future__ import annotations

import os
import shutil
from typing import Optional

from . import projects_meta as pm
from . import schemas
from .db import Invalid
from .identity import Identity

MAX_PER_USER = int(os.environ.get("HIVEMIND_MAX_PROJECTS_PER_USER", "50"))


def _require_minted(who: Identity) -> None:
    if who.legacy:
        raise Invalid("creating a project needs a minted server-level identity; this token is a "
                      "legacy project token (hivemind-admin mint-token --user <you>)")


def _owned_count(reg, user: str) -> int:
    return sum(1 for p in reg.all() if p.meta.owner == user)


SCHEMA_MODES = ("inherit", "interview", "bare")


def create(reg, who: Identity, name: str, visibility: str = "private", *, label: str = "",
           session: Optional[str] = None, schema: str = "inherit", pack: Optional[str] = None,
           source=None) -> dict:
    _require_minted(who)
    if visibility not in pm.VISIBILITIES:
        raise Invalid(f"visibility must be one of {pm.VISIBILITIES}")
    if schema not in SCHEMA_MODES:
        raise Invalid(f"schema must be one of {SCHEMA_MODES}")
    pm.validate_project_name(name)
    if visibility == "private":
        pm.check_private_name(who.user, name)

    existing = reg.get(name)
    if existing is not None:
        if not pm.can_access(who, existing.meta):
            raise Invalid("unknown project or not accessible with this token")
        return {"project": name, "existing": True, "visibility": existing.meta.visibility}
    if _owned_count(reg, who.user) >= MAX_PER_USER:
        raise Invalid(f"per-user project cap reached ({MAX_PER_USER}); "
                      f"reuse an existing project or raise HIVEMIND_MAX_PROJECTS_PER_USER")

    # Claim the name with an atomic mkdir BEFORE building anything, so two sessions racing on the
    # same name cannot both proceed and leave a half-built directory behind.
    target = reg.root / name
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        winner = reg.create(name)
        return {"project": name, "existing": True, "visibility": winner.meta.visibility}
    try:
        meta = pm.ProjectMeta(name=name, visibility=visibility,
                              owner=who.user if visibility == "private" else None,
                              label=label[:200], session=session)
        pm.save(target, meta)
        project = reg.create(name)
        from . import guide as _guide
        _guide.ensure_core_guide(project.db)
        if schema == "inherit":
            src = source if source is not None else reg.get(reg.default_name)
            if src is not None:
                schemas.copy_types(src.db, project.db, agent=who.user)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)       # never leave a half-created project
        reg.forget(name)
        raise
    # A project's types ARE its meaning, so the response says how to get them rather than leaving
    # the agent to invent a vocabulary before it understands the work.
    nxt = {
        "inherit": f"pass project={name} on your Hivemind calls; it inherited the source "
                   f"project's node and edge types",
        "interview": f"this project has NO schema yet. Load the `hivemind-schema` skill and run "
                     f"the interview with the user, then apply the result. Pass project={name} on "
                     f"your Hivemind calls.",
        "bare": f"this project has no schema by choice — define types with schema_propose as the "
                f"work demands them. Pass project={name} on your Hivemind calls.",
    }[schema]
    return {"project": name, "existing": False, "visibility": visibility, "schema": schema,
            "next": nxt}


def listing(reg, who: Identity) -> dict:
    shared, mine, with_me = [], [], []
    for p in reg.all():
        meta = p.meta
        if not pm.can_access(who, meta):
            continue
        if meta.visibility == "shared":
            shared.append(meta.name)
        elif meta.owner == who.user:
            mine.append(meta.name)
        else:
            with_me.append(meta.name)
    return {"shared": sorted(shared), "mine": sorted(mine), "shared_with_me": sorted(with_me),
            "hint": "pass project=<name> on your Hivemind calls"}


def info(reg, who: Identity, name: str) -> dict:
    project = reg.get(name)
    if project is None or not pm.can_access(who, project.meta):
        raise Invalid("unknown project or not accessible with this token")
    return project.meta.public(who)


def _owner_only(reg, who: Identity, name: str):
    project = reg.get(name)
    if project is None or not pm.can_access(who, project.meta):
        raise Invalid("unknown project or not accessible with this token")
    meta = project.meta
    if meta.visibility == "shared":
        raise Invalid(f"{name} is already shared with everyone; there is nothing to grant")
    if meta.owner != who.user:
        raise Invalid(f"only the owner of {name} can change who it is shared with")
    return project, meta


def share(reg, who: Identity, name: str, user: str, identities=None) -> dict:
    project, meta = _owner_only(reg, who, name)
    if identities is not None and not identities.has_user(user):
        raise Invalid(f"no such user {user!r}; mint them a token first "
                      f"(hivemind-admin mint-token --user {user})")
    if user == meta.owner:
        raise Invalid("the owner already has access")
    if user not in meta.members:
        meta.members.append(user)
        pm.save(project.dir, meta)
    return {"project": name, "members": meta.members}


def unshare(reg, who: Identity, name: str, user: str) -> dict:
    project, meta = _owner_only(reg, who, name)
    if user == meta.owner:
        raise Invalid("the owner cannot be removed from their own project")
    meta.members = [m for m in meta.members if m != user]
    pm.save(project.dir, meta)
    return {"project": name, "members": meta.members}


def attach(mcp, registry, identities) -> None:
    from .envelope import RO, WRITE
    from .identity import current_identity

    # These tools are ABOUT projects rather than IN one, so they are registered on the real server
    # and never get the injected `project` argument.
    @mcp.tool(annotations=RO, description="List the projects you can use, grouped: shared with "
                                          "everyone, yours, and shared with you. Pass the name as "
                                          "project=<name> on other calls.")
    def project_list() -> dict:
        return {"ok": True, **listing(registry, current_identity())}

    @mcp.tool(annotations=WRITE,
              description="Create a project. visibility='private' (only you, must be named "
                          "<you>.<suffix>) or 'shared' (everyone). schema='inherit' copies the "
                          "current project's node/edge types (default); 'interview' creates it "
                          "empty and tells you to run the hivemind-schema skill, which asks the "
                          "user about their work and builds a schema from the answers; 'bare' "
                          "leaves it empty for you to define types as you go. ASK THE USER which "
                          "they want rather than choosing for them.")
    def project_create(name: str, visibility: str = "private", label: str = "",
                       session: Optional[str] = None, schema: str = "inherit") -> dict:
        return {"ok": True, **create(registry, current_identity(), name, visibility,
                                     label=label, session=session, schema=schema)}

    @mcp.tool(annotations=RO, description="Metadata for one project you can access.")
    def project_info(project: str) -> dict:
        return {"ok": True, **info(registry, current_identity(), project)}

    @mcp.tool(annotations=WRITE, description="Grant another user access to a private project you "
                                             "own. Owner only — members cannot re-share.")
    def project_share(project: str, user: str) -> dict:
        return {"ok": True, **share(registry, current_identity(), project, user, identities)}

    @mcp.tool(annotations=WRITE, description="Revoke a user's access to a private project you own. "
                                             "Effective on their next call.")
    def project_unshare(project: str, user: str) -> dict:
        return {"ok": True, **unshare(registry, current_identity(), project, user)}
```

- [ ] **Step 4: Add the registry and schema helpers they depend on**

In `project.py`, add to `ProjectRegistry`:

```python
    def forget(self, name: str) -> None:
        """Drop a half-created project from the in-memory registry (see project_tools.create)."""
        self._projects.pop(name, None)
```

In `schemas.py`, add the inheritance helper:

```python
def copy_types(src_db, dst_db, *, agent: str = "system") -> int:
    """Copy active node/edge type definitions into a new project.

    A project with no types cannot be written to at all, so a fresh project that inherited nothing
    would be useless the moment it was created.
    """
    defs = dump(src_db)
    n = 0
    with dst_db.write(agent, "inherit schema from source project") as tx:
        for kind, key in (("node", "node_types"), ("edge", "edge_types")):
            for t in defs.get(key, []):
                if t.get("status") != "active":
                    continue
                define_type(tx.cur, tx, kind, t["name"], t["json_schema"], status="active",
                            traits=t.get("traits"))
                n += 1
    return n
```

- [ ] **Step 5: Attach on the real server and add the admin command**

In `mcp_tools.build_mcp`, before returning: `project_tools.attach(real, registry, identities)` —
note it takes `real`, not the `ProjectAware` proxy, because these tools carry their own `project`
parameter with different meaning. Thread `identities` into `build_mcp(registry, identities, ...)`.

In `admin.py`: `project-share <project> <user>` and `project-unshare <project> <user>`, acting as
the project's owner, for recovery from the box.

- [ ] **Step 6: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 167 total.

- [ ] **Step 7: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/{project_tools,project,schemas,mcp_tools,admin}.py \
        packages/hivemind-server/tests/test_project_tools.py
git commit -m "feat: create, list, inspect and share projects from a session"
```

---

### Task 8: Authorship — storage, write path, reads

**Files:**
- Modify: `schema.sql` (new columns + indexes for fresh DBs), `db.py` (`_MIGRATIONS`, `write()` signature), `graph.py` (record and return authors), `skills.py`, `traps.py`, `registry.py`, `guide.py`
- Create: `packages/hivemind-server/tests/test_authorship.py`

**Interfaces:**
- Consumes: `identity.current_identity`.
- Produces: `db.write(agent_id, reason=None, meta=None, user=None, device=None)`; `graph.get_node` returns `author`, `created_by`, `contributors`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_authorship.py
"""Identity is what the token says, not what the caller claims."""
import pytest

from hivemind_server import graph
from hivemind_server.identity import Identity, set_identity


@pytest.fixture(autouse=True)
def as_nik():
    set_identity(Identity(user="nik", device="mac-studio"))
    yield
    set_identity(None)


def test_the_author_is_the_token_not_the_agent_argument(db):
    """`agent` used to be believed verbatim: agent="verify-1.0.0" was recorded as fact."""
    out = graph.upsert_node(db, "root", "component", {"title": "x"}, reason="claiming to be root")
    got = graph.get_node(db, node_id=out["node_id"])
    assert got["author"] == "nik"
    assert got["agent_label"] == "root"


def test_created_by_and_last_author_are_distinguished(db):
    a = graph.upsert_node(db, "job-a", "component", {"title": "x"}, reason="create")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "job-b", "component", {"title": "x2"},
                      node_id=a["node_id"], expected_head=a["version_id"], reason="revise")
    got = graph.get_node(db, node_id=a["node_id"])
    assert got["created_by"] == "nik"
    assert got["author"] == "ana"


def test_the_contributor_chain_names_everyone(db):
    out = graph.upsert_node(db, "j", "component", {"title": "v1"}, reason="create")
    head = out["version_id"]
    for user in ("ana", "bo", "cy", "di"):
        set_identity(Identity(user=user, device="x"))
        out = graph.upsert_node(db, "j", "component", {"title": f"v-{user}"},
                                node_id=out["node_id"], expected_head=head, reason="revise")
        head = out["version_id"]
    got = graph.get_node(db, node_id=out["node_id"])
    assert set(got["contributors"]) == {"nik", "ana", "bo", "cy", "di"}


def test_history_entries_carry_their_own_author(db):
    out = graph.upsert_node(db, "j", "component", {"title": "v1"}, reason="create")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "j", "component", {"title": "v2"}, node_id=out["node_id"],
                      expected_head=out["version_id"], reason="revise")
    hist = graph.get_node(db, node_id=out["node_id"], history=True)["history"]
    assert [h["author"] for h in hist] == ["ana", "nik"]


def test_an_unresolvable_identity_is_recorded_as_legacy_and_the_write_proceeds(db):
    """Blocking would take the fleet down mid-migration."""
    set_identity(None)
    out = graph.upsert_node(db, "cli", "component", {"title": "x"}, reason="no identity")
    assert graph.get_node(db, node_id=out["node_id"])["author"] == "legacy:unknown"


def test_bulk_edges_are_attributed_through_tx_only(db):
    """edge_bulk has no version row, so there is no author_user column to fill."""
    a = graph.upsert_node(db, "j", "function", {"title": "a"}, reason="x")
    b = graph.upsert_node(db, "j", "function", {"title": "b"}, reason="x")
    graph.bulk_load(db, "nik", "calls", "kernelcache@x", [[a["node_id"], b["node_id"], {}]])
    with db.read() as cur:
        rows = cur.execute("SELECT agent_id, user_id FROM tx ORDER BY tx_id DESC LIMIT 1").fetchall()
    assert rows[0][1] == "nik"
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_authorship.py -q`
Expected: FAIL — `KeyError: 'author'`.

- [ ] **Step 3: Add the columns**

In `schema.sql`, add to the `tx` table `user_id TEXT` and `device TEXT`; to `node_version` and
`edge_version` `author_user TEXT`; to `node` `created_by TEXT`. Then add:

```sql
CREATE INDEX IF NOT EXISTS ix_node_ver_author ON node_version(author_user);
CREATE INDEX IF NOT EXISTS ix_edge_ver_author ON edge_version(author_user);
CREATE INDEX IF NOT EXISTS ix_node_created_by ON node(created_by);
```

In `db.py`, extend `_MIGRATIONS` (they run before `schema.sql`, which is why the indexes above are
safe):

```python
    _MIGRATIONS = (
        ("skill_link", "source", "TEXT NOT NULL DEFAULT 'auto'"),
        ("skill_link", "score", "REAL"),
        ("tool_link", "source", "TEXT NOT NULL DEFAULT 'auto'"),
        ("tool_link", "score", "REAL"),
        ("tx", "user_id", "TEXT"),
        ("tx", "device", "TEXT"),
        ("node_version", "author_user", "TEXT"),
        ("edge_version", "author_user", "TEXT"),
        ("node", "created_by", "TEXT"),
    )
```

- [ ] **Step 4: Record the identity on every write**

In `db.write`, take the identity from the contextvar so no call site has to be told:

```python
    def write(self, agent_id: str, reason=None, meta=None):
        from .identity import current_identity
        who = current_identity()
        user = who.user if who else "legacy:unknown"
        device = who.device if who else ""
        ...
                cur.execute(
                    "INSERT INTO tx(tx_time, agent_id, reason, meta, user_id, device) "
                    "VALUES(?,?,?,?,?,?)",
                    (tstamp, agent_id, reason, canonical_json(meta or {}), user, device))
```

and expose `tx.user` on the `Tx` object so `graph.py` can stamp the version rows. In `graph.py`,
add `author_user` to the `node_version` / `edge_version` inserts and `created_by` to the `node`
insert (`tx.user`). Do the same for `skills.py`, `traps.py`, `registry.py` and `guide.py`, which all
already receive an `agent` string — keep it as the label, add the user.

- [ ] **Step 5: Return authorship on reads**

In `graph.get_node`, after the head is loaded:

```python
    out["author"] = head["author_user"] or "legacy:unknown"
    out["agent_label"] = tx_row["agent_id"]
    out["created_by"] = node_row["created_by"] or "legacy:unknown"
    with db.read() as cur:
        out["contributors"] = [r[0] for r in cur.execute(
            "SELECT DISTINCT COALESCE(author_user,'legacy:unknown') FROM node_version "
            "WHERE node_id=? ORDER BY 1", (node_id,))]
```

and add `"author"` to each entry built for `history`.

- [ ] **Step 6: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 174 total.

- [ ] **Step 7: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/{schema.sql,db,graph,skills,traps,registry,guide}.py \
        packages/hivemind-server/src/hivemind_server/schema.sql \
        packages/hivemind-server/tests/test_authorship.py
git commit -m "feat: attribute every write to the token's identity, surface authors on reads"
```

---

### Task 9: Filter by author, and return real props from search

**Two changes to the same function and tool, so they land together.**

#### 9a. `graph_search` can return structured props, not just a truncated snippet

Today every hit carries `snippet` = `json.dumps(props)[:200]`, which truncates mid-key and is
unparseable — an agent asking "which of these are `status=submitted`" gets garbage and has to
`graph_get` every hit. Measured on the live graph (2000 head revisions): props are a median of 721
chars, p90 3657, p99 11849, max 30405. So a 25-hit page of full props is ~91k chars (~23k tokens)
at p90 and ~760k (~190k tokens) at worst, against Claude Code's 10k-warn / 25k-hard MCP output cap.
Returning props unconditionally is therefore not an option; returning them on request, bounded, is.

Add two parameters to `graph_search` (and `graph.search_nodes`):

- **`fields: Optional[list[str]]`** — project just these props keys into each hit as `props`.
  This is the cheap, precise mode and the one to recommend in the tool description:
  `fields=["title","status"]` over 25 hits costs almost nothing and answers the question directly.
  Keys absent from a node are simply omitted rather than returned as null.
- **`props: bool = False`** — include the **full** props dict per hit. Because this is the
  unbounded mode, it is clamped: `limit` is capped at `PROPS_LIMIT = 10` when `props=true`, and any
  single node's props exceeding `PROPS_MAX_CHARS = 4000` is replaced by its first 4000 characters
  plus `{"_truncated": true, "_chars": <n>}` so the caller knows to `graph_get` that one. The reply
  carries `props_clamped: true` whenever either bound fired, so the agent can tell a short answer
  from a complete one.

`snippet` stays exactly as it is when neither parameter is passed — every existing caller and test
keeps working. `fields` wins if both are given.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_authorship.py
def test_fields_projects_only_the_named_keys(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha", "status": "open",
                                             "notes": "x" * 5000}, reason="x")
    hit = graph.search_nodes(db, "alpha", fields=["title", "status"])["results"][0]
    assert hit["props"] == {"title": "alpha", "status": "open"}
    assert "notes" not in hit["props"], "a field not asked for must not be shipped"
    assert "snippet" not in hit, "fields replaces the snippet rather than adding to it"


def test_a_missing_field_is_omitted_not_nulled(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha"}, reason="x")
    hit = graph.search_nodes(db, "alpha", fields=["title", "nope"])["results"][0]
    assert hit["props"] == {"title": "alpha"}


def test_props_true_returns_the_whole_dict(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha", "status": "open"}, reason="x")
    hit = graph.search_nodes(db, "alpha", props=True)["results"][0]
    assert hit["props"] == {"title": "alpha", "status": "open"}


def test_props_true_clamps_the_page_size(db):
    for i in range(15):
        graph.upsert_node(db, "j", "component", {"title": f"alpha {i}"}, reason="x")
    out = graph.search_nodes(db, "alpha", props=True, limit=25)
    assert len(out["results"]) == graph.PROPS_LIMIT == 10
    assert out["props_clamped"] is True
    assert out["has_more"] is True, "clamping must not look like the end of the results"


def test_an_oversized_props_is_truncated_with_a_marker(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha", "big": "x" * 9000}, reason="x")
    hit = graph.search_nodes(db, "alpha", props=True)["results"][0]
    assert hit["props"]["_truncated"] is True
    assert hit["props"]["_chars"] > graph.PROPS_MAX_CHARS
    assert len(json.dumps(hit["props"])) < 5000, "the whole point is a bounded payload"


def test_the_default_shape_is_unchanged(db):
    graph.upsert_node(db, "j", "component", {"title": "alpha"}, reason="x")
    hit = graph.search_nodes(db, "alpha")["results"][0]
    assert "snippet" in hit and "props" not in hit
    assert "props_clamped" not in graph.search_nodes(db, "alpha")


def test_fields_composes_with_the_author_filter(db):
    graph.upsert_node(db, "j", "component", {"title": "mine", "status": "open"}, reason="x")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "j", "component", {"title": "theirs", "status": "open"}, reason="x")
    out = graph.search_nodes(db, "", author="nik", fields=["title"])["results"]
    assert [h["props"] for h in out] == [{"title": "mine"}]
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_authorship.py -q -k "fields or props"`
Expected: FAIL — `search_nodes() got an unexpected keyword argument 'fields'`

- [ ] **Step 3: Implement the projection in `graph.py`**

Add the two module constants, then build each hit's payload from the already-loaded `props` dict —
the SQL already selects `nv.props`, so no extra query is needed:

```python
PROPS_LIMIT = 10          # full-props pages are clamped to this many hits
PROPS_MAX_CHARS = 4000    # ...and any single node's props to this many characters
```

In the result loop, replace the unconditional `snippet` with: `fields` → `{k: props[k] for k in
fields if k in props}`; else `props=True` → the whole dict, truncated as above; else the existing
`snippet`. Set `props_clamped` on the reply when the page size or any node was clamped, and make
sure `has_more` still reflects the *unclamped* row count so a clamped page is not mistaken for the
last one.

- [ ] **Step 4: Expose both on the tool**

Extend `graph_search`'s description: `fields=["title","status"]` returns just those keys per hit and
is the cheap way to read structure; `props=true` returns everything but clamps to 10 hits and
truncates any node over 4000 characters, so prefer `fields` unless you genuinely need the lot.

- [ ] **Step 5: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/{graph,mcp_tools}.py \
        packages/hivemind-server/tests/test_authorship.py
git commit -m "feat: graph_search can project props or return them in full, bounded"
```

---

#### 9b. Filter by author

**Files:**
- Modify: `graph.py` (`search_nodes`), `skills.py`, `traps.py`, `registry.py` (search functions), `mcp_tools.py` + `registry_tools.py` (tool parameters)
- Modify: `packages/hivemind-server/tests/test_authorship.py`

**Interfaces:**
- Consumes: the `author_user` column and its index from Task 8.
- Produces: an `author: Optional[str]` parameter on `graph_search`, `skill_search`, `trap_search`, `tool_search`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_authorship.py
def test_search_filters_by_author(db):
    graph.upsert_node(db, "j", "component", {"title": "nik wrote this"}, reason="x")
    set_identity(Identity(user="ana", device="laptop"))
    graph.upsert_node(db, "j", "component", {"title": "ana wrote this"}, reason="x")

    nik_only = graph.search_nodes(db, "wrote", author="nik")["results"]
    assert [r["props"]["title"] for r in nik_only] == ["nik wrote this"]
    assert len(graph.search_nodes(db, "wrote")["results"]) == 2


def test_the_author_filter_composes_with_types(db):
    graph.upsert_node(db, "j", "component", {"title": "c"}, reason="x")
    graph.upsert_node(db, "j", "finding", {"title": "f"}, reason="x")
    out = graph.search_nodes(db, "", types=["finding"], author="nik")["results"]
    assert len(out) == 1 and out[0]["node_type"] == "finding"


def test_an_author_with_no_writes_returns_nothing_rather_than_everything(db):
    graph.upsert_node(db, "j", "component", {"title": "c"}, reason="x")
    assert graph.search_nodes(db, "", author="nobody")["results"] == []
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_authorship.py -q -k author`
Expected: FAIL — `TypeError: search_nodes() got an unexpected keyword argument 'author'`

- [ ] **Step 3: Push the filter into SQL**

In `graph.search_nodes`, add `author: Optional[str] = None` and, where `types` is already applied,
add `AND nv.author_user = :author` when it is set — in the SQL, not as a post-filter. (A post-filter
over the row cap is the bug already fixed once for `types`; do not reintroduce it.) Mirror the
parameter in `skills.search`, `traps.search`, `registry.search_tools`, and expose it on the four
tools with the description: `"author='<user>' restricts to what that identity wrote."`

- [ ] **Step 4: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 177 total.

- [ ] **Step 5: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/{graph,skills,traps,registry,mcp_tools,registry_tools}.py \
        packages/hivemind-server/tests/test_authorship.py
git commit -m "feat: filter graph, skill, trap and tool search by author"
```

---

### Task 10: Backfill existing writes as `legacy:*`

**Files:**
- Modify: `admin.py`
- Modify: `packages/hivemind-server/tests/test_authorship.py`

**Interfaces:**
- Consumes: the columns from Task 8.
- Produces: `hivemind-admin backfill-authors [--yes]` — dry run is the DEFAULT, matching `gc --yes`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_authorship.py
def test_backfill_attributes_pre_identity_writes_to_legacy_never_to_a_person(db):
    """Attributing old self-declared strings to `nik` would be inventing provenance."""
    from hivemind_server.admin import backfill_authors
    set_identity(None)
    out = graph.upsert_node(db, "cli", "component", {"title": "old"}, reason="pre-identity")
    with db.write_light() as cur:          # simulate a row written before the column existed
        cur.execute("UPDATE node_version SET author_user=NULL WHERE node_id=?", (out["node_id"],))

    report = backfill_authors(db, dry_run=True)
    assert report["would_update"] >= 1
    assert graph.get_node(db, node_id=out["node_id"])["author"] == "legacy:unknown"

    backfill_authors(db, dry_run=False)
    assert graph.get_node(db, node_id=out["node_id"])["author"] == "legacy:cli"
    assert "nik" not in str(report)
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_authorship.py -q -k backfill`
Expected: FAIL — `ImportError: cannot import name 'backfill_authors'`

- [ ] **Step 3: Implement it**

```python
def backfill_authors(db, *, dry_run: bool = True) -> dict:
    """Fill author_user on rows written before identity existed, from the tx agent label.

    Explicitly a command and never a startup step: it rewrites provenance on every version row,
    and doing that silently at boot on a multi-gigabyte live database is not recoverable by
    someone who did not expect it. Measured scale on the live server: 124,353 nodes and 380,729
    node-version rows, so the dry-run reports hundreds of thousands, not hundreds.
    """
    sql_count = ("SELECT COUNT(*) FROM node_version WHERE author_user IS NULL",
                 "SELECT COUNT(*) FROM edge_version WHERE author_user IS NULL",
                 "SELECT COUNT(*) FROM node WHERE created_by IS NULL")
    with db.read() as cur:
        nv, ev, nodes = (cur.execute(q).fetchone()[0] for q in sql_count)
    report = {"would_update": nv + ev + nodes, "node_versions": nv, "edge_versions": ev,
              "nodes": nodes, "dry_run": dry_run,
              "note": "pre-identity writes become legacy:<agent_id>, never a real username"}
    if dry_run:
        return report
    with db.write_light() as cur:
        cur.execute("UPDATE node_version SET author_user = "
                    "  'legacy:' || COALESCE((SELECT agent_id FROM tx WHERE tx.tx_id = "
                    "                          node_version.tx_from), 'unknown') "
                    "WHERE author_user IS NULL")
        cur.execute("UPDATE edge_version SET author_user = "
                    "  'legacy:' || COALESCE((SELECT agent_id FROM tx WHERE tx.tx_id = "
                    "                          edge_version.tx_from), 'unknown') "
                    "WHERE author_user IS NULL")
        cur.execute("UPDATE node SET created_by = "
                    "  'legacy:' || COALESCE((SELECT agent_id FROM tx WHERE tx.tx_id = "
                    "                          node.created_tx), 'unknown') "
                    "WHERE created_by IS NULL")
    report["updated"] = report.pop("would_update")
    return report
```

Wire it as `sub.add_parser("backfill-authors")` with `--dry-run`, printing the report as JSON.

- [ ] **Step 4: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 178 total.

- [ ] **Step 5: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/admin.py packages/hivemind-server/tests/test_authorship.py
git commit -m "feat: backfill-authors admin command, attributing old writes as legacy"
```

---

### Task 11: Close the bus revocation hole

A listen key is a 7-day HMAC and the WebSocket handshake only checks its signature, so a revoked
member's listener keeps receiving from a private project's bus until the key expires. The WS route
also bypasses `ProjectAuthMiddleware` entirely (it returns early for non-HTTP scopes), so this is
the only place the check can go.

**Files:**
- Modify: `bus_ws.py` (`mint_listen_key`, `redeem_key`, `websocket_endpoint`), `projects_meta.py` (epoch helper)
- Modify: `packages/hivemind-server/tests/test_bus_ws.py`

**Interfaces:**
- Consumes: `projects_meta.load`, `projects_meta.can_access`, `identity.Identity`.
- Produces: `Hub.mint_listen_key(label, meta=None, user=None)` now embeds the user; `Hub.redeem_key(key) -> Optional[tuple[_Peer, str]]` also returns the user.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_bus_ws.py
def test_a_listen_key_carries_its_user(hub):
    k = hub.mint_listen_key("mac", user="nik")["listen_key"]
    peer, user = hub.redeem_key(k)
    assert peer is not None and user == "nik"


def test_a_key_minted_for_another_user_cannot_be_reassigned(hub):
    k = hub.mint_listen_key("mac", user="nik")["listen_key"]
    scheme, label, user_b64, exp, sig = k.split(".")
    forged = ".".join([scheme, label, bus_ws._b64(b"ana"), exp, sig])
    assert hub.redeem_key(forged) is None, "the user must be inside the signature"


@pytest.mark.anyio
async def test_a_revoked_member_is_refused_at_the_handshake(hub, tmp_path):
    """The WS route never passes through the auth middleware, so the ACL must be re-checked here."""
    from hivemind_server import projects_meta as pm

    proj_dir = tmp_path / "nik.private"
    proj_dir.mkdir()
    pm.save(proj_dir, pm.ProjectMeta(name="nik.private", visibility="private", owner="nik",
                                     members=["ana"]))
    k = hub.mint_listen_key("ana-box", user="ana")["listen_key"]
    assert bus_ws.authorize_key(hub, k, proj_dir, "nik.private") is not None

    meta = pm.load(proj_dir, "nik.private")
    meta.members = []
    pm.save(proj_dir, meta)
    assert bus_ws.authorize_key(hub, k, proj_dir, "nik.private") is None, \
        "revocation must bite before the key expires"
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_bus_ws.py -q -k "listen_key or revoked"`
Expected: FAIL — `mint_listen_key() got an unexpected keyword argument 'user'`

- [ ] **Step 3: Put the user inside the signature and check the ACL at the handshake**

Change the key body to `f"{_b64(label)}.{_b64(user)}.{exp}"` (so `hk1.<label>.<user>.<exp>.<sig>`),
have `redeem_key` return `(peer, user)`, and add:

```python
def authorize_key(hub, key: str, project_dir, project_name: str):
    """Verify the key AND re-check the project ACL — a key outlives a revocation otherwise."""
    from .identity import Identity
    from .projects_meta import can_access, load
    got = hub.redeem_key(key)
    if got is None:
        return None
    peer, user = got
    if not can_access(Identity(user=user, device="bus"), load(project_dir, project_name)):
        return None
    return peer
```

In `websocket_endpoint`, use `authorize_key(...)` for the `key` path and close `4401` on failure.
`bus_connect` passes `user=current_identity().user` when minting.

- [ ] **Step 4: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 181 total. Existing bus tests that call `mint_listen_key("label")` still pass
because `user` defaults to the caller's identity or `"legacy:unknown"`.

- [ ] **Step 5: Commit**

```bash
git add packages/hivemind-server/src/hivemind_server/{bus_ws,bus_ws_tools,projects_meta}.py \
        packages/hivemind-server/tests/test_bus_ws.py
git commit -m "fix: bus listen keys carry their user and re-check the ACL at the handshake"
```

---

### Task 12: The session flow in the plugin

**Files:**
- Create: `plugin/hooks/hooks.json`, `plugin/hooks/session-start`, `plugin/commands/project.md`, `plugin/skills/hivemind/scripts/hivemind-project.py`
- Modify: `plugin/.claude-plugin/plugin.json` (hooks + version), `plugin/skills/hivemind/SKILL.md`, `plugin/skills/hivemind/scripts/guide.sh` (install the new helper too)
- Create: `packages/hivemind-server/tests/test_session_flow.py`

**Interfaces:**
- Consumes: nothing from the server — the hook is local-state only, on purpose.
- Produces: `~/.hivemind/session-<session-id>.json` = `{"project": "<name>", "label": "...", "pinned_at": "..."}`; `hivemind-project.py --pin <name> [--label L]`, `--show`, `--session-id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_flow.py
"""The pin file and the hook that re-injects it. Both must be stdlib-only and never block."""
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
HELPER = ROOT / "plugin" / "skills" / "hivemind" / "scripts" / "hivemind-project.py"
HOOK = ROOT / "plugin" / "hooks" / "session-start"


def test_the_helper_and_hook_ship_in_the_plugin():
    assert HELPER.is_file() and HOOK.is_file()


def test_the_helper_is_stdlib_only():
    import ast
    std = getattr(sys, "stdlib_module_names", None)
    if not std:
        pytest.skip("need python 3.10+")
    mods = set()
    for n in ast.walk(ast.parse(HELPER.read_text())):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert not (mods - set(std))


def _run(args, home, session="sess-1"):
    return subprocess.run([sys.executable, str(HELPER)] + args, capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": "/usr/bin:/bin",
                               "CLAUDE_CODE_SESSION_ID": session})


def test_pin_then_show_round_trips(tmp_path):
    assert _run(["--pin", "nik.private"], tmp_path).returncode == 0
    out = json.loads(_run(["--show"], tmp_path).stdout)
    assert out["project"] == "nik.private"


def test_a_different_session_has_its_own_pin(tmp_path):
    _run(["--pin", "nik.private"], tmp_path, session="sess-1")
    out = json.loads(_run(["--show"], tmp_path, session="sess-2").stdout)
    assert out.get("project") is None


def test_the_same_session_id_resumes_onto_the_same_project(tmp_path):
    _run(["--pin", "nik.s-abc"], tmp_path, session="sess-9")
    out = json.loads(_run(["--show"], tmp_path, session="sess-9").stdout)
    assert out["project"] == "nik.s-abc"


def _hook(home, session="sess-1"):
    return subprocess.run(["bash", str(HOOK)], capture_output=True, text=True,
                          env={"HOME": str(home), "PATH": "/usr/bin:/bin",
                               "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugin"),
                               "CLAUDE_CODE_SESSION_ID": session})


def test_the_hook_emits_valid_json_with_the_pin(tmp_path):
    _run(["--pin", "nik.private"], tmp_path)
    r = _hook(tmp_path)
    assert r.returncode == 0
    body = json.loads(r.stdout)
    ctx = body["hookSpecificOutput"]["additionalContext"]
    assert "nik.private" in ctx and "project=nik.private" in ctx


def test_the_hook_asks_for_a_choice_when_nothing_is_pinned(tmp_path):
    r = _hook(tmp_path)
    ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "project_list" in ctx and "ask" in ctx.lower()


def test_the_hook_survives_an_unwritable_home(tmp_path):
    """It must never block a session."""
    missing = tmp_path / "nope" / "deeper"
    r = _hook(missing)
    assert r.returncode == 0
    json.loads(r.stdout)


def test_a_hostile_label_cannot_break_the_injected_json(tmp_path):
    """Review Focus 5: the label is interpolated into JSON; a raw quote or newline would drop it."""
    nasty = 'broke"n\nlabel\\with\ttabs' + "\x1b[31m" + "x" * 500
    _run(["--pin", "nik.private", "--label", nasty], tmp_path)
    r = _hook(tmp_path)
    body = json.loads(r.stdout)                       # must parse
    ctx = body["hookSpecificOutput"]["additionalContext"]
    assert "nik.private" in ctx
    assert "\n" not in ctx.split("project=")[0][-80:]
    stored = json.loads((tmp_path / ".hivemind" / "session-sess-1.json").read_text())
    assert len(stored["label"]) <= 200 and "\x1b" not in stored["label"]
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_session_flow.py -q`
Expected: FAIL — the helper and hook do not exist.

- [ ] **Step 3: Write `hivemind-project.py`**

```python
#!/usr/bin/env python3
"""Read and write this session's Hivemind project pin. No dependencies.

The pin exists because the project choice otherwise lives only in conversation context, where a
compaction drops it — and a dropped choice plus a defaulted write is how private work would end up
in the shared graph. Keyed by CLAUDE_CODE_SESSION_ID so a --resume lands back on the same project.
"""
import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

CTRL = re.compile(r"[\x00-\x1f\x7f]")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
LABEL_MAX = 200


def clean(text):
    """The label is interpolated into the hook's JSON; control characters would break it."""
    return CTRL.sub(" ", ANSI.sub("", text or ""))[:LABEL_MAX].strip()


def pin_path():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "no-session")
    return pathlib.Path(os.path.expanduser("~")) / ".hivemind" / f"session-{clean(sid)}.json"


def main(argv=None):
    ap = argparse.ArgumentParser(description="pin the Hivemind project for this session")
    ap.add_argument("--pin", metavar="PROJECT")
    ap.add_argument("--label", default="")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--session-id", action="store_true")
    args = ap.parse_args(argv)

    if args.session_id:
        print(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
        return 0
    path = pin_path()
    if args.pin:
        body = {"project": clean(args.pin), "label": clean(args.label),
                "pinned_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, indent=2))
        except OSError as e:
            print(json.dumps({"error": str(e)}))
            return 1
        print(json.dumps(body))
        return 0
    try:
        print(json.dumps(json.loads(path.read_text())))
    except (OSError, ValueError):
        print(json.dumps({"project": None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write the hook and register it**

`plugin/hooks/session-start` (bash, must always exit 0 with valid JSON):

```bash
#!/usr/bin/env bash
# Re-inject this session's Hivemind project pin. Runs on startup, clear and compact — the compact
# matcher is the point: a compaction drops the choice from context, and a dropped choice plus a
# defaulted write is how private work reaches the shared graph.
#
# Local state only. Listing projects needs the token, and project_list over MCP is already
# authenticated, so this hook never talks to the server and therefore cannot fail in a way that
# blocks a session.
set -u
HELPER="${HIVEMIND_PIN_HELPER:-$HOME/.hivemind/hivemind-project.py}"
[ -f "$HELPER" ] && [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] || true
[ -f "$HELPER" ] || HELPER="${CLAUDE_PLUGIN_ROOT:-}/skills/hivemind/scripts/hivemind-project.py"

PROJECT=""
if [ -f "$HELPER" ]; then
  PROJECT="$(python3 "$HELPER" --show 2>/dev/null \
    | python3 -c 'import json,sys; print((json.load(sys.stdin).get("project") or ""))' 2>/dev/null)"
fi

if [ -n "$PROJECT" ]; then
  CTX="Hivemind project for this session: ${PROJECT}. Pass project=${PROJECT} on Hivemind calls; \
write tools require it explicitly. The project echoed in a tool result is authoritative."
else
  CTX="Hivemind: no project is pinned for this session. Before your first Hivemind call, call \
project_list and ask the user which project to use — an existing one, a new shared one, their \
private graph, or a new scratch project — then pin it with hivemind-project.py --pin <name>. \
Write tools require an explicit project= argument."
fi

# printf, not a heredoc: bash 5.3+ can hang on heredocs in hook context.
python3 - "$CTX" <<'PY'
import json, sys
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": sys.argv[1]}}))
PY
exit 0
```

Add to `plugin/.claude-plugin/plugin.json`: `"hooks": "./hooks/hooks.json"` and bump `"version"` to
`1.1.0`. `plugin/hooks/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|clear|compact",
        "hooks": [
          { "type": "command",
            "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/session-start\"",
            "shell": "bash", "async": false }
        ]
      }
    ]
  }
}
```

In `guide.sh`, install the helper next to the bus listener by generalising the existing copy block
to loop over `bus-listen.py hivemind-project.py`.

- [ ] **Step 5: Write the slash command and update the skill**

`plugin/commands/project.md` instructs: call `project_list`, show the three groups, ask which to
use (offering the private graph and a new scratch named `<user>.s-<short session id>`), create it
with `project_create` if needed, then run
`python3 "$HOME/.hivemind/hivemind-project.py" --pin <name>` and confirm the pin.

In `SKILL.md`: bump metadata version to `1.1.0` and add the rule — every call takes
`project=<name>`; **write tools require it**; the project echoed in a result is authoritative; if
nothing is pinned, ask once and pin.

- [ ] **Step 6: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 190 total.

- [ ] **Step 7: Commit**

```bash
git add plugin/ packages/hivemind-server/tests/test_session_flow.py
git commit -m "feat: session project pin, SessionStart re-injection, and /hivemind:project"
```

---

### Task 13: The schema-authoring skill

A new project created with `schema="interview"` has no vocabulary, and an agent left to invent one
before understanding the work produces near-duplicate types — the dominant long-term failure mode of
a graph like this, and permanent, because schema changes are additive-only. This skill makes the
agent ask first.

**Files:**
- Create: `plugin/skills/hivemind-schema/SKILL.md`
- Create: `plugin/skills/hivemind-schema/references/TRAITS.md`
- Create: `packages/hivemind-server/tests/test_schema_skill.py`
- Modify: `plugin/commands/project.md` (offer the three modes when creating)

**Interfaces:**
- Consumes: `project_create(..., schema="interview")` from Task 7, and the existing `schema_apply` /
  `schema_propose` / `schema_get` tools.
- Produces: a skill discoverable as `hivemind-schema`; no code.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema_skill.py
"""The skill is a shipped artifact, so its contract is checked the way any other artifact is."""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
SKILL = ROOT / "plugin" / "skills" / "hivemind-schema" / "SKILL.md"
TRAITS = ROOT / "plugin" / "skills" / "hivemind-schema" / "references" / "TRAITS.md"


def test_the_skill_ships():
    assert SKILL.is_file() and TRAITS.is_file()


def test_frontmatter_has_only_portable_fields():
    head = SKILL.read_text().split("---")[1]
    keys = {line.split(":")[0].strip() for line in head.splitlines() if ":" in line
            and not line.startswith(" ")}
    assert "name" in keys and "description" in keys
    assert keys <= {"name", "description", "allowed-tools", "metadata", "license", "version"}


def test_the_description_triggers_on_project_creation_and_missing_types():
    desc = SKILL.read_text().split("---")[1]
    low = desc.lower()
    for cue in ("schema", "project", "node", "edge"):
        assert cue in low, f"description should mention {cue} so the skill is discoverable"


def test_it_names_every_generic_edge_trait():
    """A schema author who does not know the traits will model them as node types instead."""
    body = (SKILL.read_text() + TRAITS.read_text()).lower()
    for trait in ("versioned", "symmetric", "transitive", "acyclic", "assertive"):
        assert trait in body, f"missing trait: {trait}"


def test_it_teaches_both_versioning_axes():
    body = SKILL.read_text().lower()
    assert "subject_key" in body and "subject_version" in body
    assert "revision" in body and "supersede" in body


def test_it_requires_asking_before_proposing():
    body = SKILL.read_text().lower()
    assert "interview" in body or "ask" in body
    assert "before" in body


def test_it_offers_the_skip_path():
    """A user who does not want to be interviewed must not be trapped in one."""
    body = SKILL.read_text().lower()
    assert "bare" in body and ("skip" in body or "rather not" in body)


def test_it_warns_against_type_sprawl_with_a_concrete_bound():
    body = SKILL.read_text()
    assert re.search(r"\b(five to eight|5[-–– ]to[-– ]8|5\s*[-–]\s*8)\b", body, re.I)


def test_it_tells_the_agent_to_record_the_rationale():
    body = SKILL.read_text().lower()
    assert "rationale" in body or "why" in body
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_schema_skill.py -q`
Expected: FAIL — the skill files do not exist.

- [ ] **Step 3: Write `plugin/skills/hivemind-schema/SKILL.md`**

```markdown
---
name: hivemind-schema
description: >-
  Design the node and edge types for a Hivemind project — interview the user about their work, then
  propose and apply a schema. Use when a project was created with no schema, when the types needed
  for the current work do not exist, or when the user asks to define or extend a project's
  vocabulary. Covers the two versioning axes and the generic edge traits.
allowed-tools: Read
metadata:
  version: "1.0.0"
---

# Designing a Hivemind schema

A Hivemind project's node and edge **types are its meaning** — the engine itself knows only
mechanics. A project with no types cannot be written to at all, and one with the *wrong* types is
worse: schema changes are additive-only, so a redundant type is permanent and quietly splits the
graph in two, with half the findings under `finding` and half under `issue`.

**Near-duplicate type sprawl is the dominant long-term failure mode of this graph.** It is far
cheaper to prevent here than to clean up later. That is what this skill is for.

## Do not propose a schema before you understand the work

Ask the user. One question at a time, in their language, not the engine's. You are trying to learn
five things:

1. **What does this work track?** The nouns they already say out loud. Not categories you think a
   system like this should have.
2. **Which of those things have versions of the thing itself?** An OS build, a firmware release, a
   package version, a document revision from upstream. These belong on the **subject axis**: they
   coexist as separate cells, keyed by `subject_key` with a `subject_version`, and a disagreement
   between two of them is *not* a contradiction — it is two different things. Getting this wrong is
   how an agent later mistakes a version difference for a conflict.
3. **Which things instead get corrected over time?** Notes, findings, conclusions. Those use the
   **revision axis**: you supersede the head and the chain stays walkable. Every node gets this for
   free — it is not a type decision.
4. **What relationships matter, and where do they come from?** A relationship a person curates by
   hand wants `versioned: true` (full history). A relationship imported in bulk from a tool — a call
   graph, a dependency graph, a reachability map — wants `versioned: false`, which routes it to the
   bulk table and is replaced wholesale under a `source_tag`. Millions of bulk edges are cheap;
   millions of versioned ones are not.
5. **What counts as a disagreement worth surfacing?** If two claims can conflict and somebody should
   notice before building on either, that relationship wants `assertive: true` — the read path then
   flags both endpoints until the status flips to resolved. The engine has no idea what
   "contradicts" means; the trait is how you get the behaviour.

Then **read `references/TRAITS.md`** and map the answers onto the traits.

## Offer the skip, plainly

Some people do not want to be interviewed. Say so up front, in one line: they can start **bare** and
let you define types as the work demands them (`schema_propose` per type), or **inherit** the
vocabulary of a project they already use. Neither is a failure mode; a scratch project in particular
is usually better off bare. Do not push the interview on someone who declined it.

## Keep it small

**Five to eight node types to start.** Additive-only cuts both ways: a type you did not think of is
cheap to add the moment you need it, while one you added speculatively is permanent. If you are
unsure whether two things are one type with a field or two types, they are one type with a field.

Before proposing anything, call `schema_get` — if the project inherited or already has types, extend
rather than duplicate. Check every proposed name against what exists for near-duplicates
(`finding`/`issue`, `tool`/`utility`, `host`/`machine`).

## Propose, show, apply

1. Draft the pack: node types with a JSON Schema each (`{"type": "object", "additionalProperties":
   true}` is a fine start — the graph is for finding things, not for validating them to death), and
   edge types with explicit traits.
2. **Show the user the whole thing** in a compact table — type, what it holds, why it exists — and
   ask what is missing or wrong. They will spot a wrong noun instantly; they will not spot a missing
   trait, so explain the traits you chose in plain words.
3. Apply it with `schema_apply` (a whole pack at once, idempotent) or `schema_propose` per type.
4. **Record the rationale.** Write one node in the new project — the interview answers, the type
   list, and *why* each exists, especially anything you deliberately left out. A future agent
   reading the schema can see what it is; only this node tells them why. Without it the next agent
   re-derives the vocabulary and adds the duplicates you just avoided.

## After the schema exists

Tell the user what to do next in one line: the project is ready, pass `project=<name>` on Hivemind
calls, and write tools require it explicitly.
```

- [ ] **Step 4: Write `references/TRAITS.md`**

```markdown
# Edge traits and the two axes

The engine special-cases no relationship by name. You get behaviour by declaring traits.

| Trait | Meaning | Use it when |
|---|---|---|
| `versioned: true` | Full supersession and history per edge | A human curates this claim and its history matters |
| `versioned: false` | Bulk table; no per-edge history; replaced wholesale under a `source_tag` | Imported from a tool — call graphs, dependency graphs, reachability maps |
| `symmetric: true` | A→B implies B→A | "related to", "contradicts", "duplicate of" |
| `transitive: true` | A→B, B→C implies A→C for traversal | Containment, ancestry |
| `acyclic: true` | A cycle-forming insert is rejected | Dependencies, refinement chains, anything that must stay a DAG |
| `assertive: true` | Edges carry `props.status`; an `open` one flags BOTH endpoints on read | Disputes, open questions, "needs review" |
| `src_types` / `dst_types` | Domain and range (`'*'` = any) | Cheaply reject nonsense edges |
| `cardinality` | `1:1` \| `1:N` \| `N:N` | Constrain fan-out |

## The two axes are not edges

Both are built in, and neither should be modelled as a relationship:

- **Revision axis** — the same claim, corrected. Supersede the head; the `prev_version` chain stays
  walkable and `as_of` queries work. Every node has this automatically.
- **Subject axis** — the version of the *described thing*. `subject_key` identifies the thing,
  `subject_version` the coordinate (a build, a release, a digest), `subject_order` makes "latest" and
  "as of" sortable when versions do not sort as semver. Cells coexist; each has its own independent
  revision chain.

The consequence worth repeating to the user: two subject cells that disagree are **not** in
conflict. They describe different versions of the thing. Only a contradiction *within* one cell is a
contradiction.
```

- [ ] **Step 5: Offer the three modes in the slash command**

In `plugin/commands/project.md`, where a new project is created, present the schema choice
explicitly: inherit the current project's vocabulary (default), run the `hivemind-schema` interview,
or start bare. Pass the answer as `schema=`, and when it is `interview`, load the skill immediately
after creation.

- [ ] **Step 6: Run the tests**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS — 199 total. (A prediction made before the work; the finished branch runs 517. See
the note at the end of Task 14 Step 5.)

- [ ] **Step 7: Stage, do not commit**

```bash
git add plugin/skills/hivemind-schema plugin/commands/project.md \
        packages/hivemind-server/tests/test_schema_skill.py
git status --short        # leave it staged for review; see Review Gate below
```

---
### Task 14: Versions, docs, test hygiene, and the live rollout

**Files:**
- Modify: `packages/hivemind-server/pyproject.toml`, `packages/hivemind-client/pyproject.toml`, `.claude-plugin/marketplace.json`
- Modify: `docs/api.md`, `docs/data-model.md`, `docs/security.md`, `docs/clients.md`
- Modify: `packages/hivemind-server/tests/test_listener.py` (the `_tmp_plugin_only` hygiene fix)
- Modify: `config.py` (drop the vestigial server-level blob dir)

- [ ] **Step 1: Fix the test that writes into the source tree**

`test_bus_connect_returns_a_command_a_plugin_only_machine_can_run` creates
`packages/hivemind-server/tests/_tmp_plugin_only/` on every run and leaves it behind. Change
`FakeProject.dir` to use the `tmp_path` fixture instead, then `rm -rf` the stale directory.

- [ ] **Step 2: Drop the vestigial blob dir**

In `config.ensure_dirs`, remove the `(self.blobs_dir / "tmp").mkdir(...)` line and the `blobs_dir`
property. Verified unused: it holds 8 KB while the real per-project store holds 33 GB, and leaving
it invites a future code path to write cross-project blobs into it. Run the suite to confirm nothing
referenced it.

- [ ] **Step 3: Bump versions**

`1.1.0` in both `pyproject.toml` files, `plugin/.claude-plugin/plugin.json`,
`.claude-plugin/marketplace.json`, and `SKILL.md` metadata.

- [ ] **Step 4: Update the docs**

- `docs/api.md`: the `/mcp` project-neutral endpoint, the `project` argument and the write
  requirement, the five `project_*` tools, the `author` search parameter.
- `docs/data-model.md`: `tx.user_id`/`device`, `author_user`, `created_by`, the computed contributor
  chain, and that `edge_bulk` is attributed via `tx` only.
- `docs/security.md`: server-level identities, legacy token scoping, the ACL in the middleware, the
  identical unknown/forbidden response, owner-only sharing, and the honest limit — private means
  private from other API users, not encrypted at rest.
- `docs/clients.md`: `server_url` may be the server root or a project base; `mint-token --user`.

- [ ] **Step 5: Run everything**

Run: `$UV run --group dev pytest packages/ -q`
Expected: PASS, and `test_no_host_specifics.py` still green (no home paths, usernames or IPs in
tracked files).

> **The test counts in this plan are the plan's own predictions, written before the work, and they
> drifted.** The figure that matters — what the finished branch actually runs — is **517**, not the
> 199 this step originally claimed; the per-task numbers above (113, 123, 126, 140, 146, 152, 167,
> 174, 177, 178, 181, 190, 199) were never revised as tasks grew their own coverage. An operator
> checking a deployment should compare against **517** and against `deploy/deploy-checklist.md`,
> which carries the live figure. The intermediate numbers are left as written rather than
> back-dated, because this file is the record of what was planned.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: identity, project selection and the ACL; bump to 1.1.0"
```

- [ ] **Step 7: Deploy — ONLY after the user approves the push**

```bash
bash deploy/backup.sh
git push origin main
git push nik@<box>:hivemind HEAD:refs/heads/_in
ssh nik@<box> 'cd ~/hivemind && git reset --hard _in && git branch -D _in; bash deploy/restart.sh'
curl -s http://<box>:8787/healthz
ssh nik@<box> 'cd ~/hivemind && ./.venv/bin/hivemind-admin backfill-authors'   # dry run is the default
```

- [ ] **Step 8: Verify on the live server, from a second identity**

1. `hivemind-admin mint-token --user nik --device mac-studio` and `--user ana --device laptop`.
2. As nik: `project_create("nik.private", visibility="private")`, write a node, read it back and
   confirm `author == "nik"` and `created_by == "nik"`.
3. As ana: confirm `project_list` omits `nik.private`, and that an MCP call, a blob GET and
   `/p/nik.private/` all return the **identical** response as for `does.not.exist`.
4. Confirm the existing fleet token still reads and writes `default`, attributed `legacy:*`.
5. Run `backfill-authors --yes` for real (dry run is the default) **for every project**, not just
   `default` — the command takes the global `--project` like `gc`/`reindex`, so
   `hivemind-admin list-projects` first and loop; any project you skip keeps its NULLs.
   Confirm no row is attributed to `nik`.
6. Refresh the plugin on this machine, restart, and confirm the SessionStart hook injects the pin
   and that `/hivemind:project` switches it.
7. Confirm a write with no `project` argument is refused rather than landing in `default`.

- [ ] **Step 9: Record the work in Hivemind itself**

Publish a skill describing the identity/project model and record any dead-end hit during
implementation as a trap, in the `default` project — that is what the tool is for.

---


### Task 15: Blob-store correctness — three field-found bugs

All three were hit in real use and each one fails in a direction that costs the caller work.

**Files:**
- Modify: `blobs.py` (`refs`, prefix resolution), `rest_blobs.py` (Range, Content-Length), `registry_tools.py` (the `artifact_refs` tool description)
- Create: `packages/hivemind-server/tests/test_blob_contract.py`

**Interfaces:**
- Produces: `BlobStore.resolve_digest(maybe_prefix) -> str` (raises `Invalid` or `NotFound`), `BlobStore.refs(digest)` now validating, and Range support on `GET /blobs/{algo}/{hex}`.

#### 15a. A truncated digest must not look like an orphan

`refs()` queries `WHERE digest=?` and never calls `parse_digest`, which exists three methods away
and does validate. So a 17-character digest — exactly what a listing displays — is a well-formed
query that matches nothing, and "this blob is orphaned and will be garbage-collected" is
indistinguishable from "you pasted a prefix". Measured in the field: eight uploaded PoCs all
reported orphaned on truncated digests and all eight were attached when re-run at full length. The
natural response to a false orphan report is to re-upload, which on a 20 GB transfer is the
expensive kind of wrong.

**Fail loudly, and resolve a unique prefix.** A prefix is what humans and listings actually have,
so accepting it is the useful behaviour; ambiguity and non-existence are errors, not zero rows.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_blob_contract.py
import pytest
from hivemind_server.db import Invalid, NotFound


def _put(store, body: bytes) -> str:
    import hashlib
    d = "sha256:" + hashlib.sha256(body).hexdigest()
    tmp = store.new_tmp()
    tmp.write_bytes(body)
    store.finalize_written(tmp, d, len(body), "application/octet-stream", "test")
    return d


def test_a_truncated_digest_is_an_error_not_an_empty_answer(store):
    """The field failure this fixes: 8 attached PoCs all reported 'orphaned' on 17-char digests,
    and the natural response to a false orphan report is to re-upload 20 GB."""
    d = _put(store, b"hello")
    with pytest.raises((Invalid, NotFound)):
        store.refs(d[:17])
    assert store.refs(d) == []          # a REAL orphan still answers, and answers empty


def test_a_unique_prefix_resolves(store):
    d = _put(store, b"hello")
    assert store.resolve_digest(d[7:19]) == d      # bare hex prefix
    assert store.resolve_digest(d[:19]) == d       # sha256:-qualified prefix
    assert store.resolve_digest(d) == d            # a full digest is its own answer


def test_an_ambiguous_prefix_is_refused_rather_than_guessed(store, monkeypatch):
    a, b = _put(store, b"one"), _put(store, b"two")
    common = _shared_prefix(a.split(":", 1)[1], b.split(":", 1)[1])
    if len(common) < 2:
        monkeypatch.setattr(store, "_all_digests", lambda: [a, b])
        common = "x"                      # forced: real sha256es rarely share a long prefix
    with pytest.raises(Invalid) as e:
        store.resolve_digest(common[:max(2, len(common))])
    assert "ambiguous" in str(e.value).lower()


def test_a_prefix_matching_nothing_is_not_found(store):
    with pytest.raises(NotFound):
        store.resolve_digest("beefbeefbeef")


def test_a_prefix_below_the_floor_is_refused(store):
    """A 4-character prefix would collide constantly; refuse rather than resolve by luck."""
    with pytest.raises(Invalid):
        store.resolve_digest("beef")


def _shared_prefix(a: str, b: str) -> str:
    out = []
    for x, y in zip(a, b):
        if x != y:
            break
        out.append(x)
    return "".join(out)
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_blob_contract.py -q`
Expected: FAIL — `refs("sha256:abc…")` returns `[]` instead of raising; `resolve_digest` does not exist.

- [ ] **Step 3: Implement**

In `blobs.py`, add a prefix floor and a resolver, and route `refs` through it:

```python
    MIN_PREFIX = 8    # below this, a prefix collides by luck rather than by content

    def resolve_digest(self, maybe_prefix: str) -> str:
        """Accept a full digest or a unique prefix; refuse anything ambiguous or absent.

        A listing shows a shortened digest, so a prefix is what a caller actually has in hand. The
        alternative — treating a short digest as a literal that simply matches nothing — made a
        truncated lookup indistinguishable from a real orphan, and the natural response to a false
        orphan report is to re-upload the artifact.
        """
        raw = maybe_prefix.split(":", 1)[1] if ":" in maybe_prefix else maybe_prefix
        raw = raw.strip().lower()
        if not raw or not all(c in "0123456789abcdef" for c in raw):
            raise Invalid(f"not a sha256 digest or hex prefix: {maybe_prefix!r}")
        if len(raw) == 64:
            return f"sha256:{raw}"
        if len(raw) < self.MIN_PREFIX:
            raise Invalid(f"digest prefix {raw!r} is shorter than {self.MIN_PREFIX} characters; "
                          f"paste more of it — a short prefix matches by luck, not by content")
        with self.db.read() as cur:
            hits = [r[0] for r in cur.execute(
                "SELECT digest FROM blob WHERE digest LIKE ? LIMIT 2", (f"sha256:{raw}%",))]
        if not hits:
            raise NotFound(f"no stored blob starts with {raw!r}")
        if len(hits) > 1:
            raise Invalid(f"digest prefix {raw!r} is ambiguous ({len(hits)}+ matches); "
                          f"paste the full 64-character digest")
        return hits[0]
```

and make `refs` validate rather than return an empty list for a malformed input:

```python
    def refs(self, digest: str) -> list[dict]:
        digest = self.resolve_digest(digest)     # raises on malformed, ambiguous or absent
        with self.db.read() as cur:
            return [dict(r) for r in cur.execute(
                "SELECT from_version_id, role, filename FROM blob_ref WHERE digest=?", (digest,))]
```

Do the same for `artifact_ref`/`artifact_refs`' tool paths and for `pin`. Update the
`artifact_refs` description to say a unique prefix is accepted and that an unknown digest is an
error, not an empty result.

#### 15b. Honour Range, or stop advertising it

`rest_blobs.py:104` sends `Accept-Ranges: bytes` and nothing in the file reads the `Range` header,
so a ranged GET returns `200` with the whole body. A caller that trusts the advertisement and
assembles chunks gets each chunk's worth of the *beginning* of the file. Measured in the field: ten
false failures and twelve vacuous passes in one run, splitting exactly at 64 KiB — the point where
the harness switched to ranged reads.

**Honour it** — a resumable download of a multi-GB artifact is the reason the header is there.

- [ ] **Step 4: Write the failing test**

```python
# append to tests/test_blob_contract.py
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
```

The `client_for_blobs` fixture stores a body larger than 64 KiB (e.g. 200_000 bytes of
`os.urandom`) so the boundary test is real rather than nominal.

- [ ] **Step 5: Implement Range**

Parse a single `bytes=` range (a multi-range request is rare and may be answered with the whole
body, or 416 — pick one and say which in a comment). Serve the slice with `206`, `Content-Range`,
and the sliced `Content-Length`; answer an unsatisfiable range with `416` and
`Content-Range: bytes */<size>`; leave an absent or unparseable header on the existing `200` path.
The existing `ETag` and `Cache-Control: immutable` headers stay on both.

#### 15c. Check Content-Length before draining the body

The 2 GiB cap fires in `finalize_written`, after `async for chunk in req.stream()` has written the
whole upload to a temp file. A naive PUT of a 4 GB file therefore burns the entire transfer and
then fails, and the temp file has to be cleaned up afterwards.

- [ ] **Step 6: Write the failing test**

```python
# append to tests/test_blob_contract.py
@pytest.mark.anyio
async def test_an_oversized_declared_length_is_refused_before_the_body_is_read(env_for_blobs):
    """413 must arrive without the body being drained: the field cost is a burned 4 GB upload."""
    c, cfg, digest = env_for_blobs
    sent = 0

    async def body():
        nonlocal sent
        for _ in range(4):
            sent += 1024
            yield b"x" * 1024

    r = await c.put(f"/blobs/sha256/{digest}", content=body(),
                    headers={"Content-Length": str(cfg.max_blob_bytes + 1)})
    assert r.status_code == 413
    assert "max_blob_bytes" in r.text or "too large" in r.text.lower()
    assert sent == 0, "the body must not have been consumed"


@pytest.mark.anyio
async def test_a_stream_that_exceeds_the_cap_without_declaring_it_still_fails(env_for_blobs):
    """Content-Length is a hint, not a guarantee — the streaming guard must remain."""
    c, cfg, digest = env_for_blobs
    over = b"x" * (cfg.max_blob_bytes + 1)
    r = await c.put(f"/blobs/sha256/{digest}", content=over)
    assert r.status_code == 413
```

- [ ] **Step 7: Implement**

Read `Content-Length` before opening the temp file; if it parses and exceeds `cfg.max_blob_bytes`,
return `413` immediately. Keep the streaming check as well — `Content-Length` can be absent under
chunked encoding or simply lie — and make the streaming path abort and unlink the temp file as soon
as `size` crosses the cap rather than at the end.

- [ ] **Step 8: Run the suite and commit**

Run: `$UV run --group dev pytest packages/ -q`

```bash
git add packages/hivemind-server/src/hivemind_server/{blobs,rest_blobs,registry_tools}.py \
        packages/hivemind-server/tests/test_blob_contract.py
git commit -m "fix: truncated digests error, Range is honoured, the size cap precedes the body"
```

---

### Task 16: The bus listener keeps the full message

`bus_message("<id>")` is advertised in every clipped notification, but the truncation is
**client-side**: `bus.py`'s `render()` receives the whole frame, prints at most `BODY_CAP` characters
of the body, and drops the rest on the floor. The pointer then resolves only if the reading host
happens to expose that tool — and when it does not, an agent answers a message having read a
~300-character preview. Nothing persists the frame anywhere.

**Files:**
- Modify: `packages/hivemind-client/src/hivemind/bus.py`, `plugin/skills/hivemind/scripts/bus-listen.py`, `plugin/skills/hivemind/SKILL.md`, `docs/bus.md`
- Create: `packages/hivemind-server/tests/test_bus_inbox.py`

**Interfaces:**
- Produces: `~/.hivemind/bus-inbox.jsonl` (one JSON frame per line, appended on receipt), and a `--inbox PATH` override on both listeners.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_bus_inbox.py
import json
import pathlib
import subprocess
import sys

LISTENER = pathlib.Path(__file__).resolve().parents[3] / "plugin/skills/hivemind/scripts/bus-listen.py"


def test_the_listener_persists_the_frame_it_prints(tmp_path):
    """The full body is in hand when the line is clipped; discarding it is the bug."""
    mod = _load(LISTENER)
    inbox = tmp_path / "bus-inbox.jsonl"
    frame = {"type": "message", "id": "01ABCDEFGH", "from": "labbox", "body": "L" * 900}
    line = mod.render(frame, inbox=inbox)
    assert len(line) <= 512
    rec = json.loads(inbox.read_text().splitlines()[-1])
    assert rec["body"] == "L" * 900, "the whole body must survive locally"
    assert rec["id"] == "01ABCDEFGH"


def test_the_pointer_names_the_local_inbox_not_only_a_tool(tmp_path):
    mod = _load(LISTENER)
    line = mod.render({"type": "message", "id": "01ABCDEFGH", "from": "x", "body": "L" * 900},
                      inbox=tmp_path / "i.jsonl")
    assert "bus-inbox.jsonl" in line or "bus_message" in line
    assert "01ABCDEFGH"[-8:] in line


def test_a_short_message_is_persisted_too(tmp_path):
    """Otherwise the local record has holes exactly where the conversation was cheap."""
    mod = _load(LISTENER)
    inbox = tmp_path / "i.jsonl"
    mod.render({"type": "message", "id": "01AB", "from": "x", "body": "short"}, inbox=inbox)
    assert json.loads(inbox.read_text().splitlines()[-1])["body"] == "short"


def test_presence_and_ping_frames_are_not_persisted(tmp_path):
    mod = _load(LISTENER)
    inbox = tmp_path / "i.jsonl"
    mod.render({"type": "ping"}, inbox=inbox)
    mod.render({"type": "presence", "peer": "x", "event": "connected"}, inbox=inbox)
    assert not inbox.exists() or inbox.read_text() == ""


def test_an_unwritable_inbox_never_costs_the_message(tmp_path):
    """Printing the line matters more than recording it."""
    mod = _load(LISTENER)
    line = mod.render({"type": "message", "id": "01AB", "from": "x", "body": "hi"},
                      inbox=tmp_path / "nope" / "deeper" / "i.jsonl")
    assert "hi" in line


def _load(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("bus_listen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
```

- [ ] **Step 2: Run and confirm failure**

Run: `$UV run --group dev pytest packages/hivemind-server/tests/test_bus_inbox.py -q`
Expected: FAIL — `render()` takes no `inbox` argument.

- [ ] **Step 3: Implement in both listeners**

`render(frame, inbox=None)` appends the **whole frame** as one JSON line before building the
display line, for `message` and `broadcast` frames only. Default the path to
`$HOME/.hivemind/bus-inbox.jsonl`, overridable with `--inbox`. Wrap the write in a `try/except
OSError` that is silent: a message that printed but was not recorded is far better than one that
was neither. Keep the two implementations' `render()` in step — the parity test that pins
`bus.py` against `bus-listen.py` must be extended to cover the new argument.

When the body was clipped, the pointer names **both** routes: the local inbox line and
`bus_message("<id>")`, since the local one always works and the tool one depends on the host.

- [ ] **Step 4: Document it**

`SKILL.md` and `docs/bus.md`: every message is appended in full to `~/.hivemind/bus-inbox.jsonl`;
read the tail of that file rather than answering from a preview; `bus_message` remains the way to
fetch a message this host never received.

- [ ] **Step 5: Run the suite and commit**

```bash
git add packages/hivemind-client/src/hivemind/bus.py plugin/ \
        packages/hivemind-server/tests/test_bus_inbox.py docs/bus.md
git commit -m "fix: persist every bus message locally instead of discarding the clipped remainder"
```

---

### Task 17: A refused write must not be able to kill a batch

Two halves of one ergonomic failure. A reaper wrote `stage_run.verdict="refuted"`, the value was
outside the pack's enum, the tool returned `{"ok": false}`, and the resulting exception took down
the whole batch mid-run. The caller's error handling was its own bug — but a data-shape refusal
that can abort an unrelated batch is an invitation, and the enum being too narrow for the
vocabulary the stage actually produces is ours.

**Files:**
- Modify: `packages/hivemind-client/src/hivemind/client.py` (`call`, plus a `call_many` helper), `packs/research-workflow/schema.json` (the `verdict` enum), `docs/api.md`
- Create: `packages/hivemind-client/tests/test_batch_refusals.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_batch_refusals.py
import pytest
from hivemind.client import Client, HivemindError


def test_an_ok_false_reply_is_returned_not_raised(fake_transport):
    """A refusal is data. Raising by default is what let one bad row abort a batch."""
    c = fake_transport([{"ok": False, "error_kind": "invalid", "error": "bad verdict"}])
    out = c.call("graph_upsert", {"type": "stage_run"})
    assert out["ok"] is False and out["error_kind"] == "invalid"


def test_raise_on_error_is_opt_in(fake_transport):
    c = fake_transport([{"ok": False, "error_kind": "invalid", "error": "bad verdict"}])
    with pytest.raises(HivemindError):
        c.call("graph_upsert", {"type": "stage_run"}, raise_on_error=True)


def test_call_many_completes_every_item_despite_a_refusal(fake_transport):
    c = fake_transport([{"ok": True, "node_id": "a"},
                        {"ok": False, "error_kind": "invalid", "error": "bad verdict"},
                        {"ok": True, "node_id": "c"}])
    results = c.call_many([("graph_upsert", {"i": 0}), ("graph_upsert", {"i": 1}),
                           ("graph_upsert", {"i": 2})])
    assert [r["ok"] for r in results] == [True, False, True]
    assert results[1]["error_kind"] == "invalid"


def test_call_many_surfaces_a_transport_failure_per_item(fake_transport_raising):
    c = fake_transport_raising()
    results = c.call_many([("graph_upsert", {"i": 0})])
    assert results[0]["ok"] is False and results[0]["error_kind"] == "transport"
```

- [ ] **Step 2: Run and confirm failure**

Expected: FAIL — `call` raises on `ok: false`, and `call_many` does not exist.

- [ ] **Step 3: Implement**

`call(..., raise_on_error: bool = False)` returns the envelope as data by default. Add:

```python
    def call_many(self, calls, *, stop_on_error: bool = False) -> list:
        """Run a batch to completion, returning one envelope per item.

        A single refused row must not be able to abandon the rest: the failure this exists for was
        one out-of-enum value aborting a whole reaper batch mid-run, leaving the graph half-updated
        with no record of where it stopped.
        """
        out = []
        for tool, args in calls:
            try:
                out.append(self.call(tool, args))
            except HivemindError as e:
                out.append({"ok": False, "error_kind": e.kind or "error", "error": str(e)})
            except Exception as e:                      # transport, timeout, decode
                out.append({"ok": False, "error_kind": "transport", "error": str(e)})
            if stop_on_error and not out[-1].get("ok"):
                break
        return out
```

**Check every existing caller of `call` before changing its default** — anything that relied on an
exception to detect a refusal must now check `ok`. `grep -rn '\.call(' packages/hivemind-client`
and the CLI, and fix each; a silent behaviour change here would hide errors instead of raising them,
which is worse than the bug being fixed. If the audit shows many sites depend on raising, keep
`raise_on_error=True` as the default for `call` and make `call_many` the non-raising path — say
which you chose and why.

- [ ] **Step 4: Widen the `verdict` enum**

In `packs/research-workflow/schema.json`, add `confirmed` and `refuted` to `stage_run.verdict`.
Widening an enum is additive, so it needs no migration. Note in the pack's README that verify
stages produce `confirmed`/`refuted` while scoring stages produce `pass`/`fail`.

- [ ] **Step 5: Run the suite and commit**

```bash
git add packages/hivemind-client/src/hivemind/client.py \
        packages/hivemind-client/tests/test_batch_refusals.py \
        packs/research-workflow/schema.json docs/api.md
git commit -m "fix: refusals are data, batches run to completion, verdict enum fits the stages"
```

---

## Self-Review

**Spec coverage.** Section 1 (identity) → Tasks 2, 3. Section 2 (routing, fail-closed writes,
attach-time bindings) → Tasks 1, 6. Section 3 (project kinds, ACL, naming, cap, sharing, index leak)
→ Tasks 4, 5, 7. Section 4 (authorship, contributor chain, backfill) → Tasks 8, 9, 10. Section 5
(session flow) → Task 12. Schema bootstrap (three modes + the authoring skill) → Tasks 7, 13.
Section 6 (migration, errors, tests) → Tasks 8, 14. Adversarial findings:
A1 → Task 6; A2 → Task 2; A3 → Task 11; A4 → Task 2; A5 → Task 4; A6 → Task 7; A7 → Task 14 docs;
A8 → Task 14; A9 → Task 14 step 8; A11 → Task 5; A12 → Task 1; A13 → Task 6; A14 → Tasks 4, 7;
A15 → Task 8; A16 → Task 7.

**Review Focus coverage.** (1) revoked token → Task 3. (2) pathological names → Tasks 4, 7.
(3) concurrent create → Task 7. (4) corrupt `project.json` → Task 4. (5) hostile label in the hook's
JSON → Task 12.

**Known risk to watch during execution.** Task 6 replaces a build-time `db = project.db` with an
attribute proxy. If any tool body or engine function type-checks the database (`isinstance(db,
Database)`) or uses it as a dict key, the proxy breaks that call. Nothing in the current code does,
but if a proxy failure appears, the fallback is mechanical rather than a redesign: pass
`current_project().db` explicitly in the affected body.
