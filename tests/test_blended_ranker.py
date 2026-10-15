"""blended_ranker: one probability scale across spread and total, honest
handling of the model, points assignment, constraints, logging and grading."""
from datetime import datetime

import pytest

import blended_ranker as br

KICKOFF = "2026-10-17T16:00:00.000Z"
NOW = datetime(2026, 10, 16, 12, 0, 0)


# ---- pure math -----------------------------------------------------------------------

def test_outcome_distribution_sums_to_one_and_matches_hand_calc():
    d = br.outcome_distribution([0.5, 0.5])
    assert d == pytest.approx([0.25, 0.5, 0.25])
    d = br.outcome_distribution([0.6] * 5)
    assert sum(d) == pytest.approx(1.0)
    assert d[5] == pytest.approx(0.6 ** 5)


def test_summary_perfect_probability_is_the_product():
    picks = [{"p_win_est": p, "points": 5 - i} for i, p in enumerate([0.55, 0.55, 0.54, 0.54, 0.53])]
    s = br.summarize_picks(picks)
    assert s["p_perfect"] == pytest.approx(0.55 * 0.55 * 0.54 * 0.54 * 0.53, abs=1e-4)
    assert s["max_points"] == 15
    assert s["expected_points"] == pytest.approx(sum(p["p_win_est"] * p["points"] for p in picks), abs=1e-3)
    assert s["p_ge3"] > s["p_ge4"] > s["p_perfect"]


def test_a_realistic_week_is_honestly_a_long_shot_for_perfect():
    """The whole point of printing it: estimates in the 50-55% band make 5/5
    a low-single-digit event."""
    picks = [{"p_win_est": p, "points": 5 - i} for i, p in enumerate([0.551, 0.551, 0.543, 0.543, 0.533])]
    assert br.summarize_picks(picks)["p_perfect"] < 0.06


def mk(gid, market, p, gap=1.0, source="market_gap"):
    return {"game_id": gid, "market": market, "p_win_est": p, "abs_gap": gap, "source": source}


def test_points_go_to_the_highest_probability_first():
    top = br.select_top([mk(1, "total", 0.53), mk(2, "total", 0.56), mk(3, "spread", 0.54)])
    assert [(c["game_id"], c["points"]) for c in top] == [(2, 5), (3, 4), (1, 3)]


def test_points_assignment_maximizes_expected_points():
    """Rearrangement inequality: any other ordering of the same picks yields
    no more expected points."""
    import itertools
    ps = [0.56, 0.54, 0.53, 0.52, 0.51]
    best = sum(p * pts for p, pts in zip(sorted(ps, reverse=True), [5, 4, 3, 2, 1]))
    for perm in itertools.permutations(ps):
        assert sum(p * pts for p, pts in zip(perm, [5, 4, 3, 2, 1])) <= best + 1e-12


def test_per_game_cap_limits_a_single_game():
    cands = [mk(1, "spread", 0.58), mk(1, "total", 0.57), mk(2, "total", 0.56)]
    top = br.select_top(cands, top_n=5, max_per_game=1)
    assert [(c["game_id"], c["market"]) for c in top] == [(1, "spread"), (2, "total")]


def test_selection_is_deterministic_on_ties():
    a = br.select_top([mk(2, "total", 0.53), mk(1, "total", 0.53)])
    b = br.select_top([mk(1, "total", 0.53), mk(2, "total", 0.53)])
    assert [c["game_id"] for c in a] == [c["game_id"] for c in b] == [1, 2]


# ---- the model gets one honest number ---------------------------------------------------

def test_model_probability_is_pooled_and_flat_across_edge_sizes():
    """Raw bucket hit rates were non-monotonic noise (chi-square p~0.17); the
    ranker must not let a bigger model edge buy a higher probability."""
    assert br.model_pick_p(0.5) == br.model_pick_p(6.0) == br.model_pick_p(25.0)
    assert 0.50 < br.model_pick_p(6.0) < 0.51


def test_model_never_outranks_even_a_small_market_gap():
    from move_signal import phi, SPREAD_SIGMA
    weakest_gap_candidate = phi(0.5 / SPREAD_SIGMA)        # a half-point gap
    assert br.model_pick_p(8.0) < weakest_gap_candidate


# ---- end to end on the DB ------------------------------------------------------------------

