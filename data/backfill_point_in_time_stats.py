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
                wins, losses, source, fetched_at,
                offense_plays, defense_plays, offense_drives, defense_drives
            ) VALUES (NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                year, week, team,
                offense.get("ppa"), defense.get("ppa"),
                offense.get("successRate"), defense.get("successRate"),
                defense.get("havoc", {}).get("total"),
                SOURCE, now,
                offense.get("plays"), defense.get("plays"),
                offense.get("drives"), defense.get("drives"),
            ),
        )
        rows_added += 1

    conn.commit()
    print(f"{year} week {week}: {rows_added} rows added ({len(teams)} teams)")
    return rows_added, "ok"


def fetch_season_games(year):
    """Every FBS game for `year`'s regular season, from CFBD's own /games.
    Returns the list, or None if every retry failed (callers leave
    games_played NULL rather than guess).

    Games-played MUST come from here and not from our `games` table: that
    table only holds games that had betting lines, and FBS-vs-FCS games
    mostly don't. Verified live 2026-10-14 against 2025: CFBD 888 completed
    games vs. our 761, Alabama 13 vs. 12 -- normalizing plays by our count
    would inflate pace for every team that plays an FCS opponent."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = fetch_stats.requests.get(
                f"{CFBD_BASE}/games", headers=HEADERS,
                params={"year": year, "classification": "fbs", "seasonType": "regular"},
                timeout=60,
            )
            if resp.status_code == 429:
                time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"{year} /games: request failed ({e}), retry {attempt + 1}/{MAX_RETRIES}")
            time.sleep(BASE_BACKOFF_SECONDS * (2 ** attempt))
    return None


def games_played_by_team(games, end_week):
    """{team: number of COMPLETED games with week <= end_week}. Pure.
    Mirrors the cutoff of /stats/season/advanced?endWeek=N, so numerator
    (plays through N) and denominator (games through N) cover the same games."""
    counts = {}
    for g in games or []:
        if not g.get("completed") or g.get("week") is None or g["week"] > end_week:
            continue
        for side in ("homeTeam", "awayTeam"):
            team = g.get(side)
            if team:
                counts[team] = counts.get(team, 0) + 1
    return counts


def backfill_pace_week(conn, year, week, games=None):
    """Fill pace columns (plays/drives/games_played) on EXISTING
    cfbd_point_in_time rows for (year, week), leaving every other column --
    the EPA/success/havoc values already relied on by the backtest --
    exactly as stored. Idempotent: rows that already have pace are skipped
    with zero API calls.

    Returns (rows_updated, status): 'skipped' (nothing missing), 'ok',
    'failed' (advanced-stats fetch gave up), or 'no_games' (rows updated
    with plays/drives but /games was unavailable, so games_played stays
    NULL -- never guessed). `ppa_mismatch` rows are reported via print: the
    re-fetched EPA differing from the stored EPA means CFBD has revised
    history since the original snapshot, in which case the pace snapshot is
    from a slightly different data vintage than the EPA beside it."""
    missing = conn.execute(
        "SELECT COUNT(*) FROM team_game_stats WHERE season = ? AND week = ? AND source = ? "
        "AND (offense_plays IS NULL OR games_played IS NULL)",
        (year, week, SOURCE),
    ).fetchone()[0]
    if missing == 0:
        return 0, "skipped"

    need_stats = conn.execute(
        "SELECT COUNT(*) FROM team_game_stats WHERE season = ? AND week = ? AND source = ? "
        "AND offense_plays IS NULL", (year, week, SOURCE),
    ).fetchone()[0] > 0

    by_team = {}
    if need_stats:
        teams = fetch_point_in_time_stats(year, week)
        if teams is None:
            return 0, "failed"
        by_team = {t.get("team"): t for t in teams if t.get("team")}

    gp = games_played_by_team(games, week) if games else {}

    rows = conn.execute(
        "SELECT id, team, offense_plays, offense_epa_play FROM team_game_stats "
        "WHERE season = ? AND week = ? AND source = ?", (year, week, SOURCE),
    ).fetchall()
    updated = 0
    mismatches = 0
    for row_id, team, have_plays, stored_epa in rows:
        sets, params = [], []
        if have_plays is None:
            t = by_team.get(team)
            if t is None:
                continue
            off, de = t.get("offense", {}), t.get("defense", {})
            sets += ["offense_plays = ?", "defense_plays = ?", "offense_drives = ?", "defense_drives = ?"]
            params += [off.get("plays"), de.get("plays"), off.get("drives"), de.get("drives")]
            if stored_epa is not None and off.get("ppa") is not None and abs(off["ppa"] - stored_epa) > 1e-6:
                mismatches += 1
        if team in gp:
            sets.append("games_played = ?")
            params.append(gp[team])
        if not sets:
            continue
        conn.execute(f"UPDATE team_game_stats SET {', '.join(sets)} WHERE id = ?", params + [row_id])
        updated += 1
    conn.commit()
    if mismatches:
        print(f"{year} week {week}: {mismatches} team(s) have EPA that differs from the stored snapshot "
              f"(CFBD revised history) -- pace is from today's vintage, EPA beside it from the original.")
    return updated, ("ok" if gp else "no_games")


def fill_games_played(conn, year, week):
    """Live-path helper: fill games_played for a week whose rows were just
    inserted by backfill_week() (which already captured plays/drives from the
    same response). One /games request. Safe to call when already filled."""
    games = fetch_season_games(year)
    if games is None:
        return 0, "no_games"
    return backfill_pace_week(conn, year, week, games=games)


def main_pace(args):
    """CLI: python data/backfill_point_in_time_stats.py --pace --start-year 2019 --end-year 2026"""
    failed, no_games = [], []
    with db.log_run("cfbd_pace") as run:
        conn = db.get_connection()
        try:
            total = 0
            for year in range(args.start_year, args.end_year + 1):
                games = fetch_season_games(year)
                if games is None:
                    print(f"{year}: /games unavailable after retries -- skipping season")
                    failed.append((year, "games"))
                    continue
                for week in range(args.start_week, args.end_week + 1):
                    n, status = backfill_pace_week(conn, year, week, games=games)
                    total += n
                    if status == "failed":
                        failed.append((year, week))
                    elif status == "no_games":
                        no_games.append((year, week))
                    if status != "skipped":
                        print(f"{year} week {week}: {n} rows ({status})")
                        time.sleep(0.5)
            run["rows_added"] = total
        finally:
            conn.close()
    print(f"\nDone. {run['rows_added']} rows updated.")
    if failed:
        print("FAILED (re-run to retry just these):", failed)
    if no_games:
        print("games_played unavailable for:", no_games)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2019)
    parser.add_argument("--end-year", type=int, default=datetime.utcnow().year - 1)
    parser.add_argument("--start-week", type=int, default=1)
    parser.add_argument("--end-week", type=int, default=15)
    parser.add_argument("--force", action="store_true", help="Re-fetch weeks already ingested")
    parser.add_argument("--pace", action="store_true",
                         help="Fill plays/drives/games_played on already-ingested rows only (no EPA changes)")
    args = parser.parse_args()
    if args.pace:
        return main_pace(args)

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
