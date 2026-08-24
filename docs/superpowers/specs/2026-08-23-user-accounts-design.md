# User Accounts and Listing Editing — Design

**Date:** 2026-08-23
**Status:** Awaiting review

## Goal

Give listing owners real accounts. Today a listing is owned by a secret URL
(`/listing/{manage_token}`), which cannot be recovered, cannot be edited, and
cannot group several games under one owner. After this change, a user signs in
with Google, GitHub, or an email and password, and edits their own listings.

## Decisions Already Made

| Decision | Choice | Reason |
| --- | --- | --- |
| Auth methods | Google OAuth, GitHub OAuth, email + password | Asked for by the owner |
| Password reset | Emailed one-time link | Asked for by the owner |
| Backwards compatibility | None. `manage_token` is removed | Site is pre-launch; the only row in the database is a test |
| Submit flow | Form first, sign-in wall on submit, draft preserved | Asked for by the owner |
| Listings per user | Many | A studio with several games should need one account |
| Secrets | Environment variables | Keeps secrets out of both git and the database |
| Mail transport | Resend HTTP API | One HTTP call; no SMTP ports, TLS modes, or async SMTP library |

## Deployment Context

The app runs on a Lenovo server in Docker (container `outbid-arcade`, port
8080), fronted by a Cloudflare Tunnel serving `outbidarcade.lol`. The database
is a SQLite file at `/home/gagejack/outbid-data/app.db` on the host, mounted
into the container at `/data`. Deploys are `git pull` on the server followed by
a rebuild.

Environment variables are supplied with `--env-file`, described under
Configuration below.

## Data Model

All tables live in the same `app.db` file, created by `init_db()` in `db.py` on
startup. No manual SQL is ever run by hand.

### New tables

```sql
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL DEFAULT '',   -- '' means OAuth-only
    display_name  TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    last_login_at INTEGER
);

CREATE TABLE identities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider     TEXT NOT NULL,        -- 'google' | 'github'
    provider_uid TEXT NOT NULL,        -- provider's immutable id, never the email
    created_at   INTEGER NOT NULL,
    UNIQUE(provider, provider_uid)
);

CREATE TABLE reset_tokens (
    token_hash TEXT PRIMARY KEY,       -- sha256 of the emailed token
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    used_at    INTEGER
);

CREATE TABLE drafts (
    id         TEXT PRIMARY KEY,       -- random id, held in a short-lived cookie
    payload    TEXT NOT NULL,          -- JSON of the submitted form fields
    created_at INTEGER NOT NULL
);

CREATE INDEX idx_listings_user ON listings(user_id);
CREATE INDEX idx_identities_user ON identities(user_id);
```

### Changes to existing tables

- `listings`: add `user_id INTEGER REFERENCES users(id)`, drop `manage_token`.
- `sessions`: add `user_id INTEGER REFERENCES users(id)`, nullable. `NULL` keeps
  its present meaning, an operator session. Non-null is a user session. One
  table and one lookup path serves both; the admin flow is untouched.

Because the site is pre-launch, `init_db()` recreates `listings` rather than
migrating it. The single test row is discarded.

### Why `provider_uid` and not email

Google and GitHub both let a user change their email address. Keying the
identity on the provider's immutable id means an email change does not orphan
the account.

## Authentication Flows

### Email and password

Registration takes an email, a password of at least 10 characters (matching the
existing operator rule in `main.py`), and an optional display name. Passwords go
through the `hash_password` / `verify_password` pair already in `db.py`, which
uses scrypt with a per-password salt. No new crypto is introduced.

On success a session row is created and the `oa_user` cookie is set with
`httponly`, `samesite=lax`, and `secure`.

### OAuth (Google and GitHub)

Both providers use the standard server-side Authorization Code flow:

1. `GET /auth/{provider}` generates a random `state`, stores it in a short-lived
   cookie, and redirects to the provider.
2. The provider redirects back to `/auth/{provider}/callback` with `code` and
   `state`.
3. The `state` from the query is compared against the cookie. A mismatch aborts
   the login. This is the CSRF defence and is not optional.
4. The server exchanges `code` for an access token, sending `client_secret`
   server-to-server. The secret never reaches the browser.
