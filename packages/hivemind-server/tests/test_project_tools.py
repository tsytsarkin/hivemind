"""The project lifecycle: create, list, inspect, share, unshare — and who may do which."""
import concurrent.futures as cf
import json

import httpx
import pytest
from conftest import Lifespan, _call, _parse, _post

from hivemind_server import bus_ws
from hivemind_server import project_tools as pt
from hivemind_server import projects_meta as pm
from hivemind_server import schemas
from hivemind_server.db import Invalid
from hivemind_server.identity import Identity, IdentityStore


@pytest.fixture()
def reg(tmp_path):
    from hivemind_server.project import ProjectRegistry
    r = ProjectRegistry(tmp_path / "projects", max_blob_bytes=1 << 20, blob_grace_seconds=1)
    r.discover()
    return r


NIK = Identity(user="nik", device="mac-studio")
ANA = Identity(user="ana", device="laptop")
EVE = Identity(user="eve", device="vps")
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
    """Review Focus 2: validation happens before any mkdir.

    Asserted as "the projects root did not change" rather than "reg.root / name does not exist":
    for "" and ".." that path IS the root (or its parent), which exists either way, so the obvious
    spelling of this assertion cannot fail.
    """
    before = sorted(p.name for p in reg.root.iterdir())
    with pytest.raises(Invalid):
        pt.create(reg, NIK, name, visibility="private")
    assert sorted(p.name for p in reg.root.iterdir()) == before


def test_racing_sessions_get_one_project_and_all_agree_on_it(reg):
    """Review Focus 3: dir+db creation is not transactional.

    Careful about what this pins. `_CREATE_LOCK` is held across the whole of create, so these eight
    threads run strictly sequentially and threads 2-8 return at the `existing is not None` branch
    without ever reaching mkdir — this test would pass with `exist_ok=True`. What it holds is the
    lock plus the idempotent return: no racer loses a create for a reason it cannot see, and no two
    projects appear under one name. The ATOMIC CLAIM itself is pinned by the three tests that
    pre-create the directory on disk (test_a_project_another_process_created_is_not_confirmed_either
    and the two after it), which fail with `exist_ok=True`.
    """
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = [f for f in cf.as_completed(
            [ex.submit(pt.create, reg, NIK, "nik.race", "private") for _ in range(8)])]
    ok = [r for r in results if not r.exception()]
    assert len(ok) >= 1
    assert sum(1 for p in reg.root.iterdir() if p.name == "nik.race") == 1
    assert reg.get("nik.race").meta.owner == "nik"
    # In-process the name is claimed under one lock, so every racer resolves to the same project
    # rather than some of them losing: a lost race here would be an agent's create failing for no
    # reason the agent can see.
    assert len(ok) == 8, [r.exception() for r in results if r.exception()]
    assert sum(1 for r in ok if r.result()["existing"] is False) == 1, "exactly one creator"


def test_a_private_project_is_never_observable_as_shared(reg, monkeypatch):
    """Project.__init__ stamps visibility=shared when project.json is absent, and
    ProjectRegistry.create publishes the project into _projects on return — so the metadata has to
    be on disk BEFORE the Project is constructed. Swap that order and a request served concurrently
    reads the new private project as shared, i.e. as readable by everyone.
    """
    seen = []
    real_make = reg._make

    def spy(name):
        # What a concurrent reader would see at the moment the project becomes constructible.
        seen.append(pm.load(reg.root / name, name).owner)
        return real_make(name)

    monkeypatch.setattr(reg, "_make", spy)
    pt.create(reg, NIK, "nik.ordered", visibility="private")
    assert seen == ["nik"], "the metadata must be written before the Project is constructed"
    assert reg.get("nik.ordered").meta.visibility == "private"


def test_the_per_user_cap_is_enforced(reg, monkeypatch):
    monkeypatch.setattr(pt, "MAX_PER_USER", 3)
    for i in range(3):
        pt.create(reg, NIK, f"nik.p{i}", visibility="private")
    with pytest.raises(Invalid) as e:
        pt.create(reg, NIK, "nik.p4", visibility="private")
    assert "cap" in str(e.value).lower()


