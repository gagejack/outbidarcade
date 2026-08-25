import importlib

import pytest


@pytest.fixture
def auth(app_modules):
    import auth
    importlib.reload(auth)
    return auth


FORM = {
    "title": "Ghost Signal",
    "tagline": "A submarine horror game played by sonar alone",
    "url": "https://ghostsignal.example",
    "image_url": "",
    "studio": "Two people and a cat",
    "email": "dev@studio.com",
    "platforms": "PC,VR",
}


def test_listing_records_its_owner(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    assert database.get_listing(created["id"])["user_id"] == uid


def test_created_listing_has_no_token(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    assert "token" not in database.create_listing(FORM, 12, uid)


def test_owner_owns_their_listing(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    assert database.owns_listing(uid, created["id"]) is True


def test_stranger_does_not_own_it(auth, database):
    owner = auth.create_user("dev@studio.com", "correct horse battery")
    other = auth.create_user("someone@else.com", "correct horse battery")
    created = database.create_listing(FORM, 12, owner)
    assert database.owns_listing(other, created["id"]) is False


def test_ownership_of_a_missing_listing_is_false(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    assert database.owns_listing(uid, 999) is False


def test_user_listings_are_listed_newest_first(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    first = database.create_listing(dict(FORM, title="First"), 12, uid)
    second = database.create_listing(dict(FORM, title="Second"), 12, uid)
    ids = [row["id"] for row in database.listings_for_user(uid)]
    assert ids == [second["id"], first["id"]]


def test_user_listings_exclude_other_owners(auth, database):
    owner = auth.create_user("dev@studio.com", "correct horse battery")
    other = auth.create_user("someone@else.com", "correct horse battery")
    database.create_listing(FORM, 12, owner)
    assert database.listings_for_user(other) == []


def test_update_changes_fields_and_slug(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    database.update_listing(created["id"], dict(FORM, title="Deep Signal",
                                                tagline="Now with more sonar and dread"))
    listing = database.get_listing(created["id"])
    assert listing["title"] == "Deep Signal"
    assert listing["slug"] == "deep-signal"


def test_update_does_not_touch_money(auth, database):
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    bid = database.bids_for(created["id"])[0]
    database.confirm_bid(bid["id"])
    before = database.get_listing(created["id"])["total"]
    database.update_listing(created["id"], dict(FORM, title="Deep Signal"))
    assert database.get_listing(created["id"])["total"] == before


def test_edit_shows_on_the_board_immediately(auth, database):
    """db.py caches the board. An edit must drop that cache."""
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    created = database.create_listing(FORM, 12, uid)
    database.confirm_bid(database.bids_for(created["id"])[0]["id"])
    assert database.board()[0]["title"] == "Ghost Signal"  # warms the cache
    database.update_listing(created["id"], dict(FORM, title="Deep Signal"))
    assert database.board()[0]["title"] == "Deep Signal", (
        "update_listing must call _invalidate_board()"
    )


def test_submitting_does_not_drop_the_board_cache(auth, database):
    """A pending bid is not on the board, so the cache must survive."""
    uid = auth.create_user("dev@studio.com", "correct horse battery")
    first = database.create_listing(FORM, 12, uid)
    database.confirm_bid(database.bids_for(first["id"])[0]["id"])
    database.board()  # warm
    database.create_listing(dict(FORM, title="Second Game"), 12, uid)
    assert database._board_cache is not None, (
        "create_listing should not invalidate: pending bids are not on the board"
    )
