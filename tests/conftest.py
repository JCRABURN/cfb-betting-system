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


@pytest.fixture(autouse=True)
def _no_live_cfbd_games(monkeypatch):
    """backfill_point_in_time_stats.fetch_season_games() is a live CFBD call.
    Default it to "unavailable" for every test so none can reach the network
    by accident; tests of pace logic override it explicitly."""
    import backfill_point_in_time_stats as pit
    monkeypatch.setattr(pit, "fetch_season_games", lambda year: None)