def test_the_cap_counts_shared_projects_too(reg, monkeypatch):
    """Otherwise visibility='shared' is an unbounded-creation bypass: the cap exists to stop an
    agent loop from creating databases without end, and a shared one is just as much a database."""
    monkeypatch.setattr(pt, "MAX_PER_USER", 2)
    pt.create(reg, NIK, "team-a", visibility="shared")
    pt.create(reg, NIK, "team-b", visibility="shared")
    with pytest.raises(Invalid) as e:
        pt.create(reg, NIK, "team-c", visibility="shared")
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
    assert pt.listing(reg, EVE)["shared_with_me"] == []


def test_an_admin_cannot_share_someone_elses_private_project(reg):
    """A16: an admin who could share could grant themselves read access."""
    pt.create(reg, NIK, "nik.private", visibility="private")
    with pytest.raises(Invalid):
        pt.share(reg, ADMIN, "nik.private", "root")
    assert pt.listing(reg, ADMIN)["shared_with_me"] == []


def test_a_stranger_cannot_share_and_learns_nothing(reg):
    pt.create(reg, NIK, "nik.private", visibility="private")
    with pytest.raises(Invalid) as e:
        pt.share(reg, EVE, "nik.private", "eve")
    assert "nik.private" not in str(e.value), "the refusal must not confirm the project exists"


def test_a_member_cannot_revoke_another_member(reg):
    """Revocation is owner-only for the same reason granting is: otherwise anyone let in can
    rearrange who else is in."""
    pt.create(reg, NIK, "nik.redteam", visibility="private")
    pt.share(reg, NIK, "nik.redteam", "ana")
    pt.share(reg, NIK, "nik.redteam", "eve")
    with pytest.raises(Invalid):
        pt.unshare(reg, ANA, "nik.redteam", "eve")
    assert "nik.redteam" in pt.listing(reg, EVE)["shared_with_me"]


def test_unsharing_a_non_member_is_refused_rather_than_a_silent_no_op(reg):
    """validate_username only catches a MALFORMED name. A well-formed one that is simply not a
    member used to succeed, bump last_touched and revoke nothing — so the owner believes access is
    gone while the real member keeps reading."""
    pt.create(reg, NIK, "nik.redteam", visibility="private")
    pt.share(reg, NIK, "nik.redteam", "ana")
    with pytest.raises(Invalid) as e:
        pt.unshare(reg, NIK, "nik.redteam", "ana2")
    assert "ana" in str(e.value), "the refusal names who IS a member, so the typo is obvious"
    assert reg.get("nik.redteam").meta.members == ["ana"]
    assert pt.unshare(reg, NIK, "nik.redteam", "ana")["members"] == []


def test_sharing_an_already_shared_project_is_an_error(reg):
    pt.create(reg, NIK, "team", visibility="shared")
    with pytest.raises(Invalid):
        pt.share(reg, NIK, "team", "ana")


def test_the_owner_cannot_be_unshared(reg):
    pt.create(reg, NIK, "nik.private", visibility="private")
    with pytest.raises(Invalid):
        pt.unshare(reg, NIK, "nik.private", "nik")


def test_sharing_with_an_unminted_user_is_refused(tmp_path, reg):
    """A share is silent until the grantee calls, so a typo'd username would look like it worked
    and quietly grant nobody."""
    ids = IdentityStore(tmp_path / "identities.json")
    ids.mint("ana", "laptop")
    pt.create(reg, NIK, "nik.redteam", visibility="private")
    with pytest.raises(Invalid) as e:
        pt.share(reg, NIK, "nik.redteam", "eve", identities=ids)
    assert "eve" in str(e.value)
    assert pt.share(reg, NIK, "nik.redteam", "ana", identities=ids)["members"] == ["ana"]
    assert reg.get("nik.redteam").meta.members == ["ana"]


@pytest.mark.parametrize("user", ["Eve", "eve.x", "", "a" * 40])
def test_an_ill_formed_username_is_never_written_into_members(reg, user):
    pt.create(reg, NIK, "nik.redteam", visibility="private")
    with pytest.raises(Invalid):
        pt.share(reg, NIK, "nik.redteam", user)
    assert reg.get("nik.redteam").meta.members == []


def test_creating_a_name_you_already_own_returns_it(reg):
    a = pt.create(reg, NIK, "nik.private", visibility="private")
    b = pt.create(reg, NIK, "nik.private", visibility="private")
    assert a["project"] == b["project"] and b["existing"] is True


