"""gambling_view.build_totals_view: live totals movement (2026-10-14)."""
import gambling_view as gv

KICKOFF = "2026-10-17T16:00:00.000Z"


def game(conn, gid=1):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, start_date) "
        "VALUES (?, 2026, 7, 'H', 'A', ?)", (gid, KICKOFF))


def row(conn, fetched_at, total, book="draftkings", gid=1, spread=-3.0):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, 2026, 7, 'H', 'A', ?, ?, ?, 'current', 't', ?)",
        (gid, book, spread, total, fetched_at))


def test_total_moves_up_same_book(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    row(conn, "2026-10-14T19:00:00", 50.5)
    row(conn, "2026-10-16T19:00:00", 53.0)
    conn.commit()
    v = gv.build_totals_view(conn, 2026, 7)
    conn.close()
    g = v["games"][0]
    assert (g["first_seen_total"], g["latest_total"], g["movement"], g["direction"]) == (50.5, 53.0, 2.5, "up")
    assert g["same_book_match"] and g["baseline_is_first_seen"]


def test_total_moves_down(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    row(conn, "2026-10-14T19:00:00", 55.0)
    row(conn, "2026-10-16T19:00:00", 52.5)
    conn.commit()
    g = gv.build_totals_view(conn, 2026, 7)["games"][0]
    conn.close()
    assert (g["movement"], g["direction"]) == (-2.5, "down")


def test_in_play_total_never_counts_as_movement(temp_db):
    """Regression for the Stanford @ Wake Forest class of bug, on totals."""
    conn = temp_db.get_connection()
    game(conn)
    row(conn, "2026-10-14T19:00:00", 50.5)
    row(conn, "2026-10-16T19:00:00", 51.0)
    row(conn, "2026-10-17T17:30:00", 74.5)       # after the 16:00 UTC kickoff
    conn.commit()
    g = gv.build_totals_view(conn, 2026, 7)["games"][0]
    conn.close()
    assert g["latest_total"] == 51.0 and g["movement"] == 0.5


def test_game_with_spread_but_no_total_is_skipped_not_dropped(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    row(conn, "2026-10-14T19:00:00", None)
    conn.commit()
    v = gv.build_totals_view(conn, 2026, 7)
    conn.close()
    assert v["games"] == []
    assert v["skipped"][0]["reason"] == "no_total_recorded"


def test_sorted_by_largest_move_first(temp_db):
    conn = temp_db.get_connection()
    for gid in (1, 2):
        game(conn, gid)
    row(conn, "2026-10-14T19:00:00", 50.0, gid=1)
    row(conn, "2026-10-16T19:00:00", 50.5, gid=1)
    row(conn, "2026-10-14T19:00:00", 50.0, gid=2)
    row(conn, "2026-10-16T19:00:00", 54.0, gid=2)
    conn.commit()
    ids = [g["game_id"] for g in gv.build_totals_view(conn, 2026, 7)["games"]]
    conn.close()
    assert ids == [2, 1]
