"""CLV baseline + self-healing recompute (2026-10-14).

The learning lane's consensus_spread follows the market, so CLV is measured
from the FIRST line a pick was made at (picks.line_movement), and any CLV that
was computed against a post-kickoff in-play snapshot gets corrected."""
import json

import post_game_audit as pga

KICKOFF = "2026-10-03T16:00:00.000Z"


def seed(conn, gid=1, side="Away", spread=-12.5, line_movement=None, clv=0.0, status="settled"):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, start_date, completed, "
        "home_points, away_points) VALUES (?, 2026, 5, 'Home', 'Away', ?, 1, 30, 10)", (gid, KICKOFF))
    conn.execute(
        "INSERT INTO picks (game_id, week, year, home_team, away_team, consensus_spread, "
        "recommended_side, line_movement, clv, qualifies, status, pick_type, created_at) "
        "VALUES (?, 5, 2026, 'Home', 'Away', ?, ?, ?, ?, 1, ?, 'live', 'x')",
        (gid, spread, side, line_movement, clv, status))


def line(conn, fetched_at, spread, gid=1, book="consensus"):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, "
        "line_type, source, fetched_at) VALUES (?, 2026, 5, 'Home', 'Away', ?, ?, 'current', 't', ?)",
        (gid, book, spread, fetched_at))


def test_baseline_prefers_first_seen_line_over_followed_line():
    lm = json.dumps({"first_spread": -12.5})
    assert pga.pick_baseline_spread(lm, -9.0) == -12.5


def test_baseline_falls_back_to_consensus_spread_when_unrecorded():
    assert pga.pick_baseline_spread(None, -9.0) == -9.0
    assert pga.pick_baseline_spread("not json", -9.0) == -9.0
    assert pga.pick_baseline_spread(json.dumps({}), -9.0) == -9.0


def test_recompute_ignores_the_in_play_snapshot(temp_db):
    """Stanford @ Wake Forest: pre-game -13.5, in-game -33.5. The stored CLV
    came from -33.5 (garbage); it must be recomputed against -13.5."""
    conn = temp_db.get_connection()
    seed(conn, side="Away", spread=-12.5, clv=999.0)
    line(conn, "2026-10-02T19:00:00", -13.5)
    line(conn, "2026-10-03T17:51:26", -33.5)      # post-kickoff
    conn.commit()

    changed = pga.recompute_settled_clv(conn, 2026)
    clv = conn.execute("SELECT clv FROM picks WHERE game_id = 1").fetchone()[0]
    conn.close()

    assert changed == 1
    expected = pga.bh.calculate_clv("Away", "Home", -12.5, -13.5)
    assert clv == expected
    assert abs(clv) < 5            # nowhere near the ~20pt in-play artifact


def test_recompute_is_idempotent(temp_db):
    conn = temp_db.get_connection()
    seed(conn, clv=999.0)
    line(conn, "2026-10-02T19:00:00", -13.5)
    conn.commit()
    assert pga.recompute_settled_clv(conn, 2026) == 1
    assert pga.recompute_settled_clv(conn, 2026) == 0
    conn.close()


def test_recompute_measures_from_first_seen_line(temp_db):
    """consensus_spread has followed the market to -13.0, but the pick was
    first made at -12.5; CLV must use -12.5."""
    conn = temp_db.get_connection()
    seed(conn, side="Away", spread=-13.0, line_movement=json.dumps({"first_spread": -12.5}), clv=999.0)
    line(conn, "2026-10-02T19:00:00", -13.5)
    conn.commit()
    pga.recompute_settled_clv(conn, 2026)
    clv = conn.execute("SELECT clv FROM picks WHERE game_id = 1").fetchone()[0]
    conn.close()
    assert clv == pga.bh.calculate_clv("Away", "Home", -12.5, -13.5)


def test_recompute_never_touches_result_or_status(temp_db):
    conn = temp_db.get_connection()
    seed(conn, clv=999.0)
    conn.execute("UPDATE picks SET result = 'win', unit_pl = 0.909 WHERE game_id = 1")
    line(conn, "2026-10-02T19:00:00", -13.5)
    conn.commit()
    pga.recompute_settled_clv(conn, 2026)
    row = conn.execute("SELECT status, result, unit_pl FROM picks WHERE game_id = 1").fetchone()
    conn.close()
    assert row == ("settled", "win", 0.909)


def test_recompute_skips_pending_picks(temp_db):
    conn = temp_db.get_connection()
    seed(conn, clv=None, status="pending")
    line(conn, "2026-10-02T19:00:00", -13.5)
    conn.commit()
    assert pga.recompute_settled_clv(conn, 2026) == 0
    conn.close()
