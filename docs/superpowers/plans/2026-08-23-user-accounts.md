# User Accounts and Listing Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace secret-URL listing ownership with real user accounts (Google OAuth, GitHub OAuth, email + password with reset), and let owners edit their listings.

**Architecture:** Three new modules beside the existing `db.py` / `main.py` pair. `auth.py` holds password hashing, sessions, and reset tokens. `oauth.py` holds the two providers and the Authorization Code exchange. `mail.py` holds outbound email. Routes live in `main.py` alongside the existing ones. Storage stays SQLite through the existing `connect()` helper. Half-finished submissions are parked in a `drafts` table so they survive an OAuth redirect.

**Tech Stack:** FastAPI, Jinja2, SQLite (stdlib `sqlite3`), `httpx` for provider calls, `pytest` for tests. Password hashing reuses the stdlib `hashlib.scrypt` wrapper already in `db.py`.

**Spec:** `docs/superpowers/specs/2026-08-23-user-accounts-design.md`

## Global Constraints

- Python 3.12 in the container (`Dockerfile` uses `python:3.12-slim`). Do not use syntax newer than 3.12.
- State lives ONLY under `/data` (`DATA_DIR` env var, defaults to `/data`). Never write state anywhere else.
- App serves on port 8080 in production. Tests use their own port.
- No secrets in the repo or in the database. Secrets arrive as environment variables only.
- Every new dependency must be added to `requirements.txt` with a `>=` floor.
- Public domain is `https://outbidarcade.lol`. OAuth callbacks are `https://outbidarcade.lol/auth/google/callback` and `https://outbidarcade.lol/auth/github/callback`.
- Session cookie name for users is `oa_user`. The operator cookie stays `oa_admin` and its behaviour must not change.
- Minimum password length is 10 characters, matching the existing operator rule in `main.py`.
- Every feature degrades when its env vars are missing: no provider credentials means the button is hidden; no `RESEND_API_KEY` means reset links are logged instead of emailed.
- The site is pre-launch. No migration path for `manage_token` is required or wanted.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `db.py` (modify) | Schema, and all SQL. New: users, identities, reset_tokens, drafts. Changed: listings, sessions. |
| `auth.py` (create) | Password hashing wrappers, user creation and lookup, session issue/verify/revoke, reset token issue/consume, CSRF tokens. No HTTP. |
| `oauth.py` (create) | Provider config from env, authorize-URL building, code exchange, profile normalisation. No database access. |
| `mail.py` (create) | `send_reset_email()`. Uses Resend when configured, otherwise logs. |
| `main.py` (modify) | All routes, cookie handling, template rendering, rate limiting. |
| `templates/login.html`, `register.html`, `forgot.html`, `reset.html`, `dashboard.html`, `account.html`, `edit.html` (create) | New pages. |
| `templates/base.html`, `manage.html`, `submit.html` (modify) | Header auth links; ownership-based manage page; submit copy. |
| `tests/conftest.py` (create) | pytest fixtures: throwaway DATA_DIR, TestClient, fake OAuth provider. |
| `tests/test_auth.py`, `test_oauth.py`, `test_drafts.py`, `test_listings.py` (create) | Unit and route tests. |
| `tests/smoke.py` (modify) | End-to-end pass over the new flow. |

The split keeps `oauth.py` free of database calls and `auth.py` free of HTTP, so both can be tested without a running server.

---

### Task 1: Test harness and dependencies

Nothing here is user-visible. It exists so every later task can write a failing test first. The current `tests/smoke.py` needs a live server and a real database; that is not a foundation for TDD.

**Files:**
- Modify: `requirements.txt`
- Create: `tests/conftest.py`
- Create: `tests/test_harness.py`

**Interfaces:**
- Consumes: nothing.
- Produces: pytest fixtures `client` (a `fastapi.testclient.TestClient` bound to a throwaway `DATA_DIR`) and `db_path` (the `Path` to that database).

- [ ] **Step 1: Add dependencies**

Append to `requirements.txt`:

```
httpx>=0.27
pytest>=8.0
```

`httpx` is used by `oauth.py` for provider calls and by `TestClient` internally. `pytest` is a test-only dependency but the project has no separate dev requirements file, so it goes here.

- [ ] **Step 2: Write the fixtures**

Create `tests/conftest.py`:

```python
"""Fixtures that give every test a private, empty database.

db.py reads DATA_DIR at import time, so the env var must be set and the
module reloaded before the app is imported.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def app_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://localhost:8080")
    import db

    importlib.reload(db)
    import main

    importlib.reload(main)
    db.init_db()
    return main, db


@pytest.fixture
def client(app_modules):
    from fastapi.testclient import TestClient

    main, _ = app_modules
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def database(app_modules):
    _, db = app_modules
    return db
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_harness.py`:

```python
def test_client_serves_the_board(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_each_test_gets_an_empty_database(client, database):
    assert database.board() == []


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_harness.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'httpx'` or `pytest` not installed, until Step 5.

- [ ] **Step 5: Install and re-run**

Run: `pip install -r requirements.txt && pytest tests/test_harness.py -v`
Expected: 3 passed.

If `TestClient` raises about a missing `httpx`, the install did not pick up the new lines — check `requirements.txt` was saved.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt tests/conftest.py tests/test_harness.py
git commit -m "test: add pytest harness with isolated per-test database"
```

---

### Task 2: Users, identities, and sessions in the schema

**Files:**
- Modify: `db.py:42-93` (the `init_db()` function)
- Create: `tests/test_schema.py`

**Interfaces:**
- Consumes: `db.connect()`, `db.init_db()`.
- Produces: tables `users`, `identities`, `reset_tokens`, `drafts`; column `listings.user_id`; column `sessions.user_id`; `listings.manage_token` removed.

- [ ] **Step 1: Write the failing test**

Create `tests/test_schema.py`:

```python
def columns(database, table):
    with database.connect() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_users_table_exists(database):
    assert {"id", "email", "password_hash", "display_name",
            "created_at", "last_login_at"} <= columns(database, "users")


def test_identities_table_exists(database):
    assert {"id", "user_id", "provider", "provider_uid",
            "created_at"} <= columns(database, "identities")


def test_reset_tokens_table_exists(database):
    assert {"token_hash", "user_id", "expires_at",
            "used_at"} <= columns(database, "reset_tokens")


def test_drafts_table_exists(database):
    assert {"id", "payload", "created_at"} <= columns(database, "drafts")


def test_listings_owned_by_user_not_token(database):
    cols = columns(database, "listings")
    assert "user_id" in cols
    assert "manage_token" not in cols


def test_sessions_carry_optional_user(database):
    assert "user_id" in columns(database, "sessions")


def test_email_is_unique_case_insensitively(database):
    import sqlite3
    now = 1
    with database.connect() as conn:
        conn.execute("INSERT INTO users(email, created_at) VALUES('a@b.com', ?)", (now,))
    try:
        with database.connect() as conn:
            conn.execute("INSERT INTO users(email, created_at) VALUES('A@B.COM', ?)", (now,))
    except sqlite3.IntegrityError:
        return
    raise AssertionError("duplicate email in different case was allowed")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_schema.py -v`
Expected: FAIL — `sqlite3.OperationalError: no such table: users`.

- [ ] **Step 3: Add the tables**

In `db.py`, inside `init_db()`'s `executescript` block, add after the existing `sessions` table:

```sql
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL DEFAULT '',
    display_name  TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER
);
CREATE TABLE IF NOT EXISTS identities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,
    provider_uid TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    UNIQUE(provider, provider_uid)
);
CREATE TABLE IF NOT EXISTS reset_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    used_at    INTEGER
);
CREATE TABLE IF NOT EXISTS drafts (
    id         TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identities_user ON identities(user_id);
CREATE INDEX IF NOT EXISTS idx_listings_user ON listings(user_id);
```

- [ ] **Step 4: Change the two existing tables**

In the same `executescript`, change the `listings` table definition: remove the `manage_token TEXT NOT NULL,` line and add `user_id INTEGER REFERENCES users(id),` in its place.

Change the `sessions` table definition to:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL
);
```

`CREATE TABLE IF NOT EXISTS` will not alter a table that already exists. Because the site is pre-launch, add this immediately before the `executescript` call to drop the stale shape:

```python
        # Pre-launch: the ownership model changed from a secret token to a
        # user account, so the old listings/sessions tables are dropped
        # rather than migrated. See docs/superpowers/specs/2026-08-23-user-accounts-design.md
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
        if cols and "manage_token" in cols:
            conn.executescript(
                "DROP TABLE IF EXISTS bids;"
                "DROP TABLE IF EXISTS events;"
                "DROP TABLE IF EXISTS listings;"
                "DROP TABLE IF EXISTS sessions;"
            )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_schema.py tests/test_harness.py -v`
Expected: 10 passed.

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_schema.py
git commit -m "feat: add users, identities, reset tokens and drafts tables"
```

---

### Task 3: Password and user helpers

**Files:**
- Create: `auth.py`
- Create: `tests/test_auth.py`

**Interfaces:**
- Consumes: `db.connect()`, `db.hash_password()`, `db.verify_password()`.
- Produces:
  - `create_user(email: str, password: str = "", display_name: str = "") -> int`
  - `get_user_by_email(email: str) -> dict | None`
  - `get_user(user_id: int) -> dict | None`
  - `check_login(email: str, password: str) -> dict | None`
  - `set_password(user_id: int, password: str) -> None`
  - `password_problem(password: str) -> str` — returns "" when acceptable

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'auth'`.

- [ ] **Step 3: Write the implementation**

Create `auth.py`:

```python
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
SESSION_DAYS = 30
RESET_TTL = 3600
DRAFT_TTL = 86400


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
```

Note the deliberate ordering in `check_login`: a missing user, an empty stored hash, and a wrong password all return `None` the same way, so the caller cannot tell them apart.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_auth.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add auth.py tests/test_auth.py
git commit -m "feat: add user accounts and password checking"
```

---

### Task 4: User sessions

**Files:**
- Modify: `auth.py`
- Modify: `db.py:146-169` (`new_session`, `session_valid`, `drop_session`)
- Create: `tests/test_sessions.py`

**Interfaces:**
- Consumes: Task 3's `create_user`.
- Produces:
  - `start_session(user_id: int) -> str`
  - `user_for_session(token: str | None) -> dict | None`
  - `end_session(token: str | None) -> None`
  - `end_all_sessions(user_id: int) -> None`
- `db.new_session()` keeps its current no-argument signature so the operator flow is untouched.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sessions.py`:

```python
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
    stale = int(time.time()) - (auth.SESSION_DAYS * 86400) - 60
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
```

The last two matter: one `sessions` table serves both, so a user session must never grant operator access and vice versa.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_sessions.py -v`
Expected: FAIL — `AttributeError: module 'auth' has no attribute 'start_session'`.

- [ ] **Step 3: Separate operator sessions from user sessions in `db.py`**

Replace `db.new_session()` and `db.session_valid()` with:

```python
def new_session() -> str:
    """An operator session. User sessions are auth.start_session()."""
    token = secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at) VALUES(?, NULL, ?)",
            (token, int(time.time())),
        )
    return token


