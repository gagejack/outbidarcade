import pytest


@pytest.fixture
def auth(app_modules):
    import auth
    import importlib
    importlib.reload(auth)
    return auth


def test_create_and_fetch_user(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    user = auth.get_user(uid)
    assert user["email"] == "dev@studio.com"


def test_password_is_not_stored_in_the_clear(auth, database):
    auth.create_user("dev@studio.com", "correct horse battery")
    with database.connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert "correct horse battery" not in stored
    assert stored.startswith("scrypt$")


def test_login_accepts_the_right_password(auth):
    auth.create_user("dev@studio.com", "correct horse battery")
    assert auth.check_login("dev@studio.com", "correct horse battery")


def test_login_rejects_the_wrong_password(auth):
    auth.create_user("dev@studio.com", "correct horse battery")
    assert auth.check_login("dev@studio.com", "wrong") is None


def test_login_is_case_insensitive_on_email(auth):
    auth.create_user("dev@studio.com", "correct horse battery")
    assert auth.check_login("DEV@STUDIO.COM", "correct horse battery")


def test_oauth_only_user_cannot_log_in_with_empty_password(auth):
    auth.create_user("dev@studio.com")
    assert auth.check_login("dev@studio.com", "") is None


def test_unknown_email_returns_none(auth):
    assert auth.check_login("nobody@nowhere.com", "x") is None


def test_short_passwords_are_rejected(auth):
    assert auth.password_problem("short") != ""
    assert auth.password_problem("a" * 10) == ""


def test_set_password_replaces_the_old_one(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    auth.set_password(uid, "a brand new secret")
    assert auth.check_login("dev@studio.com", "correct horse battery") is None
    assert auth.check_login("dev@studio.com", "a brand new secret")
