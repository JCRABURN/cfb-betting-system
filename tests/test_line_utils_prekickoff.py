"""line_utils: pre-kickoff-only 'latest' and totals helpers (2026-10-14).

Regression for the Stanford @ Wake Forest incident: a 'current' row fetched
after kickoff (an in-game blowout line, -33.5) was returned as the latest
line after -12.5 / -13.5 pre-game numbers."""
import line_utils as lu

KICKOFF = "2026-10-03T16:00:00.000Z"


def game(conn, gid=1):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, start_date) "
        "VALUES (?, 2026, 5, 'H', 'A', ?)", (gid, KICKOFF))


def line(conn, fetched_at, spread, total=None, book="draftkings", line_type="current", gid=1):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, 2026, 5, 'H', 'A', ?, ?, ?, ?, 'test', ?)",
        (gid, book, spread, total, line_type, fetched_at))


def test_latest_line_ignores_in_play_snapshot(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    line(conn, "2026-10-01T19:00:00", -12.5)
    line(conn, "2026-10-02T19:00:00", -13.5)
    line(conn, "2026-10-03T17:51:26", -33.5)          # after the 16:00 UTC kickoff
    conn.commit()
    assert lu.get_latest_line(conn, 1, prefer_book="draftkings")["home_spread"] == -13.5
    conn.close()


def test_consensus_branch_also_ignores_in_play(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    line(conn, "2026-10-02T19:00:00", -13.0, book="consensus")
    line(conn, "2026-10-03T17:51:26", -33.0, book="consensus")
    conn.commit()
    assert lu.get_latest_line(conn, 1)["home_spread"] == -13.0
    conn.close()


def test_latest_line_exactly_at_kickoff_is_excluded(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    line(conn, "2026-10-02T19:00:00", -13.5)
    line(conn, "2026-10-03T16:00:00", -20.0)
    conn.commit()
    assert lu.get_latest_line(conn, 1, prefer_book="draftkings")["home_spread"] == -13.5
    conn.close()


def test_historical_closing_rows_are_not_filtered(temp_db):
    """Backfilled 'closing' rows carry the backfill run's fetched_at (long
    after kickoff) and are NOT in-play prices -- they must still resolve."""
    conn = temp_db.get_connection()
    game(conn)
    line(conn, "2027-01-01T00:00:00", -7.0, line_type="closing")
    conn.commit()
    assert lu.get_latest_line(conn, 1, prefer_book="draftkings")["home_spread"] == -7.0
    conn.close()


def test_unknown_kickoff_does_not_filter(temp_db):
    conn = temp_db.get_connection()
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team) VALUES (2, 2026, 5, 'H', 'A')")
    line(conn, "2026-10-03T17:51:26", -9.0, gid=2)
    conn.commit()
    assert lu.get_latest_line(conn, 2, prefer_book="draftkings")["home_spread"] == -9.0
    conn.close()


def test_latest_total_is_pre_kickoff_and_prefers_requested_book(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    line(conn, "2026-10-01T19:00:00", -3.0, total=50.5, book="draftkings")
    line(conn, "2026-10-02T19:00:00", -3.0, total=52.5, book="draftkings")
    line(conn, "2026-10-02T19:00:00", None, total=51.0, book="fanduel")
    line(conn, "2026-10-03T17:00:00", -3.0, total=71.5, book="draftkings")   # in play
    conn.commit()
    t = lu.get_latest_total(conn, 1, prefer_book="draftkings")
    assert (t["total"], t["book"]) == (52.5, "draftkings")
    assert lu.get_latest_total(conn, 1, prefer_book="fanduel")["total"] == 51.0
    conn.close()


def test_latest_total_none_when_no_totals(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    line(conn, "2026-10-01T19:00:00", -3.0)
    conn.commit()
    assert lu.get_latest_total(conn, 1) is None
    conn.close()


def test_first_seen_total_prefers_real_book_and_oldest(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    line(conn, "2026-10-01T19:00:00", -3.0, total=50.5, book="fanduel")
    line(conn, "2026-10-02T19:00:00", -3.0, total=52.5, book="draftkings")
    line(conn, "2026-10-01T09:00:00", -3.0, total=49.0, book="draftkings")
    line(conn, "2026-10-01T08:00:00", -3.0, total=40.0, book="consensus")
    conn.commit()
    r = lu.get_first_seen_total_real_book(conn, 1)
    assert (r["total"], r["book"]) == (49.0, "draftkings")   # real book, oldest snapshot, never consensus
    conn.close()
