import json

import post_game_audit as pga


def insert_game(conn, game_id, season, week, home, away, home_pts=None, away_pts=None, completed=0):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, home_points, away_points, completed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, home, away, home_pts, away_pts, completed),
    )


def insert_pick(conn, game_id, week, season, home, away, spread, side, status="pending"):
    conn.execute(
        "INSERT INTO picks (game_id, week, year, home_team, away_team, consensus_spread, "
        "recommended_side, status, pick_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live', 'now')",
        (game_id, week, season, home, away, spread, side, status),
    )


def insert_line(conn, game_id, season, week, home, away, home_spread, line_type, book="consensus"):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, "
        "line_type, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test', 'now')",
        (game_id, season, week, home, away, book, home_spread, line_type),
    )


# ---------------------------------------------------------------------------
# is_hook
# ---------------------------------------------------------------------------

def test_hook_when_decided_by_exactly_half_point_on_half_point_line():
    assert pga.is_hook(-3.5, 0.5) is True
    assert pga.is_hook(-3.5, -0.5) is True


def test_not_a_hook_when_margin_is_larger():
    assert pga.is_hook(-3.5, 2.5) is False


def test_not_a_hook_on_a_whole_number_line():
    """A whole-number line can push exactly -- that's not a hook, it's grade_ats's 'push'."""
    assert pga.is_hook(-3.0, 0.5) is False


def test_not_a_hook_when_line_is_whole_and_margin_is_one():
    assert pga.is_hook(-3.0, 1.0) is False


# ---------------------------------------------------------------------------
# persist_final_scores
# ---------------------------------------------------------------------------

def test_persist_final_scores_updates_finished_games(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B")
    conn.commit()

    updated = pga.persist_final_scores(conn, {1: {"homePoints": 30, "awayPoints": 10}})

    row = conn.execute("SELECT home_points, away_points, completed FROM games WHERE game_id = 1").fetchone()
    conn.close()

    assert updated == 1
    assert row == (30, 10, 1)


def test_persist_final_scores_ignores_games_with_no_score_yet(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B")
    conn.commit()

    updated = pga.persist_final_scores(conn, {1: {"homePoints": None, "awayPoints": None}})

    row = conn.execute("SELECT completed FROM games WHERE game_id = 1").fetchone()
    conn.close()

    assert updated == 0
    assert row[0] == 0


# ---------------------------------------------------------------------------
# grade_pending_picks
# ---------------------------------------------------------------------------

def test_grades_a_win_for_home_favorite(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_pick(conn, 1, 3, 2026, "A", "B", spread=-3.0, side="A")
    conn.commit()

    graded, hooks = pga.grade_pending_picks(conn, 2026, 3)

    row = conn.execute("SELECT result, unit_pl, status FROM picks WHERE game_id = 1").fetchone()
    conn.close()

    assert graded == 1
    assert hooks == 0
    assert row[0] == "win"
    assert row[2] == "settled"


def test_grades_a_loss_for_away_pick(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_pick(conn, 1, 3, 2026, "A", "B", spread=-3.0, side="B")
    conn.commit()

    pga.grade_pending_picks(conn, 2026, 3)

    row = conn.execute("SELECT result FROM picks WHERE game_id = 1").fetchone()
    conn.close()

    assert row[0] == "loss"


def test_leaves_pick_pending_when_game_not_final(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", completed=0)
    insert_pick(conn, 1, 3, 2026, "A", "B", spread=-3.0, side="A")
    conn.commit()

    graded, hooks = pga.grade_pending_picks(conn, 2026, 3)

    row = conn.execute("SELECT status FROM picks WHERE game_id = 1").fetchone()
    conn.close()

    assert graded == 0
    assert row[0] == "pending"


def test_flags_a_hook_and_records_it_in_key_factors(temp_db):
    """Home favored by 3.5, wins by exactly 4 -- covered_margin = actual_margin(4) + spread(-3.5) = 0.5."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=24, away_pts=20, completed=1)
    insert_pick(conn, 1, 3, 2026, "A", "B", spread=-3.5, side="A")
    conn.commit()

    graded, hooks = pga.grade_pending_picks(conn, 2026, 3)

    row = conn.execute("SELECT result, key_factors FROM picks WHERE game_id = 1").fetchone()
    conn.close()

    assert hooks == 1
    assert row[0] == "win"
    assert json.loads(row[1]) == ["hook"]


def test_computes_clv_against_latest_line(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_pick(conn, 1, 3, 2026, "A", "B", spread=-3.0, side="A")
    insert_line(conn, 1, 2026, 3, "A", "B", home_spread=-6.0, line_type="current")
    conn.commit()

    pga.grade_pending_picks(conn, 2026, 3)

    row = conn.execute("SELECT clv FROM picks WHERE game_id = 1").fetchone()
    conn.close()

    # side == home: clv = opening_spread - closing_spread = -3.0 - (-6.0) = 3.0
    assert row[0] == 3.0


def test_clv_is_none_when_no_later_line_exists(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_pick(conn, 1, 3, 2026, "A", "B", spread=-3.0, side="A")
    conn.commit()

    pga.grade_pending_picks(conn, 2026, 3)

    row = conn.execute("SELECT clv FROM picks WHERE game_id = 1").fetchone()
    conn.close()

    assert row[0] is None


def test_only_grades_pending_picks_for_the_requested_week(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_game(conn, 2, 2026, 4, "C", "D", home_pts=20, away_pts=10, completed=1)
    insert_pick(conn, 1, 3, 2026, "A", "B", spread=-3.0, side="A")
    insert_pick(conn, 2, 4, 2026, "C", "D", spread=-3.0, side="C")
    conn.commit()

    graded, _ = pga.grade_pending_picks(conn, 2026, 3)

    week4_status = conn.execute("SELECT status FROM picks WHERE game_id = 2").fetchone()[0]
    conn.close()

    assert graded == 1
    assert week4_status == "pending"