def session_valid(token: str | None) -> bool:
    """True only for operator sessions - user_id IS NULL."""
    if not token:
        return False
    cutoff = int(time.time()) - 60 * 60 * 24 * 30
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
        row = conn.execute(
            "SELECT token FROM sessions WHERE token=? AND user_id IS NULL", (token,)
        ).fetchone()
    return row is not None
```

- [ ] **Step 4: Add the user session helpers to `auth.py`**

```python
def start_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at) VALUES(?,?,?)",
            (token, user_id, int(time.time())),
        )
    return token


def user_for_session(token: str | None) -> dict | None:
    if not token:
        return None
    cutoff = int(time.time()) - SESSION_DAYS * 86400
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token=? AND s.user_id IS NOT NULL",
            (token,),
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
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -v`
Expected: all passed, including the earlier files.

- [ ] **Step 6: Commit**

```bash
git add auth.py db.py tests/test_sessions.py
git commit -m "feat: add user sessions, kept distinct from operator sessions"
```

---

### Task 5: Reset tokens

**Files:**
- Modify: `auth.py`
- Create: `tests/test_reset.py`

**Interfaces:**
- Consumes: Tasks 3 and 4.
- Produces:
  - `issue_reset(user_id: int) -> str` — returns the raw token; only the hash is stored
  - `consume_reset(token: str) -> int | None` — returns `user_id`, marks used

- [ ] **Step 1: Write the failing test**

Create `tests/test_reset.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_reset.py -v`
Expected: FAIL — `AttributeError: module 'auth' has no attribute 'issue_reset'`.

- [ ] **Step 3: Implement**

Add to `auth.py`:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_reset.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add auth.py tests/test_reset.py
git commit -m "feat: add single-use, hashed, expiring reset tokens"
```

---

### Task 6: CSRF tokens

The existing forms have no CSRF protection. `samesite=lax` does not cover top-level POST navigation, and this task adds account-changing POSTs, so the protection is added now and applied to every form.

**Files:**
- Modify: `auth.py`
- Create: `tests/test_csrf.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `new_csrf() -> str`
  - `csrf_ok(cookie_value: str | None, form_value: str | None) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_csrf.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_csrf.py -v`
Expected: FAIL — `AttributeError: module 'auth' has no attribute 'new_csrf'`.

- [ ] **Step 3: Implement**

Add to `auth.py`:

```python
def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie_value: str | None, form_value: str | None) -> bool:
    if not cookie_value or not form_value:
        return False
    return secrets.compare_digest(cookie_value, form_value)
```

This is the double-submit cookie pattern: the same random value is placed in a cookie and in a hidden form field. A cross-site attacker can force the browser to send the cookie but cannot read it to populate the field.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_csrf.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add auth.py tests/test_csrf.py
git commit -m "feat: add double-submit CSRF tokens"
```

---

### Task 7: Mail sending

**Files:**
- Create: `mail.py`
- Create: `tests/test_mail.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `send_reset_email(to_email: str, reset_url: str) -> bool` — `True` when handed to Resend, `False` when logged instead.

- [ ] **Step 1: Write the failing test**

Create `tests/test_mail.py`:

```python
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
```

The last test matters: a mail outage must not turn the forgot-password page into a 500, because that would also reveal which addresses exist.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_mail.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mail'`.

- [ ] **Step 3: Implement**

Create `mail.py`:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mail.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add mail.py tests/test_mail.py
git commit -m "feat: send reset email via Resend, log the link when unconfigured"
```

---

### Task 8: OAuth providers

**Files:**
- Create: `oauth.py`
- Create: `tests/test_oauth.py`

**Interfaces:**
- Consumes: nothing. This module never touches the database.
- Produces:
  - `enabled_providers() -> list[str]`
  - `is_enabled(provider: str) -> bool`
  - `authorize_url(provider: str, state: str) -> str`
  - `fetch_profile(provider: str, code: str) -> dict | None` — returns `{"provider": str, "uid": str, "email": str, "email_verified": bool, "name": str}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_oauth.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_oauth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'oauth'`.

- [ ] **Step 3: Implement**

Create `oauth.py`:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_oauth.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add oauth.py tests/test_oauth.py
git commit -m "feat: add Google and GitHub authorization code flow"
```

---

### Task 9: Identity linking

**Files:**
- Modify: `auth.py`
- Create: `tests/test_linking.py`

**Interfaces:**
- Consumes: Task 3's user helpers, Task 8's profile shape.
- Produces: `user_from_profile(profile: dict) -> tuple[dict | None, str]` — `(user, "")` on success, `(None, message)` on refusal.

- [ ] **Step 1: Write the failing test**

Create `tests/test_linking.py`:

```python
import importlib

import pytest


@pytest.fixture
def auth(app_modules):
    import auth
    importlib.reload(auth)
    return auth


def profile(**over):
    base = {
        "provider": "google",
        "uid": "1234",
        "email": "dev@studio.com",
        "email_verified": True,
        "name": "Dev Person",
    }
    base.update(over)
    return base


def test_new_verified_profile_creates_a_user(auth):
    user, error = auth.user_from_profile(profile())
    assert error == ""
    assert user["email"] == "dev@studio.com"
    assert user["password_hash"] == ""


def test_returning_profile_finds_the_same_user(auth):
    first, _ = auth.user_from_profile(profile())
    second, _ = auth.user_from_profile(profile())
    assert first["id"] == second["id"]


def test_verified_email_links_to_an_existing_password_account(auth):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    user, error = auth.user_from_profile(profile())
    assert error == ""
    assert user["id"] == uid


def test_unverified_email_never_links(auth):
    auth.create_user("dev@studio.com", "correct horse battery")
    user, error = auth.user_from_profile(profile(email_verified=False))
    assert user is None
    assert "verified" in error.lower()


def test_unverified_email_never_creates(auth):
    user, error = auth.user_from_profile(profile(email_verified=False))
    assert user is None
    assert auth.get_user_by_email("dev@studio.com") is None


def test_missing_email_is_refused(auth):
    user, error = auth.user_from_profile(profile(email=""))
    assert user is None
    assert error != ""


def test_identity_survives_a_provider_email_change(auth):
    first, _ = auth.user_from_profile(profile())
    second, error = auth.user_from_profile(profile(email="new@studio.com"))
    assert error == ""
    assert second["id"] == first["id"]


def test_two_providers_link_to_one_account(auth):
    google, _ = auth.user_from_profile(profile(provider="google", uid="g1"))
    github, _ = auth.user_from_profile(profile(provider="github", uid="h1"))
    assert google["id"] == github["id"]
```

`test_identity_survives_a_provider_email_change` is why `provider_uid` is the key rather than the email.

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_linking.py -v`
Expected: FAIL — `AttributeError: module 'auth' has no attribute 'user_from_profile'`.

- [ ] **Step 3: Implement**

Add to `auth.py`:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_linking.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add auth.py tests/test_linking.py
git commit -m "feat: link OAuth identities, refusing unverified provider emails"
```

---

### Task 10: Draft storage

**Files:**
- Modify: `auth.py`
- Create: `tests/test_drafts.py`

**Interfaces:**
- Consumes: nothing beyond `db.connect()`.
- Produces:
  - `save_draft(payload: dict) -> str`
  - `load_draft(draft_id: str | None) -> dict | None`
  - `delete_draft(draft_id: str | None) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_drafts.py`:

```python
import importlib
import time

import pytest


@pytest.fixture
def auth(app_modules):
    import auth
    importlib.reload(auth)
    return auth


FORM = {
    "title": "Ghost Signal",
    "tagline": "A submarine horror game played by sonar alone",
    "url": "https://ghostsignal.example",
    "image_url": "",
    "studio": "Two people and a cat",
    "email": "dev@studio.com",
    "amount": "12",
    "platforms": "PC,VR",
}


def test_draft_round_trips(auth):
    assert auth.load_draft(auth.save_draft(FORM)) == FORM


def test_unknown_draft_is_none(auth):
    assert auth.load_draft("nope") is None
    assert auth.load_draft(None) is None


def test_deleted_draft_is_gone(auth):
    draft_id = auth.save_draft(FORM)
    auth.delete_draft(draft_id)
    assert auth.load_draft(draft_id) is None


def test_expired_draft_is_not_returned(auth, database):
    draft_id = auth.save_draft(FORM)
    stale = int(time.time()) - auth.DRAFT_TTL - 60
    with database.connect() as conn:
        conn.execute("UPDATE drafts SET created_at=? WHERE id=?", (stale, draft_id))
    assert auth.load_draft(draft_id) is None


def test_draft_ids_are_unpredictable(auth):
    assert len({auth.save_draft(FORM) for _ in range(50)}) == 50
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_drafts.py -v`
Expected: FAIL — `AttributeError: module 'auth' has no attribute 'save_draft'`.

- [ ] **Step 3: Implement**

Add to `auth.py`:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_drafts.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add auth.py tests/test_drafts.py
git commit -m "feat: park half-finished submissions in a drafts table"
```

---

### Task 11: Listing ownership queries

**Files:**
- Modify: `db.py` (`create_listing`, `get_listing_by_token` removed, new helpers)
- Create: `tests/test_ownership.py`

**Interfaces:**
- Consumes: Task 2's `listings.user_id`.
- Produces:
  - `create_listing(data: dict, amount: int, user_id: int) -> dict` — returns `{"id": int}`; the `token` key is gone
  - `listings_for_user(user_id: int) -> list[dict]`
  - `owns_listing(user_id: int, listing_id: int) -> bool`
  - `update_listing(listing_id: int, data: dict) -> None`
- `get_listing_by_token()` is deleted.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ownership.py`:

