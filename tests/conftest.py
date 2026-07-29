import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (ROOT, os.path.join(ROOT, "data"), os.path.join(ROOT, "models")):
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest
import db


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point db.DB_PATH at an isolated temp file for the duration of a test."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test_cfb.db"))
    db.init_db()
    return db
