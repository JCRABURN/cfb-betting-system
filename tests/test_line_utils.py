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


def insert_line(conn, game_id, season, week, home, away, home_spread, line_type, book, total=45.0,
                 fetched_at="now"):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', ?)",
        (game_id, season, week, home, away, book, home_spread, total, line_type, fetched_at),
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


def test_prefer_book_returns_newest_snapshot_not_oldest(temp_db):
    """Regression for the live bug found 2026-08-12 (North Carolina @ TCU,
    game_id=401856766): betting_lines is append-only, so a book can have
    MULTIPLE 'current' rows over time. Without ORDER BY fetched_at DESC,
    .fetchone() returned whichever row SQLite's unspecified default order
    surfaced -- in practice the OLDEST (first-inserted) row, exactly
    backwards for a function named get_latest_line."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-7.0, line_type="current", book="draftkings",
                fetched_at="2026-08-03T21:05:04.115305")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-7.0, line_type="current", book="draftkings",
                fetched_at="2026-08-04T16:13:49.004684")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-7.5, line_type="current", book="draftkings",
                fetched_at="2026-08-08T13:49:39.722585")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-7.5, line_type="current", book="draftkings",
                fetched_at="2026-08-11T15:02:35.482675")
    conn.commit()

    line = lu.get_latest_line(conn, 1, prefer_book="draftkings")
    conn.close()

    assert line["home_spread"] == -7.5
    assert line["book"] == "draftkings"


def test_consensus_branch_returns_newest_snapshot_not_oldest(temp_db):
    """Same append-only defect, no prefer_book -- consensus branch."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="current", book="consensus",
                fetched_at="2026-08-03T21:05:04.115305")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-4.5, line_type="current", book="consensus",
                fetched_at="2026-08-11T15:02:35.482675")
    conn.commit()

    line = lu.get_latest_line(conn, 1)
    conn.close()

    assert line["home_spread"] == -4.5
    assert line["book"] == "consensus"


def test_book_agnostic_fallback_returns_newest_snapshot_not_alphabetical_book(temp_db):
    """The third branch used to `ORDER BY book` (alphabetical), which is
    blind to recency in exactly the same way -- 'betmgm' would always win
    over 'fanduel' regardless of which is actually the latest pull. No
    consensus row present here, so this exercises the book-agnostic
    fallback specifically. 'betmgm' sorts before 'fanduel' alphabetically
    but is the OLDER snapshot -- the old code would have wrongly returned
    it."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="current", book="betmgm",
                fetched_at="2026-08-03T21:05:04.115305")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.5, line_type="current", book="fanduel",
                fetched_at="2026-08-11T15:02:35.482675")
    conn.commit()

    line = lu.get_latest_line(conn, 1)
    conn.close()

    assert line["home_spread"] == -3.5
    assert line["book"] == "fanduel"


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


def test_real_book_opener_picks_oldest_of_duplicates(temp_db):
    """Regression (external review follow-up, accepted 2026-08-12): same
    duplicate-row exposure get_latest_line() had -- 'opening' rows are
    write-once per (game_id, book) under normal operation, but
    backfill_historical_lines.py --force re-ingests without deleting old
    rows first. Opening must pick the OLDEST (the true first-seen open),
    opposite direction from get_latest_line()."""
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="draftkings",
                fetched_at="2023-09-01T00:00:00")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.5, line_type="opening", book="draftkings",
                fetched_at="2023-09-15T00:00:00")
    conn.commit()

    line = lu.get_opening_line_real_book(conn, 1)
    conn.close()

    assert line["home_spread"] == -3.0  # the earlier (genuine) open, not the --force re-ingest


def test_real_book_opener_none_when_only_consensus_or_historical_books_exist(temp_db):
    conn = temp_db.get_connection()
    insert_game(conn, 1, 2023, 5, "X", "Y")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="consensus")
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="opening", book="Bovada")
    conn.commit()

    line = lu.get_opening_line_real_book(conn, 1)
    conn.close()

    assert line is None