```python
import importlib

import pytest


@pytest.fixture
def auth(app_modules):
    import auth
    importlib.reload(auth)
    return auth


FORM = {
    "title": "Ghost Signal",
    "tagline": "A submarine horror game played by sonar alone",
    "url": "https://ghostsignal.example",
    "image_url": "",
    "studio": "Two people and a cat",
    "email": "dev@studio.com",
    "platforms": "PC,VR",
}


def test_listing_records_its_owner(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    assert database.get_listing(created["id"])["user_id"] == uid


def test_created_listing_has_no_token(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    assert "token" not in database.create_listing(FORM, 12, uid)


def test_owner_owns_their_listing(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    assert database.owns_listing(uid, created["id"]) is True


def test_stranger_does_not_own_it(auth, database):
    owner = auth.create_user("dev@studio.com", "correct horse battery")
    other = auth.create_user("someone@else.com", "correct horse battery")
    created = database.create_listing(FORM, 12, owner)
    assert database.owns_listing(other, created["id"]) is False


def test_ownership_of_a_missing_listing_is_false(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    assert database.owns_listing(uid, 999) is False


def test_user_listings_are_listed_newest_first(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    first = database.create_listing(dict(FORM, title="First"), 12, uid)
    second = database.create_listing(dict(FORM, title="Second"), 12, uid)
    ids = [row["id"] for row in database.listings_for_user(uid)]
    assert ids == [second["id"], first["id"]]


def test_user_listings_exclude_other_owners(auth, database):
    owner = auth.create_user("dev@studio.com", "correct horse battery")
    other = auth.create_user("someone@else.com", "correct horse battery")
    database.create_listing(FORM, 12, owner)
    assert database.listings_for_user(other) == []


def test_update_changes_fields_and_slug(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    database.update_listing(created["id"], dict(FORM, title="Deep Signal",
                                                tagline="Now with more sonar and dread"))
    listing = database.get_listing(created["id"])
    assert listing["title"] == "Deep Signal"
    assert listing["slug"] == "deep-signal"


def test_update_does_not_touch_money(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    bid = database.bids_for(created["id"])[0]
    database.confirm_bid(bid["id"])
    before = database.get_listing(created["id"])["total"]
    database.update_listing(created["id"], dict(FORM, title="Deep Signal"))
    assert database.get_listing(created["id"])["total"] == before
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_ownership.py -v`
Expected: FAIL — `TypeError: create_listing() takes 2 positional arguments but 3 were given`.

- [ ] **Step 3: Rewrite `create_listing` and delete the token lookup**

In `db.py`, replace `create_listing` with:

```python
def create_listing(data: dict, amount: int, user_id: int) -> dict:
    now = int(time.time())
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO listings(slug, title, tagline, url, image_url, studio, platforms,"
            " email, user_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                slugify(data["title"]),
                data["title"],
                data["tagline"],
                data["url"],
                data.get("image_url", ""),
                data.get("studio", ""),
                data.get("platforms", ""),
                data.get("email", ""),
                user_id,
                now,
            ),
        )
        listing_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO bids(listing_id, amount, status, created_at) VALUES(?,?,'pending',?)",
            (listing_id, amount, now),
        )
    return {"id": listing_id}
```

Delete `get_listing_by_token()` entirely.

- [ ] **Step 4: Add the ownership helpers**

```python
def owns_listing(user_id: int, listing_id: int) -> bool:
    if not user_id or not listing_id:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM listings WHERE id=? AND user_id=?", (listing_id, user_id)
        ).fetchone()
    return row is not None


def listings_for_user(user_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.*, COALESCE(SUM(CASE WHEN b.status='confirmed' THEN b.amount END), 0)"
            " AS total, COUNT(CASE WHEN b.status='pending' THEN 1 END) AS pending_count"
            " FROM listings l LEFT JOIN bids b ON b.listing_id=l.id"
            " WHERE l.user_id=? GROUP BY l.id ORDER BY l.id DESC",
            (user_id,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["platform_list"] = [p for p in item["platforms"].split(",") if p]
        out.append(item)
    return out


def update_listing(listing_id: int, data: dict) -> None:
    """Editable fields only. Money and status are never touched here."""
    with connect() as conn:
        conn.execute(
            "UPDATE listings SET slug=?, title=?, tagline=?, url=?, image_url=?,"
            " studio=?, platforms=? WHERE id=?",
            (
                slugify(data["title"]),
                data["title"],
                data["tagline"],
                data["url"],
                data.get("image_url", ""),
                data.get("studio", ""),
                data.get("platforms", ""),
                listing_id,
            ),
        )
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_ownership.py -v`
Expected: 9 passed.

`pytest tests/ -v` will now fail in `main.py` because `/submit` still calls the old `create_listing`. Task 13 fixes that. If you want a green suite between tasks, run only the files named in each task.

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_ownership.py
git commit -m "feat: own listings by user id, drop the manage token lookup"
```

---

### Task 12: Auth routes and pages

**Files:**
- Modify: `main.py`
- Modify: `templates/base.html`
- Create: `templates/login.html`, `templates/register.html`, `templates/forgot.html`, `templates/reset.html`
- Create: `tests/test_auth_routes.py`

**Interfaces:**
- Consumes: Tasks 3-9.
- Produces: `current_user(request)`, `set_session_cookie(resp, token)`, `csrf_field(request)`; routes `/register`, `/login`, `/logout`, `/forgot`, `/reset/{token}`, `/auth/{provider}`, `/auth/{provider}/callback`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_auth_routes.py`:

```python
def register(client, email="dev@studio.com", password="correct horse battery"):
    page = client.get("/register")
    token = extract_csrf(page.text)
    return client.post(
        "/register",
        data={"email": email, "password": password, "confirm": password,
              "display_name": "Dev", "csrf": token},
        follow_redirects=False,
    )


def extract_csrf(html):
    import re
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field in form"
    return match.group(1)


def test_register_creates_a_session(client):
    resp = register(client)
    assert resp.status_code == 303
    assert client.cookies.get("oa_user")


def test_register_rejects_a_short_password(client):
    page = client.get("/register")
    resp = client.post("/register", data={
        "email": "dev@studio.com", "password": "short", "confirm": "short",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 400


def test_register_rejects_mismatched_confirmation(client):
    page = client.get("/register")
    resp = client.post("/register", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "confirm": "something else entirely",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 400


def test_register_rejects_a_duplicate_email(client):
    register(client)
    client.post("/logout", data={"csrf": "x"}, follow_redirects=False)
    page = client.get("/register")
    resp = client.post("/register", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "confirm": "correct horse battery",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 400


def test_register_without_csrf_is_refused(client):
    client.get("/register")
    resp = client.post("/register", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "confirm": "correct horse battery"}, follow_redirects=False)
    assert resp.status_code == 400


def test_login_then_logout(client):
    register(client)
    page = client.get("/login")
    resp = client.post("/login", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 303
    logout = client.post("/logout", data={"csrf": extract_csrf(page.text)},
                         follow_redirects=False)
    assert logout.status_code == 303


def test_login_with_a_wrong_password_fails(client):
    register(client)
    page = client.get("/login")
    resp = client.post("/login", data={
        "email": "dev@studio.com", "password": "not the password",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 401


def test_login_page_hides_providers_when_unconfigured(client):
    assert "Continue with Google" not in client.get("/login").text


def test_forgot_says_the_same_thing_for_any_address(client):
    register(client)
    page = client.get("/forgot")
    known = client.post("/forgot", data={"email": "dev@studio.com",
                                         "csrf": extract_csrf(page.text)})
    unknown = client.post("/forgot", data={"email": "nobody@nowhere.com",
                                           "csrf": extract_csrf(page.text)})
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text


def test_reset_sets_a_new_password_and_kills_sessions(client, app_modules):
    main, _ = app_modules
    import auth
    register(client)
    user = auth.get_user_by_email("dev@studio.com")
    token = auth.issue_reset(user["id"])
    page = client.get(f"/reset/{token}")
    assert page.status_code == 200
    resp = client.post(f"/reset/{token}", data={
        "password": "a completely new secret", "confirm": "a completely new secret",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 303
    assert auth.check_login("dev@studio.com", "correct horse battery") is None
    assert auth.check_login("dev@studio.com", "a completely new secret")


def test_a_used_reset_link_stops_working(client, app_modules):
    import auth
    register(client)
    user = auth.get_user_by_email("dev@studio.com")
    token = auth.issue_reset(user["id"])
    page = client.get(f"/reset/{token}")
    client.post(f"/reset/{token}", data={
        "password": "a completely new secret", "confirm": "a completely new secret",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert client.get(f"/reset/{token}").status_code == 404


def test_oauth_start_is_404_when_provider_is_off(client):
    assert client.get("/auth/google", follow_redirects=False).status_code == 404


def test_unknown_provider_is_404(client):
    assert client.get("/auth/facebook", follow_redirects=False).status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_auth_routes.py -v`
Expected: FAIL — 404 on `/register`.

- [ ] **Step 3: Add helpers to `main.py`**

Add the imports and helpers near the top, after `import db`:

```python
import secrets

import auth
import mail
import oauth

USER_COOKIE = "oa_user"
CSRF_COOKIE = "oa_csrf"
DRAFT_COOKIE = "oa_draft"
STATE_COOKIE = "oa_state"


def secure_cookies() -> bool:
    return oauth.base_url().startswith("https://")


def current_user(request: Request) -> dict | None:
    return auth.user_for_session(request.cookies.get(USER_COOKIE))


def set_session_cookie(resp, token: str) -> None:
    resp.set_cookie(USER_COOKIE, token, httponly=True, samesite="lax",
                    secure=secure_cookies(), max_age=auth.SESSION_DAYS * 86400)


def csrf_for(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or auth.new_csrf()


def csrf_valid(request: Request, form_value: str) -> bool:
    return auth.csrf_ok(request.cookies.get(CSRF_COOKIE), form_value)
```

Extend `render()` so every template can see the user, the CSRF token and the providers. Replace the existing `render` with:

