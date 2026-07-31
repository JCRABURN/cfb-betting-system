"""
card_generator.py
Builds the weekly card: every lined FBS game for one (season, week), with
the EPA-only model's predicted side, edge (points), and a confidence flag
(see _assign_confidence below -- NOT a graduated ranking by edge size, see
why). This is the single shared output both eventual contest consumers
(SplashSports, pick'em pool) will read from later; it does not encode
either contest's own rules (entry format, scoring, unit sizing) -- just the
model's view of every game.

Reuses the SAME fitted model as the validated backtest (backtest_harness.py
+ baseline_epa.py) -- intercept+coefficient fit via OLS on seasons strictly
before the target season, predictions built from get_team_stats_as_of's
point-in-time snapshots. No new model, no new fitting logic: this is a
different consumer of the same walk-forward machinery, not a fork of it.

Two deliberate departures from backtest_harness.py, both because a card and
a backtest answer different questions:

1. Games are NOT filtered to completed=1 (backtest_harness.list_games()
   only grades finished games; a card is for games that haven't been played
   yet). See line_utils.list_all_games().
2. Edge is computed against the LATEST available line, not the opening line
   (line_utils.get_latest_line()), because a card informs a bet placed NOW,
   against today's number -- not a backtest's fixed historical entry point.
   The live in-season path (fetch_odds.py) writes line_type='current' for
   this; the historical archive instead uses 'closing' for the same concept
   (confirmed live: 2024 week 10 has 0 'current' rows, only 'opening' and
   'closing') -- get_latest_line() tries both so this one function works
   unmodified against either data source.

list_all_games()/get_latest_line() now live in line_utils.py, shared with
the two drift views (gambling_view.py, pool_view.py) that also need "every
game this week" and "the latest line for a game" -- pulled out once a
second consumer needed the identical logic.

No "top 5 by edge" here anymore. The first version of this generator ranked
games by raw edge size on the assumption that a bigger gap between the
model and the market meant a stronger pick. A dedicated backtest check
(bucketing the 2021-2025 edge>=3 bet-subset by edge size) disproved that:
no bucket reliably beats the ~52.4% breakeven line, and the biggest-edge
bucket (10+, 50.7% ATS, 2 of 5 seasons below 50%) is the WORST of the
three, not the best -- consistent with large predicted margins coming from
extrapolation in lopsided games, where a single-feature linear model is
least calibrated. Ranking by edge was therefore backwards: it surfaced the
model's least reliable picks as its most confident ones. See
_assign_confidence()'s docstring for the fix and ARCHITECTURE.md §19 for
the full bucket data.

IMPORTANT: testing this against a historical week only validates the CARD
LOGIC (format, that missing data is skipped not crashed, that the flag
matches the edge threshold) against known data. It does NOT make a
current-season card trustworthy -- that still depends on the live weekly
fetch path (see ARCHITECTURE.md §18's Week 0/1 verification items), which
is a separate, not-yet-verified concern. Nor does it mean the MODEL is
ready to run in either contest -- see ARCHITECTURE.md §19's bottom line.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import backtest_harness as bh
import baseline_epa as epa
from line_utils import list_all_games, get_latest_line


# The one bucket boundary the 2021-2025 edge-bucket backtest actually
# supports: edge >= 10 is the demonstrably WORST-performing slice (50.7%
# ATS aggregate, below 50% in 2 of 5 seasons) -- not a threshold picked to
# produce a nice-looking split. See ARCHITECTURE.md §19.
LARGE_EDGE_LOW_CONFIDENCE_THRESHOLD = 10.0


def _assign_confidence(entries):
    """No graduated ranking by edge size. The backtest showed none of the
    3-6 / 6-10 / 10+ edge buckets reliably beats breakeven, and the gap
    between the two smaller buckets (51.5% vs 52.0%) is noise-level, not a
    validated difference -- inventing a 5-tier scale out of that would just
    be a different way of pretending edge size means something it doesn't.
    Capping/normalizing edge to rescue a ranking was considered and
    rejected for the same reason: it would be engineering around a model
    that hasn't demonstrated an edge signal, not fixing a bug.

    What the backtest DOES support is a negative signal: edge >= 10 is the
    one bucket shown to underperform the other two, plausibly because it's
    where the linear model is extrapolating hardest (large predicted
    margins in lopsided games). So every game gets "standard" except that
    one flagged bucket, which gets "low_confidence_large_edge" -- the
    opposite of what the original rank-by-edge scheme did with these same
    games. Does NOT reorder `entries` -- edge size is not a quality
    ranking, so there's nothing to sort by."""
    for entry in entries:
        entry["confidence"] = (
            "low_confidence_large_edge"
            if entry["edge"] >= LARGE_EDGE_LOW_CONFIDENCE_THRESHOLD
            else "standard"
        )
    return entries


def build_card(conn, season, week):
    """Returns the full card for one (season, week): every lined game with
    side/edge/confidence (in game_id order -- not ranked by edge, see
    _assign_confidence), a `flagged_large_edge` list surfacing which games
    hit the low-confidence threshold (informational, not a "top picks"
    list), and a `skipped` list (games with no point-in-time stats yet, or
    no line yet) so gaps are visible rather than silently dropped."""
    all_prior = bh.available_seasons_before(conn, season)
    rows, ys = bh.build_training_set(conn, epa.epa_differential, all_prior)
    intercept, coefs = bh.fit_multilinear(rows, ys)

    entries = []
    skipped = []

    for game_id, home_team, away_team, start_date in list_all_games(conn, season, week):
        stats = bh.get_pregame_stats(conn, home_team, away_team, season, week, start_date)
        if stats is None:
            skipped.append({
                "game_id": game_id, "home_team": home_team, "away_team": away_team,
                "reason": "missing_pregame_stats",
            })
            continue

        line = get_latest_line(conn, game_id)
        if line is None:
            skipped.append({
                "game_id": game_id, "home_team": home_team, "away_team": away_team,
                "reason": "no_line",
            })
            continue

        predicted_margin = epa.predict_margin(stats, intercept, coefs)
        # market-implied home margin = -home_spread (home_spread negative
        # means home favored by that many points) -- same convention as
        # backtest_harness.run_walk_forward's edge_home computation.
        market_home_margin = -line["home_spread"]
        edge_home = predicted_margin - market_home_margin
        side = home_team if edge_home > 0 else away_team
        edge = round(abs(edge_home), 2)

        entries.append({
            "game_id": game_id,
            "home_team": home_team,
            "away_team": away_team,
            "start_date": start_date,
            "market_home_spread": line["home_spread"],
            "line_book": line["book"],
            "line_type": line["line_type"],
            "predicted_home_margin": round(predicted_margin, 2),
            "side": side,
            "edge": edge,
        })

    _assign_confidence(entries)

    return {
        "season": season,
        "week": week,
        "model": "epa_only",
        "intercept": round(intercept, 4),
        "coefficient": round(coefs[0], 4),
        "games": entries,
        "flagged_large_edge": [e for e in entries if e["confidence"] == "low_confidence_large_edge"],
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
        card = build_card(conn, args.season, args.week)
    finally:
        conn.close()

    os.makedirs("data/cards", exist_ok=True)
    out_path = f"data/cards/week_{args.week}_{args.season}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(card, f, indent=2)

    print(f"Season {args.season} Week {args.week}: {len(card['games'])} lined games "
          f"({len(card['flagged_large_edge'])} flagged low_confidence_large_edge), "
          f"{len(card['skipped'])} skipped. Saved to {out_path}")


if __name__ == "__main__":
    main()
