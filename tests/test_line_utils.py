"""
Tests for line_utils.py's prefer_book behavior specifically -- the rest of
get_latest_line()/list_all_games() is already covered by
tests/test_card_generator.py (unchanged after the module move, since
card_generator.py now imports these same functions from here).
"""

import line_utils as lu


def insert_game(conn, game_id, season, week, home, away, completed=0,
                 start_date="2023-01-01T00:00:00.000Z"):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, completed, start_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, home, away, completed, start_date),
    )


def insert_line(conn, game_id, season, week, home, away, home_spread, line_type, book, total=45.0):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', 'now')",
        (game_id, season, week, home, away, book, home_spread, total, line_type),
    )


def test_prefer_book_returns_that_books_own_line_over_consensus(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.5, line_type="current", book="consensus")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-4.0, line_type="current", book="Bovada")
    conn.commit()

    line = lu.get_latest_line(conn, 1, prefer_book="Bovada")
    conn.close()

    assert line["home_spread"] == -4.0
    assert line["book"] == "Bovada"


def test_prefer_book_falls_back_to_consensus_when_that_book_has_no_line(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.5, line_type="current", book="consensus")
    conn.commit()

    line = lu.get_latest_line(conn, 1, prefer_book="Bovada")
    conn.close()

    assert line["home_spread"] == -3.5
    assert line["book"] == "consensus"


def test_prefer_book_checks_current_before_closing_for_that_book(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-9.0, line_type="closing", book="Bovada")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-4.0, line_type="current", book="Bovada")
    conn.commit()

    line = lu.get_latest_line(conn, 1, prefer_book="Bovada")
    conn.close()

    assert line["home_spread"] == -4.0
    assert line["line_type"] == "current"


def test_omitting_prefer_book_still_prefers_consensus(temp_db):
    """Omitting prefer_book must fall straight to the existing
    consensus-then-any-book behavior (already covered in
    test_card_generator.py) -- not silently require a book preference."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-4.0, line_type="current", book="Bovada")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.5, line_type="current", book="consensus")
    conn.commit()

    line = lu.get_latest_line(conn, 1)
    conn.close()

    assert line["book"] == "consensus"
    assert line["home_spread"] == -3.5


# ---------------------------------------------------------------------------
# get_opening_line_real_book -- built 2026-08-04 for gambling_view.py's
# same-book comparison, deliberately never prefers 'consensus'
# ---------------------------------------------------------------------------

def test_real_book_opener_ignores_consensus_even_when_present(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.2, line_type="opening", book="consensus")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="draftkings")
    conn.commit()

    line = lu.get_opening_line_real_book(conn, 1)
    conn.close()

    assert line["book"] == "draftkings"
    assert line["home_spread"] == -3.0


def test_real_book_opener_follows_preference_order(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="betmgm")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.2, line_type="opening", book="fanduel")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.5, line_type="opening", book="draftkings")
    conn.commit()

    line = lu.get_opening_line_real_book(conn, 1)
    conn.close()

    assert line["book"] == "draftkings"


def test_real_book_opener_falls_back_when_first_preference_missing(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="betmgm")
    conn.commit()

    line = lu.get_opening_line_real_book(conn, 1)
    conn.close()

    assert line["book"] == "betmgm"


def test_real_book_opener_none_when_only_consensus_or_historical_books_exist(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="consensus")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="Bovada")
    conn.commit()

    line = lu.get_opening_line_real_book(conn, 1)
    conn.close()

    assert line is None
