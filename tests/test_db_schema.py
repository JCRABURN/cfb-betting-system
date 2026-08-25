def test_init_db_creates_all_tables(temp_db):
    conn = temp_db.get_connection()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "teams", "games", "betting_lines", "team_game_stats",
        "weather", "injuries", "picks", "ingestion_runs",
    }
    assert expected.issubset(tables)
    conn.close()


def test_team_game_stats_has_success_and_havoc_columns(temp_db):
    conn = temp_db.get_connection()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(team_game_stats)")}
    assert {"offense_success_rate", "defense_success_rate", "havoc_rate"}.issubset(cols)
    conn.close()


def test_picks_pick_type_defaults_to_live(temp_db):
    conn = temp_db.get_connection()
    conn.execute("INSERT INTO picks (week, year, created_at) VALUES (1, 2025, '2025-01-01')")
    conn.commit()
    row = conn.execute("SELECT pick_type, status FROM picks").fetchone()
    assert row == ("live", "pending")
    conn.close()


def test_log_run_records_success(temp_db):
    with temp_db.log_run("test_source") as run:
        run["rows_added"] = 7
    conn = temp_db.get_connection()
    row = conn.execute(
        "SELECT source, status, rows_added FROM ingestion_runs WHERE source = 'test_source'"
    ).fetchone()
    assert row == ("test_source", "success", 7)
    conn.close()


def test_log_run_records_error_and_reraises(temp_db):
    import pytest
    with pytest.raises(ValueError):
        with temp_db.log_run("failing_source") as run:
            raise ValueError("boom")
    conn = temp_db.get_connection()
    row = conn.execute(
        "SELECT status, error FROM ingestion_runs WHERE source = 'failing_source'"
    ).fetchone()
    assert row[0] == "error"
    assert "boom" in row[1]
    conn.close()


# ---------------------------------------------------------------------------
# Raw payload archival + contest_entries schema (external review, accepted
# 2026-08-04)
# ---------------------------------------------------------------------------

def test_new_tables_exist(temp_db):
    conn = temp_db.get_connection()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"raw_payloads", "contest_entries", "contest_entry_corrections"}.issubset(tables)
    conn.close()


def test_integrity_check_true_on_healthy_db(temp_db):
    assert temp_db.integrity_check() is True


def test_archive_raw_payload_round_trips_and_checksums(temp_db):
    import gzip
    import hashlib

    conn = temp_db.get_connection()
    body = '{"hello": "world"}'
    payload_id = temp_db.archive_raw_payload(
        conn, "cfbd", "/games", {"year": 2026}, body, 200, "test.v1",
    )
    conn.commit()

    row = conn.execute(
        "SELECT provider, endpoint, http_status, checksum, body_gzip, parser_version, "
        "rows_accepted, rows_rejected FROM raw_payloads WHERE id = ?",
        (payload_id,),
    ).fetchone()
    conn.close()

    provider, endpoint, http_status, checksum, body_gzip, parser_version, accepted, rejected = row
    assert provider == "cfbd"
    assert endpoint == "/games"
    assert http_status == 200
    assert checksum == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert gzip.decompress(body_gzip).decode("utf-8") == body
    assert parser_version == "test.v1"
    assert accepted == 0 and rejected == 0  # not yet updated


def test_update_raw_payload_counts(temp_db):
    conn = temp_db.get_connection()
    payload_id = temp_db.archive_raw_payload(
        conn, "cfbd", "/games", {}, "[]", 200, "test.v1",
    )
    conn.commit()

    temp_db.update_raw_payload_counts(conn, payload_id, rows_accepted=5, rows_rejected=2)
    conn.commit()

    row = conn.execute(
        "SELECT rows_accepted, rows_rejected FROM raw_payloads WHERE id = ?", (payload_id,),
    ).fetchone()
    conn.close()
    assert row == (5, 2)


def test_archive_raw_payload_redacts_secret_params(temp_db):
    """The Odds API's apiKey lives in query params, unlike CFBD's header
    key -- redaction has to be a property of archive_raw_payload() itself,
    not something each caller remembers to do (external review, accepted
    2026-08-04)."""
    import json

    conn = temp_db.get_connection()
    payload_id = temp_db.archive_raw_payload(
        conn, "the_odds_api", "/sports/x/odds",
        {"apiKey": "supersecret123", "regions": "us"}, "[]", 200, "test.v1",
    )
    conn.commit()

    stored_params = conn.execute(
        "SELECT request_params FROM raw_payloads WHERE id = ?", (payload_id,),
    ).fetchone()[0]
    conn.close()

    parsed = json.loads(stored_params)
    assert parsed["apiKey"] == "<redacted>"
    assert parsed["regions"] == "us"
    assert "supersecret123" not in stored_params


def test_contest_entries_unique_constraint(temp_db):
    conn = temp_db.get_connection()
    conn.execute(
        "INSERT INTO contest_entries (contest, season, week, game_id, raw_home_team, raw_away_team, "
        "normalized_home_team, normalized_away_team, locked_home_spread, picked_side, locked_at, source) "
        "VALUES ('pool', 2026, 1, 1, 'X', 'Y', 'X', 'Y', -3.0, 'X', 'now', 'test')"
    )
    conn.commit()

    import sqlite3
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO contest_entries (contest, season, week, game_id, raw_home_team, raw_away_team, "
            "normalized_home_team, normalized_away_team, locked_home_spread, picked_side, locked_at, source) "
            "VALUES ('pool', 2026, 1, 1, 'X', 'Y', 'X', 'Y', -3.5, 'X', 'later', 'test')"
        )


