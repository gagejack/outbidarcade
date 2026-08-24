# Outbid Arcade — site notes

## What this is
A pay-to-rank leaderboard for video games, in the shape of outbid.lol
(customer brief: "Outbid.lol but for video games", 2026-08-22).

## Confirmed mechanics
- Pay what you want. The amount paid IS the score. Board sorts by score, highest first.
- Minimum first bid $2, whole dollars. Top-ups minimum $1.
- Top-ups stack: a listing's total is the sum of its confirmed bids.
- Bids never expire and are never refunded. Ties go to whoever got there first.
- Games only (games, demos, mods, jam entries, store pages). Rules page spells this out.

## How money is handled
No payment processor is wired in (no secrets in the repo, by contract).
Flow instead: visitor submits -> bid is "pending" -> operator confirms it in
/admin once payment lands -> listing goes public.
- The operator pastes their own payment link (Stripe payment link, PayPal.me,
  Ko-fi...) in /admin. It is stored in SQLite under /data, never in the repo.
- /admin has a "free mode" toggle that auto-confirms every bid. Useful for a
  launch week or a demo run. Off by default.

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

## One worker, on purpose
The app must run on a single Uvicorn worker. Three things live in process
memory and would silently break with more:

- the rate limiter (`main.py`, `_HITS`) — every limit would multiply by the
  worker count, so "10 login attempts per 15 minutes" would become 80
- the board cache (`db.py`, `_board_cache`) — caches would diverge between
  workers after a write, showing different boards to different visitors
- SQLite allows one writer at a time; more processes means contention and
  `database is locked` errors, not more throughput

Routes are sync `def`, so FastAPI already serves them from a threadpool —
concurrency is there without extra workers. The real ceiling is SQLite, and
the answer to outgrowing it is Postgres, not more processes on one file.

Writes that change what the board shows must call `db._invalidate_board()`.
Today that is `confirm_bid`, `reject_bid`, `set_hidden`, `delete_listing` and
`update_listing`. `create_listing` deliberately does not: it inserts a pending
bid, and pending bids are filtered out of the board by `HAVING total > 0`.

Session expiry is swept on write (`new_session`, `start_session`) and filtered
on read inside the SELECT. Keep that split — sweeping on read would put a
database write on every page view.

## Open questions for the owner
- Which payment link to use (Stripe payment link is the simplest).
- Whether to run a free launch week (free mode) before switching to paid.
- Whether listings should ever be time-limited (currently: never, by design).

## Testing
Unit and route tests, no server needed:

    pip install -r requirements.txt
    pytest tests/ --ignore=tests/smoke.py

`tests/smoke.py` walks the whole flow end to end against a running instance
with a throwaway DATA_DIR:

    DATA_DIR=/tmp/oa uvicorn main:app --port 8099 &
    python tests/smoke.py http://127.0.0.1:8099