def test_creating_a_name_in_someone_elses_namespace_tells_you_nothing_about_it(reg):
    """The prefix rule refuses this before the ACL is ever consulted, so the message can echo the
    name the caller typed — but it must be the SAME message whether that project exists or not."""
    pt.create(reg, ANA, "ana.private", visibility="private")
    with pytest.raises(Invalid) as real:
        pt.create(reg, NIK, "ana.private", visibility="shared")
    with pytest.raises(Invalid) as fake:
        pt.create(reg, NIK, "ana.nothing", visibility="shared")
    assert str(real.value).replace("ana.private", "X") == str(fake.value).replace("ana.nothing", "X")
    assert reg.get("ana.private").meta.visibility == "private", "and it is left alone"


def test_another_user_cannot_squat_your_namespace(reg):
    """The `<user>.` prefix is only unsquattable if the SHARED tier obeys it too: a shared
    `nik.scratch` created by ana is a world-readable project sitting on the name nik's private
    create would resolve to."""
    with pytest.raises(Invalid) as e:
        pt.create(reg, ANA, "nik.scratch", visibility="shared")
    assert "nik" in str(e.value)
    assert reg.get("nik.scratch") is None
    assert not (reg.root / "nik.scratch").exists()


@pytest.mark.parametrize("discovered", [True, False], ids=["in the registry", "on disk only"])
def test_a_private_create_never_hands_back_a_shared_project(reg, discovered):
    """Both existing-project branches. Silently returning the shared project it found is how an
    agent that asked for private writes private work into a graph every user can read."""
    d = reg.root / "nik.scratch"
    d.mkdir()
    pm.save(d, pm.ProjectMeta(name="nik.scratch", visibility="shared", owner="ana"))
    if discovered:
        reg.discover()
    with pytest.raises(Invalid) as e:
        pt.create(reg, NIK, "nik.scratch", visibility="private")
    assert "shared" in str(e.value) and "private" in str(e.value)
    # ...and the same guard the other way round, so neither direction is the silent one.
    pt.create(reg, NIK, "nik.own", visibility="private")
    with pytest.raises(Invalid):
        pt.create(reg, NIK, "nik.own", visibility="shared")
    assert reg.get("nik.own").meta.visibility == "private"


def test_the_mismatch_refusal_names_a_project_that_really_resolves(reg):
    """The message tells the caller to "pass project=<name>", so that must work. The disk-only
    branch used to refuse before publishing the project into the registry, and
    envelope.resolve_project denies a name ProjectRegistry.get cannot find — so the advice was false
    until the next restart.
    """
    from hivemind_server import envelope
    from hivemind_server.identity import set_identity

    d = reg.root / "nik.ondisk"
    d.mkdir()
    pm.save(d, pm.ProjectMeta(name="nik.ondisk", visibility="shared", owner="nik"))
    with pytest.raises(Invalid) as e:
        pt.create(reg, NIK, "nik.ondisk", visibility="private")
    assert "project=nik.ondisk" in str(e.value)
    assert reg.get("nik.ondisk") is not None, "the advice needs the project in the registry"
    # Follow the advice for real. The contextvars are set with reset tokens rather than the public
    # setter, which has no getter to restore from — a stray require_auth would leak into later tests.
    tok_reg = envelope._REGISTRY.set(reg)
    tok_auth = envelope._REQUIRE_AUTH.set(True)
    set_identity(NIK)
    try:
        assert envelope.resolve_project("nik.ondisk", requires=True).name == "nik.ondisk"
    finally:
        set_identity(None)
        envelope._REQUIRE_AUTH.reset(tok_auth)
        envelope._REGISTRY.reset(tok_reg)


def test_the_existing_answer_says_who_owns_it(reg):
    """Visibility agreeing is not the same as the project being yours: a private project in your own
    namespace that ana owns and shared with you (a pre-rule squat, or a hand-made directory) passes
    every guard, so the answer has to show whose it is."""
    d = reg.root / "nik.notmine"
    d.mkdir()
    pm.save(d, pm.ProjectMeta(name="nik.notmine", visibility="private", owner="ana",
                              members=["nik"]))
    disk = pt.create(reg, NIK, "nik.notmine", visibility="private")          # the _adopt branch
    assert (disk["existing"], disk["visibility"], disk["owner"]) == (True, "private", "ana")
    again = pt.create(reg, NIK, "nik.notmine", visibility="private")         # the registry branch
    assert again["owner"] == "ana"
    assert pt.create(reg, NIK, "nik.mineown", visibility="private")["existing"] is False
    assert pt.create(reg, NIK, "nik.mineown", visibility="private")["owner"] == "nik"


