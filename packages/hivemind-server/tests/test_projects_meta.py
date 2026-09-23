import json
import os

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


def test_nobody_is_not_somebody(tmp_path):
    """An unauthenticated caller reaches nothing, not even a shared project."""
    d, meta = _write(tmp_path, name="default", visibility="shared", owner=None)
    assert not pm.can_access(None, meta)


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


@pytest.mark.parametrize("body", ["[]", '"x"', "3", "null"])
def test_valid_json_that_is_not_an_object_fails_closed(tmp_path, body):
    """A JSON array/string/number/null parses fine and has no visibility at all; it is metadata we
    cannot read, so it denies like any other corruption."""
    d = tmp_path / "broken"
    d.mkdir()
    (d / "project.json").write_text(body)
    meta = pm.load(d, "broken")
    assert (meta.visibility, meta.owner, meta.members) == ("private", None, [])
    assert not pm.can_access(NIK, meta) and not pm.can_access(ADMIN, meta)


@pytest.mark.parametrize("members", ['"ab"', '{"ana": 1}', "5", "true", '"ana"'])
def test_an_ill_typed_member_list_fails_closed_instead_of_granting(tmp_path, members):
    """The one corruption mode that could GRANT rather than deny: list("ab") is ['a', 'b'] and
    list({"ana": 1}) is ['ana'], so coercing would hand a hand-corrupted file's bytes to real
    single-character usernames. It must resolve to unreachable like every other bad ACL."""
    d = tmp_path / "nik.p"
    d.mkdir()
    (d / "project.json").write_text(
        '{"visibility": "private", "owner": "nik", "members": %s}' % members)
    meta = pm.load(d, "nik.p")
    assert (meta.visibility, meta.owner, meta.members) == ("private", None, [])
    for user in ("a", "b", "ab", "ana", "nik"):
        assert not pm.can_access(Identity(user=user, device="box"), meta), \
            f"a corrupt members field must not grant {user}"


def test_a_missing_or_null_member_list_is_simply_empty(tmp_path):
    """`null` and an absent key mean "nobody shared yet" — that denies on its own, so it is not a
    corruption and must not make an otherwise valid private project unreachable for its owner."""
    d = tmp_path / "nik.p"
    d.mkdir()
    (d / "project.json").write_text('{"visibility": "private", "owner": "nik", "members": null}')
    assert pm.load(d, "nik.p").members == []
    assert pm.can_access(NIK, pm.load(d, "nik.p"))


def test_saving_an_unknown_visibility_is_refused_at_the_write(tmp_path):
    """Fail-closed is for corrupt files; a first-party write of "restricted" is a caller bug, and
    persisting it would leave a silently unreachable project with nothing naming the cause."""
    d, meta = _write(tmp_path, name="nik.p", visibility="private", owner="nik")
    meta.visibility = "restricted"
    with pytest.raises(Invalid):
        pm.save(d, meta)
    assert pm.load(d, "nik.p").visibility == "private", "the refused write left the file alone"
    assert sorted(p.name for p in d.iterdir()) == ["project.json"], "and no temp file behind"


