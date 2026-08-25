"""Google and GitHub sign-in, server-side Authorization Code flow.

The client secret is only ever sent server-to-server. This module holds no
database access: it turns a code into a normalised profile and stops there.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlencode

import httpx

log = logging.getLogger("outbid.oauth")

PROVIDERS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "userinfo": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
    "github": {
        "authorize": "https://github.com/login/oauth/authorize",
        "token": "https://github.com/login/oauth/access_token",
        "userinfo": "https://api.github.com/user",
        "emails": "https://api.github.com/user/emails",
        "scope": "read:user user:email",
    },
}


def _credentials(provider: str) -> tuple[str, str]:
    prefix = provider.upper()
    return (
        os.environ.get(f"{prefix}_CLIENT_ID", ""),
        os.environ.get(f"{prefix}_CLIENT_SECRET", ""),
    )


def base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")


def redirect_uri(provider: str) -> str:
    return f"{base_url()}/auth/{provider}/callback"


def is_enabled(provider: str) -> bool:
    if provider not in PROVIDERS:
        return False
    client_id, secret = _credentials(provider)
    return bool(client_id and secret)


def enabled_providers() -> list[str]:
    return [name for name in PROVIDERS if is_enabled(name)]


def authorize_url(provider: str, state: str) -> str:
    if not is_enabled(provider):
        return ""
    client_id, _ = _credentials(provider)
    conf = PROVIDERS[provider]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(provider),
        "scope": conf["scope"],
        "state": state,
        "response_type": "code",
    }
    return f"{conf['authorize']}?{urlencode(params)}"


def _exchange(client: httpx.Client, provider: str, code: str) -> str:
    client_id, secret = _credentials(provider)
    resp = client.post(
        PROVIDERS[provider]["token"],
        data={
            "client_id": client_id,
            "client_secret": secret,
            "code": code,
            "redirect_uri": redirect_uri(provider),
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json().get("access_token", "")


def fetch_profile(provider: str, code: str) -> dict | None:
    """Turn an authorization code into a normalised profile, or None."""
    if not is_enabled(provider):
        return None
    conf = PROVIDERS[provider]
    try:
        with httpx.Client(timeout=10) as client:
            token = _exchange(client, provider, code)
            if not token:
                return None
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            info = client.get(conf["userinfo"], headers=headers)
            info.raise_for_status()
            data = info.json()

            if provider == "google":
                return {
                    "provider": "google",
                    "uid": str(data.get("sub", "")),
                    "email": (data.get("email") or "").strip(),
                    "email_verified": bool(data.get("email_verified")),
                    "name": (data.get("name") or "").strip(),
                }

            emails = client.get(conf["emails"], headers=headers)
            emails.raise_for_status()
            entries = emails.json() or []
            primary = next(
                (e for e in entries if e.get("primary")),
                entries[0] if entries else {},
            )
            return {
                "provider": "github",
                "uid": str(data.get("id", "")),
                "email": (primary.get("email") or "").strip(),
                "email_verified": bool(primary.get("verified")),
                "name": (data.get("name") or data.get("login") or "").strip(),
            }
    except Exception:
        log.exception("OAuth profile fetch failed for %s", provider)
        return None