def test_the_refusal_is_the_exact_sentence_the_middleware_uses():
    """Two literals in two files. The moment they drift, "no such project" and "not yours" become
    distinguishable again and the existence oracle this plan spent a task removing is back."""
    from hivemind_server.app import PROJECT_DENIED
    assert pt.DENIED == PROJECT_DENIED["error"]


def test_a_project_another_process_created_is_not_confirmed_either(reg):
    """A name claimed on disk since discover() lands in the mkdir(exist_ok=False) branch, where
    there is no registry entry to check the ACL against. It must deny like any other — and must not
    construct a Project, whose __init__ would stamp `shared` over the owner's private metadata.

    This and the two tests after it are what pin the atomic claim: relax it to `exist_ok=True` and
    create walks straight into the build, overwriting ana's project.json with nik's.
    """
    # A name in nik's OWN namespace that ana owns: created before the prefix rule existed, or by
    # hand. Nothing syntactic stops nik here, so this is the branch the ACL has to hold.
    d = reg.root / "nik.squatted"
    d.mkdir()
    pm.save(d, pm.ProjectMeta(name="nik.squatted", visibility="private", owner="ana"))
    with pytest.raises(Invalid) as e:
        pt.create(reg, NIK, "nik.squatted", visibility="private")
    assert str(e.value) == pt.DENIED, "no name, no visibility, nothing about why"
    assert pm.load(d, "nik.squatted").owner == "ana", "the owner's metadata is untouched"
    assert reg.get("nik.squatted") is None, "and no Project was constructed for it"


def test_a_name_you_own_on_disk_is_adopted_rather_than_restamped(reg):
    """The same branch, for the caller who may have it: adopt the existing project."""
    d = reg.root / "nik.claimed"
    d.mkdir()
    pm.save(d, pm.ProjectMeta(name="nik.claimed", visibility="private", owner="nik"))
    out = pt.create(reg, NIK, "nik.claimed", visibility="private")
    assert (out["project"], out["existing"], out["visibility"]) == ("nik.claimed", True, "private")
    assert reg.get("nik.claimed").meta.owner == "nik", "and it became usable on this server"


def test_a_half_built_directory_denies_rather_than_being_adopted(reg):
    """A claimed name with no metadata yet is a creation in flight (or a hand-made directory):
    unreadable metadata denies, exactly as projects_meta.load decides."""
    (reg.root / "nik.mid").mkdir()
    with pytest.raises(Invalid):
        pt.create(reg, NIK, "nik.mid", visibility="private")


def test_a_legacy_identity_cannot_create_a_private_project(reg):
    """A14: a legacy client_id like `mac-studio` cannot satisfy the <user>. prefix rule."""
    legacy = Identity(user="legacy:mac-studio", device="mac-studio", legacy=True,
                      project_scope="default")
    with pytest.raises(Invalid) as e:
        pt.create(reg, legacy, "legacy:mac-studio.private", visibility="private")
    assert "minted" in str(e.value) or "legacy" in str(e.value)


def test_a_legacy_identity_cannot_create_a_shared_project_either(reg):
    """The prefix rule is not what stops it — a legacy token has no server-level user at all."""
    legacy = Identity(user="legacy:mac-studio", device="mac-studio", legacy=True,
                      project_scope="default")
    with pytest.raises(Invalid):
        pt.create(reg, legacy, "team", visibility="shared")
    assert reg.get("team") is None


def _seed_widget(reg):
    src = reg.get(reg.default_name)
    with src.db.write("setup", "seed") as tx:
        schemas.define_type(tx.cur, tx, "node", "widget",
                            {"type": "object", "additionalProperties": True}, status="active")
        schemas.define_type(tx.cur, tx, "edge", "wires", {"type": "object"}, status="active",
                            traits={"acyclic": True, "versioned": False, "src_types": ["widget"]})
        schemas.define_type(tx.cur, tx, "node", "draft",
                            {"type": "object"}, status="proposed")
        schemas.define_type(tx.cur, tx, "node", "gadget", {"type": "object"}, status="active",
                            traits={"parent": "widget"})
    return src


