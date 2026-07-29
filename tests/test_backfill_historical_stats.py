import requests
import pytest

import fetch_stats
import backfill_historical_stats as backfill


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


FIXTURES = {
    "/ratings/sp": [
        {"team": "Georgia", "rating": 30.5},
        {"team": "Alabama", "rating": 28.0},
    ],
    "/stats/season/advanced": [
        {
            "team": "Georgia",
            "offense": {"epa_per_play": 0.25, "successRate": 0.48},
            "defense": {"epa_per_play": -0.15, "successRate": 0.35, "havoc": {"total": 0.18}},
        },
        {
            "team": "Alabama",
            "offense": {"epa_per_play": 0.20, "successRate": 0.44},
            "defense": {"epa_per_play": -0.10, "successRate": 0.38, "havoc": {"total": 0.15}},
        },
    ],
    "/records": [
        {"team": "Georgia", "total": {"wins": 13, "losses": 1}},
        {"team": "Alabama", "total": {"wins": 12, "losses": 2}},
    ],
    "/teams": [
        {"id": 61, "school": "Georgia", "conference": "SEC", "classification": "fbs"},
        {"id": 333, "school": "Alabama", "conference": "SEC", "classification": "fbs"},
    ],
}


@pytest.fixture
def fake_cfbd(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        for suffix, payload in FIXTURES.items():
            if url.endswith(suffix):
                return FakeResponse(payload)
        raise AssertionError(f"Unexpected URL requested in test: {url}")

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_backfill_inserts_expected_rows(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    total = backfill.backfill_season(conn, 2019)
    conn.close()

    assert total == 2

    conn = temp_db.get_connection()
    rows = conn.execute(
        "SELECT team, sp_rating, offense_epa_play, defense_epa_play, "
        "offense_success_rate, defense_success_rate, havoc_rate, wins, losses, "
        "game_id, week, source "
        "FROM team_game_stats WHERE season = 2019 ORDER BY team"
    ).fetchall()
    conn.close()

    assert rows == [
        ("Alabama", 28.0, 0.20, -0.10, 0.44, 0.38, 0.15, 12, 2, None, None, "cfbd_historical_backfill"),
        ("Georgia", 30.5, 0.25, -0.15, 0.48, 0.35, 0.18, 13, 1, None, None, "cfbd_historical_backfill"),
    ]


def test_backfill_is_idempotent(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_season(conn, 2019)
    backfill.backfill_season(conn, 2019)  # rerun without --force
    count = conn.execute(
        "SELECT COUNT(*) FROM team_game_stats WHERE season = 2019"
    ).fetchone()[0]
    conn.close()
    assert count == 2  # not 4


def test_backfill_skips_api_calls_for_already_ingested_season(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_season(conn, 2019)
    calls_after_first_run = len(fake_cfbd)

    backfill.backfill_season(conn, 2019)  # should skip entirely, no new API calls
    conn.close()

    assert len(fake_cfbd) == calls_after_first_run


def test_backfill_force_refetches(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_season(conn, 2019)
    calls_after_first_run = len(fake_cfbd)

    backfill.backfill_season(conn, 2019, force=True)
    conn.close()

    assert len(fake_cfbd) > calls_after_first_run


def test_upsert_teams(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    teams = backfill.fetch_teams(2019)
    backfill.upsert_teams(conn, teams)

    rows = conn.execute("SELECT team_id, school, conference FROM teams ORDER BY school").fetchall()
    conn.close()

    assert rows == [(333, "Alabama", "SEC"), (61, "Georgia", "SEC")]


def test_upsert_teams_updates_not_duplicates(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    teams = backfill.fetch_teams(2019)
    backfill.upsert_teams(conn, teams)

    # Simulate conference realignment and re-upsert
    teams[0]["conference"] = "Big Ten"
    backfill.upsert_teams(conn, teams)

    count = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    conn.close()
    assert count == 2  # still 2 rows, not 4 — upsert, not append
