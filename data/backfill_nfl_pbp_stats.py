"""
backfill_nfl_pbp_stats.py
NFL point-in-time weekly team stats (NFL scope, 2026-08-24): the NFL
analog of team_game_stats' cfbd_point_in_time backfill, but computed
locally from raw play-by-play instead of fetched pre-aggregated from a
vendor -- confirmed during feasibility research that this is NOT the same
"SP+ blocked, no history at all" problem the college side hit (see
MODEL_DESIGN.md / ARCHITECTURE.md §13): nflverse's play-by-play is raw
data with a precomputed `epa` column, not a vendor rating exposing only
current state, so any cumulative-through-week-N cut is just an aggregation
this project does itself.

Source: nflverse-data's GitHub Releases, NOT a REST API -- static
per-season files, no auth, no rate limit:
    https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz
Confirmed live 2026-08-24: files exist for every season 1999-2025, and
`epa`/`success` are 100% populated on every run/pass play even in the
earliest (1999) file, not a later-years-only feature. `.csv.gz` chosen
over the `.parquet`/`.rds`/`.qs` alternatives nflverse also offers --
stdlib `gzip`+`csv` handles it with no new dependency, matching this
project's "check stdlib first" rule (contrast with openpyxl, needed
because the SBR archive is genuinely binary .xlsx with no stdlib path).

Aggregation: regular-season run/pass plays only (season_type='REG',
play_type in {'run','pass'}) -- postseason excluded, same reasoning
CFBD's own point-in-time backfill scopes to a clean week-number
progression, not a bracket. For each team, each week N holds
CUMULATIVE-THROUGH-WEEK-N offense/defense EPA-per-play and success rate --
same "week=N includes week N's own results, join against week N-1 to
predict week N" discipline as get_team_stats_as_of() and
backfill_point_in_time_stats.py's own documented convention. A bye week
still gets a row (carrying the prior week's cumulative numbers forward
unchanged, since no new plays exist to add) -- this matters for later
lookups, so a join for the week after a bye still finds a real row.

Idempotent: a season already ingested (source=nflverse_pbp) is skipped
unless --force. Each season file is ~15-20MB compressed; a full 1999-2025
backfill downloads roughly 500MB total and takes real time -- run in
smaller --start-year/--end-year batches if that matters.

Usage:
    python data/backfill_nfl_pbp_stats.py --start-year 2024 --end-year 2024
    python data/backfill_nfl_pbp_stats.py --start-year 1999 --end-year 2025
"""

import os
import sys
import io
import csv
import gzip
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import requests

PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
SOURCE = "nflverse_pbp"

# Indices into the 8-slot per-(team,week) accumulator list, kept as a flat
# list rather than a dict for speed -- this loop runs once per scrimmage
# play in a ~45k-play season file.
_OFF_EPA_SUM, _OFF_EPA_N, _DEF_EPA_SUM, _DEF_EPA_N = 0, 1, 2, 3
_OFF_SUCC_SUM, _OFF_SUCC_N, _DEF_SUCC_SUM, _DEF_SUCC_N = 4, 5, 6, 7


def fetch_pbp_gzip(season):
    """Returns the raw gzip bytes for one season's play-by-play file, or
    None on failure. A single request -- no retry/backoff machinery: this
    is a static public release asset, not a rate-limited API."""
    url = PBP_URL.format(season=season)
    try:
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
    except Exception as e:
        print(f"{season}: failed to fetch play-by-play ({e})")
        return None
    return resp.content


