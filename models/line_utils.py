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


def _kickoff_cutoff(conn, game_id):
    """First 19 chars ('YYYY-MM-DDTHH:MM:SS', UTC) of the game's scheduled
    kickoff, or None if unknown. betting_lines.fetched_at is naive UTC ISO
    and games.start_date is ISO-Z, so a plain string compare on this prefix
    is a correct chronological compare."""
    row = conn.execute("SELECT start_date FROM games WHERE game_id = ?", (game_id,)).fetchone()
    return row[0][:19] if row and row[0] else None


# Appended to any query over betting_lines for ONE game. Applies ONLY to
# line_type='current' (the live path): those are the only rows that can be
# in-play prices. 'opening'/'closing' rows from the historical backfill have
# a fetched_at of the backfill run itself (long after kickoff) and are not
# in-play, so they must not be filtered. Found live 2026-10-14: a Saturday
# pull landing after kickoff stored Stanford @ Wake Forest at -33.5 (an
# in-game blowout line) as 'current' after -12.5/-13.5 pre-game; 17% of
# settled picks had CLV computed against such a snapshot.
# A fetched_at that is not an ISO timestamp (never true in production; test
# fixtures use the literal 'now') is not comparable, so it is kept rather
# than string-compared -- 'now' > '2023-...' lexicographically would
# otherwise read as "after kickoff" and silently drop the row.
_PRE_KICKOFF_SQL = (
    "AND (? IS NULL OR line_type != 'current' "
    "OR fetched_at NOT GLOB '[0-9][0-9][0-9][0-9]-*' "
    "OR substr(fetched_at, 1, 19) < ?)"
)


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
    lacks a later number).

    betting_lines is APPEND-ONLY per (game_id, line_type, book) -- each
    fetch_odds.py pull writes a NEW 'current' row rather than updating one
    in place (db.py schema comment), so more than one row can match any of
    the three queries below once a game has been pulled more than once.
    Every branch orders by `fetched_at DESC LIMIT 1` so `.fetchone()`
    deterministically gets the newest snapshot -- without it, SQLite
    returns rows in unspecified (in practice: insertion/rowid) order, i.e.
    the OLDEST snapshot, which is exactly backwards for a function named
    get_latest_line (found live 2026-08-12: this silently returned a
    week-old DraftKings number and made every game's line "drift" read as
    0.0, since opener and "latest" were resolving to the same stale row).
    The book-agnostic fallback used to `ORDER BY book` (alphabetical, blind
    to recency) instead -- also wrong for the same reason, now fixed the
    same way.

    PRE-KICKOFF ONLY (2026-10-14): a 'current' row fetched at/after the
    game's scheduled kickoff is an in-play price, not a market number
    anyone could have bet, and is excluded -- see _PRE_KICKOFF_SQL. "Latest"
    therefore means latest PRE-GAME line, which is also what CLV and the
    drift views mean by it."""
    cutoff = _kickoff_cutoff(conn, game_id)
    if prefer_book is not None:
        for line_type in ("current", "closing"):
            row = conn.execute(
                "SELECT home_spread, total FROM betting_lines "
                "WHERE game_id = ? AND line_type = ? AND book = ? AND home_spread IS NOT NULL "
                + _PRE_KICKOFF_SQL + " ORDER BY fetched_at DESC LIMIT 1",
                (game_id, line_type, prefer_book, cutoff, cutoff),
            ).fetchone()
            if row is not None:
                return {"home_spread": row[0], "total": row[1], "book": prefer_book, "line_type": line_type}

    for line_type in ("current", "closing"):
        row = conn.execute(
            "SELECT home_spread, total FROM betting_lines "
            "WHERE game_id = ? AND line_type = ? AND book = 'consensus' "
            + _PRE_KICKOFF_SQL + " ORDER BY fetched_at DESC LIMIT 1",
            (game_id, line_type, cutoff, cutoff),
        ).fetchone()
        if row is not None and row[0] is not None:
            return {"home_spread": row[0], "total": row[1], "book": "consensus", "line_type": line_type}

        row = conn.execute(
            "SELECT home_spread, total, book FROM betting_lines "
            "WHERE game_id = ? AND line_type = ? AND home_spread IS NOT NULL "
            + _PRE_KICKOFF_SQL + " ORDER BY fetched_at DESC LIMIT 1",
            (game_id, line_type, cutoff, cutoff),
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
    opener for this game (the caller must skip, not fall back further).

    ORDER BY fetched_at ASC LIMIT 1 (oldest wins, opposite direction from
    get_latest_line() above): 'opening' rows are write-once per week under
    NORMAL operation (fetch_odds.opening_line_recorded() guards against a
    second pull re-tagging one), so this was "safe" the same way
    get_latest_line() was safe before real snapshots accumulated -- until
    backfill_historical_lines.py --force re-ingests a week WITHOUT deleting
    its old rows first, at which point duplicate 'opening' rows exist and
    an unordered .fetchone() becomes exploitable the same way (external
    review follow-up, accepted 2026-08-12). The TRUE opener is whichever
    row was fetched FIRST, hence ASC -- not DESC, which would return the
    duplicate closest to the --force re-run instead of the genuine open."""
    for book in REAL_BOOK_PREFERENCE:
        row = conn.execute(
            "SELECT home_spread, total FROM betting_lines "
            "WHERE game_id = ? AND line_type = 'opening' AND book = ? AND home_spread IS NOT NULL "
            "ORDER BY fetched_at ASC LIMIT 1",
            (game_id, book),
        ).fetchone()
        if row is not None:
            return {"home_spread": row[0], "total": row[1], "book": book}
    return None


