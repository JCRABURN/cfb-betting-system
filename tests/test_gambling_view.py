import gambling_view as gv


def insert_game(conn, game_id, season, week, home, away, completed=0,
                 start_date="2023-01-01T00:00:00.000Z"):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, completed, start_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, home, away, completed, start_date),
    )


def insert_line(conn, game_id, season, week, home, away, home_spread, line_type, book="consensus", total=45.0):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', 'now')",
        (game_id, season, week, home, away, book, home_spread, total, line_type),
    )


def test_movement_toward_home_when_home_spread_drops(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="Bovada")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-6.5, line_type="closing", book="Bovada")
    conn.commit()

    view = gv.build_gambling_view(conn, 2023, 5)
    conn.close()

    game = view["games"][0]
    assert game["movement"] == -3.5
    assert game["direction"] == "toward_home"
    assert game["magnitude"] == 3.5
    assert game["same_book_match"] is True


def test_movement_toward_away_when_home_spread_rises(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-6.0, line_type="opening", book="Bovada")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-2.0, line_type="closing", book="Bovada")
    conn.commit()

    view = gv.build_gambling_view(conn, 2023, 5)
    conn.close()

    game = view["games"][0]
    assert game["movement"] == 4.0
    assert game["direction"] == "toward_away"


def test_no_movement_is_flat(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="Bovada")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="closing", book="Bovada")
    conn.commit()

    view = gv.build_gambling_view(conn, 2023, 5)
    conn.close()

    assert view["games"][0]["direction"] == "flat"
    assert view["games"][0]["magnitude"] == 0.0


def test_falls_back_to_a_different_book_and_flags_it(temp_db):
    """Bovada opened it, but only DraftKings has a later number -- must
    still return a result (not skip), but flag same_book_match=False so
    the mismatch is visible, not silently presented as a clean comparison."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="Bovada")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-5.0, line_type="closing", book="DraftKings")
    conn.commit()

    view = gv.build_gambling_view(conn, 2023, 5)
    conn.close()

    game = view["games"][0]
    assert game["latest_book"] == "DraftKings"
    assert game["same_book_match"] is False


def test_games_sorted_by_magnitude_descending(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "A", "B")
    insert_line(conn, 1, 2023, 5, "A", "B", home_spread=-3.0, line_type="opening", book="Bovada")
    insert_line(conn, 1, 2023, 5, "A", "B", home_spread=-4.0, line_type="closing", book="Bovada")  # magnitude 1.0

    insert_game(conn, 2, 2023, 5, "C", "D")
    insert_line(conn, 2, 2023, 5, "C", "D", home_spread=-3.0, line_type="opening", book="Bovada")
    insert_line(conn, 2, 2023, 5, "C", "D", home_spread=-9.0, line_type="closing", book="Bovada")  # magnitude 6.0
    conn.commit()

    view = gv.build_gambling_view(conn, 2023, 5)
    conn.close()

    magnitudes = [g["magnitude"] for g in view["games"]]
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert view["games"][0]["game_id"] == 2


def test_missing_opening_line_is_skipped_not_crashed(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="closing", book="Bovada")
    conn.commit()

    view = gv.build_gambling_view(conn, 2023, 5)
    conn.close()

    assert view["games"] == []
    assert view["skipped"][0]["reason"] == "no_opening_line"


def test_no_model_fields_present():
    """This view must never carry a model prediction/side/edge field --
    it's a pure market read (ARCHITECTURE.md §19-20: no demonstrated edge)."""
    import inspect
    source = inspect.getsource(gv)
    for forbidden in ("predicted_margin", "recommended_side", '"edge":', '"confidence":'):
        assert forbidden not in source
