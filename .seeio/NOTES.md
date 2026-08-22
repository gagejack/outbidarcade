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

## Operator account
/admin is claimed on first visit: whoever sets the password first owns the
board. There is no reset. This must be done before the site is shared.

## Open questions for the owner
- Which payment link to use (Stripe payment link is the simplest).
- Whether to run a free launch week (free mode) before switching to paid.
- Whether listings should ever be time-limited (currently: never, by design).

## Testing
`tests/smoke.py` walks the whole flow (submit, confirm, top-up, hide, delete)
against a running instance with a throwaway DATA_DIR:

    DATA_DIR=/tmp/oa uvicorn main:app --port 8099 &
    python tests/smoke.py http://127.0.0.1:8099
