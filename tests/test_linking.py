import importlib

import pytest


@pytest.fixture
def auth(app_modules):
    import auth
    importlib.reload(auth)
    return auth


def profile(**over):
    base = {
        "provider": "google",
        "uid": "1234",
        "email": "dev@studio.com",
        "email_verified": True,
        "name": "Dev Person",
    }
    base.update(over)
    return base


def test_new_verified_profile_creates_a_user(auth):
    user, error = auth.user_from_profile(profile())
    assert error == ""
    assert user["email"] == "dev@studio.com"
    assert user["password_hash"] == ""


def test_returning_profile_finds_the_same_user(auth):
    first, _ = auth.user_from_profile(profile())
    second, _ = auth.user_from_profile(profile())
    assert first["id"] == second["id"]


def test_verified_email_links_to_an_existing_password_account(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    user, error = auth.user_from_profile(profile())
    assert error == ""
    assert user["id"] == uid


def test_unverified_email_never_links(auth):
    auth.create_user("dev@studio.com", "correct horse battery")
    user, error = auth.user_from_profile(profile(email_verified=False))
    assert user is None
    assert "verified" in error.lower()


def test_unverified_email_never_creates(auth):
    user, error = auth.user_from_profile(profile(email_verified=False))
    assert user is None
    assert auth.get_user_by_email("dev@studio.com") is None


def test_missing_email_is_refused(auth):
    user, error = auth.user_from_profile(profile(email=""))
    assert user is None
    assert error != ""


def test_identity_survives_a_provider_email_change(auth):
    first, _ = auth.user_from_profile(profile())
    second, error = auth.user_from_profile(profile(email="new@studio.com"))
    assert error == ""
    assert second["id"] == first["id"]


def test_two_providers_link_to_one_account(auth):
    google, _ = auth.user_from_profile(profile(provider="google", uid="g1"))
    github, _ = auth.user_from_profile(profile(provider="github", uid="h1"))
    assert google["id"] == github["id"]