def _types(db, kind="node"):
    return {t["name"]: t for t in schemas.get_schema(db)[f"{kind}_types"]}


def _parent(db, name):
    """Read the row, because the get_schema view does not carry `parent` — which is the whole
    reason copy_types reads rows too."""
    with db.read() as cur:
        return schemas.usable_type(cur, "node", name)["parent"]


def test_inherit_copies_the_source_vocabulary(reg):
    src = _seed_widget(reg)
    pt.create(reg, NIK, "nik.child", visibility="private", schema="inherit", source=src)
    child = reg.get("nik.child").db
    assert "widget" in _types(child)
    # Traits travel with the type: an acyclic, bulk edge copied as a cyclic versioned one would
    # silently change what the new project accepts.
    wires = _types(child, "edge")["wires"]
    assert (wires["acyclic"], wires["versioned"], wires["src_types"]) == (1, 0, ["widget"])
    # A node type's parent is the other field the get_schema view drops: inherited as None, `gadget`
    # would quietly stop being a widget in the new project.
    assert _parent(child, "gadget") == "widget"
    assert "draft" not in _types(child), "a proposed type is not part of the vocabulary yet"


def test_bare_starts_with_no_types_and_says_the_agent_decides(reg):
    src = _seed_widget(reg)
    out = pt.create(reg, NIK, "nik.bare", visibility="private", schema="bare", source=src)
    assert "widget" not in _types(reg.get("nik.bare").db)
    assert "schema_propose" in out["next"]


def test_interview_creates_no_types_and_asks_for_the_schema_skill(reg):
    """The server cannot interview anybody; it hands the job to the agent's skill."""
    src = _seed_widget(reg)
    out = pt.create(reg, NIK, "nik.interview", visibility="private", schema="interview", source=src)
    assert _types(reg.get("nik.interview").db) == {}
    assert out["schema"] == "interview"
    assert "hivemind-schema" in out["next"], "the response must name the skill to load"


def test_a_new_project_gets_the_framework_guide(reg):
    """It is created after startup, so app.build_app's own seeding has already run."""
    pt.create(reg, NIK, "nik.guided", visibility="private")
    from hivemind_server import guide
    assert "core" in {s["name"] for s in guide.get_index(reg.get("nik.guided").db)["sections"]}


def test_an_unknown_schema_mode_is_refused(reg):
    with pytest.raises(Invalid):
        pt.create(reg, NIK, "nik.x", visibility="private", schema="magic")
    assert reg.get("nik.x") is None


def test_an_unknown_visibility_is_refused(reg):
    with pytest.raises(Invalid):
        pt.create(reg, NIK, "nik.x", visibility="restricted")
    assert reg.get("nik.x") is None


def test_a_failed_build_leaves_nothing_behind(reg, monkeypatch):
    """Review Focus 3, the other half: the name is claimed before the build, so a build that raises
    has to unwind both the directory and the registry entry or the name is dead until a restart."""
    from hivemind_server import guide

    def boom(_db):
        raise RuntimeError("disk full")

    monkeypatch.setattr(guide, "ensure_core_guide", boom)
    with pytest.raises(RuntimeError):
        pt.create(reg, NIK, "nik.doomed", visibility="private")
    assert not (reg.root / "nik.doomed").exists()
    assert reg.get("nik.doomed") is None
    monkeypatch.undo()
    assert pt.create(reg, NIK, "nik.doomed", visibility="private")["existing"] is False


def test_info_is_refused_for_a_project_you_cannot_see(reg):
    pt.create(reg, NIK, "nik.private", visibility="private")
    assert pt.info(reg, NIK, "nik.private")["owner"] == "nik"
    for who in (ANA, ADMIN):
        with pytest.raises(Invalid) as e:
            pt.info(reg, who, "nik.private")
        # Byte-identical to the answer for a name that does not exist at all.
        with pytest.raises(Invalid) as missing:
            pt.info(reg, who, "nik.nothing")
        assert str(e.value) == str(missing.value)


# ── the MCP surface ───────────────────────────────────────────────────────────────


