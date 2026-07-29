"""
backfill_historical_stats.py
One-time (rerunnable) historical ingestion: SP+, EPA/play, success rate, and
havoc rate for all FBS teams, season by season back to 2019. Writes directly
into data/cfb.db (team_game_stats, with game_id/week left NULL since these are
season-level snapshots, not tied to a single game) — no scratch JSON.

Idempotent: a season already present with source='cfbd_historical_backfill'
is skipped entirely (no API calls made) unless --force is passed.
Incremental: run it again next year with a wider --end-year and only the new
season gets fetched.

Usage:
    python data/backfill_historical_stats.py
    python data/backfill_historical_stats.py --start-year 2019 --end-year 2023
    python data/backfill_historical_stats.py --end-year 2023 --force
"""

import os
import sys
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import fetch_stats  # reuses fetch_sp_ratings / fetch_epa_stats / fetch_team_records

CFBD_BASE = fetch_stats.CFBD_BASE
HEADERS = fetch_stats.HEADERS

SOURCE = "cfbd_historical_backfill"


def fetch_teams(year):
    """FBS team list for a given year (conference membership changes year to year)."""
    try:
        resp = fetch_stats.requests.get(
            f"{CFBD_BASE}/teams", headers=HEADERS, params={"year": year}
        )
        resp.raise_for_status()
        return [t for t in resp.json() if t.get("classification") == "fbs"]
    except Exception as e:
        print(f"Could not fetch teams for {year}: {e}")
        return []


def upsert_teams(conn, teams):
    for team in teams:
        team_id = team.get("id")
        school = team.get("school")
        if team_id is None or not school:
            continue
        conn.execute(
            """
            INSERT INTO teams (team_id, school, conference, division)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                school=excluded.school,
                conference=excluded.conference,
                division=excluded.division
            """,
            (team_id, school, team.get("conference"), team.get("division")),
        )
    conn.commit()


def season_already_ingested(conn, year):
    count = conn.execute(
        "SELECT COUNT(*) FROM team_game_stats WHERE season = ? AND source = ?",
        (year, SOURCE),
    ).fetchone()[0]
    return count > 0


def team_row_exists(conn, year, team):
    return conn.execute(
        "SELECT 1 FROM team_game_stats WHERE season = ? AND team = ? AND source = ?",
        (year, team, SOURCE),
    ).fetchone() is not None


def backfill_season(conn, year, force=False):
    if not force and season_already_ingested(conn, year):
        print(f"{year}: already ingested, skipping (use --force to re-fetch)")
        return 0

    print(f"{year}: fetching SP+/EPA/records...")
    sp_ratings = fetch_stats.fetch_sp_ratings(year)
    epa_stats = fetch_stats.fetch_epa_stats(year, None)
    records = fetch_stats.fetch_team_records(year)

    teams = sorted(set(sp_ratings) | set(epa_stats) | set(records))
    if not teams:
        print(f"{year}: no data returned from CFBD, skipping")
        return 0

    now = datetime.utcnow().isoformat()
    rows_added = 0
    for team in teams:
        if not force and team_row_exists(conn, year, team):
            continue

        sp = sp_ratings.get(team, {}).get("rating")
        team_epa = epa_stats.get(team, {})
        # CFBD calls this "ppa" (predicted points added per play) -- verified live
        # 2026-07-29 that ppa == totalPPA / plays, i.e. it's already the per-play
        # average, commonly known elsewhere as EPA/play. Kept consistent with the
        # same field used in fetch_stats.py's weekly write path.
        off_epa = team_epa.get("offense", {}).get("ppa")
        def_epa = team_epa.get("defense", {}).get("ppa")
        off_success = team_epa.get("offense", {}).get("successRate")
        def_success = team_epa.get("defense", {}).get("successRate")
        havoc = team_epa.get("defense", {}).get("havoc", {}).get("total")
        record = records.get(team, {}).get("total", {})

        conn.execute(
            """
            INSERT INTO team_game_stats (
                game_id, season, week, team, sp_rating,
                offense_epa_play, defense_epa_play,
                offense_success_rate, defense_success_rate, havoc_rate,
                wins, losses, source, fetched_at
            ) VALUES (NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                year, team, sp, off_epa, def_epa,
                off_success, def_success, havoc,
                record.get("wins"), record.get("losses"), SOURCE, now,
            ),
        )
        rows_added += 1

    conn.commit()
    print(f"{year}: {rows_added} team rows added")
    return rows_added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument(
        "--end-year", type=int, default=datetime.utcnow().year - 1,
        help="Last completed season to backfill (default: last calendar year, "
             "since the current season is handled incrementally by fetch_stats.py)",
    )
    parser.add_argument("--force", action="store_true", help="Re-fetch seasons already ingested")
    args = parser.parse_args()

    with db.log_run(SOURCE) as run:
        conn = db.get_connection()
        try:
            teams = fetch_teams(args.end_year)
            if teams:
                upsert_teams(conn, teams)
                print(f"Upserted {len(teams)} FBS teams (as of {args.end_year})")

            total = 0
            for year in range(args.start_year, args.end_year + 1):
                total += backfill_season(conn, year, force=args.force)
                time.sleep(1)  # polite pacing between seasons (3 CFBD calls each)
            run["rows_added"] = total
        finally:
            conn.close()

    print(f"Done. {run['rows_added']} rows added to {db.DB_PATH}")


if __name__ == "__main__":
    main()
