import importlib

import pytest


@pytest.fixture
def mail(monkeypatch):
    import mail
    importlib.reload(mail)
    return mail


def test_without_an_api_key_the_link_is_logged(mail, monkeypatch, caplog):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    importlib.reload(mail)
    with caplog.at_level("WARNING"):
        sent = mail.send_reset_email("dev@studio.com", "https://x.test/reset/abc")
    assert sent is False
    assert "https://x.test/reset/abc" in caplog.text


def test_with_an_api_key_resend_is_called(mail, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("MAIL_FROM", "noreply@outbidarcade.lol")
    importlib.reload(mail)
    calls = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        calls["url"] = url
        calls["json"] = kwargs.get("json")
        calls["headers"] = kwargs.get("headers")
        return FakeResponse()

    monkeypatch.setattr(mail.httpx, "post", fake_post)
    assert mail.send_reset_email("dev@studio.com", "https://x.test/reset/abc") is True
    assert calls["url"] == "https://api.resend.com/emails"
    assert calls["json"]["to"] == ["dev@studio.com"]
    assert "https://x.test/reset/abc" in calls["json"]["text"]
    assert calls["headers"]["Authorization"] == "Bearer re_test_key"


def test_a_provider_failure_does_not_raise(mail, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    importlib.reload(mail)

    def fake_post(url, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(mail.httpx, "post", fake_post)
    assert mail.send_reset_email("dev@studio.com", "https://x.test/reset/abc") is False
