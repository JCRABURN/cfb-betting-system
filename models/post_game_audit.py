"""
post_game_audit.py
Monday audit: fetches final scores for the just-completed week, grades
every pending pick recorded by card_generator.py's persist_picks_to_db(),
computes CLV and flags hooks, and settles everything in the DB. Replaces
update_results.py (deleted) and its docs/data/all_picks.json + weekly
weight-retraining machinery, both tied to the abandoned pre-EPA-only
weighted model.

Reuses backtest_harness.py's grading primitives (grade_ats, unit_pl,
calculate_clv, get_final_score) -- the exact same functions the
walk-forward backtest itself uses, so a live pick is graded under
identical rules to every historical pick, not a second, separately
maintained implementation with its own sign-convention risk.

CLV uses line_utils.get_latest_line(), not backtest_harness.get_closing_line():
the live path never writes line_type='closing' (only 'opening'/'current' --
that vocabulary is specific to the historical backfill), so get_closing_line
would always return None here. By Monday, the latest 'current' pull before
kickoff is the closing-line stand-in, same reasoning as card_generator.py's
own get_latest_line() usage. This does NOT enforce same-book matching against
the pick's own line (the `picks` table doesn't record which book a pick's
line came from) -- a known simplification versus gambling_view.py's stricter
same-book discipline.

Hook detection: a pick decided by exactly half a point -- the smallest
margin a half-point spread allows ("won/lost by the hook") -- computed
purely from the final score and the line the pick was made against, no new
data needed.

Backdoor-cover detection (a garbage-time score that flips an otherwise-
decided cover) is NOT implemented here -- it needs play-by-play/scoring-
drive timestamps CFBD exposes via a separate endpoint this project hasn't
ingested. Flagging that gap explicitly rather than faking a heuristic for
something this data can't actually show.
"""

import os
import sys
import json
import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "data"))
import db
import fetch_stats
import backtest_harness as bh
from line_utils import get_latest_line

CFBD_BASE = fetch_stats.CFBD_BASE
HEADERS = fetch_stats.HEADERS


def fetch_final_scores(year, week):
    """Final scores for every FBS game in (year, week), keyed by CFBD
    game_id. Same camelCase field mapping already verified live elsewhere
    in this project (homePoints/awayPoints, classification='fbs')."""
    resp = requests.get(
        f"{CFBD_BASE}/games", headers=HEADERS,
        params={"year": year, "week": week, "classification": "fbs"}, timeout=30,
    )
    resp.raise_for_status()
    return {g["id"]: g for g in resp.json()}


def persist_final_scores(conn, scores):
    """Writes home_points/away_points/completed=1 into `games` for every
    game CFBD reports as finished. A game not yet final (homePoints is
    None -- postponed, or CFBD hasn't posted the box score yet) is left
    untouched; grading skips it via backtest_harness.get_final_score's
    existing completed=1 check, not treated as an error."""
    updated = 0
    for game_id, g in scores.items():
        home_pts = g.get("homePoints")
        away_pts = g.get("awayPoints")
        if home_pts is None or away_pts is None:
            continue
        conn.execute(
            "UPDATE games SET home_points = ?, away_points = ?, completed = 1 WHERE game_id = ?",
            (home_pts, away_pts, game_id),
        )
        updated += 1
    conn.commit()
    return updated


def is_hook(spread, covered_margin):
    """True if the pick was decided by exactly half a point -- the
    smallest possible margin a half-point spread allows. Only meaningful
    when the line itself carries a .5; a whole-number line can push
    exactly, which grade_ats already reports as 'push', not a near-miss."""
    has_half_point = abs(spread * 2) % 2 == 1
    return has_half_point and abs(covered_margin) == 0.5


def grade_pending_picks(conn, season, week):
    """Grades every pending pick for (season, week): result, CLV, hook
    flag. A pick whose game isn't final yet is left pending, not treated
    as an error -- an in-progress or postponed game simply has no final
    score yet."""
    pending = conn.execute(
        "SELECT id, game_id, home_team, away_team, consensus_spread, recommended_side "
        "FROM picks WHERE week = ? AND year = ? AND pick_type = 'live' AND status = 'pending'",
        (week, season),
    ).fetchall()

    graded = 0
    hooks = 0
    for pick_id, game_id, home_team, away_team, spread, side in pending:
        final = bh.get_final_score(conn, game_id)
        if final is None:
            continue

        result = bh.grade_ats(side, home_team, away_team, spread, final["home_points"], final["away_points"])
        pl = bh.unit_pl(result)

        # Mirrors grade_ats's own covered_margin computation exactly (see
        # module docstring for why this isn't refactored to share code:
        # grade_ats's return signature is used throughout the backtest
        # harness and every feature test, and changing it to also return
        # the raw margin would ripple through all of them for one caller).
        actual_margin = final["home_points"] - final["away_points"]
        covered_margin = (actual_margin + spread) if side == home_team else -(actual_margin + spread)
        hook = is_hook(spread, covered_margin)

        latest = get_latest_line(conn, game_id)
        clv = bh.calculate_clv(side, home_team, spread, latest["home_spread"]) if latest else None

        conn.execute(
            "UPDATE picks SET result = ?, clv = ?, unit_pl = ?, status = 'settled', "
            "key_factors = ? WHERE id = ?",
            (result, clv, pl, json.dumps(["hook"] if hook else []), pick_id),
        )
        graded += 1
        hooks += int(hook)
    conn.commit()
    return graded, hooks


def main():
    with db.log_run("post_game_audit") as run:
        week, season = fetch_stats.get_current_week()

        scores = fetch_final_scores(season, week)
        conn = db.get_connection()
        try:
            scores_updated = persist_final_scores(conn, scores)
            graded, hooks = grade_pending_picks(conn, season, week)
        finally:
            conn.close()

        run["rows_added"] = graded
        print(f"Season {season} Week {week}: {scores_updated} game(s) scored, "
              f"{graded} pick(s) graded ({hooks} decided by a hook).")


if __name__ == "__main__":
    main()
