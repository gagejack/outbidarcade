"""Fixtures that give every test a private, empty database.

db.py reads DATA_DIR at import time, so the env var must be set and the
module reloaded before the app is imported.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def app_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://localhost:8080")
    import db

    importlib.reload(db)
    import main

    importlib.reload(main)
    db.init_db()
    return main, db


@pytest.fixture
def client(app_modules):
    from fastapi.testclient import TestClient

    main, _ = app_modules
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def database(app_modules):
    _, db = app_modules
    return db
