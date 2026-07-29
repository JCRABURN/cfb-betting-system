"""
fetch_odds.py
Pulls current spreads, line movement, and juice from The Odds API.
Stores opening and current lines for CLV tracking.
"""

import os
import sys
import json
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"

SPORT = "americanfootball_ncaaf"
REGIONS = "us"
MARKETS = "spreads"
BOOKMAKERS = "draftkings,fanduel,betmgm,caesars"


def fetch_current_lines():
    """Fetch current spreads for all upcoming CFB games."""
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
        resp.raise_for_status()
    except Exception as e:
        print(f"Odds API unavailable ({e}) — returning empty lines.")
        return []

    remaining = resp.headers.get("x-requests-remaining", "?")
    print(f"Odds API requests remaining: {remaining}")

    games = resp.json()
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

        processed.append({
            "game_id": game.get("id"),
            "home_team": home,
            "away_team": away,
            "commence_time": commence,
            "consensus_home_spread": consensus_spread,
            "books": lines,
            "fetched_at": datetime.utcnow().isoformat()
        })

    return processed


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


def find_game_id(conn, week, year, home, away):
    """Best-effort join to the CFBD games row via team name (Odds API has its own id space)."""
    row = conn.execute(
        "SELECT game_id FROM games WHERE season = ? AND week = ? "
        "AND home_team = ? AND away_team = ?",
        (year, week, home, away),
    ).fetchone()
    return row[0] if row else None


def persist_lines_to_db(games_list, week, year, line_type):
    """Write one betting_lines row per book (plus a synthetic 'consensus' row) per game."""
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    rows_added = 0
    unmatched = 0
    try:
        for game in games_list:
            home = game.get("home_team")
            away = game.get("away_team")
            game_id = find_game_id(conn, week, year, home, away)
            if game_id is None:
                unmatched += 1

            for book, sides in game.get("books", {}).items():
                home_side = sides.get(home, {})
                conn.execute(
                    """
                    INSERT INTO betting_lines (
                        game_id, season, week, home_team, away_team, book,
                        home_spread, home_moneyline, line_type, source, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'the_odds_api', ?)
                    """,
                    (
                        game_id, year, week, home, away, book,
                        home_side.get("spread"), home_side.get("juice"),
                        line_type, now,
                    ),
                )
                rows_added += 1

            conn.execute(
                """
                INSERT INTO betting_lines (
                    game_id, season, week, home_team, away_team, book,
                    home_spread, line_type, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, 'consensus', ?, ?, 'the_odds_api', ?)
                """,
                (game_id, year, week, home, away, game.get("consensus_home_spread"), line_type, now),
            )
            rows_added += 1

        conn.commit()
    finally:
        conn.close()

    if unmatched:
        print(f"WARNING: {unmatched}/{len(games_list)} games had no matching CFBD game_id "
              f"(team-name join failed) — betting_lines rows saved with game_id=NULL")
    return rows_added


def main():
    with db.log_run("odds_api") as run:
        # Get week from stats file (must run after fetch_stats.py)
        import glob
        stats_files = glob.glob("data/stats/week_*.json")
        if not stats_files:
            print("No stats files found. Run fetch_stats.py first.")
            return

        latest = sorted(stats_files)[-1]
        with open(latest, encoding="utf-8") as f:
            meta = json.load(f)
        week = meta["week"]
        year = meta["year"]

        if meta.get("offseason"):
            print("Offseason mode — no odds to fetch. Saving empty placeholders.")
            save_lines([], week, year, label="opening")
            save_lines([], week, year, label="current")
            return

        print(f"Fetching odds for Week {week}, {year}")
        current_lines = fetch_current_lines()

        # Save as opening lines if this is the first pull of the week
        opening_lines_exist = os.path.exists(
            f"data/spreads/opening_week_{week}_{year}.json"
        )
        is_opening_pull = not opening_lines_exist
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
