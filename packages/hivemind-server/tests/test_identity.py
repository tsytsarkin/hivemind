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
