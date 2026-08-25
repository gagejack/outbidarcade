import re


def extract_csrf(html):
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field in form"
    return match.group(1)


def claim(client):
    page = client.get("/admin")
    return client.post("/admin/claim", data={
        "password": "hunter2hunter2", "confirm": "hunter2hunter2",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)


def test_claim_needs_a_token(client):
    client.get("/admin")
    resp = client.post("/admin/claim", data={
        "password": "hunter2hunter2", "confirm": "hunter2hunter2"},
        follow_redirects=False)
    assert resp.status_code == 400


def test_claim_works_with_a_token(client):
    assert claim(client).status_code == 303


def test_admin_action_needs_a_token(client, database):
    claim(client)
    page = client.get("/admin")
    resp = client.post("/admin/action", data={"action": "settings",
                                              "payment_link": "https://pay.example"},
                       follow_redirects=False)
    assert resp.status_code == 400
    assert database.get_setting("payment_link") == ""


def test_admin_action_works_with_a_token(client, database):
    claim(client)
    page = client.get("/admin")
    resp = client.post("/admin/action", data={
        "action": "settings", "payment_link": "https://pay.example",
        "payment_note": "", "csrf": extract_csrf(page.text)},
        follow_redirects=False)
    assert resp.status_code == 303
    assert database.get_setting("payment_link") == "https://pay.example"


def test_operator_login_needs_a_token(client):
    claim(client)
    client.post("/admin/logout", data={"csrf": "wrong"}, follow_redirects=False)
    resp = client.post("/admin/login", data={"password": "hunter2hunter2"},
                       follow_redirects=False)
    assert resp.status_code == 400


def test_logout_needs_a_token(client):
    page = client.get("/register")
    client.post("/register", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "confirm": "correct horse battery", "csrf": extract_csrf(page.text)},
        follow_redirects=False)
    assert client.cookies.get("oa_user")
    client.post("/logout", data={"csrf": "forged"}, follow_redirects=False)
    assert client.cookies.get("oa_user"), "a forged token must not end the session"


def test_logout_works_with_a_token(client):
    page = client.get("/register")
    client.post("/register", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "confirm": "correct horse battery", "csrf": extract_csrf(page.text)},
        follow_redirects=False)
    token = extract_csrf(client.get("/").text)
    client.post("/logout", data={"csrf": token}, follow_redirects=False)
    assert not client.cookies.get("oa_user")
