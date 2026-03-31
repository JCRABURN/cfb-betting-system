"""
fetch_stats.py
Pulls team stats, SP+ ratings, EPA, schedules from the CFBD API.
Run via GitHub Actions every Tuesday morning.
"""

import os
import json
import requests
from datetime import datetime

CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "")
CFBD_BASE = "https://api.collegefootballdata.com"

HEADERS = {
    "Authorization": f"Bearer {CFBD_API_KEY}",
    "Content-Type": "application/json"
}

def get_current_week():
    """Determine the current CFB week from the schedule."""
    url = f"{CFBD_BASE}/calendar"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    weeks = resp.json()
    now = datetime.utcnow().isoformat()
    for week in weeks:
        if week.get("firstGameStart", "") <= now <= week.get("lastGameStart", "9999"):
            return week.get("week", 1), datetime.utcnow().year
    return 1, datetime.utcnow().year


def fetch_games(year, week):
    """Get all D-I games for the given week."""
    url = f"{CFBD_BASE}/games"
    params = {"year": year, "week": week, "division": "fbs"}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_sp_ratings(year):
    """Fetch SP+ ratings for all teams."""
    url = f"{CFBD_BASE}/ratings/sp"
    params = {"year": year}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return {team["team"]: team for team in resp.json()}


def fetch_epa_stats(year, week):
    """Fetch EPA (expected points added) stats per team."""
    url = f"{CFBD_BASE}/stats/season/advanced"
    params = {"year": year, "excludeGarbageTime": True}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return {item["team"]: item for item in resp.json()}


def fetch_team_records(year):
    """Fetch win/loss records."""
    url = f"{CFBD_BASE}/records"
    params = {"year": year}
    resp = requests.get(url, headers=HEADERS, params=params)
    resp.raise_for_status()
    return {item["team"]: item for item in resp.json()}


def fetch_weather(game):
    """Fetch weather forecast for a game's venue using Open-Meteo (free, no key)."""
    venue_lat = game.get("venue_latitude")
    venue_lon = game.get("venue_longitude")
    if not venue_lat or not venue_lon:
        return {}
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": venue_lat,
        "longitude": venue_lon,
        "hourly": "temperature_2m,precipitation_probability,windspeed_10m",
        "forecast_days": 7,
        "timezone": "auto"
    }
    try:
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        # Return simplified weather summary
        return {
            "temp_f": round(data["hourly"]["temperature_2m"][0] * 9/5 + 32, 1),
            "wind_mph": data["hourly"]["windspeed_10m"][0],
            "precip_pct": data["hourly"]["precipitation_probability"][0]
        }
    except Exception:
        return {}


def main():
    week, year = get_current_week()
    print(f"Fetching data for Week {week}, {year}")

    games = fetch_games(year, week)
    sp_ratings = fetch_sp_ratings(year)
    epa_stats = fetch_epa_stats(year, week)
    records = fetch_team_records(year)

    enriched_games = []
    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        enriched = {
            "game_id": game.get("id"),
            "week": week,
            "year": year,
            "home_team": home,
            "away_team": away,
            "start_time": game.get("start_date"),
            "venue": game.get("venue"),
            "neutral_site": game.get("neutral_site", False),
            "conference_game": game.get("conference_game", False),
            "home_sp": sp_ratings.get(home, {}).get("rating", None),
            "away_sp": sp_ratings.get(away, {}).get("rating", None),
            "home_offense_epa": epa_stats.get(home, {}).get("offense", {}).get("epa_per_play", None),
            "away_offense_epa": epa_stats.get(away, {}).get("offense", {}).get("epa_per_play", None),
            "home_defense_epa": epa_stats.get(home, {}).get("defense", {}).get("epa_per_play", None),
            "away_defense_epa": epa_stats.get(away, {}).get("defense", {}).get("epa_per_play", None),
            "home_record": records.get(home, {}).get("total", {}),
            "away_record": records.get(away, {}).get("total", {}),
            "weather": fetch_weather(game),
        }
        enriched_games.append(enriched)

    out_path = f"data/stats/week_{week}_{year}.json"
    os.makedirs("data/stats", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"week": week, "year": year, "games": enriched_games}, f, indent=2)
    print(f"Saved {len(enriched_games)} games to {out_path}")


if __name__ == "__main__":
    main()