```python
def render(request: Request, name: str, ctx: dict | None = None, status: int = 200):
    user = ctx.pop("_user", None) if ctx else None
    if user is None:
        user = current_user(request)
    token = csrf_for(request)
    data = {
        "request": request,
        "v": ASSET_VERSION,
        "site_name": SITE_NAME,
        "tagline": TAGLINE,
        "platforms": PLATFORMS,
        "min_first": db.MIN_FIRST_BID,
        "min_top_up": db.MIN_TOP_UP,
        "is_admin": db.session_valid(request.cookies.get("oa_admin")),
        "user": user,
        "csrf": token,
        "providers": oauth.enabled_providers(),
    }
    data.update(ctx or {})
    resp = templates.TemplateResponse(request, name, data, status_code=status)
    resp.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="lax",
                    secure=secure_cookies(), max_age=86400)
    return resp
```

- [ ] **Step 4: Add the routes**

```python
@app.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "register.html", {"error": None, "form": {}})


@app.post("/register", response_class=HTMLResponse)
async def register(request: Request):
    raw = await request.form()
    email = str(raw.get("email", "")).strip()[:120]
    password = str(raw.get("password", ""))
    confirm = str(raw.get("confirm", ""))
    display_name = str(raw.get("display_name", "")).strip()[:60]
    form = {"email": email, "display_name": display_name}

    def fail(msg: str, status: int = 400):
        return render(request, "register.html", {"error": msg, "form": form}, status=status)

    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return fail("That form expired. Try again.")
    if rate_limited(request, "register", 5, 3600):
        return fail("Too many sign-ups from here. Try again later.", 429)
    if "@" not in email or "." not in email.split("@")[-1]:
        return fail("That does not look like an email address.")
    problem = auth.password_problem(password)
    if problem:
        return fail(problem)
    if password != confirm:
        return fail("The two passwords do not match.")
    if auth.get_user_by_email(email):
        return fail("There is already an account with that address. Try signing in.")

    user_id = auth.create_user(email, password, display_name)
    resp = RedirectResponse(next_after_login(request), status_code=303)
    set_session_cookie(resp, auth.start_session(user_id))
    return resp


def next_after_login(request: Request) -> str:
    """Send a visitor with a parked submission back to finish it."""
    if request.cookies.get(DRAFT_COOKIE):
        return "/submit/resume"
    return "/dashboard"


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", {"error": None, "email": ""})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    raw = await request.form()
    email = str(raw.get("email", "")).strip()[:120]
    password = str(raw.get("password", ""))

    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return render(request, "login.html",
                      {"error": "That form expired. Try again.", "email": email}, status=400)
    if rate_limited(request, "login", 10, 900):
        return render(request, "login.html",
                      {"error": "Too many attempts. Wait 15 minutes.", "email": email},
                      status=429)
    user = auth.check_login(email, password)
    if not user:
        return render(request, "login.html",
                      {"error": "Wrong email or password.", "email": email}, status=401)
    resp = RedirectResponse(next_after_login(request), status_code=303)
    set_session_cookie(resp, auth.start_session(user["id"]))
    return resp


@app.post("/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(USER_COOKIE))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(USER_COOKIE)
    return resp


@app.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return render(request, "forgot.html", {"error": None, "sent": False})


@app.post("/forgot", response_class=HTMLResponse)
async def forgot(request: Request):
    raw = await request.form()
    email = str(raw.get("email", "")).strip()[:120]
    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return render(request, "forgot.html",
                      {"error": "That form expired. Try again.", "sent": False}, status=400)
    if not rate_limited(request, "forgot", 5, 3600):
        user = auth.get_user_by_email(email)
        if user:
            token = auth.issue_reset(user["id"])
            mail.send_reset_email(user["email"], f"{oauth.base_url()}/reset/{token}")
    # The same response either way, so this page cannot be used to discover
    # which addresses have accounts.
    return render(request, "forgot.html", {"error": None, "sent": True})


@app.get("/reset/{token}", response_class=HTMLResponse)
def reset_form(request: Request, token: str):
    if not auth.reset_is_live(token):
        return render(request, "notfound.html", {}, status=404)
    return render(request, "reset.html", {"error": None, "token": token})


@app.post("/reset/{token}", response_class=HTMLResponse)
async def reset(request: Request, token: str):
    raw = await request.form()
    password = str(raw.get("password", ""))
    confirm = str(raw.get("confirm", ""))

    def fail(msg: str):
        return render(request, "reset.html", {"error": msg, "token": token}, status=400)

    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return fail("That form expired. Try again.")
    problem = auth.password_problem(password)
    if problem:
        return fail(problem)
    if password != confirm:
        return fail("The two passwords do not match.")

    user_id = auth.consume_reset(token)
    if not user_id:
        return render(request, "notfound.html", {}, status=404)
    auth.set_password(user_id, password)
    auth.end_all_sessions(user_id)
    resp = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(resp, auth.start_session(user_id))
    return resp


@app.get("/auth/{provider}")
def oauth_start(request: Request, provider: str):
    if not oauth.is_enabled(provider):
        return render(request, "notfound.html", {}, status=404)
    if rate_limited(request, "oauth", 20, 3600):
        return render(request, "login.html",
                      {"error": "Too many attempts. Try again later.", "email": ""},
                      status=429)
    state = secrets.token_urlsafe(24)
    resp = RedirectResponse(oauth.authorize_url(provider, state), status_code=303)
    resp.set_cookie(STATE_COOKIE, f"{provider}:{state}", httponly=True,
                    samesite="lax", secure=secure_cookies(), max_age=600)
    return resp


@app.get("/auth/{provider}/callback")
def oauth_callback(request: Request, provider: str, code: str = "", state: str = ""):
    if not oauth.is_enabled(provider):
        return render(request, "notfound.html", {}, status=404)

    def fail(msg: str, status: int = 400):
        return render(request, "login.html", {"error": msg, "email": ""}, status=status)

    expected = request.cookies.get(STATE_COOKIE, "")
    if not state or expected != f"{provider}:{state}":
        return fail("That sign-in could not be verified. Start again.")
    if not code:
        return fail("That sign-in did not complete. Try again.")

    profile = oauth.fetch_profile(provider, code)
    if not profile:
        return fail("That provider could not be reached. Try again.")
    user, error = auth.user_from_profile(profile)
    if not user:
        return fail(error)

    resp = RedirectResponse(next_after_login(request), status_code=303)
    set_session_cookie(resp, auth.start_session(user["id"]))
    resp.delete_cookie(STATE_COOKIE)
    return resp
```

- [ ] **Step 5: Add `reset_is_live` to `auth.py`**

The GET handler must not consume the token:

```python
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
```

- [ ] **Step 6: Write the templates**

Create `templates/login.html`:

```html
{% extends "base.html" %}
{% block title %}Sign in · {{ site_name }}{% endblock %}
{% block body %}
<main class="page narrow">
  <p class="kicker">Your account</p>
  <h1>Sign in</h1>
  {% if error %}<p class="alert">{{ error }}</p>{% endif %}

  {% if providers %}
  <div class="card provider-card">
    {% if 'google' in providers %}
    <a class="btn btn-provider" href="/auth/google">Continue with Google</a>
    {% endif %}
    {% if 'github' in providers %}
    <a class="btn btn-provider" href="/auth/github">Continue with GitHub</a>
    {% endif %}
    <p class="muted tiny center">or use your email</p>
  </div>
  {% endif %}

  <form class="card form" method="post" action="/login">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>Email
      <input type="email" name="email" required autocomplete="email" value="{{ email or '' }}">
    </label>
    <label>Password
      <input type="password" name="password" required autocomplete="current-password">
    </label>
    <button class="btn btn-big" type="submit">Sign in</button>
    <p class="tiny muted"><a href="/forgot">Forgot your password?</a> ·
    <a href="/register">Create an account</a></p>
  </form>
</main>
{% endblock %}
```

Create `templates/register.html`:

```html
{% extends "base.html" %}
{% block title %}Create an account · {{ site_name }}{% endblock %}
{% block body %}
<main class="page narrow">
  <p class="kicker">Your account</p>
  <h1>Create an account</h1>
  <p class="lede">One account holds every game you list.</p>
  {% if error %}<p class="alert">{{ error }}</p>{% endif %}

  {% if providers %}
  <div class="card provider-card">
    {% if 'google' in providers %}
    <a class="btn btn-provider" href="/auth/google">Continue with Google</a>
    {% endif %}
    {% if 'github' in providers %}
    <a class="btn btn-provider" href="/auth/github">Continue with GitHub</a>
    {% endif %}
    <p class="muted tiny center">or use your email</p>
  </div>
  {% endif %}

  <form class="card form" method="post" action="/register">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>Email
      <input type="email" name="email" required autocomplete="email" value="{{ form.email or '' }}">
    </label>
    <label>Name <span class="opt">optional</span>
      <input name="display_name" maxlength="60" value="{{ form.display_name or '' }}" placeholder="Two people and a cat">
    </label>
    <label>Password
      <input type="password" name="password" required autocomplete="new-password">
      <span class="hint">At least 10 characters.</span>
    </label>
    <label>Confirm password
      <input type="password" name="confirm" required autocomplete="new-password">
    </label>
    <button class="btn btn-big" type="submit">Create account</button>
    <p class="tiny muted">Already have one? <a href="/login">Sign in</a>.</p>
  </form>
</main>
{% endblock %}
```

Create `templates/forgot.html`:

```html
{% extends "base.html" %}
{% block title %}Reset your password · {{ site_name }}{% endblock %}
{% block body %}
<main class="page narrow">
  <p class="kicker">Your account</p>
  <h1>Reset your password</h1>
  {% if error %}<p class="alert">{{ error }}</p>{% endif %}
  {% if sent %}
    <div class="card">
      <p>If there is an account with that address, a reset link is on its way. It
      expires in an hour and works once.</p>
      <p class="muted small">Nothing arrived? Check spam, then
      <a href="/forgot">try again</a>.</p>
    </div>
  {% else %}
    <form class="card form" method="post" action="/forgot">
      <input type="hidden" name="csrf" value="{{ csrf }}">
      <label>Email
        <input type="email" name="email" required autocomplete="email">
      </label>
      <button class="btn btn-big" type="submit">Send the link</button>
      <p class="tiny muted"><a href="/login">Back to sign in</a></p>
    </form>
  {% endif %}
</main>
{% endblock %}
```

Create `templates/reset.html`:

