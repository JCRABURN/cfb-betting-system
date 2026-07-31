"""
Tests for card_generator.py. Uses a hand-picked fixture where the training
data determines an EXACT (not approximated) OLS fit -- two training points
with distinct feature values pin down intercept+slope precisely, so every
downstream prediction, edge, and confidence tier can be checked against a
hand-computed expected value rather than just "did it run."

These tests validate the CARD LOGIC ONLY (format, that confidence tracks
edge, that ranking is correct, that missing data is skipped not crashed).
They say nothing about whether a real current-season card is trustworthy --
that depends on the live weekly fetch path (see ARCHITECTURE.md §18).
"""

import card_generator as cg


def insert_game(conn, game_id, season, week, home, away, home_pts=None, away_pts=None,
                 completed=1, start_date="2023-01-01T00:00:00.000Z"):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, home_points, away_points, "
        "completed, start_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, home, away, home_pts, away_pts, completed, start_date),
    )


def insert_stats(conn, season, week, team, off_epa, def_epa=0.0, source="cfbd_point_in_time"):
    conn.execute(
        "INSERT INTO team_game_stats (season, week, team, offense_epa_play, defense_epa_play, source, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'now')",
        (season, week, team, off_epa, def_epa, source),
    )


def insert_line(conn, game_id, season, week, home, away, home_spread, line_type, book="consensus", total=45.0):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', 'now')",
        (game_id, season, week, home, away, book, home_spread, total, line_type),
    )


def build_training_fixture(conn):
    """Season 2022: two training games whose (epa_diff, actual_margin) pairs
    exactly determine intercept=0.5, coefficient=65.0 --
      (0.3, 20): A (net epa 0.2) vs B (net epa -0.1), margin 20
      (0.1, 7):  C (net epa 0.05) vs D (net epa -0.05), margin 7
    slope = (20-7)/(0.3-0.1) = 65; intercept = 20 - 65*0.3 = 0.5.
    """
    insert_stats(conn, 2022, 1, "A", 0.2, 0.0)
    insert_stats(conn, 2022, 1, "B", -0.1, 0.0)
    insert_stats(conn, 2022, 1, "C", 0.05, 0.0)
    insert_stats(conn, 2022, 1, "D", -0.05, 0.0)
    insert_game(conn, 1, 2022, 2, "A", "B", home_pts=30, away_pts=10, completed=1)
    insert_game(conn, 2, 2022, 2, "C", "D", home_pts=24, away_pts=17, completed=1)


def build_target_week_fixture(conn):
    """Season 2023 week 5: three lined, NOT YET COMPLETED games (proving the
    card includes incomplete games, unlike backtest_harness.list_games), one
    game with stats but no line, and one game with no stats at all.

    With intercept=0.5, coef=65.0 from the training fixture:
      g10 E(net .30) vs F(net .00):  epa_diff .30 -> predicted margin 20.0
      g11 G(net .15) vs H(net -.05): epa_diff .20 -> predicted margin 13.5
      g12 I(net .05) vs J(net .05):  epa_diff .00 -> predicted margin  0.5
    """
    insert_stats(conn, 2023, 4, "E", 0.30, 0.0)
    insert_stats(conn, 2023, 4, "F", 0.00, 0.0)
    insert_stats(conn, 2023, 4, "G", 0.15, 0.0)
    insert_stats(conn, 2023, 4, "H", -0.05, 0.0)
    insert_stats(conn, 2023, 4, "I", 0.05, 0.0)
    insert_stats(conn, 2023, 4, "J", 0.05, 0.0)

    insert_game(conn, 10, 2023, 5, "E", "F", completed=0)
    insert_game(conn, 11, 2023, 5, "G", "H", completed=0)
    insert_game(conn, 12, 2023, 5, "I", "J", completed=0)
    insert_game(conn, 13, 2023, 5, "K", "L", completed=0)  # no stats for K/L at all
    insert_game(conn, 14, 2023, 5, "E", "G", completed=0)  # stats exist, but no line below

    # market home_spread (negative = home favored) -> market home margin = -home_spread
    insert_line(conn, 10, 2023, 5, "E", "F", home_spread=-13.0, line_type="current")  # market margin 13.0, edge 7.0
    insert_line(conn, 11, 2023, 5, "G", "H", home_spread=-10.0, line_type="current")  # market margin 10.0, edge 3.5
    insert_line(conn, 12, 2023, 5, "I", "J", home_spread=2.0, line_type="current")    # market margin -2.0, edge 2.5
    # game 14 (E vs G) deliberately gets no betting_lines row at all.


