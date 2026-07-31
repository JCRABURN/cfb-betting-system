import pool_view as pv


def insert_game(conn, game_id, season, week, home, away, completed=0,
                 start_date="2023-01-01T00:00:00.000Z"):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, completed, start_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, home, away, completed, start_date),
    )


def insert_line(conn, game_id, season, week, home, away, home_spread, line_type, book="consensus", total=45.0):
    insert_game(conn, game_id, season, week, home, away)
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', 'now')",
        (game_id, season, week, home, away, book, home_spread, total, line_type),
    )


def test_market_moves_toward_a_home_pick(temp_db):
    """Pool had home favored by 3; live line now favors home by 6 --
    good news for a home pick (market agrees more, not less)."""
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-6.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    conn.close()

    game = view["games"][0]
    assert game["drift"] == -3.0
    assert game["signed_drift_vs_pick"] == 3.0
    assert game["movement_vs_pick"] == "toward_pick"
    assert game["favorite_flipped"] is False


def test_market_moves_away_from_a_home_pick(temp_db):
    """Pool had home favored by 6; live line now only favors home by 1 --
    bad news for a home pick (market likes them less than at pool-lock)."""
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-1.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -6.0, "picked_side": "X"},
    ])
    conn.close()

    game = view["games"][0]
    assert game["signed_drift_vs_pick"] == -5.0
    assert game["movement_vs_pick"] == "away_from_pick"


def test_market_moves_toward_an_away_pick(temp_db):
    """Pool had home favored by 3 (away +3); live line now has away
    favored by 2 (home +2) -- good news for an away pick, and the
    favorite has flipped."""
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=2.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "Y"},
    ])
    conn.close()

    game = view["games"][0]
    assert game["signed_drift_vs_pick"] == 5.0
    assert game["movement_vs_pick"] == "toward_pick"
    assert game["favorite_flipped"] is True  # pool favored X, live favors Y


def test_favorite_flip_against_the_pick(temp_db):
    """Pool had home favored by 3, user picked home; live line now has
    away favored by 1 -- the favorite flipped against the pick."""
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=1.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    conn.close()

    game = view["games"][0]
    assert game["favorite_flipped"] is True
    assert game["movement_vs_pick"] == "away_from_pick"


def test_flat_when_no_movement(temp_db):
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    conn.close()

    assert view["games"][0]["movement_vs_pick"] == "flat"
    assert view["games"][0]["favorite_flipped"] is False


def test_sorted_worst_for_pick_first(temp_db):
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "A", "B", home_spread=-3.0, line_type="current")  # unchanged, flat
    insert_line(conn, 2, 2023, 5, "C", "D", home_spread=2.0, line_type="current")   # flipped against pick
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "A", "away_team": "B", "pool_home_spread": -3.0, "picked_side": "A"},
        {"game_id": 2, "home_team": "C", "away_team": "D", "pool_home_spread": -3.0, "picked_side": "C"},
    ])
    conn.close()

    assert view["games"][0]["game_id"] == 2  # worst (moved against pick) first
    assert view["games"][1]["game_id"] == 1


def test_missing_live_line_is_skipped_not_crashed(temp_db):
    conn = temp_db.get_connection()
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    conn.close()

    assert view["games"] == []
    assert view["skipped"][0]["reason"] == "no_live_line"


def test_load_pool_entries_reads_csv(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side\n"
        "401636915,Oklahoma State,Arizona State,-1.0,Oklahoma State\n"
        "401628409,Mississippi State,Massachusetts,-17.5,Mississippi State\n",
        encoding="utf-8",
    )

    entries = pv.load_pool_entries(str(csv_path))

    assert len(entries) == 2
    assert entries[0] == {
        "game_id": 401636915, "home_team": "Oklahoma State", "away_team": "Arizona State",
        "pool_home_spread": -1.0, "picked_side": "Oklahoma State",
    }
    assert entries[1]["pool_home_spread"] == -17.5


def test_load_pool_entries_strips_whitespace(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side\n"
        "1, Home Team , Away Team ,-3.0, Home Team \n",
        encoding="utf-8",
    )

    entries = pv.load_pool_entries(str(csv_path))

    assert entries[0]["home_team"] == "Home Team"
    assert entries[0]["picked_side"] == "Home Team"


def test_load_pool_entries_ignores_extra_columns(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side,notes\n"
        "1,A,B,-3.0,A,some note\n",
        encoding="utf-8",
    )

    entries = pv.load_pool_entries(str(csv_path))

    assert entries[0]["game_id"] == 1
    assert "notes" not in entries[0]


def test_no_model_fields_present():
    """This view must never carry a model prediction/side/edge field --
    it's a pure line-drift read (ARCHITECTURE.md §19-20: no demonstrated edge)."""
    import inspect
    source = inspect.getsource(pv)
    for forbidden in ("predicted_margin", "recommended_side", '"edge":', '"confidence":'):
        assert forbidden not in source