```html
{% extends "base.html" %}
{% block title %}Choose a new password · {{ site_name }}{% endblock %}
{% block body %}
<main class="page narrow">
  <p class="kicker">Your account</p>
  <h1>Choose a new password</h1>
  {% if error %}<p class="alert">{{ error }}</p>{% endif %}
  <form class="card form" method="post" action="/reset/{{ token }}">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>New password
      <input type="password" name="password" required autocomplete="new-password">
      <span class="hint">At least 10 characters.</span>
    </label>
    <label>Confirm password
      <input type="password" name="confirm" required autocomplete="new-password">
    </label>
    <button class="btn btn-big" type="submit">Save and sign in</button>
    <p class="tiny muted">Saving this signs out every other device.</p>
  </form>
</main>
{% endblock %}
```

- [ ] **Step 7: Update the header**

In `templates/base.html`, replace the `<nav>` block:

```html
  <nav>
    <a href="/">Board</a>
    <a href="/rules">Rules</a>
    {% if is_admin %}<a href="/admin">Admin</a>{% endif %}
    {% if user %}
      <a href="/dashboard">My games</a>
      <form class="inline-logout" method="post" action="/logout">
        <input type="hidden" name="csrf" value="{{ csrf }}">
        <button class="linkish" type="submit">Sign out</button>
      </form>
    {% else %}
      <a href="/login">Sign in</a>
    {% endif %}
    <a class="btn btn-small" href="/submit">List a game</a>
  </nav>
```

- [ ] **Step 8: Add the styles**

Append to `static/style.css`:

```css
.provider-card { display: grid; gap: .6rem; }
.btn-provider { display: block; text-align: center; }
.inline-logout { display: inline; }
.linkish { background: none; border: 0; padding: 0; font: inherit;
           color: inherit; cursor: pointer; text-decoration: underline; }
```

- [ ] **Step 9: Run tests**

Run: `pytest tests/test_auth_routes.py -v`
Expected: 13 passed.

- [ ] **Step 10: Commit**

```bash
git add main.py auth.py templates/ static/style.css tests/test_auth_routes.py
git commit -m "feat: add sign-in, registration, reset and OAuth routes"
```

---

### Task 13: Submit flow with drafts

**Files:**
- Modify: `main.py` (the `/submit` handlers, `/listing/{token}` removed)
- Modify: `templates/submit.html`
- Create: `tests/test_submit_flow.py`

**Interfaces:**
- Consumes: Task 10's draft helpers, Task 11's `create_listing`, Task 12's route helpers.
- Produces: `/submit` gated on sign-in with the form preserved; `/submit/resume`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_submit_flow.py`:

```python
import re

FORM = {
    "title": "Ghost Signal",
    "tagline": "A submarine horror game played by sonar alone",
    "url": "ghostsignal.example",
    "platforms": ["PC", "VR"],
    "amount": "12",
    "email": "dev@studio.com",
    "studio": "Two people and a cat",
}


def extract_csrf(html):
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field in form"
    return match.group(1)


def submit(client, **over):
    page = client.get("/submit")
    data = dict(FORM, csrf=extract_csrf(page.text))
    data.update(over)
    return client.post("/submit", data=data, follow_redirects=False)


def register(client, email="dev@studio.com"):
    page = client.get("/register")
    return client.post("/register", data={
        "email": email, "password": "correct horse battery",
        "confirm": "correct horse battery", "csrf": extract_csrf(page.text)},
        follow_redirects=False)


def test_the_form_is_public(client):
    assert client.get("/submit").status_code == 200


def test_signed_out_submit_redirects_to_login(client):
    resp = submit(client)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_signed_out_submit_creates_no_listing(client, database):
    submit(client)
    assert database.all_listings() == []


def test_signed_out_submit_parks_a_draft(client):
    submit(client)
    assert client.cookies.get("oa_draft")


def test_invalid_form_never_parks_a_draft(client):
    resp = submit(client, title="")
    assert resp.status_code == 400
    assert not client.cookies.get("oa_draft")


def test_signing_in_creates_the_parked_listing(client, database):
    submit(client)
    register(client)
    resume = client.get("/submit/resume", follow_redirects=False)
    assert resume.status_code == 303
    listings = database.all_listings()
    assert len(listings) == 1
    assert listings[0]["title"] == "Ghost Signal"


def test_the_parked_listing_keeps_every_field(client, database):
    submit(client)
    register(client)
    client.get("/submit/resume", follow_redirects=False)
    listing = database.all_listings()[0]
    assert listing["tagline"] == FORM["tagline"]
    assert listing["url"] == "https://ghostsignal.example"
    assert listing["studio"] == "Two people and a cat"
    assert listing["platforms"] == "PC,VR"


def test_the_parked_listing_keeps_the_bid_amount(client, database):
    submit(client)
    register(client)
    client.get("/submit/resume", follow_redirects=False)
    listing = database.all_listings()[0]
    assert database.bids_for(listing["id"])[0]["amount"] == 12


def test_the_parked_listing_belongs_to_the_new_account(client, database):
    import auth
    submit(client)
    register(client)
    client.get("/submit/resume", follow_redirects=False)
    user = auth.get_user_by_email("dev@studio.com")
    assert database.all_listings()[0]["user_id"] == user["id"]


def test_a_draft_is_used_only_once(client, database):
    submit(client)
    register(client)
    client.get("/submit/resume", follow_redirects=False)
    client.get("/submit/resume", follow_redirects=False)
    assert len(database.all_listings()) == 1