# ---------------------------------------------------------------------------
# NFL scope (external review follow-up, accepted 2026-08-24): separate
# nfl_games/nfl_team_stats tables, betting_lines shared with a league
# marker. See test_no_mixed_league_rows below for the isolation guarantee
# this whole design depends on.
# ---------------------------------------------------------------------------

def test_nfl_tables_exist(temp_db):
    conn = temp_db.get_connection()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"nfl_games", "nfl_team_stats"}.issubset(tables)
    conn.close()


def test_nfl_games_primary_key_is_text_not_integer():
    """nflverse's own game_id ('2024_01_ARI_BUF') is a string -- this is
    WHY nfl_games is a separate table from `games` at all (games.game_id
    is an INTEGER PRIMARY KEY, CFBD's own numeric id space)."""
    import db
    assert "game_id TEXT PRIMARY KEY" in db.SCHEMA


def test_betting_lines_league_defaults_to_cfb(temp_db):
    conn = temp_db.get_connection()
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, "
        "home_spread, line_type, source, fetched_at) VALUES (1, 2026, 1, 'X', 'Y', 'consensus', "
        "-3.0, 'current', 'test', 'now')"
    )
    conn.commit()
    league = conn.execute("SELECT league FROM betting_lines").fetchone()[0]
    conn.close()
    assert league == "cfb"


def test_betting_lines_migration_backfills_existing_rows_to_cfb():
    """The ALTER TABLE ADD COLUMN ... DEFAULT 'cfb' migration path,
    exercised against a DB that already has betting_lines rows written
    BEFORE the league column existed -- every one of them genuinely is
    college football data (this system had no NFL data before this)."""
    import sys
    import os
    import shutil
    import tempfile
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import db

    tmp_dir = tempfile.mkdtemp()
    pre_migration_db = os.path.join(tmp_dir, "pre.db")
    conn = __import__("sqlite3").connect(pre_migration_db)
    conn.execute(
        "CREATE TABLE betting_lines (id INTEGER PRIMARY KEY AUTOINCREMENT, game_id INTEGER, "
        "season INTEGER, week INTEGER, home_team TEXT NOT NULL, away_team TEXT NOT NULL, "
        "book TEXT NOT NULL, home_spread REAL, total REAL, home_moneyline INTEGER, "
        "away_moneyline INTEGER, line_type TEXT NOT NULL, source TEXT NOT NULL, fetched_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, "
        "line_type, source, fetched_at) VALUES (1, 2025, 1, 'X', 'Y', 'consensus', -3.0, 'current', "
        "'test', 'now')"
    )
    conn.commit()
    conn.close()

    old_path = db.DB_PATH
    try:
        db.DB_PATH = pre_migration_db
        db.init_db()
        conn = db.get_connection()
        league = conn.execute("SELECT league FROM betting_lines").fetchone()[0]
        conn.close()
    finally:
        db.DB_PATH = old_path
        shutil.rmtree(tmp_dir, ignore_errors=True)

    assert league == "cfb"


def test_no_mixed_league_rows(temp_db):
    """The isolation guarantee the whole shared-table design depends on:
    a league-scoped query must never return the other sport's rows.
    Explicitly required (external review follow-up, accepted 2026-08-24)."""
    conn = temp_db.get_connection()
    conn.execute(
        "INSERT INTO betting_lines (game_id, league, season, week, home_team, away_team, book, "
        "home_spread, line_type, source, fetched_at) VALUES (1, 'cfb', 2026, 1, 'CfbHome', "
        "'CfbAway', 'consensus', -3.0, 'current', 'test', 'now')"
    )
    conn.execute(
        "INSERT INTO betting_lines (game_id, league, season, week, home_team, away_team, book, "
        "home_spread, line_type, source, fetched_at) VALUES ('2026_01_A_B', 'nfl', 2026, 1, 'B', "
        "'A', 'consensus', -3.0, 'current', 'test', 'now')"
    )
    conn.commit()

    cfb_rows = conn.execute("SELECT home_team FROM betting_lines WHERE league = 'cfb'").fetchall()
    nfl_rows = conn.execute("SELECT home_team FROM betting_lines WHERE league = 'nfl'").fetchall()
    conn.close()

    assert cfb_rows == [("CfbHome",)]
    assert nfl_rows == [("B",)]
    assert "CfbHome" not in [r[0] for r in nfl_rows]
    assert "B" not in [r[0] for r in cfb_rows]


def test_nfl_games_and_games_are_structurally_separate_tables(temp_db):
    """games.game_id is an INTEGER PRIMARY KEY (CFBD's numeric id space);
    nfl_games.game_id is TEXT (nflverse's own string id, e.g.
    '2024_01_ARI_BUF'). A CFB game_id can never collide with an NFL
    game_id by construction -- they aren't even the same type, let alone
    the same table -- so a betting_lines row can never ambiguously
    resolve against both."""
    conn = temp_db.get_connection()
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team) "
        "VALUES (12345, 2026, 1, 'CfbHome', 'CfbAway')"
    )
    conn.execute(
        "INSERT INTO nfl_games (game_id, season, week, home_team, away_team, completed, source, fetched_at) "
        "VALUES ('2026_01_A_B', 2026, 1, 'B', 'A', 0, 'test', 'now')"
    )
    conn.commit()

    cfb_game = conn.execute("SELECT home_team FROM games WHERE game_id = 12345").fetchone()
    nfl_game = conn.execute("SELECT home_team FROM nfl_games WHERE game_id = '2026_01_A_B'").fetchone()
    cross_lookup = conn.execute("SELECT home_team FROM games WHERE game_id = '2026_01_A_B'").fetchone()
    conn.close()

    assert cfb_game == ("CfbHome",)
    assert nfl_game == ("B",)
    assert cross_lookup is None  # the NFL id genuinely doesn't exist in `games`
    conn.close()
