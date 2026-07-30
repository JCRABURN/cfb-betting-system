"""
backfill_point_in_time_stats.py
One-time (rerunnable) point-in-time historical ingestion: EPA/play, success
rate, and havoc rate, AS OF EACH WEEK, all 7 seasons back to 2019. Writes
directly into data/cfb.db's team_game_stats table -- no scratch JSON.

Why this script exists (see MODEL_DESIGN.md §1/§3): backfill_historical_stats.py
stores SEASON-FINAL stats (one row per team per season) -- using Georgia's
final 2023 SP+ to predict their week 3 2023 game means the rating already
"knows" how week 3, and every later week, turned out. That's fatal for an
honest backtest. This script instead stores one row per team per WEEK, each
holding only what was knowable as of that point in the season.

Live-verified 2026-07-30 against /stats/season/advanced with endWeek=N: the
values genuinely change as N increases (e.g. Georgia 2023 offense.ppa: 0.311
at endWeek=3, 0.377 at endWeek=8, 0.400 for the full season) -- confirmed
point-in-time capable. offense/defense.successRate and defense.havoc.total
come from the exact same call and move the same way -- confirmed too.

SP+ does NOT support this: /ratings/sp's "week" param is silently ignored --
verified live that week=3, week=8, week=13, and no week param at all all
return the IDENTICAL rating for the same team/season. CFBD only serves the
season-final SP+ number for a completed season; there is no way to backfill
point-in-time SP+ historically via this API. SP+ is deferred to live-forward
capture only (starting now, each week naturally gives a true point-in-time
value) -- NOT included in this backfill. sp_rating is left NULL on every row
this script writes.

IMPORTANT for consumers (feature engineering / the model): a row with
week=N holds stats CUMULATIVE THROUGH week N's games (i.e. "as of right
after week N finished"). To predict week N's games without lookahead, join
against week=N-1 (or the latest available week < N), never week=N itself --
week N's own row already includes week N's results.

Idempotent: a (season, week) already ingested (source=cfbd_point_in_time)
is skipped with zero API calls unless --force. Incremental: widen
--end-year later, only new seasons/weeks get fetched.

Usage:
    python data/backfill_point_in_time_stats.py
    python data/backfill_point_in_time_stats.py --start-year 2019 --end-year 2025
    python data/backfill_point_in_time_stats.py --start-year 2025 --end-year 2025 --start-week 5 --end-week 5 --force
"""

import os
import sys
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import fetch_stats

CFBD_BASE = fetch_stats.CFBD_BASE
HEADERS = fetch_stats.HEADERS
SOURCE = "cfbd_point_in_time"

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 5


def fetch_point_in_time_stats(year, end_week):
    """Cumulative EPA/success rate/havoc through end_week, all teams, one call.
    Returns the list on success (possibly empty), or None if every retry failed."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = fetch_stats.requests.get(
                f"{CFBD_BASE}/stats/season/advanced", headers=HEADERS,
                params={"year": year, "endWeek": end_week, "excludeGarbageTime": True},
                timeout=30,
            )
            if resp.status_code == 429:
                wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                print(f"{year} through week {end_week}: rate limited (429), waiting {wait}s "
                      f"(retry {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
            print(f"{year} through week {end_week}: request failed ({e}), waiting {wait}s "
                  f"(retry {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
    print(f"{year} through week {end_week}: giving up after {MAX_RETRIES} retries -- re-run "
          f"later (idempotency means only this week needs to be retried)")
    return None


def week_already_ingested(conn, year, week):
    count = conn.execute(
        "SELECT COUNT(*) FROM team_game_stats WHERE season = ? AND week = ? AND source = ?",
        (year, week, SOURCE),
    ).fetchone()[0]
    return count > 0


def backfill_week(conn, year, week, force=False):
    """Returns (rows_added, status) where status is one of:
    'skipped' (already ingested), 'ok', 'empty' (genuinely no data), 'failed' (gave up)."""
    already_ingested = week_already_ingested(conn, year, week)
    if not force and already_ingested:
        print(f"{year} week {week}: already ingested, skipping (use --force to re-fetch)")
        return 0, "skipped"

    teams = fetch_point_in_time_stats(year, week)
    if teams is None:
        return 0, "failed"
    if not teams:
        print(f"{year} week {week}: 0 teams returned")
        return 0, "empty"

    if force and already_ingested:
        # Unlike betting_lines (genuinely append-only, multiple snapshots over time
        # by design), consumers need exactly one canonical row per (team, season,
        # week) here -- --force means "replace this snapshot," not "accumulate
        # duplicates."
        conn.execute(
            "DELETE FROM team_game_stats WHERE season = ? AND week = ? AND source = ?",
            (year, week, SOURCE),
        )

    now = datetime.utcnow().isoformat()
    rows_added = 0
    for team_stats in teams:
        team = team_stats.get("team")
        if not team:
            continue
        offense = team_stats.get("offense", {})
        defense = team_stats.get("defense", {})

        conn.execute(
            """
            INSERT INTO team_game_stats (
                game_id, season, week, team, sp_rating,
                offense_epa_play, defense_epa_play,
                offense_success_rate, defense_success_rate, havoc_rate,
                wins, losses, source, fetched_at
            ) VALUES (NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                year, week, team,
                offense.get("ppa"), defense.get("ppa"),
                offense.get("successRate"), defense.get("successRate"),
                defense.get("havoc", {}).get("total"),
                SOURCE, now,
            ),
        )
        rows_added += 1

    conn.commit()
    print(f"{year} week {week}: {rows_added} rows added ({len(teams)} teams)")
    return rows_added, "ok"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--end-year", type=int, default=datetime.utcnow().year - 1)
    parser.add_argument("--start-week", type=int, default=1)
    parser.add_argument("--end-week", type=int, default=15)
    parser.add_argument("--force", action="store_true", help="Re-fetch weeks already ingested")
    args = parser.parse_args()

    empty_weeks = []
    failed_weeks = []

    with db.log_run(SOURCE) as run:
        conn = db.get_connection()
        try:
            total = 0
            for year in range(args.start_year, args.end_year + 1):
                for week in range(args.start_week, args.end_week + 1):
                    rows, status = backfill_week(conn, year, week, force=args.force)
                    total += rows
                    if status == "empty":
                        empty_weeks.append((year, week))
                    elif status == "failed":
                        failed_weeks.append((year, week))
                    time.sleep(0.5)  # polite pacing
            run["rows_added"] = total
        finally:
            conn.close()

    print(f"\nDone. {run['rows_added']} rows added to {db.DB_PATH}")

    if empty_weeks:
        print(f"\n{len(empty_weeks)} week(s) returned zero teams (eyeball for real gaps):")
        for y, w in empty_weeks:
            print(f"  {y} week {w}")

    if failed_weeks:
        print(f"\n{len(failed_weeks)} week(s) FAILED after {MAX_RETRIES} retries each -- re-run "
              f"with --force for just these to retry:")
        for y, w in failed_weeks:
            print(f"  {y} week {w}")


if __name__ == "__main__":
    main()