5. The server reads the profile: `provider_uid`, email, verified flag, name.
   Google returns `email_verified` on its userinfo response. GitHub requires the
   `user:email` scope and a `verified: true` entry from `/user/emails`.
6. Find or create the user, then set the session cookie.

Implemented with `httpx` directly. Two providers is roughly eighty lines; a
library such as Authlib would add a dependency without removing the need to
understand the flow.

### Account linking rules

On an OAuth callback, in order:

1. If `(provider, provider_uid)` already exists, sign that user in.
2. Otherwise, if the provider reports the email **verified** and a user with that
   email exists, attach a new `identities` row to that user and sign them in.
3. Otherwise, if the email is verified and unknown, create a user with an empty
   `password_hash` and attach the identity.
4. If the provider does **not** report the email verified, create nothing.
   Show an error asking the user to sign in with a password first, then link
   the provider from their account page.

Step 4 matters. Auto-linking an unverified provider email is a known
account-takeover path: an attacker registers the victim's address at a provider
that never verifies it, signs in, and inherits the account.

### Password reset

1. `POST /forgot` takes an email. The response is identical whether or not the
   address exists, so the page cannot be used to discover who has an account.
2. A 32-byte random token is generated. Its **sha256 hash** is stored; the token
   itself is only ever in the email. A database leak therefore yields no usable
   reset links.
3. The link expires after one hour and is single-use, enforced by `used_at`.
4. `POST /reset/{token}` sets the new password and deletes every other session
   for that user, so a stolen session cannot outlive the reset.

Users created through OAuth have an empty `password_hash` and may use this same
flow to set a first password. That is their recovery path if they ever lose
access to the provider.

If `RESEND_API_KEY` is absent, the reset link is written to the application log
instead of being emailed, so the flow remains testable in development.

## Submit Flow With Draft Preservation

The requirement is that a visitor fills in the game form before being asked to
sign in, and that nothing they typed is lost across a sign-in that may bounce
out to Google or GitHub and back.

1. `GET /submit` stays public and unchanged.
2. `POST /submit` validates the form exactly as it does today. Validation
   errors re-render the form, as now.
3. If the form is valid and the visitor is signed in, the listing is created and
   they are redirected to it. This is the existing path with an owner attached.
4. If the form is valid and the visitor is **not** signed in, the validated
   fields are written to `drafts` as JSON, the draft id is set in a
   ten-minute cookie, and the visitor is redirected to `/login?next=/submit/resume`.
5. After any successful sign-in or registration, `/submit/resume` reads the
   draft, creates the listing owned by the new session's user, deletes the
   draft, and redirects to the listing.

A server-side draft is used rather than a signed cookie for two reasons. A
`SameSite=Lax` cookie is not reliably returned on the cross-site POST callback
some providers use, and a form carrying an image URL can exceed the 4KB cookie
limit. A row keyed by a random id survives both.

Drafts older than 24 hours are deleted opportunistically, following the pattern
`session_valid()` already uses for expired sessions.

## Listing Editing

`GET /listing/{id}/edit` and `POST /listing/{id}/edit`, both requiring that the
signed-in user owns the listing. A non-owner receives the existing 404 page
rather than a 403, so the endpoint does not confirm that a listing exists.

Editable: title, tagline, url, image_url, studio, platforms.

Not editable: bid amounts, totals, rank, or status. Money is append-only and
stays under operator control, exactly as it is now.

