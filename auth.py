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
