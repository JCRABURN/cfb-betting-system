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

# Bumped whenever the parsing logic below (the camelCase/ppa/classification
# field extraction, not the archival mechanism itself) changes in a way
# that would matter for replaying an archived raw_payloads row through a
# fixed parser (external review, accepted 2026-08-04).
PARSER_VERSION = "fetch_stats.v1"


def get_current_week(conn=None):
    """Return current CFB week. `conn`, if given, archives the /calendar
    response it fetches (external review, accepted 2026-08-04) -- omit it
    (the default) for the many callers that just want week/year and don't
    need to duplicate archival work already done elsewhere in the same run.

    Deliberately does NOT catch calendar API failures and fall back to week 1.
    Week 1 always has real, completed games once the season has started, so a
    mid-season /calendar failure (network blip, rate limit, schema change)
    would otherwise cause main() to silently fetch and persist stale week-1
    data as if it were the current week -- the `if not games` guard in
    main() doesn't catch this, since week 1's games list is never empty.
    Letting this raise means db.log_run() logs it to ingestion_runs as a
    failed run and the GitHub Actions step fails visibly instead.

    "No active week found" (every week's window has passed `now`, or the
    calendar is genuinely pre-season) is a different, legitimate case and
    still defaults to week 1 softly -- main()'s `if not games` guard does
    correctly no-op that case into an offseason placeholder.

    Found live while wiring build_dashboard.py (2026-08-01): /calendar
    requires a `year` query param -- omitting it isn't "give me every
    year," it's an outright 400 ("Validation Failed", {"year": {"message":
    "year"}}). This function had never actually been called without one
    (every prior manual check in this project explicitly passed
    year=<some year> when testing /calendar directly), so it had silently
    never worked at all until this was caught. Confirmed live: with
    year=2026, returns 200 and 16 calendar entries.
    """
    weeks = get_calendar(datetime.utcnow().year, conn=conn)
    now = datetime.utcnow().isoformat()
    for week in weeks:
        if week.get("firstGameStart", "") <= now <= week.get("lastGameStart", "9999"):
            return week.get("week", 1), datetime.utcnow().year
    print("No active week found in calendar, defaulting to Week 1 (offseason)")
    return 1, datetime.utcnow().year


def get_calendar(year, conn=None):
    """Raw /calendar response for a season -- one entry per week, each with
    firstGameStart/lastGameStart (ISO-8601 UTC). Shared by get_current_week()
    (find which week `now` falls in) and get_week_date_range() (find a
    SPECIFIC week's boundaries, e.g. for fetch_odds.py to filter a live
    odds pull down to just that week's games -- see fetch_odds.py's
    filter_by_week(), added 2026-08-04 after mislabeled future-game rows
    were found polluting week 1's betting_lines).

    Archives the raw response (status + body) when `conn` is given, BEFORE
    raise_for_status() -- so even an error response gets captured for
    post-mortem, not just successful ones (external review, accepted
    2026-08-04)."""
    resp = requests.get(f"{CFBD_BASE}/calendar", headers=HEADERS, params={"year": year})
    payload_id = None
    if conn is not None:
        payload_id = db.archive_raw_payload(conn, "cfbd", "/calendar", {"year": year},
                                             resp.text, resp.status_code, PARSER_VERSION)
    resp.raise_for_status()
    weeks = resp.json()
    if payload_id is not None:
        db.update_raw_payload_counts(conn, payload_id, rows_accepted=len(weeks))
    return weeks


def get_week_date_range(year, week, conn=None):
    """(firstGameStart, lastGameStart) for one specific week, or None if
    that week isn't in the calendar (bad week number, or the year has no
    calendar data at all)."""
    for w in get_calendar(year, conn=conn):
        if w.get("week") == week:
            return w.get("firstGameStart"), w.get("lastGameStart")
    return None


def fetch_games(year, week, conn=None):
    """Get all FBS games for the given week.

    "division" is silently ignored by CFBD's /games endpoint (verified live
    2026-07-29: it returned 304 games -- FBS, FCS, D-II, D-III all mixed
    together -- for a week that has 52 actual FBS games). "classification" is
    the correct param name; confirmed it returns exactly the 52 FBS-vs-FBS games.
    """
    try:
        url = f"{CFBD_BASE}/games"
        params = {"year": year, "week": week, "classification": "fbs"}
        resp = requests.get(url, headers=HEADERS, params=params)
        payload_id = None
        if conn is not None:
            payload_id = db.archive_raw_payload(conn, "cfbd", "/games", params,
                                                 resp.text, resp.status_code, PARSER_VERSION)
        resp.raise_for_status()
        games = resp.json()
        if payload_id is not None:
            db.update_raw_payload_counts(conn, payload_id, rows_accepted=len(games))
        return games
    except Exception as e:
        print(f"Could not fetch games: {e}")
        return []


def fetch_sp_ratings(year, conn=None):
    """Fetch SP+ ratings for all teams."""
    try:
        url = f"{CFBD_BASE}/ratings/sp"
        params = {"year": year}
        resp = requests.get(url, headers=HEADERS, params=params)
        payload_id = None
        if conn is not None:
            payload_id = db.archive_raw_payload(conn, "cfbd", "/ratings/sp", params,
                                                 resp.text, resp.status_code, PARSER_VERSION)
        resp.raise_for_status()
        ratings = resp.json()
        if payload_id is not None:
            db.update_raw_payload_counts(conn, payload_id, rows_accepted=len(ratings))
        return {team["team"]: team for team in ratings}
    except Exception as e:
        print(f"Could not fetch SP+ ratings: {e}")
        return {}


