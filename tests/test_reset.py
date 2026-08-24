import importlib
import time

import pytest


@pytest.fixture
def auth(app_modules):
    import auth
    importlib.reload(auth)
    return auth


def test_reset_token_identifies_the_user(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    assert auth.consume_reset(auth.issue_reset(uid)) == uid


def test_raw_token_is_never_stored(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    token = auth.issue_reset(uid)
    with database.connect() as conn:
        stored = [r[0] for r in conn.execute("SELECT token_hash FROM reset_tokens")]
    assert token not in stored


def test_token_works_only_once(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    token = auth.issue_reset(uid)
    assert auth.consume_reset(token) == uid
    assert auth.consume_reset(token) is None


def test_expired_token_is_refused(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    token = auth.issue_reset(uid)
    with database.connect() as conn:
        conn.execute("UPDATE reset_tokens SET expires_at=?", (int(time.time()) - 1,))
    assert auth.consume_reset(token) is None


def test_garbage_token_is_refused(auth):
    assert auth.consume_reset("nonsense") is None
    assert auth.consume_reset("") is None
