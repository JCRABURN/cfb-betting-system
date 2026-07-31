"""
pool_view.py
Drift view for the pick'em pool: compares the LIVE line to the line the
user actually entered/locked from the pool's own Excel sheet -- a
DIFFERENT baseline than gambling_view.py's opening line, because the
pool's number isn't a real market opener, it's whatever number the pool's
sheet showed at whatever moment the user copied it in. Answers "has the
market moved enough since I locked this pick that it's worth reconsidering"
-- not "is this a good bet." No model signal here either, same reasoning
as gambling_view.py: EPA-only has no demonstrated edge (ARCHITECTURE.md
§19-20), so this stays a pure line-drift read.

Input (pool_entries): a list of dicts, one per pick already locked in the
pool --
    {"game_id": ..., "home_team": ..., "away_team": ...,
     "pool_home_spread": <float>, "picked_side": <team name>}
load_pool_entries() reads this shape from a CSV (exported from Excel, or
typed by hand) with the same column names -- kept as a separate function
from build_pool_view() on purpose, so the report logic never depends on
CSV being the input format.
"""

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
from line_utils import get_latest_line


def build_pool_view(conn, pool_entries):
    """pool_entries: see module docstring. Returns every entry with the
    live line, drift, and whether the market's implied favorite has
    flipped since the pool's number -- sorted so picks the market has
    moved most AGAINST surface first (most negative signed_drift_vs_pick),
    since those are the ones most worth a second look."""
    entries = []
    skipped = []

    for pick in pool_entries:
        game_id = pick["game_id"]
        home_team = pick["home_team"]
        away_team = pick["away_team"]
        pool_spread = pick["pool_home_spread"]
        picked_side = pick["picked_side"]

        latest = get_latest_line(conn, game_id)
        if latest is None:
            skipped.append({
                "game_id": game_id, "home_team": home_team, "away_team": away_team,
                "reason": "no_live_line",
            })
            continue

        # home_spread convention: negative = home favored.
        drift = round(latest["home_spread"] - pool_spread, 2)
        pool_favorite = home_team if pool_spread < 0 else away_team
        live_favorite = home_team if latest["home_spread"] < 0 else away_team
        favorite_flipped = pool_favorite != live_favorite

        # Reframe drift relative to the side actually picked, not home/away:
        # for a home pick, drift<0 (home favored more) is movement TOWARD
        # the pick; for an away pick, it's the opposite sign.
        signed_drift_vs_pick = round(-drift if picked_side == home_team else drift, 2)
        if signed_drift_vs_pick > 0:
            movement_vs_pick = "toward_pick"
        elif signed_drift_vs_pick < 0:
            movement_vs_pick = "away_from_pick"
        else:
            movement_vs_pick = "flat"

        entries.append({
            "game_id": game_id,
            "home_team": home_team,
            "away_team": away_team,
            "picked_side": picked_side,
            "pool_home_spread": pool_spread,
            "live_home_spread": latest["home_spread"],
            "live_book": latest["book"],
            "live_line_type": latest["line_type"],
            "drift": drift,
            "signed_drift_vs_pick": signed_drift_vs_pick,
            "movement_vs_pick": movement_vs_pick,
            "favorite_flipped": favorite_flipped,
        })

    entries.sort(key=lambda e: (e["signed_drift_vs_pick"], e["game_id"]))

    return {
        "view": "pool_drift",
        "games": entries,
        "skipped": skipped,
    }


def load_pool_entries(path):
    """Load pool picks from a CSV with header row:
        game_id,home_team,away_team,pool_home_spread,picked_side
    game_id must match the CFBD game_id in the `games` table (the same id
    card_generator.py/gambling_view.py use) -- there's no team-name
    resolution here the way fetch_odds.py has for the live odds feed, since
    the user is entering these by hand and can look the id up once per
    game rather than needing free-text matching. No other schema beyond
    these five columns; extra columns are ignored."""
    entries = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            entries.append({
                "game_id": int(row["game_id"]),
                "home_team": row["home_team"].strip(),
                "away_team": row["away_team"].strip(),
                "pool_home_spread": float(row["pool_home_spread"]),
                "picked_side": row["picked_side"].strip(),
            })
    return entries


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Path to a pool-picks CSV, see load_pool_entries()")
    args = parser.parse_args()

    pool_entries = load_pool_entries(args.csv)

    conn = db.get_connection()
    try:
        view = build_pool_view(conn, pool_entries)
    finally:
        conn.close()

    os.makedirs("data/line_views", exist_ok=True)
    out_path = "data/line_views/pool_drift_latest.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(view, f, indent=2)

    print(f"Pool view: {len(view['games'])} picks, {len(view['skipped'])} skipped. Saved to {out_path}")


if __name__ == "__main__":
    main()
