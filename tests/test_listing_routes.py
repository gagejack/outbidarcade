import re

FORM = {
    "title": "Ghost Signal",
    "tagline": "A submarine horror game played by sonar alone",
    "url": "ghostsignal.example",
    "platforms": ["PC", "VR"],
    "amount": "12",
    "email": "dev@studio.com",
}


def extract_csrf(html):
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field in form"
    return match.group(1)


def register(client, email):
    page = client.get("/register")
    return client.post("/register", data={
        "email": email, "password": "correct horse battery",
        "confirm": "correct horse battery", "csrf": extract_csrf(page.text)},
        follow_redirects=False)


def make_listing(client):
    page = client.get("/submit")
    resp = client.post("/submit", data=dict(FORM, csrf=extract_csrf(page.text)),
                       follow_redirects=False)
    return int(resp.headers["location"].rsplit("/", 1)[1])


def test_dashboard_requires_sign_in(client):
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_dashboard_lists_your_games(client):
    register(client, "dev@studio.com")
    make_listing(client)
    assert "Ghost Signal" in client.get("/dashboard").text


def test_dashboard_hides_other_peoples_games(client):
    register(client, "dev@studio.com")
    make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    register(client, "someone@else.com")
    assert "Ghost Signal" not in client.get("/dashboard").text


def test_owner_sees_the_manage_page(client):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    assert client.get(f"/listing/{listing_id}").status_code == 200


def test_stranger_gets_404_on_manage(client):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    register(client, "someone@else.com")
    assert client.get(f"/listing/{listing_id}").status_code == 404


def test_signed_out_visitor_gets_404_on_manage(client):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    assert client.get(f"/listing/{listing_id}").status_code == 404


def test_owner_can_edit(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    page = client.get(f"/listing/{listing_id}/edit")
    assert page.status_code == 200
    resp = client.post(f"/listing/{listing_id}/edit", data={
        "title": "Deep Signal",
        "tagline": "Now with more sonar and much more dread",
        "url": "deepsignal.example",
        "image_url": "",
        "studio": "Two people and a cat",
        "platforms": ["PC"],
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 303
    listing = database.get_listing(listing_id)
    assert listing["title"] == "Deep Signal"
    assert listing["platforms"] == "PC"


def test_editing_cannot_change_the_total(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    database.confirm_bid(database.bids_for(listing_id)[0]["id"])
    before = database.get_listing(listing_id)["total"]
    page = client.get(f"/listing/{listing_id}/edit")
    client.post(f"/listing/{listing_id}/edit", data={
        "title": "Deep Signal", "tagline": "Now with more sonar and much more dread",
        "url": "deepsignal.example", "amount": "99999", "total": "99999",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert database.get_listing(listing_id)["total"] == before


def test_stranger_cannot_edit(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    register(client, "someone@else.com")
    assert client.get(f"/listing/{listing_id}/edit").status_code == 404
    page = client.get("/dashboard")
    resp = client.post(f"/listing/{listing_id}/edit", data={
        "title": "Stolen", "tagline": "This should never be saved",
        "url": "evil.example", "csrf": extract_csrf(page.text)},
        follow_redirects=False)
    assert resp.status_code == 404
    assert database.get_listing(listing_id)["title"] == "Ghost Signal"


def test_edit_rejects_a_bad_url(client):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    page = client.get(f"/listing/{listing_id}/edit")
    resp = client.post(f"/listing/{listing_id}/edit", data={
        "title": "Deep Signal", "tagline": "Now with more sonar and much more dread",
        "url": "not a url at all", "csrf": extract_csrf(page.text)},
        follow_redirects=False)
    assert resp.status_code == 400


def test_owner_can_top_up(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    page = client.get(f"/listing/{listing_id}")
    resp = client.post(f"/listing/{listing_id}/topup", data={
        "amount": "8", "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 303
    assert len(database.bids_for(listing_id)) == 2


def test_stranger_cannot_top_up(client, database):
    register(client, "dev@studio.com")
    listing_id = make_listing(client)
    client.post("/logout", data={"csrf": extract_csrf(client.get("/login").text)},
                follow_redirects=False)
    register(client, "someone@else.com")
    page = client.get("/dashboard")
    resp = client.post(f"/listing/{listing_id}/topup", data={
        "amount": "8", "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 404
    assert len(database.bids_for(listing_id)) == 1


def test_account_page_shows_the_email(client):
    register(client, "dev@studio.com")
    assert "dev@studio.com" in client.get("/account").text


def test_account_requires_sign_in(client):
    resp = client.get("/account", follow_redirects=False)
    assert resp.status_code == 303


def test_linking_attaches_to_the_existing_account(client, app_modules):
    """The bug this fixes: linking used to create a second account when the
    provider's email differed from the registered one."""
    import auth
    main, _ = app_modules
    register(client, "dev@studio.com")
    user = auth.get_user_by_email("dev@studio.com")

    profile = {"provider": "google", "uid": "g-1",
               "email": "dev@work-example.com",  # deliberately different
               "email_verified": True, "name": "Dev"}
    ok, error = auth.link_provider_to_user(user["id"], profile)
    assert ok and error == ""
    assert auth.identities_for(user["id"]) == ["google"]
    assert auth.get_user_by_email("dev@work-example.com") is None, (
        "linking must not create a second account"
    )


def test_linking_is_idempotent(client, app_modules):
    import auth
    register(client, "dev@studio.com")
    user = auth.get_user_by_email("dev@studio.com")
    profile = {"provider": "google", "uid": "g-1", "email": "dev@studio.com",
               "email_verified": True, "name": "Dev"}
    assert auth.link_provider_to_user(user["id"], profile)[0] is True
    assert auth.link_provider_to_user(user["id"], profile)[0] is True
    assert auth.identities_for(user["id"]) == ["google"]


def test_linking_refuses_an_identity_owned_by_someone_else(client, app_modules):
    import auth
    first = auth.create_user("first@studio.com", "correct horse battery")
    second = auth.create_user("second@studio.com", "correct horse battery")
    profile = {"provider": "google", "uid": "g-1", "email": "first@studio.com",
               "email_verified": True, "name": "First"}
    assert auth.link_provider_to_user(first, profile)[0] is True
    ok, error = auth.link_provider_to_user(second, profile)
    assert ok is False
    assert "already linked" in error
    assert auth.identities_for(second) == []


def test_linking_does_not_require_a_verified_email(client, app_modules):
    """The session proves identity, so the takeover risk that forces
    verification in user_from_profile does not apply here."""
    import auth
    user = auth.create_user("dev@studio.com", "correct horse battery")
    profile = {"provider": "github", "uid": "h-1", "email": "dev@studio.com",
               "email_verified": False, "name": "Dev"}
    assert auth.link_provider_to_user(user, profile)[0] is True
    assert auth.identities_for(user) == ["github"]