Every field reuses the validation already in `main.py` — `clean_url()` for
links, the same length limits, the same platform whitelist. Editing the title
recomputes `slug`.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/register` | Registration form |
| POST | `/register` | Create account, sign in |
| GET | `/login` | Sign-in form, with provider buttons |
| POST | `/login` | Email and password sign-in |
| POST | `/logout` | End session |
| GET | `/auth/{provider}` | Begin OAuth |
| GET | `/auth/{provider}/callback` | Finish OAuth |
| GET | `/forgot` | Request a reset |
| POST | `/forgot` | Send the reset email |
| GET | `/reset/{token}` | New-password form |
| POST | `/reset/{token}` | Apply the new password |
| GET | `/dashboard` | The signed-in user's listings |
| GET | `/account` | Email, password, linked providers |
| GET | `/listing/{id}` | Manage one listing (owner only) |
| GET/POST | `/listing/{id}/edit` | Edit listing fields (owner only) |
| POST | `/listing/{id}/topup` | Top up (owner only) |
| GET | `/submit/resume` | Create the listing from a saved draft |

`/listing/{token}` is replaced by `/listing/{id}`, guarded by ownership rather
than by knowing a secret.

## Rate Limiting

The existing `rate_limited()` helper covers the new endpoints:

| Bucket | Limit |
| --- | --- |
| `login` | 10 per 15 minutes |
| `register` | 5 per hour |
| `forgot` | 5 per hour |
| `oauth` | 20 per hour |

## Configuration

Seven new environment variables. All are optional, and each feature hides itself
when its variables are missing: no Google credentials means no Google button,
and no Resend key means reset links are logged rather than emailed.

```
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
RESEND_API_KEY=
MAIL_FROM=noreply@outbidarcade.lol
BASE_URL=https://outbidarcade.lol
```

`BASE_URL` is required for OAuth and reset links to be absolute and correct
behind the Cloudflare Tunnel, where the app cannot infer its own public origin.

On the server these live in `/home/gagejack/outbid.env`, readable only by the
owner (`chmod 600`), and are passed in at run time:

```bash
docker run -d --name outbid-arcade \
  -p 8080:8080 \
  -v /home/gagejack/outbid-data:/data \
  --env-file /home/gagejack/outbid.env \
  outbid-arcade
```

The file is listed in `.gitignore` and never committed.

### Provider registration

Both providers need the callback URL registered exactly, including scheme and
path:

- Google Cloud Console, OAuth 2.0 Client ID, Web application:
  `https://outbidarcade.lol/auth/google/callback`
- GitHub, Settings, Developer settings, OAuth Apps:
  `https://outbidarcade.lol/auth/github/callback`

## Security Notes

- Session cookies are `httponly`, `samesite=lax`, `secure`. The tunnel
  terminates TLS, so `secure` is correct in production; it is relaxed only when
  `BASE_URL` begins with `http://localhost`.
- Every state-changing POST is protected by a CSRF token, including the ones
  that exist today. `samesite=lax` alone does not cover top-level POST
  navigation.
- OAuth `state` is random, single-use, and compared on return.
- Reset tokens are stored hashed, expire in one hour, and are single-use.
- A password reset invalidates the user's other sessions.
- Ownership is checked on the server for every listing route. The listing id in
  the URL is never trusted on its own.
- Login, registration, and reset responses do not reveal whether an address is
  registered.

## Testing

`tests/smoke.py` is extended to cover, against a throwaway `DATA_DIR`:

1. Register, sign out, sign back in.
2. Wrong password is rejected.
3. Submit while signed out, sign in, and confirm the draft becomes a listing
   with the correct owner and no lost fields.
4. Edit a listing; confirm the changes persist.
5. Confirm a second user receives a 404 on the first user's listing and edit
   pages.
6. Request a reset, read the token from the log, apply it, confirm the old
   password fails and other sessions are dead.
7. Confirm a reset token cannot be used twice.

OAuth is covered by a fake provider fixture rather than live calls to Google or
GitHub, so the suite stays offline and deterministic.

## Files Touched

| File | Change |
| --- | --- |
| `db.py` | New tables, user and session helpers, draft and reset helpers, ownership queries |
| `main.py` | Auth routes, OAuth handlers, edit routes, draft resume, CSRF |
| `auth.py` | New. Provider configuration, the OAuth exchange, mail sending |
| `templates/login.html` | New |
| `templates/register.html` | New |
| `templates/forgot.html`, `templates/reset.html` | New |
| `templates/dashboard.html`, `templates/account.html` | New |
| `templates/edit.html` | New |
| `templates/manage.html` | Ownership-based, no token link section |
| `templates/base.html` | Sign in / dashboard / sign out in the header |
| `templates/submit.html` | Note that submitting requires an account |
| `requirements.txt` | Add `httpx` |
| `tests/smoke.py` | The cases above |
| `NOTES.md` | Record the accounts model and required variables |

## Out of Scope

- Email verification for password registration. Reset covers recovery, and an
  unverified address costs nothing here because listings are operator-confirmed
  anyway.
- Two-factor authentication.
- Transferring a listing between accounts. The operator can do it directly in
  the database if it ever comes up.
- Any change to how money, bids, or confirmation work.
