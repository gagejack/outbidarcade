"""SQLite storage for Outbid Arcade.

/data is the only path that survives deploys, and it may be empty or absent
on first boot, so every helper here goes through init_db().
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "app.db"

MIN_FIRST_BID = 2
MIN_TOP_UP = 1


@contextmanager
def connect():
    """Open, commit and always close. Every helper below goes through this."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL,
                title TEXT NOT NULL,
                tagline TEXT NOT NULL,
                url TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                studio TEXT NOT NULL DEFAULT '',
                platforms TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                manage_token TEXT NOT NULL,
                hidden INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                live_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS bids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                confirmed_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                listing_id INTEGER,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bids_listing ON bids(listing_id);
            """
        )
        # Migrations for databases created by an older build. /data outlives
        # deploys, so new columns have to be added, not assumed.
        for table, column, decl in (("events", "listing_id", "INTEGER"),):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ---------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "payment_link": "",
    "payment_note": "",
    "auto_confirm": "0",
    "admin_hash": "",
}


def get_setting(key: str, default: str | None = None) -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, "") if default is None else default
    return row["value"]


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ------------------------------------------------------------------- admin


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt.encode("utf-8"), n=16384, r=8, p=1
    ).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$", 2)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


def admin_exists() -> bool:
    return bool(get_setting("admin_hash"))


SESSION_TTL = 60 * 60 * 24 * 30


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with connect() as conn:
        # Expired sessions are swept here rather than on every page view, which
        # would put a write on read-only traffic.
        conn.execute("DELETE FROM sessions WHERE created_at < ?", (now - SESSION_TTL,))
        conn.execute("INSERT INTO sessions(token, created_at) VALUES(?, ?)", (token, now))
    return token


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    cutoff = int(time.time()) - SESSION_TTL
    with connect() as conn:
        row = conn.execute(
            "SELECT token FROM sessions WHERE token=? AND created_at >= ?", (token, cutoff)
        ).fetchone()
    return row is not None


def drop_session(token: str | None) -> None:
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


# ---------------------------------------------------------------- listings


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:48] or "game"


# The board is read on nearly every request and only changes when a bid is
# confirmed or a listing is hidden/deleted, so it is cached in memory and
# dropped explicitly by those writes. Single process, so no locking.
_board_cache: list[dict] | None = None


def _invalidate_board() -> None:
    global _board_cache
    _board_cache = None


BOARD_SQL = """
    SELECT l.*,
           COALESCE(SUM(CASE WHEN b.status='confirmed' THEN b.amount END), 0) AS total,
           MAX(CASE WHEN b.status='confirmed' THEN b.confirmed_at END) AS last_bid_at,
           COUNT(CASE WHEN b.status='confirmed' THEN 1 END) AS bid_count
    FROM listings l
    LEFT JOIN bids b ON b.listing_id = l.id
    WHERE l.hidden = 0
    GROUP BY l.id
    HAVING total > 0
    ORDER BY total DESC, l.live_at ASC, l.id ASC