@pytest.fixture()
def served(projects_dir):
    """The real app over a shared `default`, plus a minted server-level identity for nik."""
    from hivemind_server import app as appmod
    from hivemind_server.config import Config

    (projects_dir / "default").mkdir(parents=True)
    application = appmod.build_app(Config())
    nik = IdentityStore(application.state.cfg.identities_path).mint("nik", "mac-studio")
    return application, nik


@pytest.mark.anyio
async def test_the_lifecycle_tools_work_over_mcp(served):
    """They are registered on the REAL server, not the ProjectAware proxy: they are about projects
    rather than in one, and their own `project` argument would collide with an injected one."""
    application, nik = served
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        listed = _parse(await _post(c, "", nik, "tools/list"))["result"]["tools"]
        tools = {t["name"]: t for t in listed}
        assert {"project_list", "project_create", "project_info", "project_share",
                "project_unshare"} <= set(tools)
        assert "project" not in (tools["project_create"]["inputSchema"].get("properties") or {}), \
            "no injected project argument, or it would shadow the one these tools mean"
        assert "project" in tools["project_info"]["inputSchema"]["required"], \
            "and theirs is their own, mandatory argument"
        # The description has to state the rules it enforces; an agent that learns the dotted-name
        # rule only from a refusal spends a round trip on it, and these consumers read literally.
        desc = tools["project_create"]["description"]
        assert "<you>.<suffix>" in desc and "dot" in desc.lower()
        assert "ASK THE USER" in desc

        out = _call(await _post(c, "", nik, "tools/call", {
            "name": "project_create",
            "arguments": {"name": "nik.fromtool", "schema": "bare", "label": "from a session"}}, 2))
        assert (out["ok"], out["visibility"], out["existing"]) == (True, "private", False)

        mine = _call(await _post(c, "", nik, "tools/call",
                                 {"name": "project_list", "arguments": {}}, 3))
        assert "nik.fromtool" in mine["mine"] and "default" in mine["shared"]

        info = _call(await _post(c, "", nik, "tools/call",
                                 {"name": "project_info",
                                  "arguments": {"project": "nik.fromtool"}}, 4))
        assert (info["owner"], info["label"]) == ("nik", "from a session")

        # A refusal comes back as the envelope, not as a protocol-level error the agent cannot act on
        bad = _call(await _post(c, "", nik, "tools/call",
                                {"name": "project_create", "arguments": {"name": "ana.x"}}, 5))
        assert (bad["ok"], bad["error_kind"]) == (False, "invalid")


@pytest.mark.anyio
async def test_a_new_project_is_immediately_usable_on_both_surfaces(served):
    """The neutral endpoint must work or the tool created a dead project — and so must the
    project's own prefix.

    build_app used to mount the prefixes once, from the projects the registry held at startup, so
    everything under /p/<name>/ (blob upload, guide, catalogs, its own /mcp, the bus) 404'd until
    somebody restarted the server. project_tools._serve now installs the same two routes on the
    live app at creation, so both surfaces answer the moment the tool returns.
    """
    application, nik = served
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        made = _call(await _post(c, "", nik, "tools/call", {
            "name": "project_create",
            "arguments": {"name": "nik.live", "schema": "bare"}}, 2))
        got = _call(await _post(c, "", nik, "tools/call", {
            "name": "schema_propose",
            "arguments": {"kind": "node", "name": "note", "json_schema": {"type": "object"},
                          "project": "nik.live"}}, 3))
        assert got["ok"] is True, got
        # Its own prefix answers — with the owner's token, so this is the router AND the ACL, not
        # one masking the other. 404 here is the regression this test exists to catch.
        r = await c.get("/p/nik.live/healthz", headers={"Authorization": f"Bearer {nik}"})
        assert r.status_code == 200, r.text
        assert r.json()["project"] == "nik.live", r.text
        # The reply must not still tell the agent to go and restart something.
        assert "restart" not in made["note"].lower(), made["note"]


