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
    # auto_confirm is off by default, so payment is still due: the redirect
    # goes to /checkout/{bid_id} (Stripe), not straight to the listing.
    assert "/checkout/" in resp.headers["location"]
    assert len(database.all_listings()) == 1


def test_double_resume_creates_only_one_listing(client, database):
    submit(client)
    register(client)
    client.get("/submit/resume", follow_redirects=False)
    client.get("/submit/resume", follow_redirects=False)
    assert len(database.all_listings()) == 1
    assert len(database.bids_for(database.all_listings()[0]["id"])) == 1
