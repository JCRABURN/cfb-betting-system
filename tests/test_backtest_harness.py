"""
Adversarial tests for backtest_harness.py. These don't just confirm the
happy path works -- each one deliberately plants a "leak" (a future value,
a same-week row, a closing line where an opener is expected) and asserts
the harness refuses it. That's the difference between a harness that looks
honest and one that is, per MODEL_DESIGN.md §4.
"""

import pytest

import backtest_harness as bh


def insert_game(conn, game_id, season, week, home, away, home_pts=None, away_pts=None, completed=1):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, home_points, away_points, completed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, home, away, home_pts, away_pts, completed),
    )


def insert_stats(conn, season, week, team, off_epa, def_epa=0.0, source="cfbd_point_in_time"):
    conn.execute(
        "INSERT INTO team_game_stats (season, week, team, offense_epa_play, defense_epa_play, source, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'now')",
        (season, week, team, off_epa, def_epa, source),
    )


def insert_line(conn, game_id, season, week, home_spread, line_type, book="consensus", total=45.0):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, ?, ?, 'H', 'A', ?, ?, ?, ?, 'test', 'now')",
        (game_id, season, week, book, home_spread, total, line_type),
    )


# ---------------------------------------------------------------------------
# get_team_stats_as_of -- the point-in-time accessor
# ---------------------------------------------------------------------------

def test_returns_most_recent_snapshot_strictly_before_target_week(temp_db):
    conn = temp_db.get_connection()
    insert_stats(conn, 2023, 3, "Georgia", 0.10)
    insert_stats(conn, 2023, 6, "Georgia", 0.20)
    conn.commit()

    result = bh.get_team_stats_as_of(conn, "Georgia", 2023, 9)
    conn.close()
    assert result["offense_epa_play"] == 0.20
    assert result["as_of_week"] == 6


def test_adversarial_plant_a_future_row_at_target_week_and_confirm_it_is_refused(temp_db):
    """Plants a wildly-wrong value AT the target week itself and confirms
    the accessor does not return it -- proof this isn't just 'usually right'."""
    conn = temp_db.get_connection()
    insert_stats(conn, 2023, 5, "Georgia", 0.15)
    insert_stats(conn, 2023, 9, "Georgia", 999.0)  # the leak: same week as target, absurd value
    conn.commit()

    result = bh.get_team_stats_as_of(conn, "Georgia", 2023, 9)
    conn.close()
    assert result["offense_epa_play"] == 0.15
    assert result["offense_epa_play"] != 999.0


def test_no_prior_week_data_returns_none(temp_db):
    conn = temp_db.get_connection()
    insert_stats(conn, 2023, 1, "Georgia", 0.10)  # week 1 itself -- not < 1
    conn.commit()
    result = bh.get_team_stats_as_of(conn, "Georgia", 2023, 1)
    conn.close()
    assert result is None


def test_ignores_non_point_in_time_source(temp_db):
    """A season-final (cfbd_historical_backfill) row must never satisfy the
    point-in-time accessor, even if it's the only row present."""
    conn = temp_db.get_connection()
    insert_stats(conn, 2023, None, "Georgia", 0.30, source="cfbd_historical_backfill")
    conn.commit()
    result = bh.get_team_stats_as_of(conn, "Georgia", 2023, 9)
    conn.close()
    assert result is None


# ---------------------------------------------------------------------------
# get_opening_line / get_closing_line -- line-timing accessors
# ---------------------------------------------------------------------------

def test_opening_line_only_returns_opening_type(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "H", "A")
    insert_line(conn, 1, 2023, 5, -3.5, "opening")
    insert_line(conn, 1, 2023, 5, -5.0, "closing")
    conn.commit()
    result = bh.get_opening_line(conn, 1)
    conn.close()
    assert result["home_spread"] == -3.5


