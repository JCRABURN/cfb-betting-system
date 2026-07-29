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