def game(conn, gid, home="H", away="A", pool_spread=-3.5, pool_total=50.5, start=KICKOFF,
         completed=0, hp=None, ap=None):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, start_date, completed, "
        "home_points, away_points) VALUES (?, 2026, 7, ?, ?, ?, ?, ?, ?)",
        (gid, f"{home}{gid}", f"{away}{gid}", start, completed, hp, ap))
    conn.execute(
        "INSERT INTO pool_lines (contest, season, week, game_id, home_team, away_team, pool_home_spread, "
        "pool_total, source, ingested_at) VALUES ('pool', 2026, 7, ?, ?, ?, ?, ?, 't', 'now')",
        (gid, f"{home}{gid}", f"{away}{gid}", pool_spread, pool_total))


def line(conn, gid, spread, total, fetched="2026-10-16T09:00:00"):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, 2026, 7, 'H', 'A', 'consensus', ?, ?, 'current', 't', ?)",
        (gid, spread, total, fetched))


def card_entry(gid, predicted, confidence="standard"):
    return {"game_id": gid, "predicted_home_margin": predicted, "confidence": confidence, "edge": 5.0}


def test_spread_and_total_candidates_are_ranked_together(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1)
    game(conn, 2)
    line(conn, 1, -3.5, 53.5)      # total gap +3.0 -> Over, no spread gap
    line(conn, 2, -6.5, 50.5)      # spread gap -3.0 (home), no total gap
    conn.commit()
    rep = br.build_blend(conn, 2026, 7, now=NOW)
    conn.close()
    got = {(p["game_id"], p["market"]): p for p in rep["picks"]}
    assert (1, "total") in got and (2, "spread") in got
    assert got[(1, "total")]["side"] == "over" and got[(2, "spread")]["side"] == "home"
    assert {p["points"] for p in rep["picks"]} == set(range(5, 5 - len(rep["picks"]), -1))


def test_market_gap_decides_the_spread_and_the_model_only_gets_an_agree_flag(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, pool_total=None)
    line(conn, 1, -6.5, None)                                   # gap says HOME at pool -3.5
    conn.commit()
    # Model thinks away covers: predicted home margin 0 vs pool -3.5 => edge_home = -3.5 => away
    card = {"games": [card_entry(1, 0.0)]}
    rep = br.build_blend(conn, 2026, 7, card=card, now=NOW)
    conn.close()
    p = rep["picks"][0]
    assert p["source"] == "market_gap" and p["side"] == "home"
    assert p["model_side"] == "away" and p["model_agrees"] is False
    assert len(rep["picks"]) == 1                              # one candidate per (game, market)


def test_model_fills_only_when_there_is_no_gap_and_is_marked_weak(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, pool_total=None)
    line(conn, 1, -3.5, None)                                   # market == pool: no gap
    conn.commit()
    card = {"games": [card_entry(1, 8.0)]}                      # model likes home by a lot vs -3.5
    rep = br.build_blend(conn, 2026, 7, card=card, now=NOW)
    conn.close()
    p = rep["picks"][0]
    assert p["source"] == "spread_model" and p["tier"] == "weak"
    assert p["side"] == "home" and p["p_win_est"] == pytest.approx(br.model_pick_p(8.0))
    assert any("weak" in w for w in rep["warnings"])


@pytest.mark.parametrize("flag", ["low_confidence_large_edge", "no_pick_extrapolation"])
def test_doctrine_flagged_model_picks_are_never_candidates(temp_db, flag):
    conn = temp_db.get_connection()
    game(conn, 1, pool_total=None)
    line(conn, 1, -3.5, None)
    conn.commit()
    rep = br.build_blend(conn, 2026, 7, card={"games": [card_entry(1, 20.0, confidence=flag)]}, now=NOW)
    conn.close()
    assert rep["picks"] == []


def test_started_games_are_never_candidates(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, start="2026-10-15T00:00:00.000Z")
    line(conn, 1, -9.0, 60.0, fetched="2026-10-14T09:00:00")
    conn.commit()
    rep = br.build_blend(conn, 2026, 7, now=NOW)
    conn.close()
    assert rep["picks"] == []


def test_single_snapshot_gap_is_flagged(temp_db):
    conn = temp_db.get_connection()
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team, start_date) "
                 "VALUES (1, 2026, 7, 'H', 'A', ?)", (KICKOFF,))
    conn.execute("INSERT INTO pool_lines (contest, season, week, game_id, home_team, away_team, "
                 "pool_home_spread, pool_total, source, ingested_at) VALUES ('pool', 2026, 7, 1, 'H', 'A', -3.5, 50.5, 't', 'n')")
    # one per-book snapshot only
    conn.execute("INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
                 "line_type, source, fetched_at) VALUES (1, 2026, 7, 'H', 'A', 'draftkings', -3.5, 53.5, 'current', 't', "
                 "'2026-10-16T09:00:00')")
    conn.execute("INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
                 "line_type, source, fetched_at) VALUES (1, 2026, 7, 'H', 'A', 'consensus', -3.5, 53.5, 'current', 't', "
                 "'2026-10-16T09:00:00')")
    conn.commit()
    rep = br.build_blend(conn, 2026, 7, now=NOW)
    conn.close()
    assert rep["picks"][0]["single_snapshot"] is True
    assert any("single market snapshot" in w for w in rep["warnings"])


