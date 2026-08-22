"""Outbid Arcade — a pay-to-rank leaderboard for video games.

Rules: pay what you want, the amount is your score, the board sorts by score,
bids never expire and top-ups stack. State lives in SQLite under /data.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db

ASSET_VERSION = "1"
PLATFORMS = ["PC", "Steam", "Switch", "PS5", "Xbox", "Mobile", "Web", "VR", "itch.io"]
SITE_NAME = "Outbid Arcade"
TAGLINE = "The pay-to-rank leaderboard for video games."


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
    data = {
        "request": request,
        "v": ASSET_VERSION,
        "site_name": SITE_NAME,
        "tagline": TAGLINE,
        "platforms": PLATFORMS,
        "min_first": db.MIN_FIRST_BID,
        "min_top_up": db.MIN_TOP_UP,
        "is_admin": db.session_valid(request.cookies.get("oa_admin")),
    }
    data.update(ctx or {})
    return templates.TemplateResponse(request, name, data, status_code=status)


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
            "stats": db.stats(),
            "events": db.recent_events(6),
        },
    )


@app.get("/api/board")
def api_board():
    rows = [
        {
            "rank": r["rank"],
            "id": r["id"],
            "title": r["title"],
            "total": r["total"],
            "url": r["url"],
        }
        for r in db.board()
    ]
    return JSONResponse({"board": rows, "stats": db.stats()})


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

    created = db.create_listing(form, value)
    if db.get_setting("auto_confirm") == "1":
        pending = db.bids_for(created["id"])
        if pending:
            db.confirm_bid(pending[0]["id"])
    return RedirectResponse(f"/listing/{created['token']}", status_code=303)


@app.get("/listing/{token}", response_class=HTMLResponse)
def manage(request: Request, token: str):
    listing = db.get_listing_by_token(token)
    if not listing:
        return render(request, "notfound.html", {}, status=404)
    return render(
        request,
        "manage.html",
        {
            "listing": listing,
            "bids": db.bids_for(listing["id"]),
            "stats": db.stats(),
            "payment_link": db.get_setting("payment_link"),
            "payment_note": db.get_setting("payment_note"),
            "error": None,
        },
    )


@app.post("/listing/{token}/topup", response_class=HTMLResponse)
def topup(request: Request, token: str, amount: str = Form("")):
    listing = db.get_listing_by_token(token)
    if not listing:
        return render(request, "notfound.html", {}, status=404)
    value, err = parse_amount(amount, db.MIN_TOP_UP)
    if not err and rate_limited(request, "topup", 10, 3600):
        err = "Slow down a moment, then try again."
    if err:
        return render(
            request,
            "manage.html",
            {
                "listing": listing,
                "bids": db.bids_for(listing["id"]),
                "stats": db.stats(),
                "payment_link": db.get_setting("payment_link"),
                "payment_note": db.get_setting("payment_note"),
                "error": err,
            },
            status=400,
        )
    bid_id = db.add_bid(listing["id"], value)
    if db.get_setting("auto_confirm") == "1":
        db.confirm_bid(bid_id)
    return RedirectResponse(f"/listing/{token}#bids", status_code=303)


@app.get("/game/{listing_id}", response_class=HTMLResponse)
def game(request: Request, listing_id: int):
    listing = db.get_listing(listing_id)
    if not listing or listing["hidden"] or listing["total"] <= 0:
        return render(request, "notfound.html", {}, status=404)
    return render(request, "game.html", {"listing": listing, "stats": db.stats()})


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
def admin_claim(request: Request, password: str = Form(""), confirm: str = Form("")):
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
def admin_login(request: Request, password: str = Form("")):
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
def admin_logout(request: Request):
    db.drop_session(request.cookies.get("oa_admin"))
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("oa_admin")
    return resp


@app.post("/admin/action")
def admin_action(
    request: Request,
    action: str = Form(""),
    bid_id: int = Form(0),
    listing_id: int = Form(0),
    payment_link: str = Form(""),
    payment_note: str = Form(""),
    auto_confirm: str = Form(""),
):
    if not require_admin(request):
        return RedirectResponse("/admin", status_code=303)
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
