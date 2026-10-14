"""Pace data: plays/drives capture, authoritative games-played, pace-only
backfill, and the point-in-time accessor (2026-10-14)."""
import pytest

import backfill_point_in_time_stats as pit
import backtest_harness as bh


def stats_entry(team, ppa=0.1, plays=300, drives=40, d_plays=290, d_drives=39):
    return {"team": team,
            "offense": {"ppa": ppa, "successRate": 0.5, "plays": plays, "drives": drives},
            "defense": {"ppa": -0.05, "successRate": 0.4, "plays": d_plays, "drives": d_drives,
                        "havoc": {"total": 0.15}}}


def game(week, home, away, completed=True):
    return {"week": week, "homeTeam": home, "awayTeam": away, "completed": completed}


def seed_row(conn, team, week, season=2025, plays=None, gp=None, epa=0.1):
    conn.execute(
        "INSERT INTO team_game_stats (season, week, team, offense_epa_play, source, fetched_at, "
        "offense_plays, defense_plays, offense_drives, defense_drives, games_played) "
        "VALUES (?, ?, ?, ?, 'cfbd_point_in_time', 'now', ?, ?, ?, ?, ?)",
        (season, week, team, epa, plays, plays, 40 if plays else None, 39 if plays else None, gp))


# ---- games_played_by_team (pure) ------------------------------------------------

def test_games_played_counts_both_sides_through_end_week_only():
    games = [game(1, "A", "B"), game(2, "A", "C"), game(3, "B", "C")]
    assert pit.games_played_by_team(games, 2) == {"A": 2, "B": 1, "C": 1}
    assert pit.games_played_by_team(games, 3) == {"A": 2, "B": 2, "C": 2}


def test_games_played_ignores_unplayed_games():
    games = [game(1, "A", "B"), game(2, "A", "C", completed=False)]
    assert pit.games_played_by_team(games, 5) == {"A": 1, "B": 1}


def test_games_played_empty_or_none_is_empty():
    assert pit.games_played_by_team(None, 5) == {}
    assert pit.games_played_by_team([], 5) == {}


# ---- capture from the response the weekly path already receives ------------------

def test_backfill_week_stores_plays_and_drives_from_the_same_response(temp_db, monkeypatch):
    monkeypatch.setattr(pit, "fetch_point_in_time_stats",
                        lambda year, week: [stats_entry("Georgia", plays=312, drives=41, d_plays=288, d_drives=40)])
    conn = temp_db.get_connection()
    pit.backfill_week(conn, 2025, 5)
    row = conn.execute("SELECT offense_plays, defense_plays, offense_drives, defense_drives, games_played "
                       "FROM team_game_stats WHERE team='Georgia'").fetchone()
    conn.close()
    assert row == (312, 288, 41, 40, None)      # games_played is filled separately, never guessed


def test_missing_plays_field_is_stored_as_null_not_zero(temp_db, monkeypatch):
    entry = {"team": "X", "offense": {"ppa": 0.1}, "defense": {"ppa": 0.0, "havoc": {}}}
    monkeypatch.setattr(pit, "fetch_point_in_time_stats", lambda year, week: [entry])
    conn = temp_db.get_connection()
    pit.backfill_week(conn, 2025, 5)
    assert conn.execute("SELECT offense_plays FROM team_game_stats").fetchone()[0] is None
    conn.close()


# ---- pace-only backfill -----------------------------------------------------------

def test_pace_backfill_fills_pace_and_leaves_epa_exactly_as_stored(temp_db, monkeypatch):
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 5, epa=0.3141)          # historical row: no pace yet
    conn.commit()
    monkeypatch.setattr(pit, "fetch_point_in_time_stats",
                        lambda year, week: [stats_entry("Georgia", ppa=0.3141, plays=300, drives=40)])
    games = [game(w, "Georgia", f"Opp{w}") for w in range(1, 6)]

    n, status = pit.backfill_pace_week(conn, 2025, 5, games=games)
    row = conn.execute("SELECT offense_epa_play, offense_plays, games_played FROM team_game_stats").fetchone()
    conn.close()
    assert (n, status) == (1, "ok")
    assert row == (0.3141, 300, 5)