def test_a_project_with_no_metadata_at_all_fails_closed(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    meta = pm.load(d, "bare")
    assert (meta.visibility, meta.owner, meta.members) == ("private", None, [])
    assert not pm.can_access(NIK, meta)


def test_metadata_is_cached_but_notices_a_change(tmp_path):
    d, meta = _write(tmp_path, name="nik.p", visibility="private", owner="nik")
    assert pm.load(d, "nik.p").members == []
    raw = json.loads((d / "project.json").read_text())
    raw["members"] = ["ana"]
    (d / "project.json").write_text(json.dumps(raw))
    assert pm.load(d, "nik.p").members == ["ana"], "a share must take effect without a restart"


def test_the_cache_actually_avoids_re_reading_the_file(tmp_path):
    """The previous test passes even with no cache at all, so pin the cache itself: rewrite the
    file with the SAME size and restore its mtime, and the stamped cache must serve the old copy."""
    d, meta = _write(tmp_path, name="nik.p", visibility="private", owner="nik")
    path = d / "project.json"
    assert pm.load(d, "nik.p").owner == "nik"
    st = path.stat()
    raw = json.loads(path.read_text())
    raw["owner"] = "ana"                      # same length as "nik", so the size stamp is unchanged
    path.write_text(json.dumps(raw, indent=2))
    assert path.stat().st_size == st.st_size, "test needs a byte-identical size to pin the cache"
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert pm.load(d, "nik.p").owner == "nik", "an unchanged stamp must not re-read the file"


def test_two_projects_of_the_same_name_under_different_roots_do_not_share_a_cache_entry(tmp_path):
    """The cache key has to be the file path: on a filesystem with coarse mtime granularity two
    same-named projects whose project.json has the same size would otherwise cross-read ACLs —
    one deployment's private project answering with another's owner."""
    a, _ = _write(tmp_path / "a", name="team", visibility="private", owner="nik")
    b, _ = _write(tmp_path / "b", name="team", visibility="private", owner="ana")
    pa, pb = a / "project.json", b / "project.json"
    assert pa.stat().st_size == pb.stat().st_size, "test needs equal sizes to force a collision"
    st = pa.stat()
    os.utime(pb, ns=(st.st_atime_ns, st.st_mtime_ns))     # identical stamps, different files
    assert pm.load(a, "team").owner == "nik"
    assert pm.load(b, "team").owner == "ana"


def test_a_caller_cannot_corrupt_the_cache_by_mutating_what_it_got(tmp_path):
    """Sharing mutates `meta.members` before saving (project_tools.share), so a load must hand
    back a private copy or an abandoned mutation would linger in the cache as granted access."""
    d, _ = _write(tmp_path, name="nik.p", visibility="private", owner="nik")
    pm.load(d, "nik.p").members.append("ana")
    assert pm.load(d, "nik.p").members == []


def test_saving_is_atomic_and_leaves_no_temp_file(tmp_path):
    d, meta = _write(tmp_path, name="nik.p", visibility="private", owner="nik")
    assert sorted(p.name for p in d.iterdir()) == ["project.json"]
    assert meta.created and meta.last_touched, "save stamps creation and touch times"


def test_members_are_only_visible_to_the_people_in_the_project(tmp_path):
    d, meta = _write(tmp_path, name="nik.redteam", visibility="private", owner="nik",
                     members=["ana"])
    assert pm.load(d, "nik.redteam").public(NIK)["members"] == ["ana"]
    assert pm.load(d, "nik.redteam").public(ANA)["members"] == ["ana"]
    assert "members" not in pm.load(d, "nik.redteam").public(ADMIN)
    assert "members" not in pm.load(d, "nik.redteam").public(None)


@pytest.mark.parametrize("name", ["default", "nik.private", "a", "nik.s-5e7858ce", "a" * 64])
def test_valid_project_names(name):
    assert pm.validate_project_name(name) == name


@pytest.mark.parametrize("name", ["", "Nik", ".nik", "-nik", "a" * 65, "nik/x", "..", "nik x"])
def test_invalid_project_names(name):
    with pytest.raises(Invalid):
        pm.validate_project_name(name)


@pytest.mark.parametrize("name", ["nik\n", "default\n", ("a" * 64) + "\n", "nik\n.x"])
def test_project_names_with_trailing_whitespace_are_invalid(name):
    """Same trap as validate_username: re.match's `$` also matches just before a trailing newline,
    so match() would accept "default\n" as the `default` project — a second, invisible directory."""
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


def test_a_project_without_metadata_becomes_shared_on_construction(tmp_path):
    """A directory that predates project.json has been readable by everyone all along; adopting it
    as `shared` is the migration, and it must not be re-stamped once it exists."""
    from hivemind_server.project import Project

    d = tmp_path / "default"
    d.mkdir()
    p = Project("default", d, max_blob_bytes=1024, blob_grace_seconds=60)
    assert (p.meta.visibility, p.meta.owner) == ("shared", None)
    stamped = p.meta.created

    meta = p.meta
    meta.visibility, meta.owner = "private", "nik"
    pm.save(d, meta)
    again = Project("default", d, max_blob_bytes=1024, blob_grace_seconds=60)
    assert (again.meta.visibility, again.meta.owner) == ("private", "nik")
    assert again.meta.created == stamped, "an existing project.json is never overwritten"


@pytest.mark.parametrize("members", ["ab", {"ana": 1}, ["ana", 7], None, ("ana",)])
def test_saving_an_ill_typed_member_list_is_refused_at_the_write(tmp_path, members):
    """The mirror of test_an_ill_typed_member_list_fails_closed_instead_of_granting, on the write
    side: as_json() coerces with list(self.members), so members="ab" would persist ["a", "b"] and
    GRANT the single-character users a and b on the next read. The read side cannot fix that — by
    then the file is a perfectly valid ACL — so the write has to refuse."""
    d, meta = _write(tmp_path, name="nik.p", visibility="private", owner="nik")
    meta.members = members
    with pytest.raises(Invalid):
        pm.save(d, meta)
    assert pm.load(d, "nik.p").members == [], "the refused write left the file alone"
    assert sorted(p.name for p in d.iterdir()) == ["project.json"], "and no temp file behind"
