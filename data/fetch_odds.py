"""
fetch_odds.py
Pulls current spreads, line movement, and juice from The Odds API.
Stores opening and current lines for CLV tracking.
"""

import os
import json
import requests
from datetime import datetime

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
        with open(path) as f:
            return {g["game_id"]: g for g in json.load(f)}
    return {}


def save_lines(data, week, year, label="current"):
    """Save line data to disk."""
    os.makedirs("data/spreads", exist_ok=True)
    path = f"data/spreads/{label}_week_{week}_{year}.json"
    with open(path, "w") as f:
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


def main():
    # Get week from stats file (must run after fetch_stats.py)
    import glob
    stats_files = glob.glob("data/stats/week_*.json")
    if not stats_files:
        print("No stats files found. Run fetch_stats.py first.")
        return

    latest = sorted(stats_files)[-1]
    with open(latest) as f:
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
    if not opening_lines_exist:
        save_lines(current_lines, week, year, label="opening")

    opening_lines = load_opening_lines(week, year)
    current_with_movement = calculate_line_movement(current_lines, opening_lines)

    save_lines(current_with_movement, week, year, label="current")
    print(f"Processed {len(current_lines)} games with line movement data.")


if __name__ == "__main__":
    main()