def _weekly_totals(gz_bytes):
    """One pass over the season's plays -> {(team, week): [8 accumulator
    slots]} for THAT WEEK ALONE (not yet cumulative). Regular season
    run/pass plays with a usable epa/success/posteam/defteam/week only."""
    weekly = {}
    with gzip.open(io.BytesIO(gz_bytes), "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("season_type") != "REG":
                continue
            if row.get("play_type") not in ("run", "pass"):
                continue
            week_s, posteam, defteam = row.get("week"), row.get("posteam"), row.get("defteam")
            epa_s, succ_s = row.get("epa"), row.get("success")
            if not week_s or not posteam or not defteam:
                continue
            if epa_s in (None, "") or succ_s in (None, ""):
                continue
            week = int(week_s)
            epa = float(epa_s)
            succ = float(succ_s)

            off = weekly.setdefault((posteam, week), [0.0, 0, 0.0, 0, 0.0, 0, 0.0, 0])
            off[_OFF_EPA_SUM] += epa
            off[_OFF_EPA_N] += 1
            off[_OFF_SUCC_SUM] += succ
            off[_OFF_SUCC_N] += 1

            de = weekly.setdefault((defteam, week), [0.0, 0, 0.0, 0, 0.0, 0, 0.0, 0])
            de[_DEF_EPA_SUM] += epa
            de[_DEF_EPA_N] += 1
            de[_DEF_SUCC_SUM] += succ
            de[_DEF_SUCC_N] += 1
    return weekly


def cumulative_point_in_time(weekly):
    """Per-week totals -> cumulative-through-week-N rows: [(team, week,
    offense_epa_play, defense_epa_play, offense_success_rate,
    defense_success_rate), ...]. A row appears for every week from a
    team's first game onward, including bye weeks (carried forward
    unchanged, since no new plays exist to add that week)."""
    if not weekly:
        return []
    teams = sorted({team for team, _ in weekly})
    max_week = max(week for _, week in weekly)
    rows = []
    for team in teams:
        running = [0.0, 0, 0.0, 0, 0.0, 0, 0.0, 0]
        for week in range(1, max_week + 1):
            wk = weekly.get((team, week))
            if wk is not None:
                for i in range(8):
                    running[i] += wk[i]
            if running[_OFF_EPA_N] == 0 and running[_DEF_EPA_N] == 0:
                continue  # team hasn't played its first game of the season yet
            off_epa = running[_OFF_EPA_SUM] / running[_OFF_EPA_N] if running[_OFF_EPA_N] else None
            def_epa = running[_DEF_EPA_SUM] / running[_DEF_EPA_N] if running[_DEF_EPA_N] else None
            off_succ = running[_OFF_SUCC_SUM] / running[_OFF_SUCC_N] if running[_OFF_SUCC_N] else None
            def_succ = running[_DEF_SUCC_SUM] / running[_DEF_SUCC_N] if running[_DEF_SUCC_N] else None
            rows.append((team, week, off_epa, def_epa, off_succ, def_succ))
    return rows


def season_already_ingested(conn, season):
    return conn.execute(
        "SELECT COUNT(*) FROM nfl_team_stats WHERE season = ? AND source = ?", (season, SOURCE),
    ).fetchone()[0] > 0


def backfill_season(conn, season, force=False):
    """Returns (rows_added, status)."""
    if not force and season_already_ingested(conn, season):
        print(f"{season}: already ingested, skipping (use --force to re-fetch)")
        return 0, "skipped"

    gz_bytes = fetch_pbp_gzip(season)
    if gz_bytes is None:
        return 0, "failed"

    weekly = _weekly_totals(gz_bytes)
    rows = cumulative_point_in_time(weekly)
    if not rows:
        print(f"{season}: 0 rows computed")
        return 0, "empty"

    if force:
        conn.execute("DELETE FROM nfl_team_stats WHERE season = ? AND source = ?", (season, SOURCE))

    now = datetime.utcnow().isoformat()
    for team, week, off_epa, def_epa, off_succ, def_succ in rows:
        conn.execute(
            """
            INSERT INTO nfl_team_stats (
                season, week, team, offense_epa_play, defense_epa_play,
                offense_success_rate, defense_success_rate, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (season, week, team, off_epa, def_epa, off_succ, def_succ, SOURCE, now),
        )
    conn.commit()
    print(f"{season}: {len(rows)} rows ({len({t for t, *_ in rows})} teams)")
    return len(rows), "ok"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=1999)
    parser.add_argument("--end-year", type=int, default=datetime.utcnow().year)
    parser.add_argument("--force", action="store_true", help="Re-fetch seasons already present")
    args = parser.parse_args()

    empty_seasons, failed_seasons = [], []

    with db.log_run(SOURCE) as run:
        conn = db.get_connection()
        try:
            total = 0
            for season in range(args.start_year, args.end_year + 1):
                rows, status = backfill_season(conn, season, force=args.force)
                total += rows
                if status == "empty":
                    empty_seasons.append(season)
                elif status == "failed":
                    failed_seasons.append(season)
            run["rows_added"] = total
        finally:
            conn.close()

    print(f"\nDone. {run['rows_added']} rows added to {db.DB_PATH}")
    if empty_seasons:
        print(f"\n{len(empty_seasons)} season(s) computed 0 rows: {empty_seasons}")
    if failed_seasons:
        print(f"\n{len(failed_seasons)} season(s) FAILED to fetch -- re-run with --force for just "
              f"these: {failed_seasons}")


if __name__ == "__main__":
    main()
