from tests.fakes import FakeCredentialStore


def test_credentials_never_appear_in_repr() -> None:
    store = FakeCredentialStore({"qq-smtp": "secret-auth-code"})

    assert store.get("qq-smtp") == "secret-auth-code"
    assert "secret-auth-code" not in repr(store)
