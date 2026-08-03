"""
card_generator.py
Builds the weekly card: every lined FBS game for one (season, week), with
the EPA-only model's predicted side, edge (points), and a confidence flag
(see _assign_confidence below -- NOT a graduated ranking by edge size, see
why). This is the single shared output both eventual contest consumers
(SplashSports, pick'em pool) will read from later; it does not encode
either contest's own rules (entry format, scoring, unit sizing) -- just the
model's view of every game.

Week 1 games use prior-season-final EPA as a fallback input (no in-season
data can exist yet -- see backtest_harness.get_prior_season_final_stats(),
per MODEL_DESIGN.md §6's week 1 decision). Every such game is explicitly
marked `uses_prior_season_data: true` and confidence-capped to
"low_confidence_prior_season_data" (never "standard"), and surfaced in
`flagged_prior_season_data` -- visible on the dashboard, not silent.

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

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "data"))
import db
import fetch_stats
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
    games.

    Week 1 games get a THIRD, distinct state -- "low_confidence_prior_season_data"
    -- and it takes priority over the edge check, per MODEL_DESIGN.md §6's
    week 1 decision: "prior season's final EPA as a rough prior... with
    confidence heavily capped." This is a different reason for caution than
    a large edge (data provenance -- roster turnover, transfer portal, over
    an offseason -- not extrapolation risk), so a week 1 game can never
    read as "standard" regardless of how small its edge looks; the input
    feeding that edge is already known-weak. Does NOT reorder `entries` --
    neither edge size nor data provenance is a quality ranking, so there's
    nothing to sort by."""
    for entry in entries:
        if entry["uses_prior_season_data"]:
            entry["confidence"] = "low_confidence_prior_season_data"
        elif entry["edge"] >= LARGE_EDGE_LOW_CONFIDENCE_THRESHOLD:
            entry["confidence"] = "low_confidence_large_edge"
        else:
            entry["confidence"] = "standard"
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

        # Explicit and visible, not inferred from the confidence label alone
        # (MODEL_DESIGN.md §6: week 1 must be visibly flagged, not silent) --
        # true whenever EITHER team's stats came from
        # backtest_harness.get_prior_season_final_stats() (week 1 only).
        uses_prior_season_data = (
            stats["home_stats"]["is_prior_season_fallback"]
            or stats["away_stats"]["is_prior_season_fallback"]
        )

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
            "uses_prior_season_data": uses_prior_season_data,
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
        "flagged_prior_season_data": [e for e in entries if e["uses_prior_season_data"]],
        "skipped": skipped,
    }


def persist_picks_to_db(conn, card):
    """Insert a pending `picks` row per card game, for post_game_audit.py to
    grade once the week's games finish. Idempotent: skips any game that
    already has a pick_type='live' row (pending or settled) for this
    game_id, so re-running the card generator mid-week doesn't duplicate.

    Reuses the existing `picks` table (designed for the pre-EPA-only
    weighted model) pragmatically rather than migrating the schema:
    consensus_spread <- market_home_spread, projected_spread <-
    predicted_home_margin, recommended_side <- side, and the new
    confidence flag is stored in confidence_signals (declared as a
    JSON-encoded list; holds a 1-item list here, e.g. ["standard"]).
    units/key_factors/weather/risk_flags/qualifies don't apply to this
    model and are left at their inapplicable defaults (0/empty/NULL/False)
    rather than populated with invented values."""
    import json
    from datetime import datetime

    now = datetime.utcnow().isoformat()
    rows_added = 0
    for game in card["games"]:
        exists = conn.execute(
            "SELECT 1 FROM picks WHERE game_id = ? AND pick_type = 'live'",
            (game["game_id"],),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """
            INSERT INTO picks (
                game_id, week, year, home_team, away_team,
                consensus_spread, projected_spread, edge, recommended_side,
                units, confidence_signals, key_factors, line_movement, weather,
                risk_flags, qualifies, status, pick_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, '[]', NULL, '{}', '[]', 0, 'pending', 'live', ?)
            """,
            (
                game["game_id"], card["week"], card["season"],
                game["home_team"], game["away_team"],
                game["market_home_spread"], game["predicted_home_margin"], game["edge"],
                game["side"], json.dumps([game["confidence"]]), now,
            ),
        )
        rows_added += 1
    conn.commit()
    return rows_added


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=None,
                         help="Defaults to the current week's season via fetch_stats.get_current_week()")
    parser.add_argument("--week", type=int, default=None,
                         help="Defaults to the current week via fetch_stats.get_current_week()")
    args = parser.parse_args()

    with db.log_run("card_generator") as run:
        season, week = args.season, args.week
        if season is None or week is None:
            week, season = fetch_stats.get_current_week()

        conn = db.get_connection()
        try:
            card = build_card(conn, season, week)
            run["rows_added"] = persist_picks_to_db(conn, card)
        finally:
            conn.close()

        os.makedirs("data/cards", exist_ok=True)
        out_path = f"data/cards/week_{week}_{season}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(card, f, indent=2)

        print(f"Season {season} Week {week}: {len(card['games'])} lined games "
              f"({len(card['flagged_large_edge'])} flagged low_confidence_large_edge), "
              f"{len(card['skipped'])} skipped, {run['rows_added']} new picks persisted. "
              f"Saved to {out_path}")


if __name__ == "__main__":
    main()
