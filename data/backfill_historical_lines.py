"""
backfill_historical_lines.py
One-time (rerunnable) historical ingestion: opening + closing spreads and
totals from CFBD's /lines endpoint, week by week, season by season back to
2019. Writes directly into data/cfb.db's betting_lines table -- no scratch
JSON.

CFBD's /lines uses its own team naming (matches games.home_team/away_team
exactly -- verified live 2026-07-29 against 2025 week 10, same numeric game
`id` too), unlike The Odds API's mascot-suffixed names. The team-name
resolver from fetch_odds.py is still applied defensively before the insert,
per instruction, in case a provider-level name ever disagrees with CFBD's
own -- it's a no-op (exact match) when names already agree, which is the
common case here.

CFBD's spread convention: `spread` is the home team's line (positive = home
is the underdog getting points, negative = home favored) -- confirmed live:
homeTeam=Florida, awayTeam=Georgia, spread=7, formattedSpread="Georgia -7"
(Georgia favored by 7, so Florida/home gets +7). This matches our existing
home_spread column convention with no sign flip needed.

Idempotent: a (season, week) already ingested is skipped with zero API
calls unless --force. Incremental: widen --end-year later, only new
seasons/weeks get fetched.

Usage:
    python data/backfill_historical_lines.py
    python data/backfill_historical_lines.py --start-year 2019 --end-year 2025
    python data/backfill_historical_lines.py --start-year 2025 --end-year 2025 --start-week 10 --end-week 10 --force
"""

import os
import sys
import time
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import fetch_stats
from fetch_odds import resolve_school_name, load_school_names

CFBD_BASE = fetch_stats.CFBD_BASE
HEADERS = fetch_stats.HEADERS
SOURCE = "cfbd_historical_lines"


MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 5


