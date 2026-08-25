"""
backfill_nfl_games.py
NFL schedule/results/closing-line backfill (NFL scope, 2026-08-24): one
call, one file -- nflverse/nfldata's games.csv, a public GitHub raw file
(no auth, no rate limit), covering 1999-2025 (confirmed live). Populates
nfl_games (this project's own authoritative NFL schedule table -- separate
from `games`, see db.py's schema comment for why) and, for every COMPLETED
game, a closing-line betting_lines row (league='nfl').

spread_line is NOT documented by nflverse as opening or closing -- checked
their own data dictionary directly, it just says "the spread line for the
game." Empirically determined instead (2026-08-24 feasibility research):
cross-referenced spread_line against nflverse's own labeled 2021 openers
(initial_lines.csv) for real games -- every one differed from spread_line
by a realistic in-week movement amount, one row confirmed spread_line
matches a real, independently-sourced closing number exactly. spread_line
is a closing line. Genuine opening-line coverage instead comes from the
committed sportsbookreviewsonline.com archive -- see
backfill_nfl_historical_lines.py.

Only COMPLETED games get a betting_lines row: for a game that hasn't been
played yet, spread_line is just the CURRENT market number, not a genuine
close -- live/in-progress-week odds are fetch_nfl_odds.py's job instead
(line_type='current'), not this backfill's.

Idempotent: a season already ingested (source=nflverse_games) is skipped
unless --force. Since the whole archive is one small file (~2MB), a run
always re-downloads it (cheap, no rate limit) but only writes seasons not
already present.

Usage:
    python data/backfill_nfl_games.py
    python data/backfill_nfl_games.py --start-year 1999 --end-year 2025
    python data/backfill_nfl_games.py --start-year 2025 --end-year 2025 --force
"""

import os
import sys
import csv
import io
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import requests

GAMES_CSV_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
SOURCE = "nflverse_games"


def fetch_games_csv():
    """Returns the parsed rows of nflverse's games.csv (list of dicts), or
    None on failure -- a single request, no retry/backoff machinery: this
    is a static public file, not a rate-limited API."""
    try:
        resp = requests.get(GAMES_CSV_URL, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"Failed to fetch {GAMES_CSV_URL}: {e}")
        return None
    return list(csv.DictReader(io.StringIO(resp.text)))


def season_already_ingested(conn, season):
    count = conn.execute(
        "SELECT COUNT(*) FROM nfl_games WHERE season = ? AND source = ?", (season, SOURCE),
    ).fetchone()[0]
    return count > 0


def _to_float(s):
    if s in (None, "", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(s):
    v = _to_float(s)
    return int(v) if v is not None else None


def ingest_season(conn, rows, season, force=False):
    """Returns (games_added, lines_added, status)."""
    if not force and season_already_ingested(conn, season):
        print(f"{season}: already ingested, skipping (use --force to re-fetch)")
        return 0, 0, "skipped"

    if force:
        conn.execute("DELETE FROM nfl_games WHERE season = ? AND source = ?", (season, SOURCE))
        conn.execute(
            "DELETE FROM betting_lines WHERE league = 'nfl' AND season = ? AND source = ?",
            (season, SOURCE),
        )

    now = datetime.utcnow().isoformat()
    games_added = 0
    lines_added = 0
    for row in rows:
        if int(row["season"]) != season:
            continue

        game_id = row["game_id"]
        home_team, away_team = row["home_team"], row["away_team"]
        home_score, away_score = _to_int(row["home_score"]), _to_int(row["away_score"])
        completed = 1 if (home_score is not None and away_score is not None) else 0

        conn.execute(
            """
            INSERT INTO nfl_games (
                game_id, season, week, game_type, gameday, home_team, away_team,
                home_score, away_score, completed, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                home_score=excluded.home_score, away_score=excluded.away_score,
                completed=excluded.completed
            """,
            (game_id, season, int(row["week"]), row["game_type"], row["gameday"],
             home_team, away_team, home_score, away_score, completed, SOURCE, now),
        )
        games_added += 1

        if not completed:
            continue
        # nflverse's own convention is the OPPOSITE of this project's:
        # their dictionary states "a positive number means the home team
        # was favored" -- every other ingestion path in this project
        # (fetch_odds.py, backfill_historical_lines.py, this file's own
        # sibling backfill_nfl_historical_lines.py) uses negative=home-
        # favored instead. Caught live 2026-08-24 by the exact
        # opener/closer verification this task asked for: the real
        # Dallas @ TampaBay 2021 closing line came back +10.0 here
        # (TB, the home team and the real favorite, shown POSITIVE)
        # while the same game's SBR-sourced closing row correctly showed
        # -10.0. Negated here so every league='nfl' row shares one sign
        # convention regardless of source.
        spread_line = _to_float(row["spread_line"])
        total_line = _to_float(row["total_line"])
        if spread_line is None and total_line is None:
            continue
        home_spread = -spread_line if spread_line is not None else None
        conn.execute(
            """
            INSERT INTO betting_lines (
                game_id, league, season, week, home_team, away_team, book,
                home_spread, total, line_type, source, fetched_at
            ) VALUES (?, 'nfl', ?, ?, ?, ?, 'nflverse', ?, ?, 'closing', ?, ?)
            """,
            (game_id, season, int(row["week"]), home_team, away_team,
             home_spread, total_line, SOURCE, now),
        )
        lines_added += 1

    conn.commit()
    print(f"{season}: {games_added} games, {lines_added} closing lines")
    return games_added, lines_added, "ok"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=1999)
    parser.add_argument("--end-year", type=int, default=datetime.utcnow().year)
    parser.add_argument("--force", action="store_true", help="Re-ingest seasons already present")
    args = parser.parse_args()

    with db.log_run(SOURCE) as run:
        rows = fetch_games_csv()
        if rows is None:
            raise RuntimeError("Could not fetch nflverse games.csv -- see error above")

        conn = db.get_connection()
        try:
            total_games = 0
            total_lines = 0
            for season in range(args.start_year, args.end_year + 1):
                g, l, status = ingest_season(conn, rows, season, force=args.force)
                total_games += g
                total_lines += l
            run["rows_added"] = total_games + total_lines
        finally:
            conn.close()

    print(f"\nDone. {total_games} nfl_games rows, {total_lines} closing betting_lines rows.")


if __name__ == "__main__":
    main()
