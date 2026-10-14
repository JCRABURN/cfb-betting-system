"""fetch_odds: totals parsing, totals persistence, in-play guard (2026-10-14)."""
from datetime import datetime

import fetch_odds as fo


def _raw_game(commence="2099-01-01T18:00:00Z"):
    return {
        "id": "x1", "home_team": "Alpha", "away_team": "Beta", "commence_time": commence,
        "bookmakers": [
            {"key": "draftkings", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Alpha", "point": -3.0, "price": -110},
                    {"name": "Beta", "point": 3.0, "price": -110}]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 51.5, "price": -108},
                    {"name": "Under", "point": 51.5, "price": -112}]},
            ]},
            {"key": "fanduel", "markets": [
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 52.5, "price": -110},
                    {"name": "Under", "point": 52.5, "price": -110}]},
            ]},
        ],
    }


def test_markets_requests_totals():
    assert "totals" in fo.MARKETS and "spreads" in fo.MARKETS


def test_parse_totals_per_book_and_consensus():
    totals, consensus = fo.parse_totals(_raw_game())
    assert totals["draftkings"] == {"total": 51.5, "over_price": -108, "under_price": -112}
    assert totals["fanduel"]["total"] == 52.5
    assert consensus == 52.0


def test_parse_totals_empty_when_no_totals_market():
    game = _raw_game()
    for b in game["bookmakers"]:
        b["markets"] = [m for m in b["markets"] if m["key"] != "totals"]
    assert fo.parse_totals(game) == ({}, None)


def test_parse_totals_tolerates_malformed_blocks():
    game = {"bookmakers": [
        {"key": "dk", "markets": [{"key": "totals", "outcomes": [{"name": "Over"}]}]},
        {"key": "fd", "markets": None},
        {"key": "mgm"},
    ]}
    assert fo.parse_totals(game) == ({}, None)


def test_is_in_play_boundaries():
    now = datetime(2026, 10, 3, 17, 0, 0)
    assert fo.is_in_play("2026-10-03T16:00:00Z", now) is True    # kicked off an hour ago
    assert fo.is_in_play("2026-10-03T17:00:00Z", now) is True    # exactly kickoff: not a pre-game line
    assert fo.is_in_play("2026-10-03T17:00:01Z", now) is False
    assert fo.is_in_play(None, now) is False                      # keep, never silently drop
    assert fo.is_in_play("garbage", now) is False


def _processed(raw):
    totals, consensus_total = fo.parse_totals(raw)
    return {
        "home_team": "Alpha", "away_team": "Beta", "commence_time": raw["commence_time"],
        "consensus_home_spread": -3.0, "consensus_total": consensus_total, "totals": totals,
        "books": {"draftkings": {"Alpha": {"spread": -3.0, "juice": -110},
                                  "Beta": {"spread": 3.0, "juice": -110}}},
    }


def _seed_game(temp_db):
    conn = temp_db.get_connection()
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team) VALUES (7, 2026, 7, 'Alpha', 'Beta')"
    )
    conn.commit()
    conn.close()


def test_persist_writes_totals_on_book_rows_consensus_and_total_only_book(temp_db):
    _seed_game(temp_db)
    fo.persist_lines_to_db([_processed(_raw_game())], 7, 2026, "current")

    conn = temp_db.get_connection()
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT book, home_spread, total FROM betting_lines WHERE game_id = 7")}
    conn.close()
    assert rows["draftkings"] == (-3.0, 51.5)
    assert rows["fanduel"] == (None, 52.5)        # total-only book still recorded
    assert rows["consensus"] == (-3.0, 52.0)


def test_persist_skips_in_play_games(temp_db):
    _seed_game(temp_db)
    added = fo.persist_lines_to_db(
        [_processed(_raw_game(commence="2020-01-01T00:00:00Z"))], 7, 2026, "current")

    conn = temp_db.get_connection()
    n = conn.execute("SELECT COUNT(*) FROM betting_lines").fetchone()[0]
    conn.close()
    assert added == 0 and n == 0


def test_spread_only_payload_still_persists(temp_db):
    """No totals market at all (e.g. a book/feed without it) must not break
    the spread path -- totals are strictly additive."""
    _seed_game(temp_db)
    raw = _raw_game()
    for b in raw["bookmakers"]:
        b["markets"] = [m for m in b["markets"] if m["key"] != "totals"]
    fo.persist_lines_to_db([_processed(raw)], 7, 2026, "current")

    conn = temp_db.get_connection()
    rows = {r[0]: r[1:] for r in conn.execute(
        "SELECT book, home_spread, total FROM betting_lines WHERE game_id = 7")}
    conn.close()
    assert rows["draftkings"] == (-3.0, None)
    assert rows["consensus"] == (-3.0, None)
