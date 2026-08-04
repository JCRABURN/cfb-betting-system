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
    conn.close()
