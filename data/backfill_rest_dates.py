"""
backfill_rest_dates.py
Narrowly-scoped, idempotent supplemental backfill: recovers ONLY the
prior-game DATE (not a full game row) for teams whose actual most recent
game isn't in the FBS-only `games` table -- typically an FBS-vs-FCS buy
game (see ARCHITECTURE.md/Phase 3 for why `games` is FBS-only by design).
Needed so backtest_harness.get_days_rest() can compute correct rest for
these teams' next FBS game, instead of treating the gap as "no prior game."

Found via the rest/schedule feature test (MODEL_DESIGN.md's "Later
features" plan): 204 (team, season, week) gaps across 2021-2025, all
resolvable by fetching CFBD's /games endpoint WITHOUT a classification
filter for the affected (season, week) -- confirmed live this returns
FBS-vs-FCS games directly, with dates. A few needed the week before that
too (a team on a real bye immediately after their FCS-opponent game).

Idempotent: skips a (team, season, week) combo already recorded unless
--force. Only queries the SPECIFIC weeks where a gap is actually detected
(via a small per-season-week cache) -- does not re-fetch a whole season,
and does not touch `games` at all (that table's FBS-only scope is
untouched; this is a separate, purpose-built lookup).

Usage:
    python data/backfill_rest_dates.py
    python data/backfill_rest_dates.py --start-year 2021 --end-year 2025
"""

import os
import sys
import time
import argparse
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "models"))
import db
import fetch_stats
import backtest_harness as bh

CFBD_BASE = fetch_stats.CFBD_BASE
HEADERS = fetch_stats.HEADERS
SOURCE = "cfbd_supplemental_dates"

MAX_LOOKBACK_WEEKS = 4


def find_gaps(conn, seasons):
    """Every (team, season, week) that WOULD be gradeable (both teams have
    point-in-time EPA available) but get_days_rest() still can't find a
    prior game -- i.e. a genuine rest gap worth spending an API call on, not
    every game (no point recovering rest for a game that's ungradeable for
    an unrelated reason anyway)."""
    gaps = set()
    for season in seasons:
        for week in bh.list_weeks(conn, season):
            for game_id, home_team, away_team, home_points, away_points, start_date in bh.list_games(conn, season, week):
                home_ok = bh.get_team_stats_as_of(conn, home_team, season, week) is not None
                away_ok = bh.get_team_stats_as_of(conn, away_team, season, week) is not None
                if not (home_ok and away_ok):
                    continue
                if bh.get_days_rest(conn, home_team, season, start_date) is None:
                    gaps.add((home_team, season, week))
                if bh.get_days_rest(conn, away_team, season, start_date) is None:
                    gaps.add((away_team, season, week))
    return gaps


def fetch_week_all_divisions(year, week):
    """CFBD's /games with NO classification filter -- returns FBS-vs-FCS
    games too, confirmed live. Used only to recover dates, never stored as
    full game rows (no score, no FK to games, no opponent tracking)."""
    resp = fetch_stats.requests.get(
        f"{CFBD_BASE}/games", headers=HEADERS, params={"year": year, "week": week}, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def already_recorded(conn, team, season, week):
    return conn.execute(
        "SELECT 1 FROM supplemental_game_dates WHERE team = ? AND season = ? AND week = ?",
        (team, season, week),
    ).fetchone() is not None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end-year", type=int, default=datetime.utcnow().year - 1)
    parser.add_argument("--force", action="store_true", help="Re-resolve gaps already recorded")
    args = parser.parse_args()

    seasons = list(range(args.start_year, args.end_year + 1))

    with db.log_run(SOURCE) as run:
        conn = db.get_connection()
        try:
            gaps = find_gaps(conn, seasons)
            print(f"{len(gaps)} rest gap(s) found across seasons {seasons}")
            if not args.force:
                gaps = {(t, s, w) for t, s, w in gaps if not already_recorded(conn, t, s, w)}
                print(f"{len(gaps)} not already recorded")

            now = datetime.utcnow().isoformat()
            rows_added = 0
            unresolved = []
            week_cache = {}

            for team, season, week in sorted(gaps):
                target_row = conn.execute(
                    "SELECT start_date FROM games WHERE season = ? AND week = ? AND (home_team = ? OR away_team = ?) LIMIT 1",
                    (season, week, team, team),
                ).fetchone()
                if target_row is None:
                    continue
                target_date = target_row[0]

                resolved_date = None
                resolved_classification = None
                for lookback in range(1, MAX_LOOKBACK_WEEKS + 1):
                    lookup_week = week - lookback
                    if lookup_week < 1:
                        break
                    cache_key = (season, lookup_week)
                    if cache_key not in week_cache:
                        week_cache[cache_key] = fetch_week_all_divisions(season, lookup_week)
                        time.sleep(0.3)
                    for g in week_cache[cache_key]:
                        home, away = g.get("homeTeam"), g.get("awayTeam")
                        g_date = g.get("startDate")
                        if team in (home, away) and g_date and g_date < target_date:
                            if resolved_date is None or g_date > resolved_date:
                                resolved_date = g_date
                                resolved_classification = g.get("awayClassification") if team == home else g.get("homeClassification")
                    if resolved_date is not None:
                        break

                if resolved_date is None:
                    unresolved.append((team, season, week))
                    continue

                conn.execute(
                    "INSERT INTO supplemental_game_dates "
                    "(team, season, week, start_date, opponent_classification, source, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (team, season, week, resolved_date, resolved_classification, SOURCE, now),
                )
                rows_added += 1

            conn.commit()
            run["rows_added"] = rows_added
        finally:
            conn.close()

    print(f"Done. {run['rows_added']} rows added to {db.DB_PATH}")
    if unresolved:
        print(f"{len(unresolved)} gap(s) still unresolved after {MAX_LOOKBACK_WEEKS}-week lookback:")
        for u in unresolved:
            print(f"  {u}")


if __name__ == "__main__":
    main()