def fetch_lines(year, week):
    """Returns the games list on success (possibly empty -- a genuine bye/off week),
    or None if every retry was exhausted (a real failure, distinct from "no games")."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = fetch_stats.requests.get(
                f"{CFBD_BASE}/lines", headers=HEADERS,
                params={"year": year, "week": week, "seasonType": "regular"},
                timeout=30,
            )
            if resp.status_code == 429:
                wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
                print(f"{year} week {week}: rate limited (429), waiting {wait}s "
                      f"(retry {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            wait = BASE_BACKOFF_SECONDS * (2 ** attempt)
            print(f"{year} week {week}: request failed ({e}), waiting {wait}s "
                  f"(retry {attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
    print(f"{year} week {week}: giving up after {MAX_RETRIES} retries -- re-run later "
          f"(idempotency means only this week needs to be retried, not the whole backfill)")
    return None


def week_already_ingested(conn, year, week):
    count = conn.execute(
        "SELECT COUNT(*) FROM betting_lines WHERE season = ? AND week = ? AND source = ?",
        (year, week, SOURCE),
    ).fetchone()[0]
    return count > 0


def resolve_and_track(schools, raw_name, stats):
    """Wraps resolve_school_name() to tally exact / fallback-resolved / unresolved,
    so the run can report an overall team-name match rate at the end."""
    if raw_name in schools:
        stats["exact"] += 1
        return raw_name
    resolved = resolve_school_name(schools, raw_name)
    if resolved != raw_name:
        stats["resolved_via_fallback"] += 1
    else:
        stats["unresolved"] += 1
    return resolved


def backfill_week(conn, year, week, schools, resolution_stats, force=False):
    """Returns (rows_added, status) where status is one of:
    'skipped' (already ingested), 'ok', 'empty' (genuinely no games), 'failed' (gave up)."""
    if not force and week_already_ingested(conn, year, week):
        print(f"{year} week {week}: already ingested, skipping (use --force to re-fetch)")
        return 0, "skipped"

    games = fetch_lines(year, week)
    if games is None:
        return 0, "failed"
    if not games:
        print(f"{year} week {week}: 0 games returned (bye week / no schedule)")
        return 0, "empty"

    now = datetime.utcnow().isoformat()
    rows_added = 0
    skipped_out_of_scope = 0
    for game in games:
        game_id = game.get("id")
        # /lines' "classification" param is silently ignored (same bug as /games'
        # "division" -- verified live 2026-07-29), so it returns FCS-vs-FCS games
        # too; filter client-side instead of trusting the query param.
        if game.get("homeClassification") != "fbs" or game.get("awayClassification") != "fbs":
            skipped_out_of_scope += 1
            continue

        # `games` is NOT pre-populated for historical weeks -- fetch_stats.py only
        # ever writes the current week, and backfill_historical_stats.py is
        # season-level (no game_id at all). Discovered this live when a first pass
        # skipped every single game as "no matching games row." /lines' own
        # response has everything needed (id/season/week/seasonType/startDate/
        # homeTeam/awayTeam/homeScore/awayScore -- note "homeScore"/"awayScore"
        # here, NOT "homePoints"/"awayPoints" like /games uses for the same
        # concept), so upsert games here instead of assuming it already exists.
        # Only touches score/completed on conflict so a richer row already
        # written by fetch_stats.py (venue, lat/long, neutral_site) isn't clobbered.
        home_score = game.get("homeScore")
        conn.execute(
            """
            INSERT INTO games (
                game_id, season, week, season_type, start_date,
                home_team, away_team, home_points, away_points, completed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                home_points=COALESCE(games.home_points, excluded.home_points),
                away_points=COALESCE(games.away_points, excluded.away_points),
                completed=MAX(games.completed, excluded.completed)
            """,
            (
                game_id, year, week, game.get("seasonType"), game.get("startDate"),
                game.get("homeTeam"), game.get("awayTeam"),
                home_score, game.get("awayScore"),
                1 if home_score is not None else 0,
            ),
        )

        home = resolve_and_track(schools, game.get("homeTeam", ""), resolution_stats)
        away = resolve_and_track(schools, game.get("awayTeam", ""), resolution_stats)

        for line in game.get("lines", []):
            provider = line.get("provider")
            if not provider:
                continue
            spread_open = line.get("spreadOpen")
            spread_close = line.get("spread")
            total_open = line.get("overUnderOpen")
            total_close = line.get("overUnder")

            if spread_open is not None or total_open is not None:
                conn.execute(
                    """
                    INSERT INTO betting_lines (
                        game_id, season, week, home_team, away_team, book,
                        home_spread, total, line_type, source, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'opening', ?, ?)
                    """,
                    (game_id, year, week, home, away, provider, spread_open, total_open, SOURCE, now),
                )
                rows_added += 1

            if spread_close is not None or total_close is not None:
                conn.execute(
                    """
                    INSERT INTO betting_lines (
                        game_id, season, week, home_team, away_team, book,
                        home_spread, total, line_type, source, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'closing', ?, ?)
                    """,
                    (game_id, year, week, home, away, provider, spread_close, total_close, SOURCE, now),
                )
                rows_added += 1

    conn.commit()
    print(f"{year} week {week}: {rows_added} rows added ({len(games)} games from CFBD, "
          f"{skipped_out_of_scope} skipped as out-of-scope/non-FBS)")
    return rows_added, "ok"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--end-year", type=int, default=datetime.utcnow().year - 1)
    parser.add_argument("--start-week", type=int, default=1)
    parser.add_argument("--end-week", type=int, default=15)
    parser.add_argument("--force", action="store_true", help="Re-fetch weeks already ingested")
    args = parser.parse_args()

    resolution_stats = {"exact": 0, "resolved_via_fallback": 0, "unresolved": 0}
    empty_weeks = []
    failed_weeks = []

    with db.log_run(SOURCE) as run:
        conn = db.get_connection()
        try:
            schools = load_school_names(conn)
            if not schools:
                print("WARNING: teams table is empty -- run backfill_historical_stats.py first "
                      "for accurate team-name resolution.")
            total = 0
            for year in range(args.start_year, args.end_year + 1):
                for week in range(args.start_week, args.end_week + 1):
                    rows, status = backfill_week(conn, year, week, schools, resolution_stats, force=args.force)
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

    total_names = sum(resolution_stats.values())
    if total_names:
        print(f"\nTeam-name resolution across this run: {total_names} name lookups")
        for k, v in resolution_stats.items():
            print(f"  {k}: {v} ({100 * v / total_names:.1f}%)")

    if empty_weeks:
        print(f"\n{len(empty_weeks)} week(s) returned zero games (bye/off weeks -- eyeball for real gaps):")
        for y, w in empty_weeks:
            print(f"  {y} week {w}")

    if failed_weeks:
        print(f"\n{len(failed_weeks)} week(s) FAILED after {MAX_RETRIES} retries each -- re-run with "
              f"--force for just these to retry (idempotency means the rest won't be re-fetched):")
        for y, w in failed_weeks:
            print(f"  {y} week {w}")


if __name__ == "__main__":
    main()