def test_resume_without_a_draft_goes_to_the_form(client):
    register(client)
    resp = client.get("/submit/resume", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/submit")


def test_signed_in_submit_creates_the_listing_directly(client, database):
    register(client)
    resp = submit(client)
    assert resp.status_code == 303
    assert "/listing/" in resp.headers["location"]
    assert len(database.all_listings()) == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_submit_flow.py -v`
Expected: FAIL — the old handler calls `create_listing` with two arguments.

- [ ] **Step 3: Rewrite the submit handlers**

Replace the `/submit` POST handler and delete the `/listing/{token}` GET and topup handlers:

```python
@app.post("/submit", response_class=HTMLResponse)
async def submit(request: Request):
    raw = await request.form()

    def field(name: str, limit: int = 200) -> str:
        return str(raw.get(name, "")).strip()[:limit]

    form = {
        "title": field("title", 70),
        "tagline": field("tagline", 140),
        "url": field("url", 400),
        "image_url": field("image_url", 400),
        "studio": field("studio", 60),
        "email": field("email", 120),
        "amount": field("amount", 20),
        "platforms": ",".join(
            [p for p in raw.getlist("platforms") if p in PLATFORMS][:4]
        ),
    }

    def fail(msg: str):
        return render(
            request, "submit.html",
            {"stats": db.stats(), "form": form, "error": msg}, status=400
        )

    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return fail("That form expired. Try again.")
    if field("website"):  # honeypot
        return fail("Something went wrong. Try again.")
    if rate_limited(request, "submit", 5, 3600):
        return fail("That is a lot of submissions from one place. Try again in a bit.")
    if len(form["title"]) < 2:
        return fail("Your game needs a name.")
    if len(form["tagline"]) < 8:
        return fail("Add a one-line pitch, at least a few words.")
    link = clean_url(form["url"])
    if not link:
        return fail("A working link to the game is required (Steam, itch.io, your own site).")
    form["url"] = link
    form["image_url"] = clean_url(form["image_url"])
    value, err = parse_amount(form["amount"], db.MIN_FIRST_BID)
    if err:
        return fail(err)

    user = current_user(request)
    if not user:
        # Park the validated form so nothing is retyped after signing in.
        # A DB row rather than a cookie: SameSite=Lax cookies are not reliably
        # returned on an OAuth callback, and this payload can exceed 4KB.
        draft_id = auth.save_draft(form)
        resp = RedirectResponse("/login", status_code=303)
        resp.set_cookie(DRAFT_COOKIE, draft_id, httponly=True, samesite="lax",
                        secure=secure_cookies(), max_age=3600)
        return resp

    return listing_from_form(form, value, user["id"])


def listing_from_form(form: dict, amount: int, user_id: int):
    created = db.create_listing(form, amount, user_id)
    if db.get_setting("auto_confirm") == "1":
        pending = db.bids_for(created["id"])
        if pending:
            db.confirm_bid(pending[0]["id"])
    return RedirectResponse(f"/listing/{created['id']}", status_code=303)


@app.get("/submit/resume")
def submit_resume(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    draft = auth.load_draft(request.cookies.get(DRAFT_COOKIE))
    if not draft:
        resp = RedirectResponse("/submit", status_code=303)
        resp.delete_cookie(DRAFT_COOKIE)
        return resp
    value, err = parse_amount(draft.get("amount", ""), db.MIN_FIRST_BID)
    if err:
        resp = RedirectResponse("/submit", status_code=303)
        resp.delete_cookie(DRAFT_COOKIE)
        return resp
    auth.delete_draft(request.cookies.get(DRAFT_COOKIE))
    resp = listing_from_form(draft, value, user["id"])
    resp.delete_cookie(DRAFT_COOKIE)
    return resp
```

- [ ] **Step 4: Update the submit template**

In `templates/submit.html`, add the CSRF field immediately after the `<form ...>` tag:

```html
    <input type="hidden" name="csrf" value="{{ csrf }}">
```

Replace the closing note under the submit button:

```html
    <p class="tiny muted">{% if user %}This goes on your account at
    {{ user.email }}.{% else %}You will sign in on the next screen — nothing you
    typed here is lost.{% endif %} Your listing goes live once the operator
    confirms the payment. Bids are final and never refunded.</p>
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_submit_flow.py -v`
Expected: 12 passed.

- [ ] **Step 6: Commit**

```bash
git add main.py templates/submit.html tests/test_submit_flow.py
git commit -m "feat: gate submit on sign-in, preserving the form across it"
```

---

### Task 14: Dashboard, manage, edit and account

**Files:**
- Modify: `main.py`
- Modify: `templates/manage.html`
- Create: `templates/dashboard.html`, `templates/edit.html`, `templates/account.html`
- Create: `tests/test_listing_routes.py`

**Interfaces:**
- Consumes: Task 11's ownership helpers, Task 12's route helpers.
- Produces: `/dashboard`, `/account`, `/listing/{id}`, `/listing/{id}/edit`, `/listing/{id}/topup`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_listing_routes.py`:

```python
import re

FORM = {
    "title": "Ghost Signal",
    "tagline": "A submarine horror game played by sonar alone",
    "url": "ghostsignal.example",
    "platforms": ["PC", "VR"],
    "amount": "12",
    "email": "dev@studio.com",
}


def extract_csrf(html):
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field in form"
    return match.group(1)


def register(client, email):
    page = client.get("/register")
    return client.post("/register", data={
        "email": email, "password": "correct horse battery",
        "confirm": "correct horse battery", "csrf": extract_csrf(page.text)},
        follow_redirects=False)


def make_listing(client):
    page = client.get("/submit")
    resp = client.post("/submit", data=dict(FORM, csrf=extract_csrf(page.text)),
                       follow_redirects=False)
    return int(resp.headers["location"].rsplit("/", 1)[1])


def test_dashboard_requires_sign_in(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_dashboard_lists_your_games(client):
    register(client, "dev@studio.com")
    make_listing(client)
    assert "Ghost Signal" in client.get("/dashboard").text


def test_dashboard_hides_other_peoples_games(client):
    register(client, "dev@studio.com")
    make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    register(client, "someone@else.com")
    assert "Ghost Signal" not in client.get("/dashboard").text


def test_owner_sees_the_manage_page(client):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    assert client.get(f"/listing/{listing_id}").status_code == 200


def test_stranger_gets_404_on_manage(client):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    register(client, "someone@else.com")
    assert client.get(f"/listing/{listing_id}").status_code == 404


def test_signed_out_visitor_gets_404_on_manage(client):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    assert client.get(f"/listing/{listing_id}").status_code == 404


def test_owner_can_edit(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    page = client.get(f"/listing/{listing_id}/edit")
    assert page.status_code == 200
    resp = client.post(f"/listing/{listing_id}/edit", data={
        "title": "Deep Signal",
        "tagline": "Now with more sonar and much more dread",
        "url": "deepsignal.example",
        "image_url": "",
        "studio": "Two people and a cat",
        "platforms": ["PC"],
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 303
    listing = database.get_listing(listing_id)
    assert listing["title"] == "Deep Signal"
    assert listing["platforms"] == "PC"


def test_editing_cannot_change_the_total(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    database.confirm_bid(database.bids_for(listing_id)[0]["id"])
    before = database.get_listing(listing_id)["total"]
    page = client.get(f"/listing/{listing_id}/edit")
    client.post(f"/listing/{listing_id}/edit", data={
        "title": "Deep Signal", "tagline": "Now with more sonar and much more dread",
        "url": "deepsignal.example", "amount": "99999", "total": "99999",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert database.get_listing(listing_id)["total"] == before


def test_stranger_cannot_edit(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    register(client, "someone@else.com")
    assert client.get(f"/listing/{listing_id}/edit").status_code == 404
    page = client.get("/dashboard")
    resp = client.post(f"/listing/{listing_id}/edit", data={
        "title": "Stolen", "tagline": "This should never be saved",
        "url": "evil.example", "csrf": extract_csrf(page.text)},
        follow_redirects=False)
    assert resp.status_code == 404
    assert database.get_listing(listing_id)["title"] == "Ghost Signal"


def test_edit_rejects_a_bad_url(client):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    page = client.get(f"/listing/{listing_id}/edit")
    resp = client.post(f"/listing/{listing_id}/edit", data={
        "title": "Deep Signal", "tagline": "Now with more sonar and much more dread",
        "url": "not a url at all", "csrf": extract_csrf(page.text)},
        follow_redirects=False)
    assert resp.status_code == 400


def test_owner_can_top_up(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    page = client.get(f"/listing/{listing_id}")
    resp = client.post(f"/listing/{listing_id}/topup", data={
        "amount": "8", "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 303
    assert len(database.bids_for(listing_id)) == 2


def test_stranger_cannot_top_up(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    register(client, "someone@else.com")
    page = client.get("/dashboard")
    resp = client.post(f"/listing/{listing_id}/topup", data={
        "amount": "8", "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 404
    assert len(database.bids_for(listing_id)) == 1


def test_account_page_shows_the_email(client):
    register(client, "dev@studio.com")
    assert "dev@studio.com" in client.get("/account").text


def test_account_requires_sign_in(client):
    resp = client.get("/account", follow_redirects=False)
    assert resp.status_code == 303
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_listing_routes.py -v`
Expected: FAIL — 404 on `/dashboard`.

- [ ] **Step 3: Add the routes**

```python
def owned_listing_or_none(request: Request, listing_id: int):
    """Return the listing only if the signed-in user owns it.

    A non-owner is given the same 404 as a missing listing, so the route
    never confirms that a listing exists.
    """
    user = current_user(request)
    if not user or not db.owns_listing(user["id"], listing_id):
        return None, user
    return db.get_listing(listing_id), user


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "dashboard.html", {
        "_user": user,
        "listings": db.listings_for_user(user["id"]),
        "stats": db.stats(),
    })


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "account.html", {
        "_user": user,
        "linked": auth.identities_for(user["id"]),
        "stats": db.stats(),
    })


@app.get("/listing/{listing_id}", response_class=HTMLResponse)
def manage(request: Request, listing_id: int):
    listing, _ = owned_listing_or_none(request, listing_id)
    if not listing:
        return render(request, "notfound.html", {}, status=404)
    return render(request, "manage.html", {
        "listing": listing,
        "bids": db.bids_for(listing_id),
        "stats": db.stats(),
        "payment_link": db.get_setting("payment_link"),
        "payment_note": db.get_setting("payment_note"),
        "error": None,
    })


@app.get("/listing/{listing_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, listing_id: int):
    listing, _ = owned_listing_or_none(request, listing_id)
    if not listing:
        return render(request, "notfound.html", {}, status=404)
    return render(request, "edit.html", {
        "listing": listing, "form": listing, "stats": db.stats(), "error": None,
    })


@app.post("/listing/{listing_id}/edit", response_class=HTMLResponse)
async def edit(request: Request, listing_id: int):
    listing, _ = owned_listing_or_none(request, listing_id)
    if not listing:
        return render(request, "notfound.html", {}, status=404)
    raw = await request.form()

    def field(name: str, limit: int = 200) -> str:
        return str(raw.get(name, "")).strip()[:limit]

    form = {
        "title": field("title", 70),
        "tagline": field("tagline", 140),
        "url": field("url", 400),
        "image_url": field("image_url", 400),
        "studio": field("studio", 60),
        "platforms": ",".join(
            [p for p in raw.getlist("platforms") if p in PLATFORMS][:4]
        ),
    }

    def fail(msg: str):
        merged = dict(listing)
        merged.update(form)
        merged["platform_list"] = [p for p in form["platforms"].split(",") if p]
        return render(request, "edit.html", {
            "listing": listing, "form": merged, "stats": db.stats(), "error": msg,
        }, status=400)

    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return fail("That form expired. Try again.")
    if len(form["title"]) < 2:
        return fail("Your game needs a name.")
    if len(form["tagline"]) < 8:
        return fail("Add a one-line pitch, at least a few words.")
    link = clean_url(form["url"])
    if not link:
        return fail("A working link to the game is required.")
    form["url"] = link
    form["image_url"] = clean_url(form["image_url"])

    db.update_listing(listing_id, form)
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


@app.post("/listing/{listing_id}/topup", response_class=HTMLResponse)
async def topup(request: Request, listing_id: int):
    listing, _ = owned_listing_or_none(request, listing_id)
    if not listing:
        return render(request, "notfound.html", {}, status=404)
    raw = await request.form()
    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return render(request, "notfound.html", {}, status=404)
    value, err = parse_amount(str(raw.get("amount", "")), db.MIN_TOP_UP)
    if not err and rate_limited(request, "topup", 10, 3600):
        err = "Slow down a moment, then try again."
    if err:
        return render(request, "manage.html", {
            "listing": listing,
            "bids": db.bids_for(listing_id),
            "stats": db.stats(),
            "payment_link": db.get_setting("payment_link"),
            "payment_note": db.get_setting("payment_note"),
            "error": err,
        }, status=400)
    bid_id = db.add_bid(listing_id, value)
    if db.get_setting("auto_confirm") == "1":
        db.confirm_bid(bid_id)
    return RedirectResponse(f"/listing/{listing_id}#bids", status_code=303)
```

- [ ] **Step 4: Write the dashboard template**

Create `templates/dashboard.html`:

```html
{% extends "base.html" %}
{% block title %}My games · {{ site_name }}{% endblock %}
{% block body %}
<main class="page narrow">
  <p class="kicker">Signed in as {{ user.email }}</p>
  <h1>My games</h1>

  {% if listings %}
  <div class="dash-list">
    {% for l in listings %}
    <div class="card dash-row">
      <div>
        <h2>{{ l.title }}</h2>
        <p class="muted small">{{ l.tagline }}</p>
        <p class="mono">{{ l.total | money }}
          {% if l.pending_count %}<span class="tag tag-pending">payment pending</span>
          {% elif l.hidden %}<span class="tag">hidden</span>
          {% elif l.total > 0 %}<span class="tag tag-confirmed">live</span>{% endif %}
        </p>
      </div>
      <div class="dash-actions">
        <a class="btn btn-small" href="/listing/{{ l.id }}">Manage</a>
        <a class="btn btn-small btn-ghost" href="/listing/{{ l.id }}/edit">Edit</a>
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="card">
    <p>Nothing here yet.</p>
    <a class="btn btn-big" href="/submit">List a game</a>
  </div>
  {% endif %}

  <p class="tiny muted"><a href="/account">Account settings</a></p>
</main>
{% endblock %}
```

- [ ] **Step 5: Write the edit template**

Create `templates/edit.html`:

```html
{% extends "base.html" %}
{% block title %}Edit {{ listing.title }} · {{ site_name }}{% endblock %}
{% block body %}
<main class="page narrow">
  <p class="kicker">Editing</p>
  <h1>{{ listing.title }}</h1>
  <p class="lede">Change how your game appears on the board. Your total and rank
  are not affected.</p>

  {% if error %}<p class="alert">{{ error }}</p>{% endif %}

  <form class="card form" method="post" action="/listing/{{ listing.id }}/edit">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <label>Game name
      <input name="title" maxlength="70" required value="{{ form.title }}">
    </label>
    <label>One-line pitch
      <input name="tagline" maxlength="140" required value="{{ form.tagline }}">
    </label>
    <label>Link
      <input name="url" required value="{{ form.url }}">
    </label>
    <label>Cover image URL <span class="opt">optional</span>
      <input name="image_url" value="{{ form.image_url }}">
    </label>
    <label>Studio or dev <span class="opt">optional</span>
      <input name="studio" maxlength="60" value="{{ form.studio }}">
    </label>

    <fieldset class="platforms">
      <legend>Platforms <span class="opt">pick up to 4</span></legend>
      {% for p in platforms %}
      <label class="check"><input type="checkbox" name="platforms" value="{{ p }}"{% if p in form.platform_list %} checked{% endif %}><span>{{ p }}</span></label>
      {% endfor %}
    </fieldset>

    <button class="btn btn-big" type="submit">Save changes</button>
    <p class="tiny muted"><a href="/listing/{{ listing.id }}">Back without saving</a></p>
  </form>
</main>
{% endblock %}
```

- [ ] **Step 6: Write the account template**

Create `templates/account.html`:

```html
{% extends "base.html" %}
{% block title %}Account · {{ site_name }}{% endblock %}
{% block body %}
<main class="page narrow">
  <p class="kicker">Your account</p>
  <h1>Account</h1>

  <div class="card">
    <h2>Email</h2>
    <p class="mono">{{ user.email }}</p>
  </div>

  <div class="card">
    <h2>Sign-in methods</h2>
    <ul class="plain">
      <li>Password — {% if user.password_hash %}set{% else %}
        not set. <a href="/forgot">Set one</a> so you can sign in without a provider.{% endif %}</li>
      {% for p in ['google', 'github'] %}
        {% if p in linked %}
        <li>{{ p | capitalize }} — linked</li>
        {% elif p in providers %}
        <li>{{ p | capitalize }} — <a href="/auth/{{ p }}">link it</a></li>
        {% endif %}
      {% endfor %}
    </ul>
  </div>

  <div class="card">
    <h2>Password</h2>
    <p class="muted small">Changing your password signs out every other device.</p>
    <a class="btn" href="/forgot">Send me a reset link</a>
  </div>

  <p class="tiny muted"><a href="/dashboard">Back to my games</a></p>
</main>
{% endblock %}
```

- [ ] **Step 7: Update the manage template**

In `templates/manage.html`, replace the "Keep this link" card with an edit link, and add the CSRF field to the topup form.

Replace the whole last card:

```html
  <div class="card">
    <h2>Listing details</h2>
    <p class="muted small">Name, pitch, link, art and platforms can be changed at
    any time. Your total and rank stay as they are.</p>
    <a class="btn" href="/listing/{{ listing.id }}/edit">Edit listing</a>
  </div>
```

In the topup form, immediately after the `<form ...>` tag:

```html
      <input type="hidden" name="csrf" value="{{ csrf }}">
```

And change the form action from `/listing/{{ listing.manage_token }}/topup` to `/listing/{{ listing.id }}/topup`. Change the kicker at the top from "Your private listing page" to "Your listing".

- [ ] **Step 8: Add the styles**

Append to `static/style.css`:

```css
.dash-list { display: grid; gap: 1rem; }
.dash-row { display: flex; justify-content: space-between; align-items: center;
            gap: 1rem; flex-wrap: wrap; }
.dash-actions { display: flex; gap: .5rem; }
ul.plain { list-style: none; padding: 0; display: grid; gap: .4rem; }
```

- [ ] **Step 9: Run tests**

Run: `pytest tests/test_listing_routes.py -v`
Expected: 14 passed.

- [ ] **Step 10: Commit**

```bash
git add main.py templates/ static/style.css tests/test_listing_routes.py
git commit -m "feat: add dashboard, account page and owner-checked listing editing"
```

---

### Task 15: CSRF on the operator forms

Task 6 added the machinery and Tasks 12-14 applied it to the new forms. The operator forms still post without a token, so a logged-in operator could be made to confirm a bid by a page on another site.

**Files:**
- Modify: `main.py` (`admin_claim`, `admin_login`, `admin_action`, `admin_logout`)
- Modify: `templates/admin.html`, `templates/admin_login.html`, `templates/admin_claim.html`
- Create: `tests/test_admin_csrf.py`

**Interfaces:**
- Consumes: Task 6's `csrf_valid`.
- Produces: no new names.

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_csrf.py`:

```python
import re


def extract_csrf(html):
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field in form"
    return match.group(1)


def claim(client):
    page = client.get("/admin")
    return client.post("/admin/claim", data={
        "password": "hunter2hunter2", "confirm": "hunter2hunter2",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)


def test_claim_needs_a_token(client):
    client.get("/admin")
    resp = client.post("/admin/claim", data={
        "password": "hunter2hunter2", "confirm": "hunter2hunter2"},
        follow_redirects=False)
    assert resp.status_code == 400


def test_claim_works_with_a_token(client):
    assert claim(client).status_code == 303


def test_admin_action_needs_a_token(client, database):
    claim(client)
    page = client.get("/admin")
    resp = client.post("/admin/action", data={"action": "settings",
                                              "payment_link": "https://pay.example"},
                       follow_redirects=False)
    assert resp.status_code == 400
    assert database.get_setting("payment_link") == ""


def test_admin_action_works_with_a_token(client, database):
    claim(client)
    page = client.get("/admin")
    resp = client.post("/admin/action", data={
        "action": "settings", "payment_link": "https://pay.example",
        "payment_note": "", "csrf": extract_csrf(page.text)},
        follow_redirects=False)
    assert resp.status_code == 303
    assert database.get_setting("payment_link") == "https://pay.example"


def test_operator_login_needs_a_token(client):
    claim(client)
    client.post("/admin/logout", data={"csrf": "wrong"}, follow_redirects=False)
    resp = client.post("/admin/login", data={"password": "hunter2hunter2"},
                       follow_redirects=False)
    assert resp.status_code == 400
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_admin_csrf.py -v`
Expected: FAIL — the claim without a token returns 303, not 400.

- [ ] **Step 3: Guard the handlers**

In each of `admin_claim`, `admin_login`, `admin_action` and `admin_logout`, change the signature to take the form directly and check the token first. For `admin_claim`:

```python
@app.post("/admin/claim", response_class=HTMLResponse)
async def admin_claim(request: Request):
    raw = await request.form()
    password = str(raw.get("password", ""))
    confirm = str(raw.get("confirm", ""))
    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return render(request, "admin_claim.html",
                      {"error": "That form expired. Try again."}, status=400)
    if db.admin_exists():
        return RedirectResponse("/admin", status_code=303)
    ...
```

Keep the rest of each body unchanged. For `admin_action`, return `400` with the admin page rendered rather than redirecting when the token is missing. For `admin_logout`, a bad token returns a `303` to `/admin` without dropping the session.

- [ ] **Step 4: Add the field to the three admin templates**

In `templates/admin_login.html` and `templates/admin_claim.html`, add immediately after each `<form ...>` tag:

```html
    <input type="hidden" name="csrf" value="{{ csrf }}">
```

`templates/admin.html` contains several forms — the settings form and one per pending bid and per listing. Add the same hidden field to every one.

- [ ] **Step 5: Run tests**

Run: `pytest tests/ -v`
Expected: all passed.

- [ ] **Step 6: Commit**

```bash
git add main.py templates/ tests/test_admin_csrf.py
git commit -m "feat: require CSRF tokens on the operator forms"
```

---

### Task 16: End-to-end smoke test

**Files:**
- Modify: `tests/smoke.py`

**Interfaces:**
- Consumes: every route.
- Produces: nothing importable.

- [ ] **Step 1: Rewrite the smoke script**

Replace `tests/smoke.py` entirely:

```python
"""End to end smoke test for the board, run against a live instance.

    DATA_DIR=/tmp/oa uvicorn main:app --port 8099 &
    python tests/smoke.py http://127.0.0.1:8099

Point it at a THROWAWAY database: it claims the operator account and deletes
what it creates.
"""

import http.cookiejar as cj
import os
import re
import sys
import urllib.parse
import urllib.request

B = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE", "http://127.0.0.1:8000")
jar = cj.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def get(p, ip="1.2.3.4"):
    r = urllib.request.Request(B + p, headers={"X-Forwarded-For": ip})
    return op.open(r).read().decode()


def post(p, data, ip="1.2.3.4"):
    r = urllib.request.Request(
        B + p, data=urllib.parse.urlencode(data, doseq=True).encode(),
        headers={"X-Forwarded-For": ip})
    resp = op.open(r)
    return resp.geturl(), resp.read().decode()


def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf field found"
    return m.group(1)


def form_post(path, data, ip="1.2.3.4", page=None):
    token = csrf(page if page is not None else get(path, ip))
    return post(path, dict(data, csrf=token), ip)


# operator claims the board
form_post("/admin/claim", {"password": "hunter2hunter2", "confirm": "hunter2hunter2"},
          page=get("/admin"))

# a visitor fills the form while signed out
_, submitted = form_post("/submit", {
    "title": "Ghost Signal",
    "tagline": "A submarine horror game played by sonar alone",
    "url": "ghostsignal.example", "platforms": ["PC", "VR"],
    "amount": "12", "email": "dev@example.com"}, ip="5.5.5.5")
assert "Sign in" in submitted, "signed-out submit should land on sign-in"
assert "Ghost Signal" not in get("/"), "an unpaid listing must not be public"

# they make an account, and the parked submission becomes their listing
form_post("/register", {"email": "dev@example.com", "password": "correct horse battery",
                        "confirm": "correct horse battery", "display_name": "Dev"},
          ip="5.5.5.5", page=get("/register", "5.5.5.5"))
dash = get("/dashboard", "5.5.5.5")
assert "Ghost Signal" in dash, "the parked submission should now be listed"
listing_id = re.search(r"/listing/(\d+)", dash).group(1)

# the operator confirms the payment
admin = get("/admin")
bid = re.search(r'name="bid_id" value="(\d+)"', admin).group(1)
form_post("/admin/action", {"action": "confirm", "bid_id": bid}, page=admin)
home = get("/")
assert "Ghost Signal" in home and "#1 ON THE BOARD" in home, "should be live at #1"
assert "$13" in home, "cost to take #1 should be $13"

# the owner edits the listing
edit_page = get(f"/listing/{listing_id}/edit", "5.5.5.5")
form_post(f"/listing/{listing_id}/edit", {
    "title": "Deep Signal", "tagline": "Now with more sonar and much more dread",
    "url": "deepsignal.example", "image_url": "", "studio": "Two people and a cat",
    "platforms": ["PC"]}, ip="5.5.5.5", page=edit_page)
assert "Deep Signal" in get("/"), "the edit should show on the board"

# the owner tops up
manage = get(f"/listing/{listing_id}", "5.5.5.5")
form_post(f"/listing/{listing_id}/topup", {"amount": "8"}, ip="5.5.5.5", page=manage)
bid2 = re.search(r'name="bid_id" value="(\d+)"', get("/admin")).group(1)
form_post("/admin/action", {"action": "confirm", "bid_id": bid2}, page=get("/admin"))
assert "$20" in get("/"), "total should stack to $20"

# a stranger cannot reach it
stranger = cj.CookieJar()
stranger_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(stranger))
req = urllib.request.Request(f"{B}/listing/{listing_id}", headers={"X-Forwarded-For": "9.9.9.9"})
try:
    stranger_op.open(req)
    raise AssertionError("a stranger must not reach someone else's listing")
except urllib.error.HTTPError as exc:
    assert exc.code == 404, f"expected 404 for a stranger, got {exc.code}"

# the operator can still hide and delete
form_post("/admin/action", {"action": "hide", "listing_id": listing_id}, page=get("/admin"))
assert "Deep Signal" not in get("/"), "hidden listing must vanish from the board"
form_post("/admin/action", {"action": "unhide", "listing_id": listing_id}, page=get("/admin"))
assert "Deep Signal" in get("/"), "unhide should restore it"
form_post("/admin/action", {"action": "delete", "listing_id": listing_id}, page=get("/admin"))
assert "Deep Signal" not in get("/") and '"board":[]' in get("/api/board"), "delete should clear it"

print("all flow assertions passed")
```

- [ ] **Step 2: Run the whole unit suite**

Run: `pytest tests/ -v --ignore=tests/smoke.py`
Expected: all passed.

- [ ] **Step 3: Run the smoke test against a live instance**

```bash
rm -rf /tmp/oa && mkdir -p /tmp/oa
DATA_DIR=/tmp/oa uvicorn main:app --port 8099 &
sleep 2
python tests/smoke.py http://127.0.0.1:8099
kill %1
```

Expected: `all flow assertions passed`.

- [ ] **Step 4: Commit**

```bash
git add tests/smoke.py
git commit -m "test: cover accounts, editing and ownership in the smoke test"
```

---

### Task 17: Configuration and documentation

**Files:**
- Modify: `NOTES.md`
- Modify: `.gitignore`
- Create: `.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces: nothing importable.

- [ ] **Step 1: Write the example environment file**

Create `.env.example`:

```
# Copy to a file OUTSIDE the repo (for example /home/gagejack/outbid.env),
# fill it in, chmod 600 it, and pass it with docker run --env-file.
# Every value is optional: a feature hides itself when its keys are missing.

# Public origin. Required for OAuth callbacks and reset links to be correct
# behind the Cloudflare Tunnel, where the app cannot see its own public name.
BASE_URL=https://outbidarcade.lol

# Google: console.cloud.google.com, APIs & Services, Credentials,
# OAuth client ID, Web application.
# Authorised redirect URI: https://outbidarcade.lol/auth/google/callback
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# GitHub: Settings, Developer settings, OAuth Apps, New OAuth App.
# Authorization callback URL: https://outbidarcade.lol/auth/github/callback
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=

# Resend: resend.com, API Keys. The sending domain must be verified there.
# Without a key, reset links are written to the container log instead.
RESEND_API_KEY=
MAIL_FROM=noreply@outbidarcade.lol
```

- [ ] **Step 2: Keep real env files out of git**

Append to `.gitignore`:

```
.env
*.env
!.env.example
```

- [ ] **Step 3: Update the notes**

Replace the "Operator account" section of `NOTES.md` and add a new section after it:

```markdown
## Accounts
Anyone listing a game needs an account. Three ways in: Google, GitHub, or an
email and password. One account can hold any number of listings.

The submit form is public. Submitting while signed out parks the filled-in form
in the `drafts` table for an hour and sends the visitor to sign in; the listing
is created, with nothing retyped, as soon as they are. This survives the
redirect out to Google or GitHub and back.

Owners can edit a listing's name, pitch, link, cover art, studio and platforms.
They cannot edit money: bids stay append-only and operator-confirmed.

Password reset is an emailed link, valid for an hour and usable once. Resetting
signs out every other device. Accounts created through Google or GitHub have no
password until they set one this way, which is also their recovery path if they
lose access to the provider.

## Operator account
/admin is claimed on first visit: whoever sets the password first owns the
board. There is no reset. This must be done before the site is shared. Operator
sessions and user sessions share one table but never grant each other's access.

## Configuration
Secrets are environment variables, never committed and never stored in the
database. See `.env.example` for the full list and where each value comes from.

On the server:

    cp .env.example /home/gagejack/outbid.env
    chmod 600 /home/gagejack/outbid.env
    # fill it in, then:
    docker run -d --name outbid-arcade \
      -p 8080:8080 \
      -v /home/gagejack/outbid-data:/data \
      --env-file /home/gagejack/outbid.env \
      outbid-arcade

Everything degrades when a key is missing: no Google credentials means no
Google button, and no Resend key means reset links go to the container log
(`docker logs outbid-arcade`) instead of an inbox.
```

Replace the Testing section:

```markdown
## Testing
Unit and route tests, no server needed:

    pip install -r requirements.txt
    pytest tests/ --ignore=tests/smoke.py

`tests/smoke.py` walks the whole flow end to end against a running instance
with a throwaway DATA_DIR:

    DATA_DIR=/tmp/oa uvicorn main:app --port 8099 &
    python tests/smoke.py http://127.0.0.1:8099
```

- [ ] **Step 4: Verify the example file is tracked and real ones are not**

Run:

```bash
git check-ignore -v .env.example || echo "example is tracked, correct"
echo "GOOGLE_CLIENT_ID=x" > .env.test-ignore-check
git check-ignore -v .env.test-ignore-check && rm .env.test-ignore-check
```

Expected: the first prints "example is tracked, correct"; the second prints a `.gitignore` match.

- [ ] **Step 5: Commit**

```bash
git add NOTES.md .gitignore .env.example
git commit -m "docs: record the accounts model and required environment variables"
```

---

### Task 18: Deploy

**Files:** none changed.

**Interfaces:** none.

- [ ] **Step 1: Confirm the suite is green**

Run: `pytest tests/ -v --ignore=tests/smoke.py`
Expected: all passed. Do not deploy otherwise.

- [ ] **Step 2: Push**

```bash
git push origin main
```

- [ ] **Step 3: Create the environment file on the server**

On the Lenovo:

```bash
cd /home/gagejack/outbidarcade
git pull
cp .env.example /home/gagejack/outbid.env
chmod 600 /home/gagejack/outbid.env
nano /home/gagejack/outbid.env
```

Fill in `BASE_URL=https://outbidarcade.lol` at minimum. OAuth and mail keys can
be added later; the site runs without them, offering email and password sign-in
only, with reset links going to the log.

- [ ] **Step 4: Back up the old database**

The listings table is dropped and recreated on this boot. The current contents
are a single test row, but take a copy anyway:

```bash
cp /home/gagejack/outbid-data/app.db /home/gagejack/outbid-data/app.db.pre-accounts
```

- [ ] **Step 5: Rebuild and restart**

```bash
cd /home/gagejack/outbidarcade
docker build -t outbid-arcade .
docker stop outbid-arcade && docker rm outbid-arcade
docker run -d --name outbid-arcade \
  -p 8080:8080 \
  -v /home/gagejack/outbid-data:/data \
  --env-file /home/gagejack/outbid.env \
  outbid-arcade
docker logs --tail 20 outbid-arcade
```

Expected: uvicorn reports it is serving on `0.0.0.0:8080`, with no traceback.

- [ ] **Step 6: Check the live site**

Visit `https://outbidarcade.lol`, then:

1. `/register` — create an account.
2. `/submit` — fill the form while signed out in a private window; confirm it
   asks you to sign in and that nothing is retyped afterwards.
3. `/dashboard` — the listing is there.
4. `/listing/{id}/edit` — change the pitch and confirm it saves.
5. `/admin` — claim the operator account and confirm the bid.
6. The board shows the game.

- [ ] **Step 7: Register the OAuth applications**

Only when you want the provider buttons live. Both are free.

**Google:** console.cloud.google.com, create a project, APIs & Services,
OAuth consent screen (External, fill in the app name and your email), then
Credentials, Create credentials, OAuth client ID, Web application. Authorised
redirect URI must be exactly `https://outbidarcade.lol/auth/google/callback`.
Copy the client ID and secret into the env file.

**GitHub:** github.com/settings/developers, New OAuth App. Homepage URL
`https://outbidarcade.lol`, Authorization callback URL exactly
`https://outbidarcade.lol/auth/github/callback`. Generate a client secret and
copy both values into the env file.

Then restart the container (Step 5's last three commands). The buttons appear
on `/login` and `/register` on their own.

- [ ] **Step 8: Set up mail**

Only when you want reset emails delivered rather than logged. resend.com, add
and verify `outbidarcade.lol` as a sending domain (this means adding DNS
records in Cloudflare), create an API key, put it in the env file, restart.

Until then, a reset link can be read with `docker logs outbid-arcade`.

---

## Self-Review

**Spec coverage:**

| Spec section | Tasks |
| --- | --- |
| Data model | 2, 11 |
| Email and password auth | 3, 12 |
| OAuth flows | 8, 9, 12 |
| Account linking rules | 9 |
| Password reset | 5, 7, 12 |
| Submit with draft preservation | 10, 13 |
| Listing editing | 11, 14 |
| Routes table | 12, 13, 14 |
| Rate limiting | 12, 13, 14 |
| Configuration | 17, 18 |
| Security notes | 4, 5, 6, 9, 12, 14, 15 |
| Testing | 1, 16, and every task's tests |

**Type consistency:** `create_listing(data, amount, user_id) -> {"id": int}` is
defined in Task 11 and used in Tasks 13 and 14. `user_from_profile(profile) ->
(user | None, str)` is defined in Task 9 and used in Task 12. The profile dict
keys (`provider`, `uid`, `email`, `email_verified`, `name`) are produced in
Task 8 and consumed in Task 9. `reset_is_live` is added in Task 12 Step 5
because Task 5's `consume_reset` would otherwise burn the token on a GET.

**Ordering note:** `pytest tests/` is only green across the whole suite from
Task 13 onward. Tasks 11 and 12 leave `main.py` briefly inconsistent with
`db.py`; each task's own test file passes, which is the gate for that task.
