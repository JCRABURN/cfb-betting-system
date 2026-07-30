import requests
import pytest

import backfill_point_in_time_stats as backfill


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def team_entry(team, off_ppa, def_ppa, off_sr, def_sr, havoc):
    return {
        "season": 2023, "team": team, "conference": "SEC",
        "offense": {"ppa": off_ppa, "successRate": off_sr},
        "defense": {"ppa": def_ppa, "successRate": def_sr, "havoc": {"total": havoc}},
    }


@pytest.fixture
def fake_cfbd(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, dict(params)))
        end_week = params.get("endWeek")
        # Values genuinely differ by endWeek, mirroring the live-verified behavior.
        by_week = {
            3: team_entry("Georgia", 0.31, -0.10, 0.52, 0.30, 0.20),
            8: team_entry("Georgia", 0.38, -0.14, 0.53, 0.35, 0.18),
        }
        return FakeResponse([by_week.get(end_week, team_entry("Georgia", 0.40, -0.15, 0.52, 0.38, 0.17))])

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_backfill_week_inserts_point_in_time_row(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    rows, status = backfill.backfill_week(conn, 2023, 3)
    row = conn.execute(
        "SELECT season, week, team, sp_rating, offense_epa_play, defense_epa_play, "
        "offense_success_rate, defense_success_rate, havoc_rate, game_id, source "
        "FROM team_game_stats WHERE season=2023 AND week=3"
    ).fetchone()
    conn.close()

    assert status == "ok"
    assert rows == 1
    assert row == (2023, 3, "Georgia", None, 0.31, -0.10, 0.52, 0.30, 0.20, None, "cfbd_point_in_time")


def test_different_weeks_produce_different_values_not_overwritten(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2023, 3)
    backfill.backfill_week(conn, 2023, 8)
    rows = conn.execute(
        "SELECT week, offense_epa_play FROM team_game_stats "
        "WHERE season=2023 AND team='Georgia' ORDER BY week"
    ).fetchall()
    conn.close()

    assert rows == [(3, 0.31), (8, 0.38)]  # two distinct point-in-time snapshots, not one overwritten row


def test_idempotent_rerun_does_not_duplicate(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2023, 3)
    backfill.backfill_week(conn, 2023, 3)
    count = conn.execute(
        "SELECT COUNT(*) FROM team_game_stats WHERE season=2023 AND week=3"
    ).fetchone()[0]
    conn.close()
    assert count == 1  # not 2


def test_already_ingested_skips_without_refetch(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2023, 3)
    calls_after_first = len(fake_cfbd)
    rows, status = backfill.backfill_week(conn, 2023, 3)
    conn.close()
    assert status == "skipped"
    assert rows == 0
    assert len(fake_cfbd) == calls_after_first


def test_force_replaces_snapshot_not_duplicates(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2023, 3)
    backfill.backfill_week(conn, 2023, 3, force=True)
    count = conn.execute(
        "SELECT COUNT(*) FROM team_game_stats WHERE season=2023 AND week=3"
    ).fetchone()[0]
    conn.close()
    # Unlike betting_lines (append-only by design), exactly one canonical row per
    # (team, season, week) must exist -- --force replaces, it doesn't accumulate.
    assert count == 1


def test_sp_rating_always_null_not_fetched(temp_db, fake_cfbd):
    conn = temp_db.get_connection()
    backfill.backfill_week(conn, 2023, 3)
    row = conn.execute(
        "SELECT sp_rating FROM team_game_stats WHERE season=2023 AND week=3"
    ).fetchone()
    conn.close()
    assert row[0] is None  # SP+ deferred to live-forward capture, never backfilled here


def test_rate_limit_retries_then_succeeds(temp_db, monkeypatch):
    attempts = []

    def flaky_get(url, headers=None, params=None, timeout=None):
        attempts.append(1)
        if len(attempts) < 2:
            return FakeResponse(None, status_code=429)
        return FakeResponse([team_entry("Georgia", 0.31, -0.10, 0.52, 0.30, 0.20)])

    monkeypatch.setattr(requests, "get", flaky_get)
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)

    teams = backfill.fetch_point_in_time_stats(2023, 3)
    assert len(attempts) == 2
    assert teams[0]["team"] == "Georgia"


def test_fetch_returns_none_after_exhausting_retries(temp_db, monkeypatch):
    def always_fails(url, headers=None, params=None, timeout=None):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(requests, "get", always_fails)
    monkeypatch.setattr(backfill.time, "sleep", lambda s: None)

    assert backfill.fetch_point_in_time_stats(2023, 3) is None