def test_opening_line_missing_does_not_fall_back_to_closing(temp_db):
    """The adversarial case: only a closing line exists. Must return None,
    never silently substitute the closing number as if it were the opener."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "H", "A")
    insert_line(conn, 1, 2023, 5, -5.0, "closing")
    conn.commit()
    result = bh.get_opening_line(conn, 1)
    conn.close()
    assert result is None


def test_closing_line_only_returns_closing_type(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "H", "A")
    insert_line(conn, 1, 2023, 5, -3.5, "opening")
    insert_line(conn, 1, 2023, 5, -5.0, "closing")
    conn.commit()
    result = bh.get_closing_line(conn, 1)
    conn.close()
    assert result["home_spread"] == -5.0


def test_closing_line_prefers_same_book_as_opening_for_apples_to_apples_clv(temp_db):
    """Real-world case, confirmed live: consensus closing coverage collapses
    after 2022 (0 rows for 2024/2025) while individual books stay covered.
    When the opening came from a specific book, CLV must compare that SAME
    book's close, not mix books (which would add book-to-book noise on top
    of real market movement)."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2024, 5, "H", "A")
    insert_line(conn, 1, 2024, 5, -5.0, "closing", book="Bovada")
    insert_line(conn, 1, 2024, 5, -4.5, "closing", book="consensus")
    conn.commit()
    result = bh.get_closing_line(conn, 1, book="Bovada")
    conn.close()
    assert result["home_spread"] == -5.0
    assert result["book"] == "Bovada"


def test_closing_line_falls_back_to_consensus_then_any_book(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2024, 5, "H", "A")
    insert_line(conn, 1, 2024, 5, -4.5, "closing", book="consensus")
    conn.commit()
    # requested book ("Bovada") has no closing row -- falls back to consensus
    result = bh.get_closing_line(conn, 1, book="Bovada")
    conn.close()
    assert result["home_spread"] == -4.5
    assert result["book"] == "consensus"


def test_closing_line_none_when_nothing_exists(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2024, 5, "H", "A")
    conn.commit()
    result = bh.get_closing_line(conn, 1)
    conn.close()
    assert result is None


def test_opening_line_falls_back_to_single_book_when_no_consensus_exists(temp_db):
    """Real-world case, confirmed live: CFBD's historical archive never has a
    consensus OPENING line (only consensus closing) -- only individual books
    do, and only from 2021 on. The fallback must still work, and must FLAG
    that it's a single-book proxy, not silently pretend it's consensus."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2021, 5, "H", "A")
    insert_line(conn, 1, 2021, 5, -3.5, "opening", book="Bovada")
    conn.commit()
    result = bh.get_opening_line(conn, 1)
    conn.close()
    assert result["home_spread"] == -3.5
    assert result["book"] == "Bovada"  # flagged as non-consensus, not silently labeled consensus


def test_opening_line_prefers_consensus_over_single_book_when_both_exist(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2021, 5, "H", "A")
    insert_line(conn, 1, 2021, 5, -3.5, "opening", book="Bovada")
    insert_line(conn, 1, 2021, 5, -4.0, "opening", book="consensus")
    conn.commit()
    result = bh.get_opening_line(conn, 1)
    conn.close()
    assert result["book"] == "consensus"
    assert result["home_spread"] == -4.0


def test_opening_line_none_when_no_book_has_one_at_all(temp_db):
    """The real 2019/2020 case: zero opening lines from any book, any
    provider. Must return None -- there is nothing to fall back to, and the
    caller must skip the game, never substitute the closing line."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2019, 5, "H", "A")
    insert_line(conn, 1, 2019, 5, -5.0, "closing", book="consensus")
    conn.commit()
    result = bh.get_opening_line(conn, 1)
    conn.close()
    assert result is None


# ---------------------------------------------------------------------------
# build_feature_package -- sealed package assembly, skip-don't-substitute
# ---------------------------------------------------------------------------

def test_package_skipped_when_pregame_stats_missing(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 1, "H", "A")
    insert_line(conn, 1, 2023, 1, -3.5, "opening")
    conn.commit()
    package, reason = bh.build_feature_package(conn, 1, 2023, 1, "H", "A")
    conn.close()
    assert package is None
    assert reason == "missing_pregame_stats"


def test_package_skipped_when_opening_line_missing(temp_db):
    conn = temp_db.get_connection()
    insert_stats(conn, 2023, 3, "H", 0.1)
    insert_stats(conn, 2023, 3, "A", 0.05)
    conn.commit()
    package, reason = bh.build_feature_package(conn, 1, 2023, 5, "H", "A")
    conn.close()
    assert package is None
    assert reason == "missing_opening_line"


def test_package_built_when_everything_present(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "H", "A")
    insert_stats(conn, 2023, 3, "H", 0.10)
    insert_stats(conn, 2023, 3, "A", 0.05)
    insert_line(conn, 1, 2023, 5, -3.5, "opening")
    conn.commit()
    package, reason = bh.build_feature_package(conn, 1, 2023, 5, "H", "A")
    conn.close()
    assert reason is None
    assert package["home_stats"]["offense_epa_play"] == 0.10
    assert package["opening_spread"] == -3.5
    assert package["opening_book"] == "consensus"


