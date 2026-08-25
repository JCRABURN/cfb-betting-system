"""
fetch_nfl_odds.py
Live NFL spreads from The Odds API's `americanfootball_nfl` sport key,
using the same ODDS_API_KEY this project already has -- into
betting_lines with league='nfl', line_type='current'. Mirrors
data/fetch_odds.py's shape (raw-payload archival, consensus-row synthesis,
same-provider request pattern) but deliberately simpler: NFL's schedule is
resolved from `nfl_games` (already populated by backfill_nfl_games.py) by
exact (season, home_code, away_code) match, no CFBD-calendar week-range
filtering -- that machinery in fetch_odds.py exists specifically to drop
months-out marquee CFB games the Odds API lists early; whether the NFL
feed has the same shape is one of the things this script's first live run
needs to confirm (see below), not assumed.

NOT LIVE-VERIFIED as of 2026-08-24 -- api.the-odds-api.com was unreachable
from this environment (TLS connection reset) while CFBD worked fine from
the same machine moments apart -- the exact network-level block already
documented in ARCHITECTURE.md §11 for the CFB side, resolved there by
switching off a work WiFi, not something fixable from this environment.
Built assuming the Odds API's NFL team names follow its own standard
"City Nickname" convention (nfl_teams.ODDS_API_NFL_TEAM_TO_CODE) --
confirmed correct for every other sport this project already integrates,
but not this one specifically. Treat every part of this script that
touches team-name resolution as unverified until its first real run is
checked against an actual response.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import requests
from nfl_teams import ODDS_API_NFL_TEAM_TO_CODE

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
REGIONS = "us"
MARKETS = "spreads"
BOOKMAKERS = "draftkings,fanduel,betmgm"

PARSER_VERSION = "fetch_nfl_odds.v1"


def fetch_current_lines(conn=None):
    """Same request/archival shape as fetch_odds.fetch_current_lines() --
    see that function's docstring for why archival matters most here (the
    API key genuinely lives in `params`, not a header)."""
    url = f"{ODDS_BASE}/sports/{SPORT}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "bookmakers": BOOKMAKERS,
        "oddsFormat": "american",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        payload_id = None
        if conn is not None:
            payload_id = db.archive_raw_payload(
                conn, "the_odds_api", f"/sports/{SPORT}/odds", params,
                resp.text, resp.status_code, PARSER_VERSION,
            )
        resp.raise_for_status()
    except Exception as e:
        print(f"Odds API unavailable ({e}) -- returning empty lines.")
        return []

    remaining = resp.headers.get("x-requests-remaining", "?")
    print(f"Odds API requests remaining: {remaining}")

    games = resp.json()
    if conn is not None:
        db.update_raw_payload_counts(conn, payload_id, rows_accepted=len(games))
    return games


def resolve_and_match(conn, season, raw_home, raw_away):
    """Odds API team names -> nflverse codes -> a real nfl_games row.
    Returns (game_id, week, home_code, away_code) or None if either side
    doesn't resolve or no matching scheduled game exists -- never guessed,
    same discipline as fetch_odds.find_game_id()."""
    home_code = ODDS_API_NFL_TEAM_TO_CODE.get(raw_home)
    away_code = ODDS_API_NFL_TEAM_TO_CODE.get(raw_away)
    if home_code is None or away_code is None:
        return None
    row = conn.execute(
        "SELECT game_id, week FROM nfl_games WHERE season = ? AND home_team = ? AND away_team = ?",
        (season, home_code, away_code),
    ).fetchone()
    if row is None:
        return None
    return row[0], row[1], home_code, away_code


def persist_lines_to_db(conn, games_list, season):
    """Writes one betting_lines row per book plus a synthetic 'consensus'
    row per game, league='nfl', line_type='current' -- mirrors
    fetch_odds.persist_lines_to_db()'s shape exactly."""
    now = datetime.utcnow().isoformat()
    rows_added = 0
    unmatched = 0

    for game in games_list:
        raw_home = game.get("home_team")
        raw_away = game.get("away_team")
        match = resolve_and_match(conn, season, raw_home, raw_away)
        if match is None:
            unmatched += 1
            game_id, week, home_code, away_code = None, None, raw_home, raw_away
        else:
            game_id, week, home_code, away_code = match

        book_spreads = []
        for bookmaker in game.get("bookmakers", []):
            bk_key = bookmaker["key"]
            home_spread = None
            for market in bookmaker.get("markets", []):
                if market["key"] != "spreads":
                    continue
                for outcome in market.get("outcomes", []):
                    if outcome.get("name") == raw_home:
                        home_spread = outcome.get("point")
            if home_spread is None:
                continue
            book_spreads.append(home_spread)
            conn.execute(
                """
                INSERT INTO betting_lines (
                    game_id, league, season, week, home_team, away_team, book,
                    home_spread, line_type, source, fetched_at
                ) VALUES (?, 'nfl', ?, ?, ?, ?, ?, ?, 'current', 'the_odds_api', ?)
                """,
                (game_id, season, week, home_code, away_code, bk_key, home_spread, now),
            )
            rows_added += 1

        consensus = round(sum(book_spreads) / len(book_spreads), 1) if book_spreads else None
        conn.execute(
            """
            INSERT INTO betting_lines (
                game_id, league, season, week, home_team, away_team, book,
                home_spread, line_type, source, fetched_at
            ) VALUES (?, 'nfl', ?, ?, ?, ?, 'consensus', ?, 'current', 'the_odds_api', ?)
            """,
            (game_id, season, week, home_code, away_code, consensus, now),
        )
        rows_added += 1

    conn.commit()
    if unmatched:
        print(f"WARNING: {unmatched}/{len(games_list)} games had no matching nfl_games row "
              f"(team-name resolution failed, or that game isn't in nfl_games yet) -- "
              f"betting_lines rows saved with game_id=NULL")
    return rows_added


def main():
    with db.log_run("nfl_odds_api") as run:
        conn = db.get_connection()
        try:
            season = datetime.utcnow().year
            games = fetch_current_lines(conn=conn)
            if not games:
                print("No NFL games returned -- nothing to persist.")
                return
            rows_added = persist_lines_to_db(conn, games, season)
            run["rows_added"] = rows_added
        finally:
            conn.close()

    print(f"Persisted {rows_added} rows to {db.DB_PATH}")


if __name__ == "__main__":
    main()
