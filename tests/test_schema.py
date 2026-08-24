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