# ---------------------------------------------------------------------------
# available_seasons_before / build_training_set -- the training-side guarantee
# ---------------------------------------------------------------------------

def test_training_set_does_not_require_an_opening_line(temp_db):
    """The real bug this caught: 2019/2020 have real point-in-time stats but
    ZERO opening lines at all (confirmed live). Fitting the model only needs
    (feature, actual_margin) pairs -- no line data -- so a season with no
    opening-line coverage must still be usable as full training data."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2019, 3, "H", "A", 24, 17)  # no betting_lines row at all
    insert_stats(conn, 2019, 1, "H", 0.10)
    insert_stats(conn, 2019, 1, "A", 0.05)
    conn.commit()

    def feature_fn(package):
        return (package["home_stats"]["offense_epa_play"] - package["away_stats"]["offense_epa_play"],)

    xs, ys = bh.build_training_set(conn, feature_fn, [2019])
    conn.close()
    assert xs == [(pytest.approx(0.05),)]
    assert ys == [7]  # 24 - 17


def test_training_set_skips_rows_where_feature_fn_returns_none(temp_db):
    """The real gap havoc_rate hit: a feature_fn may be unable to compute a
    value for some rows (e.g. a field that's occasionally NULL even when the
    rest of pregame stats exist). feature_fn signals this by returning None;
    that row must be silently excluded from training, not crash or get
    treated as a zero."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2019, 3, "H", "A", 24, 17)
    insert_stats(conn, 2019, 1, "H", 0.10)
    insert_stats(conn, 2019, 1, "A", 0.05)
    insert_game(conn, 2, 2019, 3, "H2", "A2", 20, 10)
    insert_stats(conn, 2019, 1, "H2", 0.20)
    insert_stats(conn, 2019, 1, "A2", 0.05)
    conn.commit()

    def feature_fn(package):
        # simulate a feature that can't be computed for the second game
        if package["home_stats"]["offense_epa_play"] == 0.20:
            return None
        return (package["home_stats"]["offense_epa_play"] - package["away_stats"]["offense_epa_play"],)

    rows, ys = bh.build_training_set(conn, feature_fn, [2019])
    conn.close()
    assert len(rows) == 1  # only the first game -- the second was excluded, not crashed on
    assert ys == [7]


