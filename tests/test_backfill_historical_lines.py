import requests
import pytest

import backfill_historical_lines as backfill


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


FBS_GAME = {
    "id": 500001, "season": 2019, "seasonType": "regular", "week": 1,
    "startDate": "2019-08-31T19:00:00.000Z",
    "homeTeam": "Georgia", "homeClassification": "fbs", "homeScore": 30,
    "awayTeam": "Vanderbilt", "awayClassification": "fbs", "awayScore": 6,
    "lines": [
        {"provider": "consensus", "spread": -24.0, "spreadOpen": -21.0,
         "overUnder": 45.5, "overUnderOpen": 44.0},
        {"provider": "Bovada", "spread": -24.5, "spreadOpen": None,
         "overUnder": 46.0, "overUnderOpen": None},
    ],
}
FCS_GAME = {
    "id": 500002, "season": 2019, "seasonType": "regular", "week": 1,
    "startDate": "2019-08-31T16:00:00.000Z",
    "homeTeam": "Furman", "homeClassification": "fcs", "homeScore": 10,
    "awayTeam": "Wofford", "awayClassification": "fcs", "awayScore": 20,
    "lines": [{"provider": "consensus", "spread": -3.0, "spreadOpen": -3.0,
               "overUnder": 40.0, "overUnderOpen": 40.0}],
}


@pytest.fixture
def fake_cfbd(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, params))
        return FakeResponse([FBS_GAME, FCS_GAME])

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_backfill_week_creates_games_row_from_lines_response(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    rows, status = backfill.backfill_week(conn, 2019, 1, [], {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0})
    conn.close()

    assert status == "ok"
    conn = temp_db.get_connection()
    game = conn.execute(
        "SELECT game_id, home_team, away_team, home_points, away_points, completed "
        "FROM games WHERE game_id = 500001"
    ).fetchone()
    conn.close()
    assert game == (500001, "Georgia", "Vanderbilt", 30, 6, 1)


def test_fcs_vs_fcs_game_skipped_not_inserted(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2019, 1, [], {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0})
    game = conn.execute("SELECT 1 FROM games WHERE game_id = 500002").fetchone()
    lines = conn.execute("SELECT 1 FROM betting_lines WHERE game_id = 500002").fetchone()
    conn.close()
    assert game is None  # classification param is ignored by CFBD, so this must be filtered client-side
    assert lines is None


def test_opening_and_closing_rows_inserted_with_correct_values(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2019, 1, [], {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0})
    rows = conn.execute(
        "SELECT book, line_type, home_spread, total FROM betting_lines "
        "WHERE game_id = 500001 ORDER BY book, line_type"
    ).fetchall()
    conn.close()
    assert rows == [
        ("Bovada", "closing", -24.5, 46.0),
        # Bovada has no spreadOpen/overUnderOpen (both None) -> no opening row
        ("consensus", "closing", -24.0, 45.5),
        ("consensus", "opening", -21.0, 44.0),
    ]


def test_idempotent_rerun_does_not_duplicate(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2019, 1, [], {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0})
    backfill.backfill_week(conn, 2019, 1, [], {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0})
    count = conn.execute("SELECT COUNT(*) FROM betting_lines WHERE game_id = 500001").fetchone()[0]
    conn.close()
    assert count == 3  # not 6


def test_games_upsert_does_not_clobber_richer_existing_row(temp_db, fake_cfbd):
    """A row already written by fetch_stats.py (with venue/lat/long) should keep
    those fields -- the /lines-sourced upsert only touches score/completed."""
    conn = temp_db.get_connection()
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, venue) "
        "VALUES (500001, 2019, 1, 'Georgia', 'Vanderbilt', 'Sanford Stadium')"
    )
    conn.commit()
    backfill.backfill_week(conn, 2019, 1, [], {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0})
    row = conn.execute(
        "SELECT venue, home_points, away_points, completed FROM games WHERE game_id = 500001"
    ).fetchone()
    conn.close()
    assert row == ("Sanford Stadium", 30, 6, 1)


def test_week_already_ingested_skips_without_refetch(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2019, 1, [], {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0})
    calls_after_first = len(fake_cfbd)
    rows, status = backfill.backfill_week(conn, 2019, 1, [], {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0})
    conn.close()
    assert status == "skipped"
    assert len(fake_cfbd) == calls_after_first


def test_rate_limit_retries_then_succeeds(temp_db, monkeypatch):
    attempts = []

    def flaky_get(url, headers=None, params=None, timeout=None):
        attempts.append(1)
        if len(attempts) < 2:
            return FakeResponse(None, status_code=429)
        return FakeResponse([FBS_GAME])

    monkeypatch.setattr(requests, "get", flaky_get)
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)  # skip real backoff delay in tests

    games = backfill.fetch_lines(2019, 1)
    assert len(attempts) == 2
    assert games == [FBS_GAME]


def test_fetch_lines_returns_none_after_exhausting_retries(temp_db, monkeypatch):
    def always_fails(url, headers=None, params=None, timeout=None):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(requests, "get", always_fails)
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)

    assert backfill.fetch_lines(2019, 1) is None
