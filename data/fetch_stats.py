"""
fetch_stats.py
Pulls team stats, SP+ ratings, EPA, schedules from the CFBD API.
Run via GitHub Actions every Tuesday morning.
"""

import os
import sys
import json
import requests
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db

CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "")
CFBD_BASE = "https://api.collegefootballdata.com"

HEADERS = {
    "Authorization": f"Bearer {CFBD_API_KEY}",
    "Content-Type": "application/json"
}


def get_current_week():
    """Return current CFB week, defaulting gracefully during offseason."""
    try:
        url = f"{CFBD_BASE}/calendar"
        resp = requests.get(url, headers=HEADERS)
        resp.raise_for_status()
        weeks = resp.json()
        now = datetime.utcnow().isoformat()
        for week in weeks:
            if week.get("firstGameStart", "") <= now <= week.get("lastGameStart", "9999"):
                return week.get("week", 1), datetime.utcnow().year
        print("No active week found in calendar, defaulting to Week 1")
        return 1, datetime.utcnow().year
    except Exception as e:
        print(f"Calendar API unavailable ({e}), defaulting to Week 1 offseason mode")
        return 1, datetime.utcnow().year


def fetch_games(year, week):
    """Get all D-I games for the given week."""
    try:
        url = f"{CFBD_BASE}/games"
        params = {"year": year, "week": week, "division": "fbs"}
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Could not fetch games: {e}")
        return []


def fetch_sp_ratings(year):
    """Fetch SP+ ratings for all teams."""
    try:
        url = f"{CFBD_BASE}/ratings/sp"
        params = {"year": year}
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        return {team["team"]: team for team in resp.json()}
    except Exception as e:
        print(f"Could not fetch SP+ ratings: {e}")
        return {}


def fetch_epa_stats(year, week):
    """Fetch EPA stats per team."""
    try:
        url = f"{CFBD_BASE}/stats/season/advanced"
        params = {"year": year, "excludeGarbageTime": True}
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        return {item["team"]: item for item in resp.json()}
    except Exception as e:
        print(f"Could not fetch EPA stats: {e}")
        return {}


def fetch_team_records(year):
    """Fetch win/loss records."""
    try:
        url = f"{CFBD_BASE}/records"
        params = {"year": year}
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        return {item["team"]: item for item in resp.json()}
    except Exception as e:
        print(f"Could not fetch records: {e}")
        return {}


def fetch_weather(game):
    """Fetch weather forecast using Open-Meteo (free, no key required)."""
    venue_lat = game.get("venue_latitude")
    venue_lon = game.get("venue_longitude")
    if not venue_lat or not venue_lon:
        return {}
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": venue_lat,
            "longitude": venue_lon,
            "hourly": "temperature_2m,precipitation_probability,windspeed_10m",
            "forecast_days": 7,
            "timezone": "auto"
        }
        resp = requests.get(url, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        return {
            "temp_f": round(data["hourly"]["temperature_2m"][0] * 9/5 + 32, 1),
            "wind_mph": data["hourly"]["windspeed_10m"][0],
            "precip_pct": data["hourly"]["precipitation_probability"][0]
        }
    except Exception:
        return {}


def persist_to_db(games, enriched_games, epa_stats=None):
    """Write raw games/stats/weather into data/cfb.db so they survive past this CI run.

    epa_stats is the raw per-team dict from fetch_epa_stats() (not the flattened
    enriched dict), so success_rate/havoc_rate can be pulled straight from the CFBD
    response shape without changing what generate_report.py/spread_model.py consume.
    """
    epa_stats = epa_stats or {}
    now = datetime.utcnow().isoformat()
    conn = db.get_connection()
    rows_added = 0
    try:
        for game in games:
            conn.execute(
                """
                INSERT INTO games (
                    game_id, season, week, season_type, start_date,
                    home_team, away_team, venue, venue_latitude, venue_longitude,
                    neutral_site, conference_game
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(game_id) DO UPDATE SET
                    start_date=excluded.start_date,
                    venue=excluded.venue,
                    venue_latitude=excluded.venue_latitude,
                    venue_longitude=excluded.venue_longitude,
                    neutral_site=excluded.neutral_site,
                    conference_game=excluded.conference_game
                """,
                (
                    game.get("id"), game.get("season"), game.get("week"),
                    game.get("season_type"), game.get("start_date"),
                    game.get("home_team"), game.get("away_team"), game.get("venue"),
                    game.get("venue_latitude"), game.get("venue_longitude"),
                    int(bool(game.get("neutral_site"))), int(bool(game.get("conference_game"))),
                ),
            )
            rows_added += 1

        for enriched in enriched_games:
            game_id = enriched["game_id"]
            week = enriched["week"]
            year = enriched["year"]
            weather = enriched.get("weather") or {}

            for side, team, sp, off_epa, def_epa, record in (
                ("home", enriched["home_team"], enriched["home_sp"],
                 enriched["home_offense_epa"], enriched["home_defense_epa"], enriched["home_record"]),
                ("away", enriched["away_team"], enriched["away_sp"],
                 enriched["away_offense_epa"], enriched["away_defense_epa"], enriched["away_record"]),
            ):
                team_epa = epa_stats.get(team, {})
                off_success = team_epa.get("offense", {}).get("successRate")
                def_success = team_epa.get("defense", {}).get("successRate")
                havoc = team_epa.get("defense", {}).get("havoc", {}).get("total")
                conn.execute(
                    """
                    INSERT INTO team_game_stats (
                        game_id, season, week, team, sp_rating,
                        offense_epa_play, defense_epa_play,
                        offense_success_rate, defense_success_rate, havoc_rate,
                        wins, losses, source, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cfbd', ?)
                    """,
                    (
                        game_id, year, week, team, sp, off_epa, def_epa,
                        off_success, def_success, havoc,
                        (record or {}).get("wins"), (record or {}).get("losses"), now,
                    ),
                )
                rows_added += 1

            if weather:
                conn.execute(
                    """
                    INSERT INTO weather (
                        game_id, captured_at, temp_f, wind_mph, precip_pct,
                        is_forecast, source
                    ) VALUES (?, ?, ?, ?, ?, 1, 'open-meteo')
                    """,
                    (game_id, now, weather.get("temp_f"), weather.get("wind_mph"), weather.get("precip_pct")),
                )
                rows_added += 1

        conn.commit()
    finally:
        conn.close()
    return rows_added


def main():
    with db.log_run("cfbd_stats") as run:
        week, year = get_current_week()
        is_offseason = week == 1 and datetime.utcnow().month < 8
        print(f"Running in {'OFFSEASON' if is_offseason else 'SEASON'} mode")
        print(f"Fetching data for Week {week}, {year}")

        games = fetch_games(year, week)

        if not games:
            print("No games found — likely offseason. Saving empty placeholder.")
            os.makedirs("data/stats", exist_ok=True)
            out_path = f"data/stats/week_{week}_{year}.json"
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({
                    "week": week,
                    "year": year,
                    "offseason": True,
                    "games": []
                }, f, indent=2)
            print(f"Saved placeholder to {out_path}")
            return

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
                "venue_latitude": game.get("venue_latitude"),
                "venue_longitude": game.get("venue_longitude"),
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

        os.makedirs("data/stats", exist_ok=True)
        out_path = f"data/stats/week_{week}_{year}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"week": week, "year": year, "games": enriched_games}, f, indent=2)
        print(f"Saved {len(enriched_games)} games to {out_path}")

        run["rows_added"] = persist_to_db(games, enriched_games, epa_stats)
        print(f"Persisted {run['rows_added']} rows to {db.DB_PATH}")


if __name__ == "__main__":
    main()