def test_empty_slate_warns_instead_of_inventing_picks(temp_db):
    conn = temp_db.get_connection()
    rep = br.build_blend(conn, 2026, 7, now=NOW)
    conn.close()
    assert rep["picks"] == [] and rep["warnings"]


# ---- the suggestion file must never become a locked entry -------------------------------------

def test_suggestion_rows_match_the_pool_csv_format_and_load_back(tmp_path):
    import pool_view
    picks = [
        {"game_id": 1, "home_team": "Home", "away_team": "Away", "market": "spread", "side": "away",
         "pool_number": -3.5, "points": 5},
        {"game_id": 2, "home_team": "H2", "away_team": "A2", "market": "total", "side": "under",
         "pool_number": 50.5, "points": 4},
    ]
    path = tmp_path / "week_7_2026_suggested_top5.csv"
    br.write_suggestion_csv({"picks": picks}, str(path))
    loaded = pool_view.load_pool_entries(str(path))
    assert (loaded[0]["market_type"], loaded[0]["picked_side"], loaded[0]["pool_home_spread"], loaded[0]["rank"]) \
        == ("spread", "Away", -3.5, 5)
    assert (loaded[1]["market_type"], loaded[1]["picked_side"], loaded[1]["locked_total"], loaded[1]["rank"]) \
        == ("total", "under", 50.5, 4)


def test_suggestion_filename_cannot_collide_with_the_auto_ingested_pick_file():
    import pool_view
    auto = pool_view.default_csv_path(7, 2026)               # the file pool_view ingests as LOCKED picks
    suggested = "data/pool_picks/week_7_2026_suggested_top5.csv"
    assert auto != suggested and not suggested.endswith("week_7_2026.csv")


# ---- logging + grading ----------------------------------------------------------------------------

def logged(conn, wk, gid, market, side, pool, p, snap, ko):
    conn.execute(
        "INSERT INTO blend_log (season, week, snapshot_at, game_id, market, side, points, pool_number, "
        "market_number, gap, p_win_est, source, tier, kickoff) VALUES (2026, ?, ?, ?, ?, ?, 5, ?, NULL, 0, ?, "
        "'market_gap', 'signal', ?)", (wk, snap, gid, market, side, pool, p, ko))


def test_grading_uses_the_last_suggestion_before_kickoff_and_the_pool_number(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, completed=1, hp=30, ap=24)                  # margin 6, total 54
    conn.execute("UPDATE pool_lines SET pool_total = 50.5 WHERE game_id = 1")
    # early suggestion said away; the last pre-kickoff one said home -- only the last counts
    logged(conn, 7, 1, "spread", "away", -3.5, 0.52, "2026-10-14T10:00:00", KICKOFF)
    logged(conn, 7, 1, "spread", "home", -3.5, 0.55, "2026-10-16T10:00:00", KICKOFF)
    logged(conn, 7, 1, "spread", "away", -3.5, 0.60, "2026-10-17T20:00:00", KICKOFF)   # after kickoff: ignored
    conn.commit()
    g = br.grade_blend(conn, 2026)
    conn.close()
    assert (g["n"], g["w"], g["l"]) == (1, 1, 0)               # home -3.5 covers by 6
    assert g["mean_p_est"] == 0.55 and g["hit"] == 1.0


def test_ungraded_games_are_excluded_not_counted_as_losses(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, completed=0)
    logged(conn, 7, 1, "total", "over", 50.5, 0.55, "2026-10-16T10:00:00", KICKOFF)
    conn.commit()
    g = br.grade_blend(conn, 2026)
    conn.close()
    assert g["n"] == 0 and g["hit"] is None


def test_log_blend_is_append_only(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, pool_total=None)
    line(conn, 1, -6.5, None)
    conn.commit()
    rep = br.build_blend(conn, 2026, 7, now=NOW)
    br.log_blend(conn, rep)
    br.log_blend(conn, rep)
    n = conn.execute("SELECT COUNT(*) FROM blend_log").fetchone()[0]
    conn.close()
    assert n == 2 * len(rep["picks"]) and n >= 2
