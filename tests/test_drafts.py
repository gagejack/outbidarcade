import importlib
import time

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
    "amount": "12",
    "platforms": "PC,VR",
}


def test_draft_round_trips(auth):
    assert auth.load_draft(auth.save_draft(FORM)) == FORM


def test_unknown_draft_is_none(auth):
    assert auth.load_draft("nope") is None
    assert auth.load_draft(None) is None


def test_deleted_draft_is_gone(auth):
    draft_id = auth.save_draft(FORM)
    auth.delete_draft(draft_id)
    assert auth.load_draft(draft_id) is None


def test_expired_draft_is_not_returned(auth, database):
    draft_id = auth.save_draft(FORM)
    stale = int(time.time()) - auth.DRAFT_TTL - 60
    with database.connect() as conn:
        conn.execute("UPDATE drafts SET created_at=? WHERE id=?", (stale, draft_id))
    assert auth.load_draft(draft_id) is None


def test_draft_ids_are_unpredictable(auth):
    assert len({auth.save_draft(FORM) for _ in range(50)}) == 50


def test_claim_returns_the_payload_once(auth):
    draft_id = auth.save_draft(FORM)
    assert auth.claim_draft(draft_id) == FORM
    assert auth.claim_draft(draft_id) is None


def test_claim_of_unknown_or_missing_id_is_none(auth):
    assert auth.claim_draft("nope") is None
    assert auth.claim_draft(None) is None
    assert auth.claim_draft("") is None


def test_claim_ignores_expired_drafts(auth, database):
    draft_id = auth.save_draft(FORM)
    stale = int(time.time()) - auth.DRAFT_TTL - 60
    with database.connect() as conn:
        conn.execute("UPDATE drafts SET created_at=? WHERE id=?", (stale, draft_id))
    assert auth.claim_draft(draft_id) is None


def test_concurrent_claims_yield_exactly_one_winner(auth):
    """The race the sequential tests cannot reach: only one caller may win."""
    import threading
    draft_id = auth.save_draft(FORM)
    results = []
    lock = threading.Lock()

    def worker():
        got = auth.claim_draft(draft_id)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
