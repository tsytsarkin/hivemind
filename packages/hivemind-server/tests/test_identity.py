import json

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


@pytest.mark.parametrize("name", ["nik\n", "nik\t", ("a" * 32) + "\n"])
def test_invalid_usernames_trailing_whitespace(name):
    """re.match's `$` matches just before a trailing newline, so match() alone would accept
    "nik\n" as if it were "nik" — a distinct, invisible username riding a truncated length cap.
    validate_username must use fullmatch."""
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


def test_revoking_a_token_denies_immediately(tmp_path):
    """identities.json is operator-edited by design — that IS the revocation path — so removing a
    token's entry must deny it with no server restart, same as auth.TokenStore."""
    path = tmp_path / "identities.json"
    store = ident.IdentityStore(path)
    tok = store.mint("nik", "mac-studio")
    assert store.verify(tok) is not None

    data = json.loads(path.read_text())
    del data[tok]
    path.write_text(json.dumps(data))

    assert store.verify(tok) is None


def test_malformed_entry_does_not_deny_everyone_else(tmp_path):
    """identities.json is hand-editable by an operator; one entry missing "user" must not break
    verify/users/has_user for every OTHER, well-formed entry in the same file."""
    path = tmp_path / "identities.json"
    store = ident.IdentityStore(path)
    good = store.mint("nik", "mac-studio")

    data = json.loads(path.read_text())
    data["hm_broken"] = {"device": "no-user-field"}   # simulates an operator typo
    path.write_text(json.dumps(data))

    assert store.verify(good).user == "nik"
    assert store.verify("hm_broken") is None
    assert store.users() == ["nik"]
    assert store.has_user("nik") is True


def test_legacy_token_scope_matches_its_own_project_not_another(tmp_path):
    """A legacy token lives in exactly one project's tokens.json. Resolving it against that
    project must report THAT project as the scope; resolving the same token against a different
    project (whose tokens.json never held it) must refuse outright, never attribute it elsewhere."""
    from hivemind_server.auth import TokenStore

    class ProjectA:
        name = "proj-a"
    ProjectA.tokens = TokenStore(tmp_path / "a-tokens.json")
    legacy = ProjectA.tokens.mint("some-box")

    class ProjectB:
        name = "proj-b"
    ProjectB.tokens = TokenStore(tmp_path / "b-tokens.json")

    store = ident.IdentityStore(tmp_path / "identities.json")

    who_a = ident.resolve(legacy, store, ProjectA)
    assert who_a is not None
    assert who_a.project_scope == "proj-a"

    who_b = ident.resolve(legacy, store, ProjectB)
    assert who_b is None
