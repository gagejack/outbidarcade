def test_client_serves_the_board(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_each_test_gets_an_empty_database(client, database):
    assert database.board() == []


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}
