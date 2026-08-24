"""Outbound email.

Resend is used when RESEND_API_KEY is set. Without it the link is logged,
so password reset stays testable in development.
"""

from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("outbid.mail")

RESEND_URL = "https://api.resend.com/emails"
SUBJECT = "Reset your Outbid Arcade password"

BODY = """Someone asked to reset the password for this Outbid Arcade account.

Open this link to choose a new one. It expires in an hour and works once:

{url}

If this wasn't you, ignore this email. Nothing has changed.
"""


def send_reset_email(to_email: str, reset_url: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    sender = os.environ.get("MAIL_FROM", "noreply@outbidarcade.lol")
    if not api_key:
        log.warning("No RESEND_API_KEY set. Reset link for %s: %s", to_email, reset_url)
        return False
    try:
        resp = httpx.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": sender,
                "to": [to_email],
                "subject": SUBJECT,
                "text": BODY.format(url=reset_url),
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception:
        log.exception("Reset email to %s failed", to_email)
        return False
