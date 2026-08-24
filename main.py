"""Outbid Arcade — a pay-to-rank leaderboard for video games.

Rules: pay what you want, the amount is your score, the board sorts by score,
bids never expire and top-ups stack. State lives in SQLite under /data.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import auth
import db
import mail
import oauth

ASSET_VERSION = "1"
PLATFORMS = ["PC", "Steam", "Switch", "PS5", "Xbox", "Mobile", "Web", "VR", "itch.io"]
SITE_NAME = "Outbid Arcade"
TAGLINE = "The pay-to-rank leaderboard for video games."

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
                    secure=secure_cookies(), max_age=db.SESSION_TTL)


def csrf_for(request: Request) -> str:
    return request.cookies.get(CSRF_COOKIE) or auth.new_csrf()


def csrf_valid(request: Request, form_value: str) -> bool:
    return auth.csrf_ok(request.cookies.get(CSRF_COOKIE), form_value)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN201 - FastAPI lifespan signature
    db.init_db()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def money(value: int) -> str:
    return f"${int(value):,}"


def ago(ts: int | None) -> str:
    if not ts:
        return ""
    delta = max(0, int(time.time()) - int(ts))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


templates.env.filters["money"] = money
templates.env.filters["ago"] = ago


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


# ------------------------------------------------------------ light limits

_HITS: dict[str, deque] = defaultdict(deque)


def rate_limited(request: Request, bucket: str, limit: int, window: int) -> bool:
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    ip = ip or (request.client.host if request.client else "unknown")
    key = f"{bucket}:{ip}"
    now = time.time()
    if len(_HITS) > 5000:  # keep the in-memory counter from growing forever
        for stale in [k for k, v in _HITS.items() if not v or now - v[-1] > 86400]:
            _HITS.pop(stale, None)
    hits = _HITS[key]
    while hits and now - hits[0] > window:
        hits.popleft()
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


def clean_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or "." not in parsed.netloc:
        return ""
    return raw[:400]


def parse_amount(raw: str, minimum: int) -> tuple[int, str]:
    cleaned = (raw or "").replace("$", "").replace(",", "").strip()
    try:
        amount = int(float(cleaned))
    except ValueError:
        return 0, "Enter your bid as a whole number of dollars."
    if amount < minimum:
        return 0, f"The minimum here is {money(minimum)}."
    if amount > 1_000_000:
        return 0, "Bids are capped at $1,000,000. Get in touch if you are serious."
    return amount, ""


# ------------------------------------------------------------------ public


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /admin\nDisallow: /listing/\n"


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    rows = db.board()
    return render(
        request,
        "index.html",
        {
            "rows": rows,
            "stats": db.stats(rows),
            "events": db.recent_events(6),
        },
    )


@app.get("/api/board")
def api_board():
    board = db.board()
    rows = [
        {
            "rank": r["rank"],
            "id": r["id"],
            "title": r["title"],
            "total": r["total"],
            "url": r["url"],
        }
        for r in board
    ]
    return JSONResponse({"board": rows, "stats": db.stats(board)})


@app.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    return render(request, "rules.html", {"stats": db.stats()})


@app.get("/submit", response_class=HTMLResponse)
def submit_form(request: Request):
    return render(
        request,
        "submit.html",
        {"stats": db.stats(), "form": {}, "error": None},
    )


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
            request, "submit.html", {"stats": db.stats(), "form": form, "error": msg}, status=400
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
    # Claiming deletes the row and hands back the payload in one statement, so
    # a double-click cannot turn one parked form into two listings.
    draft = auth.claim_draft(request.cookies.get(DRAFT_COOKIE))
    if not draft:
        resp = RedirectResponse("/submit", status_code=303)
        resp.delete_cookie(DRAFT_COOKIE)
        return resp
    value, err = parse_amount(draft.get("amount", ""), db.MIN_FIRST_BID)
    if err:
        resp = RedirectResponse("/submit", status_code=303)
        resp.delete_cookie(DRAFT_COOKIE)
        return resp
    resp = listing_from_form(draft, value, user["id"])
    resp.delete_cookie(DRAFT_COOKIE)
    return resp


@app.get("/game/{listing_id}", response_class=HTMLResponse)
def game(request: Request, listing_id: int):
    listing = db.get_listing(listing_id)
    if not listing or listing["hidden"] or listing["total"] <= 0:
        return render(request, "notfound.html", {}, status=404)
    return render(request, "game.html", {"listing": listing, "stats": db.stats()})


# --------------------------------------------------------------- user pages


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
        "error": None,
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


# --------------------------------------------------------------------- auth


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
async def logout(request: Request):
    raw = await request.form()
    if not csrf_valid(request, str(raw.get("csrf", ""))):
        # A forced logout is low-harm but still a state change, so refuse it
        # rather than acting on a request the visitor did not make.
        return RedirectResponse("/", status_code=303)
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
    # A signed-in visitor is linking a provider to the account they already
    # have, not signing in as whoever the provider says they are.
    intent = "link" if current_user(request) else "signin"
    resp = RedirectResponse(oauth.authorize_url(provider, state), status_code=303)
    resp.set_cookie(STATE_COOKIE, f"{provider}:{intent}:{state}", httponly=True,
                    samesite="lax", secure=secure_cookies(), max_age=600)
    return resp


@app.get("/auth/{provider}/callback")
def oauth_callback(request: Request, provider: str, code: str = "", state: str = ""):
    if not oauth.is_enabled(provider):
        return render(request, "notfound.html", {}, status=404)

    def fail(msg: str, status: int = 400):
        return render(request, "login.html", {"error": msg, "email": ""}, status=status)

    expected = request.cookies.get(STATE_COOKIE, "")
    if not state or expected not in (
        f"{provider}:link:{state}",
        f"{provider}:signin:{state}",
    ):
        return fail("That sign-in could not be verified. Start again.")
    linking = expected == f"{provider}:link:{state}"
    if not code:
        return fail("That sign-in did not complete. Try again.")

    profile = oauth.fetch_profile(provider, code)
    if not profile:
        return fail("That provider could not be reached. Try again.")

    user = current_user(request)
    if linking and user:
        ok, error = auth.link_provider_to_user(user["id"], profile)
        if not ok:
            return render(request, "account.html", {
                "_user": user,
                "linked": auth.identities_for(user["id"]),
                "stats": db.stats(),
                "error": error,
            }, status=400)
        resp = RedirectResponse("/account", status_code=303)
        resp.delete_cookie(STATE_COOKIE)
        return resp

    found, error = auth.user_from_profile(profile)
    if not found:
        return fail(error)

    resp = RedirectResponse(next_after_login(request), status_code=303)
    set_session_cookie(resp, auth.start_session(found["id"]))
    resp.delete_cookie(STATE_COOKIE)
    return resp


# ------------------------------------------------------------------- admin


def require_admin(request: Request) -> bool:
    return db.session_valid(request.cookies.get("oa_admin"))


@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    if not db.admin_exists():
        return render(request, "admin_claim.html", {"error": None})
    if not require_admin(request):
        return render(request, "admin_login.html", {"error": None})
    return render(
        request,
        "admin.html",
        {
            "pending": db.pending_bids(),
            "listings": db.all_listings(),
            "stats": db.stats(),
            "payment_link": db.get_setting("payment_link"),
            "payment_note": db.get_setting("payment_note"),
            "auto_confirm": db.get_setting("auto_confirm") == "1",
        },
    )


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
    if rate_limited(request, "claim", 10, 3600):
        return render(request, "admin_claim.html", {"error": "Too many tries."}, status=429)
    if len(password) < 10:
        return render(
            request,
            "admin_claim.html",
            {"error": "Use at least 10 characters."},
            status=400,
        )
    if password != confirm:
        return render(
            request, "admin_claim.html", {"error": "The two passwords do not match."}, status=400
        )
    db.set_setting("admin_hash", db.hash_password(password))
    token = db.new_session()
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("oa_admin", token, httponly=True, samesite="lax", max_age=2592000)
    return resp


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request):
    raw = await request.form()
    password = str(raw.get("password", ""))
    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return render(request, "admin_login.html",
                      {"error": "That form expired. Try again."}, status=400)
    if rate_limited(request, "login", 10, 900):
        return render(
            request, "admin_login.html", {"error": "Too many attempts. Wait 15 minutes."}, status=429
        )
    if not db.verify_password(password, db.get_setting("admin_hash")):
        return render(request, "admin_login.html", {"error": "Wrong password."}, status=401)
    token = db.new_session()
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("oa_admin", token, httponly=True, samesite="lax", max_age=2592000)
    return resp


@app.post("/admin/logout")
async def admin_logout(request: Request):
    raw = await request.form()
    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return RedirectResponse("/admin", status_code=303)
    db.drop_session(request.cookies.get("oa_admin"))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("oa_admin")
    return resp


@app.post("/admin/action")
async def admin_action(request: Request):
    if not require_admin(request):
        return RedirectResponse("/admin", status_code=303)
    raw = await request.form()
    action = str(raw.get("action", ""))
    bid_id = int(raw.get("bid_id") or 0)
    listing_id = int(raw.get("listing_id") or 0)
    payment_link = str(raw.get("payment_link", ""))
    payment_note = str(raw.get("payment_note", ""))
    auto_confirm = str(raw.get("auto_confirm", ""))
    if not csrf_valid(request, str(raw.get("csrf", ""))):
        return render(
            request,
            "admin.html",
            {
                "pending": db.pending_bids(),
                "listings": db.all_listings(),
                "stats": db.stats(),
                "payment_link": db.get_setting("payment_link"),
                "payment_note": db.get_setting("payment_note"),
                "auto_confirm": db.get_setting("auto_confirm") == "1",
            },
            status=400,
        )
    if action == "confirm" and bid_id:
        db.confirm_bid(bid_id)
    elif action == "reject" and bid_id:
        db.reject_bid(bid_id)
    elif action == "hide" and listing_id:
        db.set_hidden(listing_id, True)
    elif action == "unhide" and listing_id:
        db.set_hidden(listing_id, False)
    elif action == "delete" and listing_id:
        db.delete_listing(listing_id)
    elif action == "settings":
        db.set_setting("payment_link", clean_url(payment_link))
        db.set_setting("payment_note", payment_note.strip()[:600])
        db.set_setting("auto_confirm", "1" if auto_confirm else "0")
    return RedirectResponse("/admin", status_code=303)