def test_pace_backfill_is_idempotent_and_makes_no_api_call_when_complete(temp_db, monkeypatch):
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 5, plays=300, gp=5)
    conn.commit()
    called = []
    monkeypatch.setattr(pit, "fetch_point_in_time_stats", lambda y, w: called.append(1))
    assert pit.backfill_pace_week(conn, 2025, 5, games=[]) == (0, "skipped")
    assert called == []
    conn.close()


def test_pace_backfill_without_games_fills_plays_but_leaves_games_played_null(temp_db, monkeypatch):
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 5)
    conn.commit()
    monkeypatch.setattr(pit, "fetch_point_in_time_stats", lambda y, w: [stats_entry("Georgia", plays=300)])
    n, status = pit.backfill_pace_week(conn, 2025, 5, games=None)
    row = conn.execute("SELECT offense_plays, games_played FROM team_game_stats").fetchone()
    conn.close()
    assert (n, status) == (1, "no_games")
    assert row == (300, None)


def test_pace_backfill_reports_failed_fetch_and_changes_nothing(temp_db, monkeypatch):
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 5)
    conn.commit()
    monkeypatch.setattr(pit, "fetch_point_in_time_stats", lambda y, w: None)
    assert pit.backfill_pace_week(conn, 2025, 5, games=[game(1, "Georgia", "X")]) == (0, "failed")
    assert conn.execute("SELECT offense_plays FROM team_game_stats").fetchone()[0] is None
    conn.close()


def test_team_missing_from_response_is_left_untouched(temp_db, monkeypatch):
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 5)
    seed_row(conn, "Ghost U", 5)
    conn.commit()
    monkeypatch.setattr(pit, "fetch_point_in_time_stats", lambda y, w: [stats_entry("Georgia", plays=300)])
    pit.backfill_pace_week(conn, 2025, 5, games=[game(1, "Georgia", "X")])
    rows = dict(conn.execute("SELECT team, offense_plays FROM team_game_stats").fetchall())
    conn.close()
    assert rows == {"Georgia": 300, "Ghost U": None}


def test_fill_games_played_when_games_endpoint_unavailable(temp_db):
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 5, plays=300)
    conn.commit()
    # conftest's autouse fixture makes fetch_season_games return None
    assert pit.fill_games_played(conn, 2025, 5) == (0, "no_games")
    conn.close()


def test_fill_games_played_uses_cfbd_games_not_the_local_table(temp_db, monkeypatch):
    """The reason this exists: our `games` table only has lined games."""
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 5, plays=300)
    conn.commit()
    # Local table knows of ONE Georgia game; CFBD knows of five (four vs unlined FCS teams).
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team, completed) "
                 "VALUES (1, 2025, 1, 'Georgia', 'Opp', 1)")
    conn.commit()
    monkeypatch.setattr(pit, "fetch_season_games",
                        lambda year: [game(w, "Georgia", f"Opp{w}") for w in range(1, 6)])
    pit.fill_games_played(conn, 2025, 5)
    gp = conn.execute("SELECT games_played FROM team_game_stats").fetchone()[0]
    conn.close()
    assert gp == 5


# ---- point-in-time accessor ---------------------------------------------------------

def test_get_pace_as_of_derives_rates_and_is_strictly_before_week(temp_db):
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 3, plays=240, gp=3)       # 80 plays/game
    seed_row(conn, "Georgia", 4, plays=300, gp=4)       # 75 plays/game
    conn.commit()
    p = bh.get_pace_as_of(conn, "Georgia", 2025, 5)     # for a week-5 game: uses week 4
    conn.close()
    assert p["as_of_week"] == 4 and p["games_played"] == 4
    assert p["plays_per_game"] == 75.0
    assert p["game_plays_per_game"] == pytest.approx((300 + 300) / 4)   # seed sets def plays = off plays
    assert p["drives_per_game"] == 10.0 and p["plays_per_drive"] == 7.5


def test_get_pace_as_of_never_uses_the_games_own_week(temp_db):
    """Week N's snapshot already includes week N's game: lookahead."""
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 5, plays=400, gp=5)
    conn.commit()
    assert bh.get_pace_as_of(conn, "Georgia", 2025, 5) is None
    conn.close()


def test_get_pace_as_of_none_without_games_played(temp_db):
    conn = temp_db.get_connection()
    seed_row(conn, "Georgia", 4, plays=300, gp=None)
    conn.commit()
    assert bh.get_pace_as_of(conn, "Georgia", 2025, 5) is None
    conn.close()
