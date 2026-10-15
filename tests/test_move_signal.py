"""move_signal: direction, grading against the FROZEN pool number, in-play
exclusion, bucketing, and the pre-registered decision rule."""
from datetime import datetime

import pytest

import move_signal as ms

KICKOFF = "2026-10-17T16:00:00.000Z"
NOW_BEFORE = datetime(2026, 10, 16, 12, 0, 0)
NOW_AFTER = datetime(2026, 10, 18, 12, 0, 0)


# ---- pure signal direction ------------------------------------------------------------

def test_spread_market_more_home_favored_than_pool_favors_home():
    s = ms.spread_signal(pool_home_spread=-3.5, market_home_spread=-6.0)
    assert (s["side"], s["gap"], s["abs_gap"]) == ("home", -2.5, 2.5)


def test_spread_market_less_home_favored_than_pool_favors_away():
    s = ms.spread_signal(pool_home_spread=-3.5, market_home_spread=-1.0)
    assert (s["side"], s["gap"]) == ("away", 2.5)


def test_spread_no_gap_is_no_signal():
    assert ms.spread_signal(-3.5, -3.5) is None


def test_total_up_is_over_down_is_under():
    assert ms.total_signal(50.5, 53.0)["side"] == "over"
    assert ms.total_signal(50.5, 48.0)["side"] == "under"
    assert ms.total_signal(50.5, 50.5) is None


def test_estimate_matches_the_measured_theory():
    """3 points of gap ~ 57.6% (the historical 3+pt bucket measured 60%)."""
    z = ms.total_signal(50.0, 53.0)["z"]
    assert ms.phi(z) == pytest.approx(0.577, abs=0.003)


def test_tiers_use_the_preregistered_thresholds():
    assert ms.tier_for(0.5) == "none"
    assert ms.tier_for(1.0) == "watch" and ms.tier_for(1.99) == "watch"
    assert ms.tier_for(2.0) == "strong"


# ---- grading is against the POOL number, never the market's -------------------------------

@pytest.mark.parametrize("side,pool,hp,ap,expected", [
    ("home", -3.5, 30, 24, "win"),     # wins by 6, covers -3.5
    ("home", -3.5, 27, 24, "loss"),    # wins by 3, does not cover -3.5
    ("home", -3.0, 27, 24, "push"),
    ("away", -3.5, 27, 24, "win"),     # away +3.5 loses by 3 -> covers
    ("away", -3.5, 30, 24, "loss"),
])
def test_grade_spread(side, pool, hp, ap, expected):
    assert ms.grade_spread(side, pool, hp, ap) == expected


@pytest.mark.parametrize("side,pool,pts,expected", [
    ("over", 50.5, 55, "win"), ("over", 50.5, 48, "loss"),
    ("under", 50.5, 48, "win"), ("under", 50.5, 55, "loss"),
    ("over", 52.0, 52, "push"),
])
def test_grade_total(side, pool, pts, expected):
    assert ms.grade_total(side, pool, pts) == expected


def test_wilson_interval_is_sane():
    lo, hi = ms.wilson(30, 50)
    assert lo < 0.6 < hi and 0.45 < lo < 0.5 and 0.70 < hi < 0.75     # 30/50 -> ~(0.46, 0.72)
    assert ms.wilson(0, 0) == (None, None)


# ---- end to end on the DB --------------------------------------------------------------------

def setup_game(conn, gid=1, completed=0, hp=None, ap=None, pool_spread=-3.5, pool_total=50.5):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, start_date, completed, "
        "home_points, away_points) VALUES (?, 2026, 7, 'Home', 'Away', ?, ?, ?, ?)",
        (gid, KICKOFF, completed, hp, ap))
    conn.execute(
        "INSERT INTO pool_lines (contest, season, week, game_id, home_team, away_team, pool_home_spread, "
        "pool_total, source, ingested_at) VALUES ('pool', 2026, 7, ?, 'Home', 'Away', ?, ?, 't', 'now')",
        (gid, pool_spread, pool_total))


def line(conn, spread, total, fetched="2026-10-16T09:00:00", gid=1, line_type="current"):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, 2026, 7, 'Home', 'Away', 'consensus', ?, ?, ?, 't', ?)",
        (gid, spread, total, line_type, fetched))


def test_build_signals_finds_both_markets_and_marks_actionable(temp_db):
    conn = temp_db.get_connection()
    setup_game(conn)
    line(conn, -6.0, 53.5)
    conn.commit()
    sigs = ms.build_signals(conn, 2026, 7, now=NOW_BEFORE)
    conn.close()
    by = {s["market"]: s for s in sigs}
    assert by["spread"]["side"] == "home" and by["spread"]["take"] == "Home -3.5"
    assert by["total"]["side"] == "over" and by["total"]["take"] == "Over 50.5"
    assert all(s["actionable"] and s["result"] is None for s in sigs)


def test_finished_game_is_graded_against_the_pool_number(temp_db):
    conn = temp_db.get_connection()
    setup_game(conn, completed=1, hp=30, ap=24, pool_spread=-3.5, pool_total=50.5)   # margin 6, total 54
    line(conn, -6.0, 53.5)
    conn.commit()
    by = {s["market"]: s for s in ms.build_signals(conn, 2026, 7, now=NOW_AFTER)}
    conn.close()
    assert by["spread"]["result"] == "win"          # home covers the POOL's -3.5
    assert by["total"]["result"] == "win"           # 54 > pool 50.5
    assert not by["spread"]["actionable"]


