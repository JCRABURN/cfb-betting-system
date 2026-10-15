"""
move_signal.py
The stale-number tracker. The pool freezes its lines Tuesday noon and you
submit picks afterward; the market keeps moving. When the market's number
drifts away from the pool's frozen number, the pool's number is mispriced by
the size of the gap, and the side the market moved toward is worth more than
its price. No model is involved -- this is the market correcting the pool.

Why this signal and not another. Historical test, 2021-2025, betting the
direction a line moved from open to close and grading against the stale
OPENING number: 49-54% under 1pt, ~52% at 1-2pts, 56-57% at 2-3pts, 60-61% at
3+pts (ARCHITECTURE.md section 32). That is what theory predicts: outcomes
scatter around the market number with sd ~15.5 points (measured: spread 15.41,
total 15.63), so a g-point gap is worth about Phi(g/15.5) -- 3 points ~57.6%.
Raw pace, by contrast, was already fully priced (section 33).

What this module does:
  * build_signals(): for every game on the pool's slate (`pool_lines`), the
    gap between the frozen pool number and the latest PRE-KICKOFF market
    number, for the spread and the total, the side that gap favors, and a
    win-probability ESTIMATE Phi(|gap|/sigma). Estimate, not a promise: it
    ignores pushes and vig and assumes the market number is unbiased.
  * track_record(): the same signals graded against the POOL number after the
    games finish, bucketed by gap size, with Wilson 95% intervals. This is the
    forward measurement: it starts accumulating now and cannot be gamed by
    hindsight because the pool numbers are stored immutably before the games.

Pre-registered reading rules (set BEFORE any live data, so they can't be
tuned to what shows up):
  * PRIMARY: |gap| >= STRONG_GAP (2.0), spread and total pooled, last
    pre-kickoff observation, one observation per (game, market).
  * Do not act on the live record until n >= 60 AND the lower 95% bound
    exceeds 50%. At the observed rate (~3 qualifying games/week) that is NOT
    reachable this season; expect n of ~20. Until then the weekly list is
    "backed by the 2021-2025 historical test", not "proven live".
  * Weeks 1-2 can be graded retrospectively for SPREADS (the market lines
    exist); totals were not collected before 2026-10-14.

Known limit: "latest pre-kickoff" is the last pull before the game (Tue/Thu/
Sat), which is at or after the moment you pick. The gap you could actually
see when picking is whichever pull preceded your submission, so the observed
gap is an upper-ish bound on what was capturable.
"""

import json
import math
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "data"))
import db
import fetch_stats
from line_utils import get_latest_line, get_latest_total

SPREAD_SIGMA = 15.4     # same measured values as pool_view (1,482 games 2021-2025)
TOTAL_SIGMA = 15.6
STRONG_GAP = 2.0        # pre-registered primary threshold
WATCH_GAP = 1.0
BUCKETS = [("<1", 0.0, 1.0), ("1-2", 1.0, 2.0), ("2-3", 2.0, 3.0), (">=3", 3.0, float("inf"))]
MIN_N_TO_ACT = 60