def fetch_epa_stats(year, week, conn=None):
    """Fetch EPA stats per team."""
    try:
        url = f"{CFBD_BASE}/stats/season/advanced"
        params = {"year": year, "excludeGarbageTime": True}
        resp = requests.get(url, headers=HEADERS, params=params)
        payload_id = None
        if conn is not None:
            payload_id = db.archive_raw_payload(conn, "cfbd", "/stats/season/advanced", params,
                                                 resp.text, resp.status_code, PARSER_VERSION)
        resp.raise_for_status()
        stats = resp.json()
        if payload_id is not None:
            db.update_raw_payload_counts(conn, payload_id, rows_accepted=len(stats))
        return {item["team"]: item for item in stats}
    except Exception as e:
        print(f"Could not fetch EPA stats: {e}")
        return {}


def fetch_team_records(year, conn=None):
    """Fetch win/loss records."""
    try:
        url = f"{CFBD_BASE}/records"
        params = {"year": year}
        resp = requests.get(url, headers=HEADERS, params=params)
        payload_id = None
        if conn is not None:
            payload_id = db.archive_raw_payload(conn, "cfbd", "/records", params,
                                                 resp.text, resp.status_code, PARSER_VERSION)
        resp.raise_for_status()
        records = resp.json()
        if payload_id is not None:
            db.update_raw_payload_counts(conn, payload_id, rows_accepted=len(records))
        return {item["team"]: item for item in records}
    except Exception as e:
        print(f"Could not fetch records: {e}")
        return {}


def fetch_weather(game):
    """Fetch weather forecast using Open-Meteo (free, no key required).

    CFBD's /games response has no venue_latitude/venue_longitude (verified
    live 2026-07-29) -- only a `venue` name and `venueId`. Getting coordinates
    requires a separate /venues lookup joined by venueId, which doesn't exist
    yet (tracked as Phase 4 work: "build a stadium lat/long reference table").
    Until then this always returns {} for real games, same as it always has.
    """
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
    response shape without an extra reshaping step.
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
                    # CFBD's /games response is camelCase (verified live 2026-07-29);
                    # venue_latitude/venue_longitude don't exist there at all -- see
                    # fetch_weather()'s docstring.
                    game.get("id"), game.get("season"), game.get("week"),
                    game.get("seasonType"), game.get("startDate"),
                    game.get("homeTeam"), game.get("awayTeam"), game.get("venue"),
                    game.get("venue_latitude"), game.get("venue_longitude"),
                    int(bool(game.get("neutralSite"))), int(bool(game.get("conferenceGame"))),
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
        # Dedicated connection for raw-payload archival only (external
        # review, accepted 2026-08-04) -- separate from persist_to_db()'s
        # own self-managed connection below, committed/closed once all of
        # this run's CFBD calls are done, on every exit path.
        archive_conn = db.get_connection()
        try:
            week, year = get_current_week(conn=archive_conn)
            is_offseason = week == 1 and datetime.utcnow().month < 8
            print(f"Running in {'OFFSEASON' if is_offseason else 'SEASON'} mode")
            print(f"Fetching data for Week {week}, {year}")

            games = fetch_games(year, week, conn=archive_conn)

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

            sp_ratings = fetch_sp_ratings(year, conn=archive_conn)
            epa_stats = fetch_epa_stats(year, week, conn=archive_conn)
            records = fetch_team_records(year, conn=archive_conn)
        finally:
            archive_conn.commit()
            archive_conn.close()

        enriched_games = []
        for game in games:
            # CFBD's /games response is camelCase (verified live 2026-07-29):
            # homeTeam/awayTeam, not home_team/away_team.
            home = game.get("homeTeam", "")
            away = game.get("awayTeam", "")
            enriched = {
                "game_id": game.get("id"),
                "week": week,
                "year": year,
                "home_team": home,
                "away_team": away,
                "start_time": game.get("startDate"),
                "venue": game.get("venue"),
                "venue_latitude": game.get("venue_latitude"),
                "venue_longitude": game.get("venue_longitude"),
                "neutral_site": game.get("neutralSite", False),
                "conference_game": game.get("conferenceGame", False),
                "home_sp": sp_ratings.get(home, {}).get("rating", None),
                "away_sp": sp_ratings.get(away, {}).get("rating", None),
                # CFBD calls this "ppa" (predicted points added per play) -- verified
                # live that ppa == totalPPA / plays, i.e. it's already the per-play
                # average, commonly known elsewhere as EPA/play.
                "home_offense_epa": epa_stats.get(home, {}).get("offense", {}).get("ppa", None),
                "away_offense_epa": epa_stats.get(away, {}).get("offense", {}).get("ppa", None),
                "home_defense_epa": epa_stats.get(home, {}).get("defense", {}).get("ppa", None),
                "away_defense_epa": epa_stats.get(away, {}).get("defense", {}).get("ppa", None),
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