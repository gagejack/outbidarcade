import importlib

import pytest


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://outbidarcade.lol")
    import oauth
    importlib.reload(oauth)
    return oauth


def test_no_credentials_means_no_providers(oauth, monkeypatch):
    for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
                 "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    importlib.reload(oauth)
    assert oauth.enabled_providers() == []
    assert oauth.is_enabled("google") is False


def test_credentials_enable_one_provider(oauth, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_CLIENT_SECRET", raising=False)
    importlib.reload(oauth)
    assert oauth.enabled_providers() == ["google"]


def test_authorize_url_carries_state_and_callback(oauth, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    importlib.reload(oauth)
    url = oauth.authorize_url("google", "state-value")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "state=state-value" in url
    assert "redirect_uri=https%3A%2F%2Foutbidarcade.lol%2Fauth%2Fgoogle%2Fcallback" in url
    assert "client_id=gid" in url


def test_authorize_url_for_a_disabled_provider_is_empty(oauth, monkeypatch):
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    importlib.reload(oauth)
    assert oauth.authorize_url("github", "s") == ""


def test_unknown_provider_is_never_enabled(oauth):
    assert oauth.is_enabled("facebook") is False
    assert oauth.authorize_url("facebook", "s") == ""


def test_google_profile_is_normalised(oauth, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    importlib.reload(oauth)

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            return FakeResponse({"access_token": "at"})

        def get(self, url, **kwargs):
            return FakeResponse({
                "sub": "1234567890",
                "email": "dev@studio.com",
                "email_verified": True,
                "name": "Dev Person",
            })

    monkeypatch.setattr(oauth.httpx, "Client", FakeClient)
    profile = oauth.fetch_profile("google", "the-code")
    assert profile == {
        "provider": "google",
        "uid": "1234567890",
        "email": "dev@studio.com",
        "email_verified": True,
        "name": "Dev Person",
    }


def test_github_reads_the_verified_primary_email(oauth, monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "hid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "hsecret")
    importlib.reload(oauth)

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            return FakeResponse({"access_token": "at"})

        def get(self, url, **kwargs):
            if url.endswith("/user/emails"):
                return FakeResponse([
                    {"email": "other@studio.com", "primary": False, "verified": True},
                    {"email": "dev@studio.com", "primary": True, "verified": True},
                ])
            return FakeResponse({"id": 42, "name": "Dev Person", "login": "devperson"})

    monkeypatch.setattr(oauth.httpx, "Client", FakeClient)
    profile = oauth.fetch_profile("github", "the-code")
    assert profile["uid"] == "42"
    assert profile["email"] == "dev@studio.com"
    assert profile["email_verified"] is True


def test_github_unverified_email_is_reported_as_unverified(oauth, monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_ID", "hid")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "hsecret")
    importlib.reload(oauth)

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            return FakeResponse({"access_token": "at"})

        def get(self, url, **kwargs):
            if url.endswith("/user/emails"):
                return FakeResponse([
                    {"email": "dev@studio.com", "primary": True, "verified": False},
                ])
            return FakeResponse({"id": 42, "name": "Dev", "login": "dev"})

    monkeypatch.setattr(oauth.httpx, "Client", FakeClient)
    assert oauth.fetch_profile("github", "the-code")["email_verified"] is False


def test_a_provider_failure_returns_none(oauth, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "gsecret")
    importlib.reload(oauth)

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            raise RuntimeError("provider down")

        def get(self, url, **kwargs):
            raise RuntimeError("provider down")

    monkeypatch.setattr(oauth.httpx, "Client", FakeClient)
    assert oauth.fetch_profile("google", "the-code") is None