"""


def board() -> list[dict]:
    global _board_cache
    if _board_cache is not None:
        return _board_cache
    with connect() as conn:
        rows = conn.execute(BOARD_SQL).fetchall()
    out = []
    for i, row in enumerate(rows, start=1):
        item = dict(row)
        item["rank"] = i
        item["platform_list"] = [p for p in item["platforms"].split(",") if p]
        out.append(item)
    _board_cache = out
    return out


def top_total(rows: list[dict] | None = None) -> int:
    rows = board() if rows is None else rows
    return rows[0]["total"] if rows else 0


def price_to_lead(rows: list[dict] | None = None) -> int:
    top = top_total(rows)
    return top + 1 if top else MIN_FIRST_BID


def get_listing(listing_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        totals = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN status='confirmed' THEN amount END), 0) AS total,"
            " COUNT(CASE WHEN status='confirmed' THEN 1 END) AS bid_count"
            " FROM bids WHERE listing_id=?",
            (listing_id,),
        ).fetchone()
    item.update(dict(totals))
    item["platform_list"] = [p for p in item["platforms"].split(",") if p]
    item["rank"] = None
    if item["total"] > 0 and not item["hidden"]:
        for entry in board():
            if entry["id"] == listing_id:
                item["rank"] = entry["rank"]
                break
    return item


def get_listing_by_token(token: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM listings WHERE manage_token=?", (token,)).fetchone()
    return get_listing(row["id"]) if row else None


def bids_for(listing_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bids WHERE listing_id=? ORDER BY id DESC", (listing_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def create_listing(data: dict, amount: int) -> dict:
    now = int(time.time())
    token = secrets.token_urlsafe(24)
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO listings(slug, title, tagline, url, image_url, studio, platforms,"
            " email, manage_token, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                slugify(data["title"]),
                data["title"],
                data["tagline"],
                data["url"],
                data.get("image_url", ""),
                data.get("studio", ""),
                data.get("platforms", ""),
                data.get("email", ""),
                token,
                now,
            ),
        )
        listing_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO bids(listing_id, amount, status, created_at) VALUES(?,?,'pending',?)",
            (listing_id, amount, now),
        )
    return {"id": listing_id, "token": token}


def add_bid(listing_id: int, amount: int) -> int:
    now = int(time.time())
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO bids(listing_id, amount, status, created_at) VALUES(?,?,'pending',?)",
            (listing_id, amount, now),
        )
    return int(cur.lastrowid)


def confirm_bid(bid_id: int) -> None:
    now = int(time.time())
    with connect() as conn:
        row = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
        if row is None or row["status"] == "confirmed":
            return
        conn.execute(
            "UPDATE bids SET status='confirmed', confirmed_at=? WHERE id=?", (now, bid_id)
        )
        listing = conn.execute(
            "SELECT * FROM listings WHERE id=?", (row["listing_id"],)
        ).fetchone()
        if listing and listing["live_at"] is None:
            conn.execute("UPDATE listings SET live_at=? WHERE id=?", (now, listing["id"]))
    # Before get_listing below: it reads the board to work out the new rank,
    # and that rank goes into the event text.
    _invalidate_board()
    listing_data = get_listing(row["listing_id"])
    if listing_data:
        rank = listing_data.get("rank")
        if listing_data["bid_count"] <= 1:
            text = f"{listing_data['title']} entered the board at ${listing_data['total']:,}"
        else:
            text = f"{listing_data['title']} topped up to ${listing_data['total']:,}"
        if rank == 1:
            text += " and took #1"
        elif rank:
            text += f" (#{rank})"
        log_event("bid", text, listing_data["id"])


def reject_bid(bid_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE bids SET status='rejected' WHERE id=?", (bid_id,))
    # A confirmed bid can be rejected back off the board, so drop the cache.
    _invalidate_board()


def set_hidden(listing_id: int, hidden: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE listings SET hidden=? WHERE id=?", (1 if hidden else 0, listing_id))
    _invalidate_board()


def delete_listing(listing_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM bids WHERE listing_id=?", (listing_id,))
        conn.execute("DELETE FROM events WHERE listing_id=?", (listing_id,))
        conn.execute("DELETE FROM listings WHERE id=?", (listing_id,))
    _invalidate_board()


def pending_bids() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT b.*, l.title, l.url, l.email, l.manage_token,"
            " COALESCE((SELECT SUM(amount) FROM bids x WHERE x.listing_id=l.id"
            "   AND x.status='confirmed'), 0) AS confirmed_total"
            " FROM bids b JOIN listings l ON l.id=b.listing_id"
            " WHERE b.status='pending' ORDER BY b.id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def all_listings() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT l.*, COALESCE(SUM(CASE WHEN b.status='confirmed' THEN b.amount END), 0)"
            " AS total FROM listings l LEFT JOIN bids b ON b.listing_id=l.id"
            " GROUP BY l.id ORDER BY total DESC, l.id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def log_event(kind: str, text: str, listing_id: int | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO events(kind, text, listing_id, created_at) VALUES(?,?,?,?)",
            (kind, text, listing_id, int(time.time())),
        )
        conn.execute(
            "DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 200)"
        )


def recent_events(limit: int = 8) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.* FROM events e LEFT JOIN listings l ON l.id = e.listing_id"
            " WHERE e.listing_id IS NULL OR (l.id IS NOT NULL AND l.hidden = 0)"
            " ORDER BY e.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def stats(rows: list[dict] | None = None) -> dict:
    rows = board() if rows is None else rows
    with connect() as conn:
        volume = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS v FROM bids WHERE status='confirmed'"
        ).fetchone()["v"]
    top = rows[0]["total"] if rows else 0
    return {
        "listings": len(rows),
        "volume": int(volume),
        "top": top,
        "to_lead": top + 1 if top else MIN_FIRST_BID,
    }
