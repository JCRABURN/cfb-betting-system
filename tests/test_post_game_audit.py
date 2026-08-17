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


def insert_contest_entry(conn, game_id, season, week, home, away, spread, picked_side, rank=None,
                          contest="pool"):
    conn.execute(
        "INSERT INTO contest_entries (contest, season, week, game_id, raw_home_team, raw_away_team, "
        "normalized_home_team, normalized_away_team, locked_home_spread, picked_side, rank, locked_at, "
        "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'now', 'test')",
        (contest, season, week, game_id, home, away, home, away, spread, picked_side, rank),
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


# ---------------------------------------------------------------------------
# grade_contest_entries -- pool-pick performance by confidence rank
# (added 2026-08-13)
# ---------------------------------------------------------------------------

def test_grade_contest_entries_grades_a_win(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="A", rank=5)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    assert report["overall"] == {"win": 1, "loss": 0, "push": 0, "n": 1, "win_pct": 1.0}
    assert report["by_rank"][5]["win"] == 1


def test_grade_contest_entries_grades_a_loss(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="B", rank=1)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    assert report["overall"]["loss"] == 1
    assert report["by_rank"][1]["loss"] == 1


def test_grade_contest_entries_grades_a_push(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=23, away_pts=20, completed=1)
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="A", rank=3)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    assert report["overall"]["push"] == 1
    assert report["overall"]["win_pct"] is None  # no decided games, only a push


def test_grade_contest_entries_skips_incomplete_games(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", completed=0)
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="A", rank=4)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    assert report["overall"]["n"] == 0
    assert report["by_rank"] == {}


def test_grade_contest_entries_buckets_by_rank(temp_db):
    """The actual question this feature exists to answer: does rank 5
    outperform rank 1 over multiple picks?"""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)  # A -3 covers big
    insert_game(conn, 2, 2026, 3, "C", "D", home_pts=10, away_pts=30, completed=1)  # C -3 loses big
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="A", rank=5)
    insert_contest_entry(conn, 2, 2026, 3, "C", "D", spread=-3.0, picked_side="C", rank=1)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    assert report["by_rank"][5] == {"win": 1, "loss": 0, "push": 0, "n": 1, "win_pct": 1.0}
    assert report["by_rank"][1] == {"win": 0, "loss": 1, "push": 0, "n": 1, "win_pct": 0.0}
    assert report["overall"]["n"] == 2


def test_grade_contest_entries_unranked_picks_bucket_under_none(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="A", rank=None)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    assert None in report["by_rank"]
    assert report["by_rank"][None]["win"] == 1
    assert 1 not in report["by_rank"]


def test_grade_contest_entries_win_pct_excludes_pushes_from_denominator(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)  # win
    insert_game(conn, 2, 2026, 3, "C", "D", home_pts=23, away_pts=20, completed=1)  # push (-3 exactly)
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="A", rank=4)
    insert_contest_entry(conn, 2, 2026, 3, "C", "D", spread=-3.0, picked_side="C", rank=4)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    bucket = report["by_rank"][4]
    assert bucket == {"win": 1, "loss": 0, "push": 1, "n": 2, "win_pct": 1.0}  # 1/1 decided, not 1/2


def test_grade_contest_entries_scoped_by_contest(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_game(conn, 2, 2026, 3, "C", "D", home_pts=10, away_pts=30, completed=1)
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="A", rank=5, contest="pool")
    insert_contest_entry(conn, 2, 2026, 3, "C", "D", spread=-3.0, picked_side="C", rank=5, contest="office_pool")
    conn.commit()

    pool_only = pga.grade_contest_entries(conn, 2026, contest="pool")
    everyone = pga.grade_contest_entries(conn, 2026)
    conn.close()

    assert pool_only["overall"]["n"] == 1
    assert everyone["overall"]["n"] == 2


def test_grade_contest_entries_scoped_by_season(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2025, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_contest_entry(conn, 1, 2025, 3, "A", "B", spread=-3.0, picked_side="A", rank=5)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    assert report["overall"]["n"] == 0


# ---------------------------------------------------------------------------
# format_rank_report
# ---------------------------------------------------------------------------

def test_format_rank_report_orders_ranks_descending_then_unranked_then_overall(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2026, 3, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_game(conn, 2, 2026, 3, "C", "D", home_pts=30, away_pts=10, completed=1)
    insert_game(conn, 3, 2026, 3, "E", "F", home_pts=30, away_pts=10, completed=1)
    insert_contest_entry(conn, 1, 2026, 3, "A", "B", spread=-3.0, picked_side="A", rank=2)
    insert_contest_entry(conn, 2, 2026, 3, "C", "D", spread=-3.0, picked_side="C", rank=5)
    insert_contest_entry(conn, 3, 2026, 3, "E", "F", spread=-3.0, picked_side="E", rank=None)
    conn.commit()

    report = pga.grade_contest_entries(conn, 2026)
    conn.close()

    text = pga.format_rank_report(report)
    lines = [l.strip() for l in text.splitlines()]
    assert lines[0].startswith("Rank 5:")
    assert lines[1].startswith("Rank 2:")
    assert lines[2].startswith("Unranked:")
    assert lines[3].startswith("Overall:")
    assert "100.0%" in lines[0]
