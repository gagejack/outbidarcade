import importlib

import pytest


@pytest.fixture
def auth(app_modules):
    import auth
    importlib.reload(auth)
    return auth


def test_matching_pair_is_accepted(auth):
    token = auth.new_csrf()
    assert auth.csrf_ok(token, token) is True


def test_mismatched_pair_is_rejected(auth):
    assert auth.csrf_ok(auth.new_csrf(), auth.new_csrf()) is False


def test_missing_values_are_rejected(auth):
    token = auth.new_csrf()
    assert auth.csrf_ok(None, token) is False
    assert auth.csrf_ok(token, None) is False
    assert auth.csrf_ok(None, None) is False
    assert auth.csrf_ok("", "") is False


def test_tokens_are_unpredictable(auth):
    assert len({auth.new_csrf() for _ in range(100)}) == 100
