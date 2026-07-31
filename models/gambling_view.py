"""
gambling_view.py
Market-movement view for the user's own separate gambling (distinct from
the pick'em pool -- see pool_view.py). For every lined game in a
(season, week), compares the OPENING line to the LATEST line from the
SAME book, and reports the direction/magnitude of movement since open.

This is a pure market read, not a model signal. EPA-only has no
demonstrated edge over the market in any slice tested (ARCHITECTURE.md
§19-20), so this view never computes or surfaces a model prediction, a
side recommendation, or an "edge" -- only where and how far the market
itself has moved, which the user reads and decides on independently. Do
not add a model-based recommendation to this view without re-deciding
that call explicitly -- it was deliberate, not an oversight.

Same-book matching, not consensus-vs-single-book: backtest_harness.
get_opening_line() returns the book its opener actually came from;
line_utils.get_latest_line(..., prefer_book=<that book>) tries that SAME
book's latest number first. Comparing two different books' numbers would
show "drift" that's really just two books disagreeing with each other, not
real market movement -- exactly the same-book discipline the user
specifically called for.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import backtest_harness as bh
from line_utils import list_all_games, get_latest_line


def build_gambling_view(conn, season, week):
    """Every lined game for (season, week) with an opening line, showing
    market movement from open to the latest available number. Games with
    no opening line, or no later line at all, are skipped (reason noted)
    rather than silently dropped. Sorted by movement magnitude, descending
    -- the biggest market moves are the actual signal this view exists to
    surface."""
    entries = []
    skipped = []

    for game_id, home_team, away_team, start_date in list_all_games(conn, season, week):
        opening = bh.get_opening_line(conn, game_id)
        if opening is None:
            skipped.append({
                "game_id": game_id, "home_team": home_team, "away_team": away_team,
                "reason": "no_opening_line",
            })
            continue

        latest = get_latest_line(conn, game_id, prefer_book=opening["book"])
        if latest is None:
            skipped.append({
                "game_id": game_id, "home_team": home_team, "away_team": away_team,
                "reason": "no_latest_line",
            })
            continue

        # home_spread convention: negative = home favored. A negative
        # movement means home_spread moved down (more favored, or less of
        # an underdog) -- i.e. the market shifted toward home.
        movement = round(latest["home_spread"] - opening["home_spread"], 2)
        if movement < 0:
            direction = "toward_home"
        elif movement > 0:
            direction = "toward_away"
        else:
            direction = "flat"

        entries.append({
            "game_id": game_id,
            "home_team": home_team,
            "away_team": away_team,
            "start_date": start_date,
            "opening_home_spread": opening["home_spread"],
            "opening_book": opening["book"],
            "latest_home_spread": latest["home_spread"],
            "latest_book": latest["book"],
            "same_book_match": latest["book"] == opening["book"],
            "movement": movement,
            "direction": direction,
            "magnitude": abs(movement),
        })

    entries.sort(key=lambda e: (-e["magnitude"], e["game_id"]))

    return {
        "season": season,
        "week": week,
        "view": "gambling_market_movement",
        "games": entries,
        "skipped": skipped,
    }


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        view = build_gambling_view(conn, args.season, args.week)
    finally:
        conn.close()

    os.makedirs("data/line_views", exist_ok=True)
    out_path = f"data/line_views/gambling_week_{args.week}_{args.season}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(view, f, indent=2)

    print(f"Season {args.season} Week {args.week} gambling view: {len(view['games'])} games, "
          f"{len(view['skipped'])} skipped. Saved to {out_path}")


if __name__ == "__main__":
    main()
