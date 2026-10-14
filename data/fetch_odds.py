"""
fetch_odds.py
Pulls current spreads, line movement, and juice from The Odds API.
Stores opening and current lines for CLV tracking.

Runs more than once a week now (see .github/workflows/midweek_line_pull.yml
-- Thursday/Saturday, in addition to the original Tuesday pull), which is
what the gambling/pool drift views (models/gambling_view.py,
models/pool_view.py) need: a later same-book number to compare an opener
against. Week/year detection used to read the latest data/stats/week_*.json
file fetch_stats.py had just written -- but that directory is gitignored
and never survives a GitHub Actions checkout, so a Thursday/Saturday run
(which does NOT re-run fetch_stats.py -- no need to, this week's games are
already in the committed data/cfb.db from Tuesday) would always fail with
"No stats files found." Now calls fetch_stats.get_current_week() directly
and checks the DB for this week's games, so it has no dependency on
whether fetch_stats.py ran earlier in the SAME job.
"""

import os
import sys
import json
import unicodedata
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import fetch_stats

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"

SPORT = "americanfootball_ncaaf"
REGIONS = "us"
# Totals added 2026-10-14. The Odds API bills one credit per market per
# region per request, so this doubles each pull (1 -> 2). At 3 scheduled
# pulls/week that is ~26 credits/month against the 500/month free tier.
MARKETS = "spreads,totals"
# Caesars dropped 2026-08-04: confirmed live (twice -- once during initial
# testing, again against a full real week 1 pull) that it never appears in
# any game's book listing. Keeping it in the config implied 4-book coverage
# that doesn't exist; three real books is what's actually available.
BOOKMAKERS = "draftkings,fanduel,betmgm"

# Bumped whenever the parsing logic below changes in a way that would
# matter for replaying an archived raw_payloads row through a fixed
# parser (external review, accepted 2026-08-04).
PARSER_VERSION = "fetch_odds.v2"  # v2: totals market parsed

# Known cases (found by spot-checking a live Odds API response against our
# CFBD-sourced teams table, 2026-07-29) where CFBD's official school name
# shares no prefix at all with The Odds API's team name -- one side uses an
# abbreviation the other doesn't. No amount of prefix-matching or accent
# normalization fixes these; more may surface over time as new pairs are hit.
# Keyed by the odds-side name (or its distinctive prefix), valued by the
# corresponding CFBD school name.
KNOWN_TEAM_ALIASES = {
    "Appalachian State": "App State",
    "UMass": "Massachusetts",
    "Southern Mississippi": "Southern Miss",
    "Louisiana-Monroe": "UL Monroe",
    "Louisiana Monroe": "UL Monroe",
    "UL-Monroe": "UL Monroe",
    # Abbreviations used by the user's SplashSports slate (Week 7, 2026).
    "ULM": "UL Monroe",
    "FIU": "Florida International",
    "FAU": "Florida Atlantic",
}


def parse_totals(game):
    """{book: {"total", "over_price", "under_price"}} from one raw Odds API
    game dict, plus the consensus (mean) total. Deliberately a SEPARATE
    structure from the spreads parse in fetch_current_lines(): the spread
    path is load-bearing for every report, so totals must not be able to
    break it -- a malformed or missing totals block yields {} / None here
    and the spread data is untouched. Over and Under outcomes share one
    point; the first one seen sets it."""
    totals = {}
    for bookmaker in game.get("bookmakers", []) or []:
        for market in bookmaker.get("markets", []) or []:
            if market.get("key") != "totals":
                continue
            entry = {}
            for outcome in market.get("outcomes", []) or []:
                name = (outcome.get("name") or "").lower()
                point = outcome.get("point")
                if point is None or name not in ("over", "under"):
                    continue
                entry.setdefault("total", point)
                entry[f"{name}_price"] = outcome.get("price")
            if "total" in entry:
                totals[bookmaker["key"]] = entry
    consensus = (
        round(sum(v["total"] for v in totals.values()) / len(totals), 1) if totals else None
    )
    return totals, consensus


