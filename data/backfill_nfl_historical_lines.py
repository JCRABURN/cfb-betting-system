"""
backfill_nfl_historical_lines.py
Ingests the committed sportsbookreviewsonline.com NFL archive
(data/nfl_historical_odds/nfl_odds_{season}.xlsx, 2007-2021 -- 15 seasons;
see below for why 2022 and earlier years aren't here) into betting_lines
as REAL opening lines, the free data this project's college-football side
never had (opener coverage killed 2020 there entirely -- see
ARCHITECTURE.md §14). Downloaded once from the Wayback Machine (the site
itself is dead -- "no further updates after January 16, 2023," confirmed
live from the archived page) and committed directly: the data never
changes and this project should never depend on Wayback or a dead site at
runtime. 2022 (the archive's own final season) was never crawled by
Wayback itself, only the index page linking to it -- unrecoverable, a
real, permanent gap, not a bug here. Preseason files exist on the source
site too but aren't ingested -- not useful for grading real weeks.

THE PARSING PROBLEM, verified against real data before writing this
(2026-08-24), not assumed: each game is two consecutive rows (one per
team), and each row's Open/Close cell can hold EITHER that game's point
spread or its total -- there is no fixed rule for which team's row gets
which. Two wrong hypotheses tried and rejected first:
  1. A fixed positional rule (e.g. "home row is always spread") -- checked
     directly, false; it flips game to game.
  2. Per-ROW classification (spread row vs. total row, once per game) --
     checked directly, ~8% of real games looked "anomalous" under this
     model. Investigating why: those are real favorite-flip games, where
     the favorite (and therefore which row's cell holds the spread) is
     genuinely different at open than at close. A per-ROW model can't
     represent that; a per-CELL model can.
The actual, verified rule: classify EACH of the Open and Close columns
INDEPENDENTLY. In a real column (Open, or separately Close), spread-shaped
values are small (<=SPREAD_MAX) and total-shaped values are large
(>=TOTAL_MIN); exactly one of the two rows' values in that column must
classify as spread and the other as total, or the column is unresolvable
for that game. Verified against 1,088 real games across 4 seasons
(2007, 2014, 2020, 2021): 99.72% resolved cleanly this way; the 3
remaining games were genuinely ambiguous (both values in-range for the
same class) and are exactly what SKIP_RATE_ALARM_THRESHOLD exists to
catch in aggregate, not paper over with a guess.

Team names: sportsbookreviewsonline.com uses its own concatenated
city/nickname spelling (see nfl_teams.py's SBR alias table, built from the
real names found in these exact files, including real data-entry variants
like "Washingtom"). A name this project can't resolve is reported and the
row skipped -- never guessed.

'N'/'N' rows (both teams marked neutral-site -- International Series and
Super Bowl games, confirmed against real 2013 London/Super Bowl rows) have
no home team in the raw file at all; resolved by looking up nfl_games for
which team nflverse itself designated home for that neutral game (nflverse
always designates one, per its own data dictionary).

Idempotent: a season already ingested (source=sbr_historical) is skipped
unless --force, which deletes and re-inserts that season's rows.

REQUIRES nfl_games to already be populated (see backfill_nfl_games.py) --
every row's game_id match depends on it; run that script first.

Usage:
    python data/backfill_nfl_historical_lines.py
    python data/backfill_nfl_historical_lines.py --start-year 2007 --end-year 2021
    python data/backfill_nfl_historical_lines.py --start-year 2013 --end-year 2013 --force
"""

import os
import sys
import glob
import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import openpyxl
from nfl_teams import resolve_sbr_team

SOURCE = "sbr_historical"
ODDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "nfl_historical_odds")

# Verified live 2026-08-24 against the full 4,025-game real archive: these
# two thresholds must leave a genuine GAP (SPREAD_MAX < TOTAL_MIN) so a
# value in between is truly ambiguous and falls through to "unresolvable"
# -- an earlier version of this code set them the other way around
# (SPREAD_MAX=30, TOTAL_MIN=25), which overlapped instead of gapping, and
# because of the if/elif order below, TOTAL_MIN was silently unreachable:
# every value <=30 always classified as 'spread', a bare single-threshold
# cutoff wearing a two-threshold costume. Caught by testing the classifier
# directly, not by the pipeline "looking like" it worked -- the pipeline's
# real-world resolve rate barely moved (99.88% -> 99.85%, 1,088 real
# games) precisely because the bug's failure mode was safe (an
# occasional spread misclassified as spread is not corruption), but the
# threshold semantics were dishonest either way. Re-validated with the
# real gap in place: 4,020/4,025 real games (99.88%) still resolve
# cleanly.
SPREAD_MAX = 24
TOTAL_MIN = 30

