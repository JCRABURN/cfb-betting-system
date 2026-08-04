"""
line_utils.py
Shared line-lookup helpers, used by card_generator.py and both drift views
(gambling_view.py, pool_view.py) -- pulled out once a second consumer needed
the same "what's the latest line for this game" logic, matching this
project's convention of factoring out shared utilities once they're used by
more than one caller (see backtest_report.py).
"""

# The three books actually covered by the live Odds API pull (Caesars
# dropped 2026-08-04 -- confirmed live, twice, that it never appears in any
# game's book listing; fetch_odds.BOOKMAKERS matches this exactly). Order
# is the preference for get_opening_line_real_book() below, not a ranking
# of book quality -- draftkings first only because it happens to have the
# most complete coverage in spot checks so far.
REAL_BOOK_PREFERENCE = ["draftkings", "fanduel", "betmgm"]


def list_all_games(conn, season, week):
    """Every FBS game scheduled for (season, week), regardless of whether
    it's been played yet -- unlike backtest_harness.list_games(), which
    intentionally only returns completed games (correct for grading, wrong
    for a card or a live line report: an upcoming game is exactly what
    these exist to cover)."""
    return conn.execute(
        "SELECT game_id, home_team, away_team, start_date FROM games "
        "WHERE season = ? AND week = ? ORDER BY game_id",
        (season, week),
    ).fetchall()


def get_latest_line(conn, game_id, prefer_book=None):
    """The most recent available market line for a game: 'current' if the
    live in-season path has written one, else 'closing' (the historical
    archive's term for the same concept -- the last number seen before
    kickoff). Confirmed live: 2024 week 10 has 0 'current' rows, only
    'opening'/'closing' -- trying both line_types is what lets this one
    function work unmodified against either data source.

    prefer_book: if given, tries that EXACT book first (across both
    line_types) before falling back to consensus-then-any-book. This is
    what lets gambling_view.py compare an opening line to the LATEST line
    from the SAME book, instead of opening-from-one-book vs.
    latest-from-a-different-book -- the same same-book discipline
    backtest_harness.get_closing_line() already applies to CLV, generalized
    here across the live path's 'current'/'closing' vocabulary split.
    Falls through to the book-agnostic behavior if that book has no line
    for either type (never silently fails just because the preferred book
    lacks a later number)."""
    if prefer_book is not None:
        for line_type in ("current", "closing"):
            row = conn.execute(
                "SELECT home_spread, total FROM betting_lines "
                "WHERE game_id = ? AND line_type = ? AND book = ? AND home_spread IS NOT NULL",
                (game_id, line_type, prefer_book),
            ).fetchone()
            if row is not None:
                return {"home_spread": row[0], "total": row[1], "book": prefer_book, "line_type": line_type}

    for line_type in ("current", "closing"):
        row = conn.execute(
            "SELECT home_spread, total FROM betting_lines "
            "WHERE game_id = ? AND line_type = ? AND book = 'consensus'",
            (game_id, line_type),
        ).fetchone()
        if row is not None and row[0] is not None:
            return {"home_spread": row[0], "total": row[1], "book": "consensus", "line_type": line_type}

        row = conn.execute(
            "SELECT home_spread, total, book FROM betting_lines "
            "WHERE game_id = ? AND line_type = ? AND home_spread IS NOT NULL "
            "ORDER BY book LIMIT 1",
            (game_id, line_type),
        ).fetchone()
        if row is not None:
            return {"home_spread": row[0], "total": row[1], "book": row[2], "line_type": line_type}
    return None


def get_opening_line_real_book(conn, game_id):
    """Like backtest_harness.get_opening_line(), but prefers a REAL book
    (REAL_BOOK_PREFERENCE order) over the synthetic 'consensus' row.

    Built specifically for gambling_view.py's same-book opener-vs-latest
    comparison: consensus is an average over whichever books happened to
    have a price at that moment, and that basket can change between the
    opening pull and a later one (a book joins or drops out) -- so
    consensus-vs-consensus can show "movement" that's really just a
    different set of books being averaged, not the market actually moving.
    A single real book's own number, compared to its own later number, has
    no such ambiguity.

    NOT used by backtest_harness.get_opening_line() (that function
    deliberately prefers consensus, and changing it would silently alter
    already-reported backtest numbers) or by card_generator.py/pool_view.py
    (which want the single best available number for a live pick, and
    consensus is the right choice there -- see get_latest_line() above,
    unchanged). Returns None if none of the three real books has an
    opener for this game (the caller must skip, not fall back further)."""
    for book in REAL_BOOK_PREFERENCE:
        row = conn.execute(
            "SELECT home_spread, total FROM betting_lines "
            "WHERE game_id = ? AND line_type = 'opening' AND book = ? AND home_spread IS NOT NULL",
            (game_id, book),
        ).fetchone()
        if row is not None:
            return {"home_spread": row[0], "total": row[1], "book": book}
    return None