def is_in_play(commence_time, now=None):
    """True if the game's scheduled commence_time is at or before `now`
    (UTC). The Odds API's /odds endpoint also returns games already in
    progress with live prices; those are not lines anyone could bet before
    kickoff and must not be stored as 'current' (see line_utils.
    _PRE_KICKOFF_SQL for the read-side counterpart and the incident that
    motivated both). Unparseable/missing commence_time returns False, so a
    game is kept rather than silently dropped."""
    if not commence_time:
        return False
    try:
        ct = datetime.strptime(commence_time[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return ct <= (now or datetime.utcnow())


def fetch_current_lines(conn=None):
    """Fetch current spreads for all upcoming CFB games. `conn`, if given,
    archives the raw response (external review, accepted 2026-08-04) --
    the API key is redacted by db.archive_raw_payload() itself regardless,
    but this is the one call site in the project where the key genuinely
    lives in `params` rather than a header, so it's the one that matters
    most for that safety net actually doing something."""
    url = f"{ODDS_BASE}/sports/{SPORT}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": REGIONS,
        "markets": MARKETS,
        "bookmakers": BOOKMAKERS,
        "oddsFormat": "american"
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if conn is not None:
            payload_id = db.archive_raw_payload(
                conn, "the_odds_api", f"/sports/{SPORT}/odds", params,
                resp.text, resp.status_code, PARSER_VERSION,
            )
        resp.raise_for_status()
    except Exception as e:
        print(f"Odds API unavailable ({e}) — returning empty lines.")
        return []

    remaining = resp.headers.get("x-requests-remaining", "?")
    print(f"Odds API requests remaining: {remaining}")

    games = resp.json()
    if conn is not None:
        db.update_raw_payload_counts(conn, payload_id, rows_accepted=len(games))
    processed = []

    for game in games:
        home = game.get("home_team")
        away = game.get("away_team")
        commence = game.get("commence_time")
        lines = {}

        for bookmaker in game.get("bookmakers", []):
            bk_key = bookmaker["key"]
            for market in bookmaker.get("markets", []):
                if market["key"] == "spreads":
                    for outcome in market.get("outcomes", []):
                        team = outcome["name"]
                        point = outcome.get("point", 0)
                        price = outcome.get("price", -110)
                        lines[bk_key] = lines.get(bk_key, {})
                        lines[bk_key][team] = {
                            "spread": point,
                            "juice": price
                        }

        # Consensus line: average across books
        all_home_spreads = [
            v[home]["spread"]
            for v in lines.values()
            if home in v
        ]
        consensus_spread = (
            round(sum(all_home_spreads) / len(all_home_spreads), 1)
            if all_home_spreads else None
        )

        try:
            totals, consensus_total = parse_totals(game)
        except Exception as e:  # never let totals take down the spread path
            print(f"WARNING: totals parse failed for {home} vs {away}: {e}")
            totals, consensus_total = {}, None

        processed.append({
            "game_id": game.get("id"),
            "home_team": home,
            "away_team": away,
            "commence_time": commence,
            "consensus_home_spread": consensus_spread,
            "consensus_total": consensus_total,
            "totals": totals,
            "books": lines,
            "fetched_at": datetime.utcnow().isoformat()
        })

    return processed


def filter_by_week(games, first_game_start, last_game_start):
    """Keeps only games whose commence_time falls within the target week's
    actual date range (from CFBD's /calendar, ISO-8601 UTC -- comparable as
    strings the same way fetch_stats.get_current_week() already does).

    The Odds API returns every CFB game it currently has a line for,
    including marquee matchups months out that books price early (Ohio
    State-Michigan, Texas-Oklahoma, etc., already listed in early August).
    Without this filter, persist_lines_to_db() stamps those with the
    CALLER's week/year regardless of when the game actually happens --
    confirmed live: 334 rows for "week 1, 2026" that were actually other
    weeks' games, none of which ever join to a real CFBD game_id since
    find_game_id() correctly scopes its lookup to the target week (they end
    up as harmless-looking but wrong NULL-game_id noise). A game missing
    commence_time entirely is dropped, not kept by default -- can't verify
    it belongs to this week, so it doesn't get to assume it does."""
    return [
        g for g in games
        if g.get("commence_time") and first_game_start <= g["commence_time"] <= last_game_start
    ]


def load_opening_lines(week, year):
    """Load previously saved opening lines if they exist."""
    path = f"data/spreads/opening_week_{week}_{year}.json"
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return {g["game_id"]: g for g in json.load(f)}
    return {}


def save_lines(data, week, year, label="current"):
    """Save line data to disk."""
    os.makedirs("data/spreads", exist_ok=True)
    path = f"data/spreads/{label}_week_{week}_{year}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Saved {label} lines to {path}")


def calculate_line_movement(current_lines, opening_lines):
    """Add movement delta to each game."""
    for game in current_lines:
        gid = game["game_id"]
        opening = opening_lines.get(gid, {})
        if opening:
            open_spread = opening.get("consensus_home_spread")
            curr_spread = game.get("consensus_home_spread")
            if open_spread is not None and curr_spread is not None:
                game["line_movement"] = round(curr_spread - open_spread, 1)
                game["opening_spread"] = open_spread
            else:
                game["line_movement"] = None
                game["opening_spread"] = None
        else:
            game["line_movement"] = None
            game["opening_spread"] = None
    return current_lines


def load_school_names(conn):
    return [r[0] for r in conn.execute("SELECT school FROM teams").fetchall()]


def _normalize(name):
    """Strip accents and apostrophes so "Hawai'i"/"Hawaii" and "San José"/
    "San Jose" compare equal without needing a hardcoded alias for every
    diacritic difference between the two APIs."""
    name = name.replace("’", "'").replace("'", "")
    nfkd = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def resolve_school_name(schools, odds_team_name):
    """The Odds API includes the mascot in team names (e.g. "TCU Horned Frogs",
    "NC State Wolfpack"); CFBD uses bare school names ("TCU", "NC State") --
    confirmed live 2026-07-29 against a real Odds API response, where every
    single game had this mismatch. Resolve via longest-known-school-name-prefix
    (after accent/apostrophe normalization), which also disambiguates cases
    where one school name is a prefix of another (e.g. "Ohio" vs "Ohio State",
    "Miami" vs "Miami (OH)" -- both pairs exist in the teams table; the longer,
    more specific match always wins). KNOWN_TEAM_ALIASES is checked first for
    the handful of cases with no shared prefix at all.

    Falls back to the raw odds_team_name if nothing matches, so a resolution
    failure never silently drops the row -- it just won't join to a CFBD game_id.
    """
    for alias, canonical in KNOWN_TEAM_ALIASES.items():
        if (odds_team_name == alias or odds_team_name.startswith(alias + " ")) and canonical in schools:
            return canonical

    normalized_name = _normalize(odds_team_name)
    for school in schools:
        if _normalize(school) == normalized_name:
            return school

    candidates = [s for s in schools if normalized_name.startswith(_normalize(s) + " ")]
    if not candidates:
        return odds_team_name
    return max(candidates, key=len)


def opening_line_recorded(conn, week, year):
    """Whether an opening line already exists in the DB for this week.

    Checked against betting_lines (the persistent store), not the local
    data/spreads/opening_week_*.json file: that file lives in a gitignored
    directory and doesn't survive between GitHub Actions runs (fresh checkout
    every time), so checking it always says "no, this is the first pull" --
    meaning a manual mid-week workflow_dispatch re-run would insert a second,
    later line mislabeled line_type='opening' and corrupt the CLV baseline
    for every game that week.
    """
    row = conn.execute(
        "SELECT 1 FROM betting_lines WHERE season = ? AND week = ? AND line_type = 'opening' LIMIT 1",
        (year, week),
    ).fetchone()
    return row is not None


def find_game_id(conn, week, year, home, away):
    """Best-effort join to the CFBD games row via team name (Odds API has its own id space)."""
    row = conn.execute(
        "SELECT game_id FROM games WHERE season = ? AND week = ? "
        "AND home_team = ? AND away_team = ?",
        (year, week, home, away),
    ).fetchone()
    return row[0] if row else None


def persist_lines_to_db(games_list, week, year, line_type):
    """Write one betting_lines row per book (plus a synthetic 'consensus' row) per game.

    Games already in progress (commence_time <= now) are skipped: the
    Odds API returns live in-play prices alongside upcoming games, and
    storing those as 'current' corrupted CLV and the movement view
    (found 2026-10-14; see is_in_play). Totals (added 2026-10-14) ride on
    the same per-book rows via the `total` column; a book that posted a
    total but no spread still gets a row (home_spread NULL), which every
    spread reader already excludes via `home_spread IS NOT NULL`."""
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    rows_added = 0
    unmatched = 0
    skipped_in_play = 0
    schools = load_school_names(conn)
    if not schools:
        print("WARNING: teams table is empty -- run data/backfill_historical_stats.py first, "
              "otherwise every team name below will fail to resolve to a CFBD game_id.")
    try:
        for game in games_list:
            if is_in_play(game.get("commence_time")):
                skipped_in_play += 1
                continue
            raw_home = game.get("home_team")
            raw_away = game.get("away_team")
            home = resolve_school_name(schools, raw_home)
            away = resolve_school_name(schools, raw_away)
            game_id = find_game_id(conn, week, year, home, away)
            if game_id is None:
                unmatched += 1

            spread_books = game.get("books", {})
            total_books = game.get("totals", {}) or {}
            for book in list(spread_books) + [b for b in total_books if b not in spread_books]:
                # sides is keyed by the odds API's own (mascot-suffixed) team names
                home_side = spread_books.get(book, {}).get(raw_home, {})
                conn.execute(
                    """
                    INSERT INTO betting_lines (
                        game_id, season, week, home_team, away_team, book,
                        home_spread, total, home_moneyline, line_type, source, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'the_odds_api', ?)
                    """,
                    (
                        game_id, year, week, home, away, book,
                        home_side.get("spread"), total_books.get(book, {}).get("total"),
                        home_side.get("juice"), line_type, now,
                    ),
                )
                rows_added += 1

            conn.execute(
                """
                INSERT INTO betting_lines (
                    game_id, season, week, home_team, away_team, book,
                    home_spread, total, line_type, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, 'consensus', ?, ?, ?, 'the_odds_api', ?)
                """,
                (game_id, year, week, home, away, game.get("consensus_home_spread"),
                 game.get("consensus_total"), line_type, now),
            )
            rows_added += 1

        conn.commit()
    finally:
        conn.close()

    if skipped_in_play:
        print(f"Skipped {skipped_in_play} game(s) already in play -- no pre-game line to store.")
    if unmatched:
        print(f"WARNING: {unmatched}/{len(games_list)} games had no matching CFBD game_id "
              f"(team-name join failed even after school-name resolution) — betting_lines "
              f"rows saved with game_id=NULL")
    return rows_added


def has_games_this_week(conn, season, week):
    """Whether the DB already has any game row for (season, week) --
    fetch_stats.py's run (Tuesday or otherwise) persists games as soon as
    it fetches them, so this is available without re-running it, as long
    as SOME earlier run this week already populated `games`."""
    return conn.execute(
        "SELECT 1 FROM games WHERE season = ? AND week = ? LIMIT 1", (season, week),
    ).fetchone() is not None


def main():
    with db.log_run("odds_api") as run:
        # Dedicated connection for raw-payload archival only (external
        # review, accepted 2026-08-04) -- separate from the several other
        # short-lived connections main() already opens for their own
        # narrow purposes below. Committed/closed once this run's calendar
        # + odds calls are done.
        archive_conn = db.get_connection()
        try:
            week, year = fetch_stats.get_current_week(conn=archive_conn)
        finally:
            archive_conn.commit()
            archive_conn.close()

        conn = db.get_connection()
        try:
            has_games = has_games_this_week(conn, year, week)
        finally:
            conn.close()

        if not has_games:
            print(f"No games in the DB for Week {week}, {year} — likely offseason "
                  f"(or fetch_stats.py hasn't run yet this week). Saving empty placeholders.")
            save_lines([], week, year, label="opening")
            save_lines([], week, year, label="current")
            return

        print(f"Fetching odds for Week {week}, {year}")
        archive_conn = db.get_connection()
        try:
            current_lines = fetch_current_lines(conn=archive_conn)

            # Drop games outside this week's actual date range (marquee
            # matchups months out that the Odds API already has lines for --
            # see filter_by_week()'s docstring). Hard-fail rather than
            # silently skip filtering if the range can't be determined:
            # writing everything unfiltered is exactly the bug this exists
            # to prevent.
            date_range = fetch_stats.get_week_date_range(year, week, conn=archive_conn)
        finally:
            archive_conn.commit()
            archive_conn.close()

        if date_range is None:
            raise RuntimeError(
                f"No calendar date range found for Week {week}, {year} -- "
                f"refusing to write odds without a week boundary to filter by."
            )
        first_game_start, last_game_start = date_range
        before = len(current_lines)
        current_lines = filter_by_week(current_lines, first_game_start, last_game_start)
        dropped = before - len(current_lines)
        if dropped:
            print(f"Filtered out {dropped} game(s) outside Week {week}'s date range "
                  f"({first_game_start} to {last_game_start}) -- other weeks' games "
                  f"already listed by the Odds API.")

        # Save as opening lines if this is the first pull of the week --
        # checked against the DB, not a local file (see opening_line_recorded).
        conn = db.get_connection()
        try:
            is_opening_pull = not opening_line_recorded(conn, week, year)
        finally:
            conn.close()
        if is_opening_pull:
            save_lines(current_lines, week, year, label="opening")

        opening_lines = load_opening_lines(week, year)
        current_with_movement = calculate_line_movement(current_lines, opening_lines)

        save_lines(current_with_movement, week, year, label="current")
        print(f"Processed {len(current_lines)} games with line movement data.")

        rows_added = persist_lines_to_db(current_with_movement, week, year, "current")
        if is_opening_pull:
            rows_added += persist_lines_to_db(current_lines, week, year, "opening")
        run["rows_added"] = rows_added
        print(f"Persisted {rows_added} rows to {db.DB_PATH}")


if __name__ == "__main__":
    main()