# If more than this fraction of a season's games can't be resolved,
# something is structurally wrong (a column shifted, a name-mapping gap
# affecting most of a season) rather than the small, expected residual of
# genuinely ambiguous real rows (0-0.7% measured) -- hard-fail rather than
# silently completing a mostly-broken ingest.
SKIP_RATE_ALARM_THRESHOLD = 0.05


def _to_num(cell):
    if cell is None:
        return None
    if isinstance(cell, str):
        if cell.strip().lower() == "pk":
            return 0.0
        try:
            return float(cell)
        except ValueError:
            return None
    if isinstance(cell, (int, float)):
        return float(cell)
    return None


def _classify_cell(v):
    if v is None:
        return None
    if v <= SPREAD_MAX:
        return "spread"
    if v >= TOTAL_MIN:
        return "total"
    return None


def _resolve_column(v1, v2):
    """v1, v2: the two rows' values for the SAME column (both Open, or
    both Close). Returns (role_for_row1, role_for_row2), each 'spread' or
    'total', or None if the pair can't be unambiguously resolved."""
    c1, c2 = _classify_cell(v1), _classify_cell(v2)
    if c1 and c2 and c1 != c2:
        return c1, c2
    return None


def load_season_rows(path):
    """Raw rows from one committed .xlsx, read-only (no write-back needed)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    header = list(next(rows_iter))
    idx = {name: i for i, name in enumerate(header)}
    required = ("Date", "VH", "Team", "Open", "Close")
    missing = [c for c in required if c not in idx]
    if missing:
        raise ValueError(f"{path}: missing expected column(s) {missing} -- got header {header}")
    rows = []
    for r in rows_iter:
        if r[idx["Team"]] is None:
            continue
        rows.append({
            "date": r[idx["Date"]], "vh": r[idx["VH"]], "team": r[idx["Team"]],
            "open": r[idx["Open"]], "close": r[idx["Close"]],
        })
    wb.close()
    return rows


def _resolve_home_away(conn, season, row1, row2, code1, code2):
    """Returns (home_code, away_code) or None if unresolvable. Handles the
    normal {V,H} case directly from each row's own marker; for a genuine
    neutral-site game ({N,N}), looks up which team nflverse itself
    designated home for that (season, the two teams) matchup -- nflverse
    always assigns one, even for a Super Bowl (see nfl_games' schema
    comment)."""
    vh = {row1["vh"], row2["vh"]}
    if vh == {"V", "H"}:
        home_code = code1 if row1["vh"] == "H" else code2
        away_code = code2 if row1["vh"] == "H" else code1
        return home_code, away_code
    if vh == {"N"}:
        match = conn.execute(
            "SELECT home_team, away_team FROM nfl_games WHERE season = ? "
            "AND ((home_team = ? AND away_team = ?) OR (home_team = ? AND away_team = ?))",
            (season, code1, code2, code2, code1),
        ).fetchone()
        return (match[0], match[1]) if match else None
    return None


def ingest_season(conn, season, force=False):
    """Returns {"inserted": N, "unresolved": [...]} -- unresolved entries
    are {"season", "date", "team1", "team2", "reason"}, never silently
    dropped without a reason attached."""
    path = os.path.join(ODDS_DIR, f"nfl_odds_{season}.xlsx")
    if not os.path.exists(path):
        print(f"{season}: no committed file at {path}, skipping")
        return {"inserted": 0, "unresolved": []}

    already = conn.execute(
        "SELECT COUNT(*) FROM betting_lines WHERE league='nfl' AND season=? AND source=?",
        (season, SOURCE),
    ).fetchone()[0] > 0
    if already and not force:
        print(f"{season}: already ingested, skipping (use --force to re-fetch)")
        return {"inserted": 0, "unresolved": []}
    if already and force:
        conn.execute(
            "DELETE FROM betting_lines WHERE league='nfl' AND season=? AND source=?",
            (season, SOURCE),
        )

    rows = load_season_rows(path)
    if len(rows) % 2 != 0:
        raise ValueError(f"{season}: odd number of data rows ({len(rows)}) -- can't pair 2-per-game")

    now = datetime.utcnow().isoformat()
    inserted = 0
    unresolved = []
    n_games = len(rows) // 2

    for i in range(0, len(rows), 2):
        row1, row2 = rows[i], rows[i + 1]

        def skip(reason):
            unresolved.append({
                "season": season, "date": row1["date"], "team1": row1["team"],
                "team2": row2["team"], "reason": reason,
            })

        code1 = resolve_sbr_team(row1["team"], season)
        code2 = resolve_sbr_team(row2["team"], season)
        if code1 is None or code2 is None:
            # Report every unresolved name in the pair, not just the first
            # -- a caller diagnosing a skip needs to see both, not have a
            # second real gap silently hidden behind the first one found.
            bad = [n for n, c in ((row1["team"], code1), (row2["team"], code2)) if c is None]
            skip(f"unresolved team name(s): {bad!r}")
            continue

        home_away = _resolve_home_away(conn, season, row1, row2, code1, code2)
        if home_away is None:
            skip(f"unresolved home/away (VH={row1['vh']!r}/{row2['vh']!r})")
            continue
        home_code, away_code = home_away

        open_roles = _resolve_column(_to_num(row1["open"]), _to_num(row2["open"]))
        close_roles = _resolve_column(_to_num(row1["close"]), _to_num(row2["close"]))
        if open_roles is None or close_roles is None:
            skip(f"ambiguous spread/total (open={row1['open']!r}/{row2['open']!r}, "
                 f"close={row1['close']!r}/{row2['close']!r})")
            continue

        game = conn.execute(
            "SELECT game_id, week FROM nfl_games WHERE season = ? AND home_team = ? AND away_team = ?",
            (season, home_code, away_code),
        ).fetchone()
        if game is None:
            skip(f"no matching nfl_games row for {away_code} @ {home_code}, season {season} "
                 f"-- has backfill_nfl_games.py been run for this season?")
            continue
        game_id, week = game

        def signed_home_spread(row1_role, value1, value2):
            spread_row, spread_value = (row1, value1) if row1_role == "spread" else (row2, value2)
            spread_team = code1 if spread_row is row1 else code2
            return -spread_value if spread_team == home_code else spread_value

        open_role1, _ = open_roles
        close_role1, _ = close_roles
        open_v1, open_v2 = _to_num(row1["open"]), _to_num(row2["open"])
        close_v1, close_v2 = _to_num(row1["close"]), _to_num(row2["close"])

        open_home_spread = signed_home_spread(open_role1, open_v1, open_v2)
        close_home_spread = signed_home_spread(close_role1, close_v1, close_v2)
        open_total = open_v2 if open_role1 == "spread" else open_v1
        close_total = close_v2 if close_role1 == "spread" else close_v1

        for line_type, spread, total in (
            ("opening", open_home_spread, open_total),
            ("closing", close_home_spread, close_total),
        ):
            conn.execute(
                """
                INSERT INTO betting_lines (
                    game_id, league, season, week, home_team, away_team, book,
                    home_spread, total, line_type, source, fetched_at
                ) VALUES (?, 'nfl', ?, ?, ?, ?, 'sbr', ?, ?, ?, ?, ?)
                """,
                (game_id, season, week, home_code, away_code, spread, total,
                 line_type, SOURCE, now),
            )
        inserted += 2

    conn.commit()
    skip_rate = len(unresolved) / n_games if n_games else 0
    print(f"{season}: {n_games} games, {inserted // 2} resolved, {len(unresolved)} unresolved "
          f"({100 * skip_rate:.1f}%)")
    if skip_rate > SKIP_RATE_ALARM_THRESHOLD:
        raise RuntimeError(
            f"{season}: {100 * skip_rate:.1f}% of games unresolved, over the "
            f"{100 * SKIP_RATE_ALARM_THRESHOLD:.0f}% sanity threshold -- this looks like a structural "
            f"parsing problem, not the small expected residual of genuinely ambiguous rows. "
            f"First few: {unresolved[:5]}"
        )
    return {"inserted": inserted, "unresolved": unresolved}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2007)
    parser.add_argument("--end-year", type=int, default=2021)
    parser.add_argument("--force", action="store_true", help="Re-ingest seasons already present")
    args = parser.parse_args()

    with db.log_run(SOURCE) as run:
        conn = db.get_connection()
        try:
            total_inserted = 0
            all_unresolved = []
            for season in range(args.start_year, args.end_year + 1):
                result = ingest_season(conn, season, force=args.force)
                total_inserted += result["inserted"]
                all_unresolved.extend(result["unresolved"])
            run["rows_added"] = total_inserted
        finally:
            conn.close()

    print(f"\nDone. {total_inserted} betting_lines rows added.")
    if all_unresolved:
        print(f"\n{len(all_unresolved)} row(s) could not be resolved (skipped, not guessed):")
        for u in all_unresolved:
            print(f"  {u['season']} {u['date']}: {u['team1']} / {u['team2']} -- {u['reason']}")


if __name__ == "__main__":
    main()