def phi(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def wilson(wins, n, z=1.96):
    """95% Wilson score interval for a win rate (wins out of n decided)."""
    if n == 0:
        return None, None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def tier_for(abs_gap):
    if abs_gap >= STRONG_GAP:
        return "strong"
    if abs_gap >= WATCH_GAP:
        return "watch"
    return "none"


def spread_signal(pool_home_spread, market_home_spread):
    """negative home spread = home favored. If the market now has home MORE
    favored than the pool's number (gap < 0), the pool's number lets you take
    home at a cheaper price than the market's; the reverse favors away.
    Returns None for no gap."""
    gap = round(market_home_spread - pool_home_spread, 2)
    if gap == 0:
        return None
    return {"market": "spread", "side": "home" if gap < 0 else "away", "gap": gap,
            "abs_gap": abs(gap), "z": abs(gap) / SPREAD_SIGMA}


def total_signal(pool_total, market_total):
    """Market total above the pool's -> the pool's lower number is the
    cheaper Over; below -> the pool's higher number is the cheaper Under."""
    gap = round(market_total - pool_total, 2)
    if gap == 0:
        return None
    return {"market": "total", "side": "over" if gap > 0 else "under", "gap": gap,
            "abs_gap": abs(gap), "z": abs(gap) / TOTAL_SIGMA}


def grade_spread(side, pool_home_spread, home_pts, away_pts):
    """Graded against the FROZEN pool number. Whole-number landing = push."""
    cover = (home_pts - away_pts) + pool_home_spread     # >0: home covers the pool number
    if cover == 0:
        return "push"
    return "win" if (cover > 0) == (side == "home") else "loss"


def grade_total(side, pool_total, total_pts):
    if total_pts == pool_total:
        return "push"
    return "win" if (total_pts > pool_total) == (side == "over") else "loss"


def build_signals(conn, season, week, contest="pool", now=None):
    """One dict per (pool game, market) that has both a pool number and a
    pre-kickoff market number. Sorted: actionable strong signals first, then by
    gap size. `result` is set once the game is final."""
    now_s = (now or datetime.utcnow()).isoformat()[:19]
    rows = conn.execute(
        "SELECT pl.game_id, pl.home_team, pl.away_team, pl.pool_home_spread, pl.pool_total, "
        "g.start_date, g.completed, g.home_points, g.away_points "
        "FROM pool_lines pl LEFT JOIN games g ON g.game_id = pl.game_id "
        "WHERE pl.contest = ? AND pl.season = ? AND pl.week = ?",
        (contest, season, week),
    ).fetchall()

    out = []
    for gid, home, away, pool_spread, pool_total, start, completed, hp, ap in rows:
        kickoff = (start or "")[:19]
        actionable = bool(kickoff) and kickoff > now_s
        final = bool(completed) and hp is not None and ap is not None

        candidates = []
        latest = get_latest_line(conn, gid)
        if latest and latest["home_spread"] is not None:
            sig = spread_signal(pool_spread, latest["home_spread"])
            if sig:
                sig.update(pool_number=pool_spread, market_number=latest["home_spread"],
                           book=latest["book"], line_type=latest["line_type"],
                           take=f"{home} {pool_spread:+.1f}" if sig["side"] == "home"
                           else f"{away} {-pool_spread:+.1f}")
                sig["result"] = grade_spread(sig["side"], pool_spread, hp, ap) if final else None
                candidates.append(sig)
        if pool_total is not None:
            lt = get_latest_total(conn, gid)
            if lt:
                sig = total_signal(pool_total, lt["total"])
                if sig:
                    sig.update(pool_number=pool_total, market_number=lt["total"],
                               book=lt["book"], line_type=lt["line_type"],
                               take=f"{sig['side'].capitalize()} {pool_total:.1f}")
                    sig["result"] = grade_total(sig["side"], pool_total, hp + ap) if final else None
                    candidates.append(sig)

        for sig in candidates:
            sig.update(game_id=gid, season=season, week=week, home_team=home, away_team=away,
                       kickoff=start, actionable=actionable, tier=tier_for(sig["abs_gap"]),
                       p_win_est=round(phi(sig["z"]), 4))
            out.append(sig)

    out.sort(key=lambda s: (not s["actionable"], -s["abs_gap"], s["game_id"]))
    return out


def _bucket_for(abs_gap):
    for label, lo, hi in BUCKETS:
        if lo <= abs_gap < hi:
            return label
    return BUCKETS[-1][0]


def summarize(signals):
    """{bucket: {n, w, l, p, hit, lo, hi}} over GRADED signals only."""
    acc = {label: {"w": 0, "l": 0, "p": 0} for label, _, _ in BUCKETS}
    for s in signals:
        if s["result"] is None:
            continue
        acc[_bucket_for(s["abs_gap"])][{"win": "w", "loss": "l", "push": "p"}[s["result"]]] += 1
    out = {}
    for label, c in acc.items():
        decided = c["w"] + c["l"]
        lo, hi = wilson(c["w"], decided)
        out[label] = {"n": decided + c["p"], **c, "decided": decided,
                      "hit": round(c["w"] / decided, 4) if decided else None,
                      "lo": round(lo, 4) if lo is not None else None,
                      "hi": round(hi, 4) if hi is not None else None}
    return out


def primary(signals):
    """The pre-registered primary read: |gap| >= STRONG_GAP, markets pooled."""
    graded = [s for s in signals if s["result"] is not None and s["abs_gap"] >= STRONG_GAP]
    w = sum(1 for s in graded if s["result"] == "win")
    l = sum(1 for s in graded if s["result"] == "loss")
    p = len(graded) - w - l
    lo, hi = wilson(w, w + l)
    return {"n": len(graded), "w": w, "l": l, "p": p,
            "hit": round(w / (w + l), 4) if w + l else None,
            "lo": round(lo, 4) if lo is not None else None,
            "hi": round(hi, 4) if hi is not None else None,
            "actionable_threshold_met": (w + l) >= MIN_N_TO_ACT and lo is not None and lo > 0.5,
            "min_n_to_act": MIN_N_TO_ACT}


def build_report(conn, season, week, contest="pool", now=None):
    weeks = [r[0] for r in conn.execute(
        "SELECT DISTINCT week FROM pool_lines WHERE contest = ? AND season = ? ORDER BY week",
        (contest, season))]
    current = build_signals(conn, season, week, contest, now)
    history = []
    for w in weeks:
        history += build_signals(conn, season, w, contest, now)
    by_market = {m: summarize([s for s in history if s["market"] == m]) for m in ("spread", "total")}
    n_pool = conn.execute(
        "SELECT COUNT(*) FROM pool_lines WHERE contest = ? AND season = ? AND week = ?",
        (contest, season, week)).fetchone()[0]
    return {
        "season": season, "week": week, "generated_at": (now or datetime.utcnow()).isoformat(),
        "thresholds": {"strong": STRONG_GAP, "watch": WATCH_GAP},
        "current": [s for s in current if s["tier"] != "none"],
        "n_pool_games_this_week": n_pool,
        "n_games_with_any_gap": len({s["game_id"] for s in current}),
        "totals_collected": any(s["market"] == "total" for s in history),
        "record": {"all": summarize(history), **by_market},
        "primary": primary(history),
        "weeks_in_record": weeks,
    }


def main():
    with db.log_run("move_signal") as run:
        week, season = fetch_stats.get_current_week()
        conn = db.get_connection()
        try:
            report = build_report(conn, season, week)
        finally:
            conn.close()

        os.makedirs("data/line_views", exist_ok=True)
        path = f"data/line_views/move_signal_week_{week}_{season}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        run["rows_added"] = len(report["current"])
        strong = [s for s in report["current"] if s["tier"] == "strong" and s["actionable"]]
        print(f"Season {season} Week {week}: {report['n_pool_games_this_week']} pool games, "
              f"{report['n_games_with_any_gap']} with any gap, "
              f"{len(report['current'])} signals >= {WATCH_GAP} ({len(strong)} strong & still upcoming). "
              f"Saved to {path}")
        if not report["totals_collected"]:
            print("  (no total signals yet: live totals have not been collected for any slate game)")
        for s in strong:
            print(f"  {s['away_team']} @ {s['home_team']}: {s['market']} gap {s['gap']:+.1f} "
                  f"-> {s['take']}  (est. {s['p_win_est']:.0%}, {s['book']})")
        p = report["primary"]
        print(f"Graded record (weeks {report['weeks_in_record']}), |gap|>={STRONG_GAP}: "
              f"{p['w']}-{p['l']}-{p['p']} "
              f"(n={p['n']}; actionable only at n>={MIN_N_TO_ACT} with lower bound >50%).")


if __name__ == "__main__":
    main()