def test_grading_never_uses_the_markets_own_number(temp_db):
    """Home won by 5: covers the pool's -3.5 but would LOSE against the
    market's -6.0. The signal is graded on the pool number."""
    conn = temp_db.get_connection()
    setup_game(conn, completed=1, hp=29, ap=24, pool_spread=-3.5, pool_total=None)
    line(conn, -6.0, None)
    conn.commit()
    s = ms.build_signals(conn, 2026, 7, now=NOW_AFTER)[0]
    conn.close()
    assert s["result"] == "win"


def test_in_play_line_never_creates_a_signal(temp_db):
    """The Stanford @ Wake Forest class: a post-kickoff blowout line must not
    manufacture a huge 'stale number'."""
    conn = temp_db.get_connection()
    setup_game(conn, pool_total=None)
    line(conn, -4.0, None, fetched="2026-10-16T09:00:00")
    line(conn, -33.5, None, fetched="2026-10-17T17:30:00")       # after the 16:00 UTC kickoff
    conn.commit()
    s = ms.build_signals(conn, 2026, 7, now=NOW_BEFORE)
    conn.close()
    assert s == [] or all(x["abs_gap"] < 1 for x in s)          # -4.0 vs -3.5 is a 0.5 gap


def test_game_without_a_pool_total_still_yields_a_spread_signal(temp_db):
    conn = temp_db.get_connection()
    setup_game(conn, pool_total=None)
    line(conn, -6.0, 53.5)
    conn.commit()
    sigs = ms.build_signals(conn, 2026, 7, now=NOW_BEFORE)
    conn.close()
    assert [s["market"] for s in sigs] == ["spread"]


def test_actionable_strong_signals_sort_first(temp_db):
    conn = temp_db.get_connection()
    setup_game(conn, gid=1, pool_total=None)
    setup_game(conn, gid=2, pool_total=None)
    line(conn, -4.5, None, gid=1)       # gap 1.0
    line(conn, -8.0, None, gid=2)       # gap 4.5
    conn.commit()
    sigs = ms.build_signals(conn, 2026, 7, now=NOW_BEFORE)
    conn.close()
    assert [s["game_id"] for s in sigs] == [2, 1]


# ---- record + the pre-registered decision rule ---------------------------------------------------

def graded(abs_gap, result, market="spread"):
    return {"abs_gap": abs_gap, "result": result, "market": market}


def test_summarize_buckets_by_gap_size_and_ignores_ungraded():
    sigs = [graded(0.5, "win"), graded(1.5, "loss"), graded(2.5, "win"), graded(2.5, "win"),
            graded(3.5, "loss"), graded(3.5, "push"), graded(2.2, None)]
    b = ms.summarize(sigs)
    assert (b["<1"]["w"], b["1-2"]["l"], b["2-3"]["w"]) == (1, 1, 2)
    assert b[">=3"]["l"] == 1 and b[">=3"]["p"] == 1 and b[">=3"]["decided"] == 1
    assert b["2-3"]["n"] == 2                         # the ungraded one is not counted


def test_primary_pools_markets_at_the_strong_threshold():
    sigs = [graded(2.0, "win", "spread"), graded(3.0, "win", "total"), graded(1.9, "win", "spread"),
            graded(2.5, "loss", "total")]
    p = ms.primary(sigs)
    assert (p["w"], p["l"], p["n"]) == (2, 1, 3)       # the 1.9 gap is below the pre-registered bar


def test_small_sample_is_never_declared_actionable_however_good():
    """10-for-10 is a great-looking record and is still not evidence. The
    rule was fixed in advance: n >= 60 AND the lower 95% bound above 50%."""
    p = ms.primary([graded(3.0, "win")] * 10)
    assert p["hit"] == 1.0 and p["actionable_threshold_met"] is False


def test_large_clearly_positive_sample_does_clear_the_bar():
    p = ms.primary([graded(3.0, "win")] * 45 + [graded(3.0, "loss")] * 25)     # 64% over 70
    assert p["actionable_threshold_met"] is True


def test_large_but_coinflip_sample_does_not_clear_the_bar():
    p = ms.primary([graded(3.0, "win")] * 36 + [graded(3.0, "loss")] * 34)     # 51% over 70
    assert p["actionable_threshold_met"] is False


def test_report_includes_retrospective_weeks_in_the_record(temp_db):
    conn = temp_db.get_connection()
    # Week 7 upcoming + a completed earlier week in the same season
    setup_game(conn, gid=1)
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team, start_date, completed, "
                 "home_points, away_points) VALUES (2, 2026, 2, 'H2', 'A2', '2026-09-12T16:00:00.000Z', 1, 30, 10)")
    conn.execute("INSERT INTO pool_lines (contest, season, week, game_id, home_team, away_team, "
                 "pool_home_spread, pool_total, source, ingested_at) "
                 "VALUES ('pool', 2026, 2, 2, 'H2', 'A2', -3.5, NULL, 't', 'now')")
    conn.execute("INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, "
                 "line_type, source, fetched_at) VALUES (2, 2026, 2, 'H2', 'A2', 'consensus', -7.0, 'current', 't', "
                 "'2026-09-11T09:00:00')")
    line(conn, -6.0, 53.5)
    conn.commit()
    rep = ms.build_report(conn, 2026, 7, now=NOW_BEFORE)
    conn.close()
    assert rep["weeks_in_record"] == [2, 7]
    assert rep["record"]["spread"][">=3"]["w"] == 1          # week 2: pool -3.5 vs market -7.0 = 3.5 gap, home covers
    assert {s["game_id"] for s in rep["current"]} == {1}