def get_latest_total(conn, game_id, prefer_book=None):
    """Most recent PRE-KICKOFF total for a game (same recency and in-play
    rules as get_latest_line), preferring `prefer_book` exactly, then the
    consensus row, then any book. Returns {"total", "book", "line_type"} or
    None. Independent of home_spread on purpose: a book can post a total
    without a spread and vice versa, and requiring both would silently
    drop games from the totals view."""
    cutoff = _kickoff_cutoff(conn, game_id)
    books = ([prefer_book] if prefer_book else []) + ["consensus", None]
    for book in books:
        for line_type in ("current", "closing"):
            sql = ("SELECT total, book FROM betting_lines WHERE game_id = ? AND line_type = ? "
                   "AND total IS NOT NULL ")
            params = [game_id, line_type]
            if book is not None:
                sql += "AND book = ? "
                params.append(book)
            sql += _PRE_KICKOFF_SQL + " ORDER BY fetched_at DESC LIMIT 1"
            params += [cutoff, cutoff]
            row = conn.execute(sql, params).fetchone()
            if row is not None:
                return {"total": row[0], "book": row[1], "line_type": line_type}
    return None


def get_first_seen_total_real_book(conn, game_id):
    """Earliest recorded total from a REAL book (REAL_BOOK_PREFERENCE
    order), as the baseline for totals movement. "First seen", not "the
    opener": totals only began being collected 2026-10-14 (MARKETS used to
    be spreads-only), so for any game already in-week by then this is the
    first snapshot we have, not the market's true opening number -- the
    view labels it accordingly. For games first pulled with totals from
    the week's opening pull onward, it IS the opener. ORDER BY fetched_at
    ASC: oldest wins, same reasoning as get_opening_line_real_book()."""
    for book in REAL_BOOK_PREFERENCE:
        row = conn.execute(
            "SELECT total FROM betting_lines WHERE game_id = ? AND book = ? "
            "AND total IS NOT NULL AND line_type IN ('opening', 'current') "
            "ORDER BY fetched_at ASC LIMIT 1",
            (game_id, book),
        ).fetchone()
        if row is not None:
            return {"total": row[0], "book": book}
    return None