@pytest.mark.anyio
async def test_bus_connect_works_on_a_freshly_created_project(served):
    """A project created in this process gets its bus route at creation, so bus_connect answers
    with a URL that actually connects.

    History, because both halves still matter. Originally bus_connect returned ok:true with a
    ws_url for a prefix that 404'd until a restart; the listener got 403, classified it `refused`,
    printed "call bus_connect for a fresh URL", and the agent looped. A refusal replaced that,
    which was the honest answer while the prefix really was dead. project_tools._serve now installs
    the route at creation, so the honest answer is a working URL again — and the refusal survives
    for the case it was written for, exercised below by taking the mount registration away.
    """
    application, nik = served
    transport = httpx.ASGITransport(app=application)
    async with Lifespan(application), httpx.AsyncClient(transport=transport,
                                                        base_url="http://t", timeout=30) as c:
        _call(await _post(c, "", nik, "tools/call", {
            "name": "project_create",
            "arguments": {"name": "nik.fresh2", "schema": "bare"}}, 2))

        out = _call(await _post(c, "", nik, "tools/call", {
            "name": "bus_connect",
            "arguments": {"label": "box", "project": "nik.fresh2"}}, 3))
        assert out["ok"] is True, out
        assert out["ws_url"].endswith("/p/nik.fresh2/bus/ws"), out
        # Everything the agent needs to act on must be present, since it is now told to act.
        for needed in ("ws_url", "monitor_command", "listen_key", "ticket", "next"):
            assert needed in out, f"{needed} missing from a connectable project: {out}"

        # And the route is really there, not merely advertised: the prefix answers over HTTP.
        r = await c.get("/p/nik.fresh2/healthz", headers={"Authorization": f"Bearer {nik}"})
        assert r.status_code == 200, r.text

        # The guard still fires for a project the router genuinely does not serve — now reachable
        # only by removing the registration, which is what "no routes" means to bus_connect.
        pdir = application.state.registry.get("nik.fresh2").dir
        bus_ws._MOUNTED.discard(bus_ws._scope(pdir))
        try:
            denied = _call(await _post(c, "", nik, "tools/call", {
                "name": "bus_connect",
                "arguments": {"label": "box", "project": "nik.fresh2"}}, 4))
            assert denied["ok"] is False and denied["error_kind"] == "bus", denied
            assert "nik.fresh2" in denied["error"], denied["error"]
            # No usable-looking credential or URL may come back with a refusal: each is a thing the
            # agent would act on, and none of them can work.
            for leak in ("ws_url", "monitor_command", "listen_key", "ticket", "next"):
                assert leak not in denied, f"{leak} handed back with a refusal: {denied}"
            assert "ws://" not in denied["error"] and "wss://" not in denied["error"], denied

            # bus_send must not promise a reconnect that cannot happen either.
            hub = bus_ws.hub_for(pdir)
            hub.mint_ticket("box")                  # a peer exists, offline, with no way to attach
            sent = _call(await _post(c, "", nik, "tools/call", {
                "name": "bus_send",
                "arguments": {"to": "box", "body": "hi", "project": "nik.fresh2"}}, 5))
            assert sent["queued"] is True, sent
            assert "queued for reconnect" not in sent["note"], sent["note"]
        finally:
            bus_ws.register_mount(pdir)             # leave the app as we found it

        # Control: `default` WAS mounted at build time, so the bus still works there.
        ok = _call(await _post(c, "", nik, "tools/call", {
            "name": "bus_connect",
            "arguments": {"label": "box", "project": "default"}}, 6))
        assert ok["ok"] is True and ok["ws_url"].endswith("/p/default/bus/ws"), ok


def test_the_admin_cli_shares_as_the_project_owner(projects_dir, monkeypatch, capsys):
    """Recovery from the box: the tools are owner-only, so an owner who has lost their token has no
    other route back in — and an operator with file access already has everything."""
    from hivemind_server import admin
    from hivemind_server import config as configmod
    from hivemind_server.config import Config

    monkeypatch.setattr(configmod, "_cfg", None)   # config() caches one Config per process
    d = projects_dir / "nik.private"
    d.mkdir(parents=True)
    pm.save(d, pm.ProjectMeta(name="nik.private", visibility="private", owner="nik"))
    IdentityStore(Config().identities_path).mint("ana", "laptop")

    assert admin.main(["project-share", "nik.private", "ana"]) == 0
    assert pm.load(d, "nik.private").members == ["ana"]
    assert admin.main(["project-unshare", "nik.private", "ana"]) == 0
    assert pm.load(d, "nik.private").members == []
    with pytest.raises(Invalid):
        admin.main(["project-share", "nik.private", "eve"])     # not a minted user
    capsys.readouterr()
