import importlib
import time

import pytest


@pytest.fixture
def auth(app_modules):
    import auth

    importlib.reload(auth)
    return auth


def test_session_round_trips_to_a_user(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    token = auth.start_session(uid)
    assert auth.user_for_session(token)["id"] == uid


def test_unknown_token_has_no_user(auth):
    assert auth.user_for_session("not-a-real-token") is None


def test_missing_token_has_no_user(auth):
    assert auth.user_for_session(None) is None
    assert auth.user_for_session("") is None


def test_ending_a_session_revokes_it(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    token = auth.start_session(uid)
    auth.end_session(token)
    assert auth.user_for_session(token) is None


def test_ending_all_sessions_revokes_every_one(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    first, second = auth.start_session(uid), auth.start_session(uid)
    auth.end_all_sessions(uid)
    assert auth.user_for_session(first) is None
    assert auth.user_for_session(second) is None


def test_expired_sessions_stop_working(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    token = auth.start_session(uid)
    stale = int(time.time()) - database.SESSION_TTL - 60
    with database.connect() as conn:
        conn.execute("UPDATE sessions SET created_at=? WHERE token=?", (stale, token))
    assert auth.user_for_session(token) is None


def test_operator_sessions_are_not_user_sessions(auth, database):
    operator_token = database.new_session()
    assert auth.user_for_session(operator_token) is None
    assert database.session_valid(operator_token) is True


def test_user_sessions_are_not_operator_sessions(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    token = auth.start_session(uid)
    assert database.session_valid(token) is False