def test_build_card_computes_expected_predictions_side_and_edge(temp_db):
    conn = temp_db.get_connection()
    build_training_fixture(conn)
    build_target_week_fixture(conn)
    conn.commit()

    card = cg.build_card(conn, 2023, 5)
    conn.close()

    assert card["season"] == 2023
    assert card["week"] == 5
    assert card["model"] == "epa_only"
    assert card["intercept"] == 0.5
    assert card["coefficient"] == 65.0

    by_id = {g["game_id"]: g for g in card["games"]}
    assert set(by_id) == {10, 11, 12}

    g10 = by_id[10]
    assert g10["predicted_home_margin"] == 20.0
    assert g10["side"] == "E"
    assert g10["edge"] == 7.0

    g11 = by_id[11]
    assert g11["predicted_home_margin"] == 13.5
    assert g11["side"] == "G"
    assert g11["edge"] == 3.5

    g12 = by_id[12]
    assert g12["predicted_home_margin"] == 0.5
    assert g12["side"] == "I"
    assert g12["edge"] == 2.5


def test_missing_stats_and_missing_line_are_skipped_not_crashed(temp_db):
    conn = temp_db.get_connection()
    build_training_fixture(conn)
    build_target_week_fixture(conn)
    conn.commit()

    card = cg.build_card(conn, 2023, 5)
    conn.close()

    game_ids_in_card = {g["game_id"] for g in card["games"]}
    assert 13 not in game_ids_in_card
    assert 14 not in game_ids_in_card

    skipped_by_id = {s["game_id"]: s["reason"] for s in card["skipped"]}
    assert skipped_by_id[13] == "missing_pregame_stats"
    assert skipped_by_id[14] == "no_line"


def test_confidence_tracks_edge_size_and_ranking_is_correct(temp_db):
    conn = temp_db.get_connection()
    build_training_fixture(conn)
    build_target_week_fixture(conn)
    conn.commit()

    card = cg.build_card(conn, 2023, 5)
    conn.close()

    games = card["games"]
    edges = [g["edge"] for g in games]
    assert edges == sorted(edges, reverse=True), "games must be ranked by edge, descending"

    confidences = [g["confidence"] for g in games]
    assert confidences == sorted(confidences, reverse=True), \
        "confidence must be non-increasing as edge decreases"
    # Exact tiers for a 3-game slate: rank 0 -> 5, rank 1 -> 4, rank 2 -> 2
    assert confidences == [5, 4, 2]

    assert card["top5"] == games[:5]
    assert len(card["top5"]) == 3  # fewer than 5 lined games this week


def test_top5_truncates_to_five_on_a_larger_slate(temp_db):
    conn = temp_db.get_connection()
    build_training_fixture(conn)
    build_target_week_fixture(conn)

    # Add 4 more lined, distinctly-edged games so the slate exceeds 5.
    for i, (team_off, team2_off, spread) in enumerate([
        (0.40, 0.10, -5.0), (0.10, 0.40, 5.0), (0.02, -0.02, -1.0), (0.25, -0.25, -8.0),
    ], start=1):
        home, away = f"M{i}", f"N{i}"
        insert_stats(conn, 2023, 4, home, team_off, 0.0)
        insert_stats(conn, 2023, 4, away, team2_off, 0.0)
        insert_game(conn, 20 + i, 2023, 5, home, away, completed=0)
        insert_line(conn, 20 + i, 2023, 5, home, away, home_spread=spread, line_type="current")
    conn.commit()

    card = cg.build_card(conn, 2023, 5)
    conn.close()

    assert len(card["games"]) == 7  # original 3 + 4 new
    assert len(card["top5"]) == 5
    assert card["top5"] == card["games"][:5]
    top5_edges = [g["edge"] for g in card["top5"]]
    assert top5_edges == sorted(top5_edges, reverse=True)


def test_list_all_games_includes_not_yet_completed_games(temp_db):
    """The key departure from backtest_harness.list_games(), which filters to
    completed=1 -- a card is for games that haven't been played yet."""
    conn = temp_db.get_connection()
    insert_game(conn, 99, 2023, 5, "X", "Y", completed=0)
    conn.commit()

    rows = cg.list_all_games(conn, 2023, 5)
    conn.close()

    assert len(rows) == 1
    assert rows[0][0] == 99


def test_get_latest_line_prefers_current_over_closing(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y", completed=0)
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-99.0, line_type="closing")  # decoy
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="current")
    conn.commit()

    line = cg.get_latest_line(conn, 1)
    conn.close()

    assert line["home_spread"] == -3.0
    assert line["line_type"] == "current"


def test_get_latest_line_falls_back_to_closing_when_no_current(temp_db):
    """Matches the historical archive's vocabulary (opening/closing, no
    'current' rows at all) -- confirmed live against 2024 week 10."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y", completed=1)
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-6.5, line_type="closing")
    conn.commit()

    line = cg.get_latest_line(conn, 1)
    conn.close()

    assert line["home_spread"] == -6.5
    assert line["line_type"] == "closing"


def test_get_latest_line_prefers_consensus_book_over_individual_book(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y", completed=0)
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-4.0, line_type="current", book="DraftKings")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.5, line_type="current", book="consensus")
    conn.commit()

    line = cg.get_latest_line(conn, 1)
    conn.close()

    assert line["home_spread"] == -3.5
    assert line["book"] == "consensus"


def test_get_latest_line_returns_none_when_no_line_exists(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y", completed=0)
    conn.commit()

    line = cg.get_latest_line(conn, 1)
    conn.close()

    assert line is None
