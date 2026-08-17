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

Also grades the user's OWN pool picks (grade_contest_entries(), added
2026-08-13) -- separate from the picks table above, straight ATS against
each pick's locked_home_spread, broken out by the 1-5 confidence rank
recorded in contest_entries.rank at pick time (see pool_view.py). The
question this answers: does a higher self-rated confidence rank actually
predict a better outcome over a season, or is it noise -- rather than
that being a gut impression revisited from memory.
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

# Same /games endpoint fetch_stats.fetch_games() calls, but a distinct
# parser_version -- this call is parsed for homePoints/awayPoints/
# completed, a different downstream shape than fetch_stats's own use of
# the same raw response (external review, accepted 2026-08-04).
PARSER_VERSION = "post_game_audit.v1"


def fetch_final_scores(year, week, conn=None):
    """Final scores for every FBS game in (year, week), keyed by CFBD
    game_id. Same camelCase field mapping already verified live elsewhere
    in this project (homePoints/awayPoints, classification='fbs').
    `conn`, if given, archives the raw response before raise_for_status()
    (external review, accepted 2026-08-04), so an error response is
    captured too, not just a successful one."""
    params = {"year": year, "week": week, "classification": "fbs"}
    resp = requests.get(f"{CFBD_BASE}/games", headers=HEADERS, params=params, timeout=30)
    payload_id = None
    if conn is not None:
        payload_id = db.archive_raw_payload(conn, "cfbd", "/games", params,
                                             resp.text, resp.status_code, PARSER_VERSION)
    resp.raise_for_status()
    games = resp.json()
    if payload_id is not None:
        db.update_raw_payload_counts(conn, payload_id, rows_accepted=len(games))
    return {g["id"]: g for g in games}


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


def grade_contest_entries(conn, season, contest=None):
    """Grades every contest_entries pick for `season` (optionally scoped to
    one `contest`; None means every contest for that season) whose game is
    complete, reusing backtest_harness.grade_ats() -- the exact same ATS
    grading rules the model's own live picks and the historical backtest
    both use, so a pool pick is graded under identical rules, not a
    second, separately maintained implementation with its own
    sign-convention risk (added 2026-08-13).

    Straight ATS grading against the LOCKED spread -- this answers "did
    the picked side cover the number actually locked at pick time," not a
    CLV comparison against a later line the way grade_pending_picks does
    for the model's own picks. There's no CLV concept for a pool pick:
    the pool's own printed number at lock time IS the bet, not a market
    entry point to be compared against where the line moved afterward.

    Returns {"overall": {...}, "by_rank": {1: {...}, ..., 5: {...}, None:
    {...}}} -- each bucket {"win", "loss", "push", "n", "win_pct"}, where
    win_pct excludes pushes from the denominator (matching
    build_dashboard.build_season_ledger's existing ats_pct convention).
    `by_rank`'s whole reason to exist: whether a rank-1 (least confident)
    pick actually performs differently than a rank-5 (most confident) one
    over a season, rather than that question being answered from memory.
    Only rank buckets that actually appear in the graded data are
    included -- no synthetic zero-rows for a rank never used. A game not
    yet final (games.completed=0, or no games row at all -- the JOIN
    simply excludes it) is left out, same as grade_pending_picks -- not
    an error, just nothing to grade yet."""
    query = (
        "SELECT ce.picked_side, ce.normalized_home_team, ce.normalized_away_team, "
        "ce.locked_home_spread, ce.rank, g.home_points, g.away_points "
        "FROM contest_entries ce "
        "JOIN games g ON g.game_id = ce.game_id "
        "WHERE ce.season = ? AND g.completed = 1"
    )
    params = [season]
    if contest is not None:
        query += " AND ce.contest = ?"
        params.append(contest)

    rows = conn.execute(query, params).fetchall()

    def _new_bucket():
        return {"win": 0, "loss": 0, "push": 0}

    overall = _new_bucket()
    by_rank = {}
    for picked_side, home, away, spread, rank, home_pts, away_pts in rows:
        if home_pts is None or away_pts is None:
            continue
        result = bh.grade_ats(picked_side, home, away, spread, home_pts, away_pts)
        overall[result] += 1
        by_rank.setdefault(rank, _new_bucket())[result] += 1

    def _finalize(bucket):
        decided = bucket["win"] + bucket["loss"]
        return {
            **bucket,
            "n": bucket["win"] + bucket["loss"] + bucket["push"],
            "win_pct": bucket["win"] / decided if decided else None,
        }

    return {
        "overall": _finalize(overall),
        "by_rank": {rank: _finalize(bucket) for rank, bucket in by_rank.items()},
    }


def format_rank_report(report):
    """Human-readable "Rank 5: 4-1-0 (80.0%)" lines for the Monday audit's
    console output, ranks descending (5 down to 1) then an "Unranked"
    line for rank=None, then the overall total."""
    lines = []
    numeric_ranks = sorted((r for r in report["by_rank"] if r is not None), reverse=True)
    for rank in numeric_ranks:
        b = report["by_rank"][rank]
        pct = f"{b['win_pct'] * 100:.1f}%" if b["win_pct"] is not None else "n/a"
        lines.append(f"  Rank {rank}: {b['win']}-{b['loss']}-{b['push']} ({pct})")
    if None in report["by_rank"]:
        b = report["by_rank"][None]
        pct = f"{b['win_pct'] * 100:.1f}%" if b["win_pct"] is not None else "n/a"
        lines.append(f"  Unranked: {b['win']}-{b['loss']}-{b['push']} ({pct})")
    o = report["overall"]
    pct = f"{o['win_pct'] * 100:.1f}%" if o["win_pct"] is not None else "n/a"
    lines.append(f"  Overall: {o['win']}-{o['loss']}-{o['push']} ({pct})")
    return "\n".join(lines)


def main():
    with db.log_run("post_game_audit") as run:
        conn = db.get_connection()
        try:
            week, season = fetch_stats.get_current_week(conn=conn)
            scores = fetch_final_scores(season, week, conn=conn)
            scores_updated = persist_final_scores(conn, scores)
            graded, hooks = grade_pending_picks(conn, season, week)
            rank_report = grade_contest_entries(conn, season)
        finally:
            conn.close()

        run["rows_added"] = graded
        print(f"Season {season} Week {week}: {scores_updated} game(s) scored, "
              f"{graded} pick(s) graded ({hooks} decided by a hook).")

        if rank_report["overall"]["n"]:
            print(f"\nContest pool performance by rank (season {season}, "
                  f"{rank_report['overall']['n']} decided pick(s) so far):")
            print(format_rank_report(rank_report))


if __name__ == "__main__":
    main()