def test_run_walk_forward_skips_predictions_where_predict_fn_returns_none(temp_db):
    """Same None-signal, at the prediction step: predict_fn returning None
    must produce a skipped record (reason='missing_feature_data'), not a
    crash or a silently wrong prediction."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2022, 3, "H", "A", 24, 17)
    insert_stats(conn, 2022, 1, "H", 0.10)
    insert_stats(conn, 2022, 1, "A", 0.05)
    insert_line(conn, 1, 2022, 3, -3.0, "opening")
    insert_game(conn, 2, 2022, 6, "H2", "A2", 30, 10)
    insert_stats(conn, 2022, 4, "H2", 0.20)
    insert_stats(conn, 2022, 4, "A2", 0.05)
    insert_line(conn, 2, 2022, 6, -6.0, "opening")

    insert_game(conn, 3, 2023, 5, "H", "A", 24, 20)
    insert_stats(conn, 2023, 3, "H", 0.10)
    insert_stats(conn, 2023, 3, "A", 0.05)  # will be treated as "unavailable" below
    insert_line(conn, 3, 2023, 5, -3.0, "opening")
    conn.commit()

    def feature_fn(package):
        return (package["home_stats"]["offense_epa_play"] - package["away_stats"]["offense_epa_play"],)

    def predict_fn(package, intercept, coefs):
        if package["home_stats"]["as_of_week"] == 3 and package["away_stats"]["as_of_week"] == 3:
            return None  # simulate the feature being unavailable for this specific game
        (x,) = feature_fn(package)
        return coefs[0] * x + intercept

    records, _ = bh.run_walk_forward(conn, [2023], feature_fn, predict_fn)
    conn.close()

    test_game = next(r for r in records if r.game_id == 3)
    assert test_game.skipped_reason == "missing_feature_data"
    assert test_game.side is None


def test_available_seasons_before_excludes_target_season(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2021, 1, "H", "A", 20, 10)
    insert_game(conn, 2, 2022, 1, "H", "A", 20, 10)
    insert_game(conn, 3, 2023, 1, "H", "A", 20, 10)
    conn.commit()
    seasons = bh.available_seasons_before(conn, 2023)
    conn.close()
    assert seasons == [2021, 2022]
    assert 2023 not in seasons


def test_adversarial_training_set_never_includes_the_target_season(temp_db):
    """Plants a season-2023 game with an absurd feature value, then builds
    the training set FOR season 2023 (which must only use seasons < 2023),
    and confirms the leak value never appears among the training xs."""
    conn = temp_db.get_connection()
    # legitimate prior-season training game
    insert_game(conn, 1, 2022, 5, "H", "A", 24, 17)
    insert_stats(conn, 2022, 3, "H", 0.10)
    insert_stats(conn, 2022, 3, "A", 0.05)
    insert_line(conn, 1, 2022, 5, -3.0, "opening")
    # the leak: a 2023 game (the season we're about to predict) with an
    # absurd feature value that must never leak into 2023's own training set
    insert_game(conn, 2, 2023, 5, "H", "A", 24, 17)
    insert_stats(conn, 2023, 3, "H", 999.0)
    insert_stats(conn, 2023, 3, "A", 0.05)
    insert_line(conn, 2, 2023, 5, -3.0, "opening")
    conn.commit()

    def feature_fn(package):
        return (package["home_stats"]["offense_epa_play"] - package["away_stats"]["offense_epa_play"],)

    seasons_before_2023 = bh.available_seasons_before(conn, 2023)
    xs, ys = bh.build_training_set(conn, feature_fn, seasons_before_2023)
    conn.close()

    assert (999.0,) not in xs
    assert len(xs) == 1  # only the legitimate 2022 game
    assert xs[0] == (pytest.approx(0.10 - 0.05),)


# ---------------------------------------------------------------------------
# fit_multilinear -- closed-form OLS correctness (generalizes fit_linear to N features)
# ---------------------------------------------------------------------------

def test_fit_multilinear_single_feature_recovers_known_slope_and_intercept():
    rows = [(0,), (1,), (2,), (3,), (4,)]
    ys = [3, 5, 7, 9, 11]  # y = 2x + 3, no noise
    intercept, coefs = bh.fit_multilinear(rows, ys)
    assert coefs[0] == pytest.approx(2.0)
    assert intercept == pytest.approx(3.0)


def test_fit_multilinear_two_features_recovers_known_coefficients():
    # y = 3 + 2*x1 - 1*x2, no noise
    rows = [(0, 0), (1, 0), (0, 1), (2, 1), (1, 2)]
    ys = [3 + 2 * x1 - 1 * x2 for x1, x2 in rows]
    intercept, coefs = bh.fit_multilinear(rows, ys)
    assert intercept == pytest.approx(3.0)
    assert coefs[0] == pytest.approx(2.0)
    assert coefs[1] == pytest.approx(-1.0)


def test_fit_multilinear_raises_on_insufficient_points():
    with pytest.raises(ValueError):
        bh.fit_multilinear([(1,)], [1])


def test_fit_multilinear_raises_on_zero_variance():
    with pytest.raises(ValueError):
        bh.fit_multilinear([(5,), (5,), (5,)], [1, 2, 3])


def test_fit_multilinear_raises_on_collinear_features():
    # x2 is always exactly 2*x1 -- perfectly collinear, singular design matrix
    with pytest.raises(ValueError):
        bh.fit_multilinear([(0, 0), (1, 2), (2, 4)], [1, 2, 3])


# ---------------------------------------------------------------------------
# mcnemar_test -- the paired-comparison instrument for feature evaluation
# ---------------------------------------------------------------------------

def test_mcnemar_significant_when_challenger_wins_lopsidedly():
    # challenger wins 40 of the 50 disagreement games baseline would have lost
    chi2, p = bh.mcnemar_test(challenger_right=40, baseline_right=10)
    assert p < 0.05


def test_mcnemar_not_significant_when_close_to_even():
    chi2, p = bh.mcnemar_test(challenger_right=26, baseline_right=24)
    assert p > 0.05


def test_mcnemar_no_disagreements_is_not_significant():
    chi2, p = bh.mcnemar_test(challenger_right=0, baseline_right=0)
    assert chi2 == 0.0
    assert p == 1.0


# ---------------------------------------------------------------------------
# grade_ats / unit_pl / calculate_clv -- correctness against known conventions
# ---------------------------------------------------------------------------

def test_grade_ats_home_favorite_covers():
    # home favored by 7 (-7), wins by 10 -> covers
    assert bh.grade_ats("H", "H", "A", -7.0, 30, 20) == "win"


def test_grade_ats_home_favorite_fails_to_cover():
    # home favored by 7, wins by only 3 -> loss for home bettors
    assert bh.grade_ats("H", "H", "A", -7.0, 23, 20) == "loss"


def test_grade_ats_push():
    # home favored by exactly 7, wins by exactly 7 -> push
    assert bh.grade_ats("H", "H", "A", -7.0, 27, 20) == "push"


def test_grade_ats_away_side():
    # home favored by 7, away side bet, home wins by only 3 -> away covers (win)
    assert bh.grade_ats("A", "H", "A", -7.0, 23, 20) == "win"


def test_unit_pl_values():
    assert bh.unit_pl("win") == pytest.approx(0.909)
    assert bh.unit_pl("loss") == -1.0
    assert bh.unit_pl("push") == 0.0


def test_calculate_clv_home_side_positive_when_line_moved_toward_favorite():
    # bet home at -3, closed at -5 -> we got the better (lower magnitude) number
    assert bh.calculate_clv("H", "H", -3.0, -5.0) == pytest.approx(2.0)


def test_calculate_clv_away_side():
    assert bh.calculate_clv("A", "H", -3.0, -5.0) == pytest.approx(-2.0)


# ---------------------------------------------------------------------------
# End-to-end: the full walk-forward loop, adversarially, proves the
# guarantee holds through the whole pipeline, not just at the unit level.
# ---------------------------------------------------------------------------

def test_walk_forward_end_to_end_refuses_a_planted_same_week_leak(temp_db):
    conn = temp_db.get_connection()
    # Season 2022 (training data for predicting 2023). Week 3, not week 1 --
    # week 1 can never have valid point-in-time stats (there's no week 0 to
    # look back to), so it would make the training set empty. Two games with
    # distinct feature values: fit_linear needs at least 2 points of variance.
    insert_game(conn, 1, 2022, 3, "H", "A", 24, 17)
    insert_stats(conn, 2022, 1, "H", 0.15)
    insert_stats(conn, 2022, 1, "A", 0.05)
    insert_stats(conn, 2022, None, "H", 0.20, source="cfbd_historical_backfill")  # must be ignored
    insert_line(conn, 1, 2022, 3, -3.0, "opening")
    insert_line(conn, 1, 2022, 3, -4.0, "closing")

    insert_game(conn, 4, 2022, 6, "H2", "A2", 30, 10)
    insert_stats(conn, 2022, 4, "H2", 0.05)
    insert_stats(conn, 2022, 4, "A2", -0.10)
    insert_line(conn, 4, 2022, 6, -6.0, "opening")
    insert_line(conn, 4, 2022, 6, -6.0, "closing")

    # Season 2023 week 5: the game under test. Its OWN point-in-time feature
    # (week 3) is legitimate; a deliberate leak is planted at week 5 (the
    # target week itself) with an absurd value that must never be used.
    insert_game(conn, 2, 2023, 5, "H", "A", 24, 20)
    insert_stats(conn, 2023, 3, "H", 0.10)
    insert_stats(conn, 2023, 3, "A", 0.05)
    insert_stats(conn, 2023, 5, "H", 999.0)  # the leak
    insert_line(conn, 2, 2023, 5, -3.0, "opening")
    insert_line(conn, 2, 2023, 5, -4.0, "closing")
    conn.commit()

    def feature_fn(package):
        return (package["home_stats"]["offense_epa_play"] - package["away_stats"]["offense_epa_play"],)

    def predict_fn(package, intercept, coefs):
        (x,) = feature_fn(package)
        return coefs[0] * x + intercept

    records, season_fits = bh.run_walk_forward(conn, [2023], feature_fn, predict_fn)
    conn.close()

    test_game = next(r for r in records if r.game_id == 2)
    assert test_game.skipped_reason is None
    # If the leak (999.0) had been used, the predicted margin would be
    # enormous. With the correct week-3 value (0.10 - 0.05 = 0.05) and a
    # slope fit from a single training point, the prediction stays sane.
    assert test_game.edge < 100  # sanity bound; a leaked 999.0 would blow this out
