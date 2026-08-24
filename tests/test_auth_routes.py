def register(client, email="dev@studio.com", password="correct horse battery"):
    page = client.get("/register")
    token = extract_csrf(page.text)
    return client.post(
        "/register",
        data={"email": email, "password": password, "confirm": password,
              "display_name": "Dev", "csrf": token},
        follow_redirects=False,
    )


def extract_csrf(html):
    import re
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf field in form"
    return match.group(1)


def test_register_creates_a_session(client):
    resp = register(client)
    assert resp.status_code == 303
    assert client.cookies.get("oa_user")


def test_register_rejects_a_short_password(client):
    page = client.get("/register")
    resp = client.post("/register", data={
        "email": "dev@studio.com", "password": "short", "confirm": "short",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 400


def test_register_rejects_mismatched_confirmation(client):
    page = client.get("/register")
    resp = client.post("/register", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "confirm": "something else entirely",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 400


def test_register_rejects_a_duplicate_email(client):
    register(client)
    client.post("/logout", data={"csrf": "x"}, follow_redirects=False)
    page = client.get("/register")
    resp = client.post("/register", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "confirm": "correct horse battery",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 400


def test_register_without_csrf_is_refused(client):
    client.get("/register")
    resp = client.post("/register", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "confirm": "correct horse battery"}, follow_redirects=False)
    assert resp.status_code == 400


def test_login_then_logout(client):
    register(client)
    page = client.get("/login")
    resp = client.post("/login", data={
        "email": "dev@studio.com", "password": "correct horse battery",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 303
    logout = client.post("/logout", data={"csrf": extract_csrf(page.text)},
                         follow_redirects=False)
    assert logout.status_code == 303


def test_login_with_a_wrong_password_fails(client):
    register(client)
    page = client.get("/login")
    resp = client.post("/login", data={
        "email": "dev@studio.com", "password": "not the password",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 401


def test_login_page_hides_providers_when_unconfigured(client):
    assert "Continue with Google" not in client.get("/login").text


def test_forgot_says_the_same_thing_for_any_address(client):
    register(client)
    page = client.get("/forgot")
    known = client.post("/forgot", data={"email": "dev@studio.com",
                                         "csrf": extract_csrf(page.text)})
    unknown = client.post("/forgot", data={"email": "nobody@nowhere.com",
                                           "csrf": extract_csrf(page.text)})
    assert known.status_code == unknown.status_code == 200
    assert known.text == unknown.text


def test_reset_sets_a_new_password_and_kills_sessions(client, app_modules):
    main, _ = app_modules
    import auth
    register(client)
    user = auth.get_user_by_email("dev@studio.com")
    token = auth.issue_reset(user["id"])
    page = client.get(f"/reset/{token}")
    assert page.status_code == 200
    resp = client.post(f"/reset/{token}", data={
        "password": "a completely new secret", "confirm": "a completely new secret",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert resp.status_code == 303
    assert auth.check_login("dev@studio.com", "correct horse battery") is None
    assert auth.check_login("dev@studio.com", "a completely new secret")


def test_a_used_reset_link_stops_working(client, app_modules):
    import auth
    register(client)
    user = auth.get_user_by_email("dev@studio.com")
    token = auth.issue_reset(user["id"])
    page = client.get(f"/reset/{token}")
    client.post(f"/reset/{token}", data={
        "password": "a completely new secret", "confirm": "a completely new secret",
        "csrf": extract_csrf(page.text)}, follow_redirects=False)
    assert client.get(f"/reset/{token}").status_code == 404


def test_oauth_start_is_404_when_provider_is_off(client):
    assert client.get("/auth/google", follow_redirects=False).status_code == 404


def test_unknown_provider_is_404(client):
    assert client.get("/auth/facebook", follow_redirects=False).status_code == 404
