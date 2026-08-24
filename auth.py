"""Accounts, sessions, reset tokens and CSRF.

Password hashing reuses db.hash_password (scrypt with a per-password salt);
this module owns the account and session shapes built on top of it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time

import db

MIN_PASSWORD = 10
RESET_TTL = 3600
DRAFT_TTL = 86400

# Session lifetime lives in db.SESSION_TTL (30 days), shared with operator
# sessions since both live in the same table. Referenced as db.SESSION_TTL.


def password_problem(password: str) -> str:
    if len(password or "") < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return ""


def create_user(email: str, password: str = "", display_name: str = "") -> int:
    now = int(time.time())
    pw_hash = db.hash_password(password) if password else ""
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO users(email, password_hash, display_name, created_at)"
            " VALUES(?,?,?,?)",
            (email.strip(), pw_hash, display_name.strip()[:60], now),
        )
    return int(cur.lastrowid)


def get_user(user_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email.strip(),)
        ).fetchone()
    return dict(row) if row else None


def check_login(email: str, password: str) -> dict | None:
    user = get_user_by_email(email)
    if not user or not user["password_hash"] or not password:
        return None
    if not db.verify_password(password, user["password_hash"]):
        return None
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET last_login_at=? WHERE id=?", (int(time.time()), user["id"])
        )
    return user


def set_password(user_id: int, password: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (db.hash_password(password), user_id),
        )


def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.connect() as conn:
        # Sweep on write, matching db.new_session(): a DELETE here keeps
        # read-only page views free of writes.
        conn.execute("DELETE FROM sessions WHERE created_at < ?", (now - db.SESSION_TTL,))
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at) VALUES(?,?,?)",
            (token, user_id, now),
        )
    return token


def user_for_session(token: str | None) -> dict | None:
    """Read-only: expiry is filtered in the SELECT, never swept here."""
    if not token:
        return None
    cutoff = int(time.time()) - db.SESSION_TTL
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token=? AND s.user_id IS NOT NULL AND s.created_at >= ?",
            (token, cutoff),
        ).fetchone()
    return dict(row) if row else None


def end_session(token: str | None) -> None:
    if not token:
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def end_all_sessions(user_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_reset(user_id: int) -> str:
    """Return the raw token. Only its hash is stored, so a database leak
    yields no usable reset links."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with db.connect() as conn:
        conn.execute("DELETE FROM reset_tokens WHERE expires_at < ?", (now,))
        conn.execute(
            "INSERT INTO reset_tokens(token_hash, user_id, expires_at) VALUES(?,?,?)",
            (_hash_token(token), user_id, now + RESET_TTL),
        )
    return token


def reset_is_live(token: str) -> bool:
    """True when the token would be accepted. Does not consume it."""
    if not token:
        return False
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM reset_tokens WHERE token_hash=?"
            " AND used_at IS NULL AND expires_at > ?",
            (_hash_token(token), int(time.time())),
        ).fetchone()
    return row is not None


def consume_reset(token: str) -> int | None:
    if not token:
        return None
    now = int(time.time())
    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM reset_tokens WHERE token_hash=?"
            " AND used_at IS NULL AND expires_at > ?",
            (_hash_token(token), now),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE reset_tokens SET used_at=? WHERE token_hash=?",
            (now, _hash_token(token)),
        )
    return int(row["user_id"])


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie_value: str | None, form_value: str | None) -> bool:
    if not cookie_value or not form_value:
        return False
    return secrets.compare_digest(cookie_value, form_value)


UNVERIFIED = (
    "That provider did not confirm your email address is verified. "
    "Sign in with a password first, then link the provider from your account page."
)


def _link_identity(user_id: int, provider: str, uid: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO identities(user_id, provider, provider_uid, created_at)"
            " VALUES(?,?,?,?)",
            (user_id, provider, uid, int(time.time())),
        )


def identities_for(user_id: int) -> list[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT provider FROM identities WHERE user_id=? ORDER BY provider",
            (user_id,),
        ).fetchall()
    return [r["provider"] for r in rows]


def user_from_profile(profile: dict) -> tuple[dict | None, str]:
    """Find or create the account behind an OAuth profile.

    Linking by email is only ever done when the provider says the address is
    verified. Auto-linking an unverified address is an account-takeover path:
    register the victim's address at a sloppy provider and inherit the account.
    """
    provider = profile.get("provider", "")
    uid = str(profile.get("uid", ""))
    email = (profile.get("email") or "").strip()

    if not provider or not uid:
        return None, "That sign-in did not complete. Try again."

    with db.connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM identities WHERE provider=? AND provider_uid=?",
            (provider, uid),
        ).fetchone()
    if row:
        found = get_user(int(row["user_id"]))
        if found:
            with db.connect() as conn:
                conn.execute(
                    "UPDATE users SET last_login_at=? WHERE id=?",
                    (int(time.time()), found["id"]),
                )
            return found, ""

    if not email:
        return None, "That provider did not share an email address."
    if not profile.get("email_verified"):
        return None, UNVERIFIED

    existing = get_user_by_email(email)
    if existing:
        _link_identity(existing["id"], provider, uid)
        return existing, ""

    user_id = create_user(email, "", profile.get("name", ""))
    _link_identity(user_id, provider, uid)
    return get_user(user_id), ""


def save_draft(payload: dict) -> str:
    """Park a validated submission while the visitor signs in.

    Server-side rather than a signed cookie: a SameSite=Lax cookie is not
    reliably returned on a cross-site OAuth callback, and a form carrying an
    image URL can exceed the 4KB cookie limit.
    """
    draft_id = secrets.token_urlsafe(24)
    now = int(time.time())
    with db.connect() as conn:
        conn.execute("DELETE FROM drafts WHERE created_at < ?", (now - DRAFT_TTL,))
        conn.execute(
            "INSERT INTO drafts(id, payload, created_at) VALUES(?,?,?)",
            (draft_id, json.dumps(payload), now),
        )
    return draft_id


def load_draft(draft_id: str | None) -> dict | None:
    if not draft_id:
        return None
    cutoff = int(time.time()) - DRAFT_TTL
    with db.connect() as conn:
        row = conn.execute(
            "SELECT payload FROM drafts WHERE id=? AND created_at >= ?",
            (draft_id, cutoff),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"])
    except ValueError:
        return None


def delete_draft(draft_id: str | None) -> None:
    if not draft_id:
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
