"""
card_generator.py
Builds the weekly card: every lined FBS game for one (season, week), with
the EPA-only model's predicted side, edge (points), and an edge-ranked
confidence tier (1-5) -- plus the top 5 games by edge. This is the single
shared output both eventual contest consumers (SplashSports, pick'em pool)
will read from later; it does not encode either contest's own rules (entry
format, scoring, unit sizing) -- just the model's view of every game.

Reuses the SAME fitted model as the validated backtest (backtest_harness.py
+ baseline_epa.py) -- intercept+coefficient fit via OLS on seasons strictly
before the target season, predictions built from get_team_stats_as_of's
point-in-time snapshots. No new model, no new fitting logic: this is a
different consumer of the same walk-forward machinery, not a fork of it.

Two deliberate departures from backtest_harness.py, both because a card and
a backtest answer different questions:

1. Games are NOT filtered to completed=1 (backtest_harness.list_games()
   only grades finished games; a card is for games that haven't been played
   yet). See list_all_games() below.
2. Edge is computed against the LATEST available line, not the opening line
   (get_latest_line() below), because a card informs a bet placed NOW,
   against today's number -- not a backtest's fixed historical entry point.
   The live in-season path (fetch_odds.py) writes line_type='current' for
   this; the historical archive instead uses 'closing' for the same concept
   (confirmed live: 2024 week 10 has 0 'current' rows, only 'opening' and
   'closing') -- get_latest_line() tries both so this one function works
   unmodified against either data source.

IMPORTANT: testing this against a historical week only validates the CARD
LOGIC (format, that confidence tracks edge, that ranking is correct) against
known data. It does NOT make a current-season card trustworthy -- that
still depends on the live weekly fetch path (see ARCHITECTURE.md §18's
Week 0/1 verification items), which is a separate, not-yet-verified concern.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import backtest_harness as bh
import baseline_epa as epa


def list_all_games(conn, season, week):
    """Every FBS game scheduled for (season, week), regardless of whether
    it's been played yet -- unlike backtest_harness.list_games(), which
    intentionally only returns completed games (correct for grading, wrong
    for a card: an upcoming game is exactly what a card exists to cover)."""
    return conn.execute(
        "SELECT game_id, home_team, away_team, start_date FROM games "
        "WHERE season = ? AND week = ? ORDER BY game_id",
        (season, week),
    ).fetchall()


def get_latest_line(conn, game_id):
    """The most recent available market line for a game: 'current' if the
    live in-season path has written one, else 'closing' (the historical
    archive's term for the same concept -- the last number seen before
    kickoff). Same consensus-then-any-book fallback as
    backtest_harness.get_opening_line()/get_closing_line(), tried across
    both line_type values in turn. Returns None if neither exists (not
    lined yet, or a team-name join failure left the row unreachable)."""
    for line_type in ("current", "closing"):
        row = conn.execute(
            "SELECT home_spread, total FROM betting_lines "
            "WHERE game_id = ? AND line_type = ? AND book = 'consensus'",
            (game_id, line_type),
        ).fetchone()
        if row is not None and row[0] is not None:
            return {"home_spread": row[0], "total": row[1], "book": "consensus", "line_type": line_type}

        row = conn.execute(
            "SELECT home_spread, total, book FROM betting_lines "
            "WHERE game_id = ? AND line_type = ? AND home_spread IS NOT NULL "
            "ORDER BY book LIMIT 1",
            (game_id, line_type),
        ).fetchone()
        if row is not None:
            return {"home_spread": row[0], "total": row[1], "book": row[2], "line_type": line_type}
    return None


def _assign_confidence(entries):
    """Edge-ranked confidence: sort by edge descending, split into quintiles
    by RANK POSITION within this week's own slate -- not a fixed point
    threshold. Adapts to each week's actual edge distribution instead of
    hardcoding magnitude cutoffs that were never validated (unlike the
    pre-EPA-only spread_model.py's tiered unit-sizing, deliberately not
    reused here). Tie-broken by game_id for determinism. Mutates and
    returns `entries`, now sorted."""
    entries.sort(key=lambda e: (-e["edge"], e["game_id"]))
    n = len(entries)
    for i, entry in enumerate(entries):
        entry["confidence"] = 5 - min(4, (i * 5) // n)
    return entries


def build_card(conn, season, week):
    """Returns the full card for one (season, week): every lined game with
    side/edge/confidence, the top 5 by edge, and a `skipped` list (games
    with no point-in-time stats yet, or no line yet) so gaps are visible
    rather than silently dropped."""
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
        "top5": entries[:5],
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

    print(f"Season {args.season} Week {args.week}: {len(card['games'])} lined games, "
          f"{len(card['skipped'])} skipped. Saved to {out_path}")


if __name__ == "__main__":
    main()
