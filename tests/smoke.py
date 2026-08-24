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
