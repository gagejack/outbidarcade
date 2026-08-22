"""End to end smoke test for the board, run against a live instance.

    DATA_DIR=/tmp/oa uvicorn main:app --port 8099 &
    python tests/smoke.py http://127.0.0.1:8099

Point it at a THROWAWAY database: it claims the operator account and deletes
what it creates.
"""

import re, urllib.request, urllib.parse, http.cookiejar as cj
import os, sys
B = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BASE", "http://127.0.0.1:8000")
jar = cj.CookieJar(); op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
def get(p, ip="1.2.3.4"):
    r = urllib.request.Request(B+p, headers={"X-Forwarded-For": ip}); return op.open(r).read().decode()
def post(p, data, ip="1.2.3.4"):
    r = urllib.request.Request(B+p, data=urllib.parse.urlencode(data, doseq=True).encode(),
                               headers={"X-Forwarded-For": ip})
    resp = op.open(r); return resp.geturl(), resp.read().decode()

post("/admin/claim", {"password": "hunter2hunter2", "confirm": "hunter2hunter2"})
url, _ = post("/submit", {"title": "Ghost Signal", "tagline": "A submarine horror game played by sonar alone",
                          "url": "ghostsignal.example", "platforms": ["PC", "VR"], "amount": "12",
                          "email": "dev@example.com"}, ip="5.5.5.5")
assert "WAITING ON PAYMENT" in get(url.replace(B, "")), "should be pending"
assert "Ghost Signal" not in get("/"), "pending listing must not be public"
admin = get("/admin")
bid = re.search(r'name="bid_id" value="(\d+)"', admin).group(1)
post("/admin/action", {"action": "confirm", "bid_id": bid})
home = get("/")
assert "Ghost Signal" in home and "#1 ON THE BOARD" in home, "should be live at #1"
assert "$13" in home, "cost to take #1 should be $13"
# top up
tok = url.rsplit("/", 1)[1]
post(f"/listing/{tok}/topup", {"amount": "8"}, ip="5.5.5.5")
bid2 = re.search(r'name="bid_id" value="(\d+)"', get("/admin")).group(1)
post("/admin/action", {"action": "confirm", "bid_id": bid2})
assert "$20" in get("/"), "total should stack to $20"
lid = re.search(r'name="listing_id" value="(\d+)"', get("/admin")).group(1)
post("/admin/action", {"action": "hide", "listing_id": lid})
assert "Ghost Signal" not in get("/"), "hidden listing must vanish from the board"
post("/admin/action", {"action": "unhide", "listing_id": lid})
assert "Ghost Signal" in get("/"), "unhide should restore it"
post("/admin/action", {"action": "delete", "listing_id": lid})
assert "Ghost Signal" not in get("/") and '"board":[]' in get("/api/board"), "delete should clear it"
print("all flow assertions passed")
