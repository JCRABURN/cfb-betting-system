import backfill_nfl_games as bng


def make_row(game_id, season, week, home, away, home_score=None, away_score=None,
             spread_line="", total_line=""):
    return {
        "game_id": game_id, "season": str(season), "week": str(week),
        "game_type": "REG", "gameday": "2021-09-09",
        "home_team": home, "away_team": away,
        "home_score": str(home_score) if home_score is not None else "",
        "away_score": str(away_score) if away_score is not None else "",
        "spread_line": spread_line, "total_line": total_line,
    }


def test_ingest_season_populates_nfl_games(temp_db):
    conn = temp_db.get_connection()
    rows = [make_row("2021_01_DAL_TB", 2021, 1, "TB", "DAL", 31, 29, "10", "52.5")]
    games_added, lines_added, status = bng.ingest_season(conn, rows, 2021)
    row = conn.execute(
        "SELECT game_id, season, week, home_team, away_team, home_score, away_score, completed "
        "FROM nfl_games WHERE game_id = '2021_01_DAL_TB'"
    ).fetchone()
    conn.close()
    assert status == "ok"
    assert games_added == 1
    assert row == ("2021_01_DAL_TB", 2021, 1, "TB", "DAL", 31, 29, 1)


def test_spread_sign_is_flipped_to_this_projects_convention(temp_db):
    """The exact bug caught live 2026-08-24: nflverse's own spread_line
    convention is positive=home-favored -- the OPPOSITE of this project's
    home_spread convention (negative=home-favored, used everywhere else:
    fetch_odds.py, backfill_historical_lines.py,
    backfill_nfl_historical_lines.py). Real case: Dallas @ TampaBay 2021
    week 1, TB (home) favored by 10 at close -- nflverse's spread_line is
    literally '10' (positive), and this must land in betting_lines as
    home_spread=-10.0, not +10.0."""
    conn = temp_db.get_connection()
    rows = [make_row("2021_01_DAL_TB", 2021, 1, "TB", "DAL", 31, 29, "10", "52.5")]
    bng.ingest_season(conn, rows, 2021)
    row = conn.execute(
        "SELECT home_spread, total, league, line_type, source FROM betting_lines "
        "WHERE game_id = '2021_01_DAL_TB'"
    ).fetchone()
    conn.close()
    assert row == (-10.0, 52.5, "nfl", "closing", "nflverse_games")


def test_home_underdog_spread_sign(temp_db):
    """Away team favored -> nflverse spread_line is negative -> this
    project's home_spread must be positive (home is the underdog)."""
    conn = temp_db.get_connection()
    rows = [make_row("2021_01_X_Y", 2021, 1, "Y", "X", 10, 30, "-6.5", "45")]
    bng.ingest_season(conn, rows, 2021)
    home_spread = conn.execute(
        "SELECT home_spread FROM betting_lines WHERE game_id = '2021_01_X_Y'"
    ).fetchone()[0]
    conn.close()
    assert home_spread == 6.5


def test_incomplete_game_gets_no_betting_lines_row(temp_db):
    """A game that hasn't been played yet -- spread_line is just the
    CURRENT market number, not a genuine close (see module docstring).
    Live odds are fetch_nfl_odds.py's job instead."""
    conn = temp_db.get_connection()
    rows = [make_row("2026_01_X_Y", 2026, 1, "Y", "X", spread_line="-3.0", total_line="44")]
    bng.ingest_season(conn, rows, 2026)
    game = conn.execute(
        "SELECT completed FROM nfl_games WHERE game_id = '2026_01_X_Y'"
    ).fetchone()
    lines = conn.execute(
        "SELECT COUNT(*) FROM betting_lines WHERE game_id = '2026_01_X_Y'"
    ).fetchone()[0]
    conn.close()
    assert game == (0,)
    assert lines == 0


def test_idempotent_skip_without_force(temp_db):
    conn = temp_db.get_connection()
    rows = [make_row("2021_01_DAL_TB", 2021, 1, "TB", "DAL", 31, 29, "10", "52.5")]
    bng.ingest_season(conn, rows, 2021)
    games_added, lines_added, status = bng.ingest_season(conn, rows, 2021)
    conn.close()
    assert status == "skipped"
    assert games_added == 0


def test_force_reingests_and_does_not_duplicate(temp_db):
    conn = temp_db.get_connection()
    rows = [make_row("2021_01_DAL_TB", 2021, 1, "TB", "DAL", 31, 29, "10", "52.5")]
    bng.ingest_season(conn, rows, 2021)
    bng.ingest_season(conn, rows, 2021, force=True)
    count = conn.execute(
        "SELECT COUNT(*) FROM betting_lines WHERE game_id = '2021_01_DAL_TB'"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_only_matching_season_rows_ingested(temp_db):
    conn = temp_db.get_connection()
    rows = [
        make_row("2021_01_A_B", 2021, 1, "B", "A", 10, 20, "3", "44"),
        make_row("2022_01_C_D", 2022, 1, "D", "C", 10, 20, "3", "44"),
    ]
    games_added, _, _ = bng.ingest_season(conn, rows, 2021)
    count = conn.execute("SELECT COUNT(*) FROM nfl_games").fetchone()[0]
    conn.close()
    assert games_added == 1
    assert count == 1
