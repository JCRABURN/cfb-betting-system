"""
blended_ranker.py
One ranked top 5 across BOTH markets (spread sides and over/unders), built the
way the pool actually scores: five picks, rank-weighted, 5 points for the
most confident through 1 point, 15 available per week.

Every candidate is reduced to a single number, its estimated probability of
winning against the POOL's frozen line, and the five best are taken and
assigned points in descending order of that probability (for a
rank-weighted pool, putting the highest-probability pick in the 5-point slot
maximizes expected points; this is the rearrangement inequality, not a
heuristic).

Where each probability comes from, and why they are not treated equally
-------------------------------------------------------------------------
  market gap   The pool froze its number Tuesday noon; the market has since
               moved. p = Phi(|gap| / sigma), sigma ~15.5 pts (measured:
               spread 15.41, total 15.63). Backed by the 2021-2025 open->close
               test (56-57% at 2-3pt, ~60% at 3+pt) and consistent with it.
               Spreads AND totals.
  spread model p comes from MODEL_EDGE_CALIBRATION: the walk-forward hit rate
               of the EPA-differential baseline by |edge| bucket. It is ~50-51%
               in every bucket (ARCHITECTURE.md sections 19-20, 33). The model
               therefore supplies candidates but almost never outranks a market
               gap, and that is the correct behavior, not a bug.

Hard rules
----------
  * The model CANNOT override a market gap. If both exist for a game's spread
    the gap decides side and probability; the model's side is carried as
    `model_agrees` for information only. Its backtest skill does not justify
    a vote.
  * Model picks flagged low_confidence_large_edge / no_pick_extrapolation are
    never candidates (the project's standing doctrine; the edge >= 10 bucket
    is its least reliable slice).
  * Only games that have not kicked off are candidates.
  * At most one candidate per (game, market) and at most MAX_PER_GAME per
    game. Correlation between a game's spread and total is NOT modeled.
  * Everything is graded against the pool's number, never the market's.

What the summary numbers mean
-----------------------------
expected_correct / expected_points / p_perfect / p_ge4 / p_ge3 assume the
five outcomes are INDEPENDENT and the estimates are right. They are an
honest ceiling on how good a week can be expected to look, not a forecast
of a particular result. With estimates in the 50-55% range, a perfect 5 is a
low-single-digit-percent event, and the output says so.
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
import move_signal as ms
from line_utils import _kickoff_cutoff

TOP_N = 5
MAX_PER_GAME = 2
EXCLUDED_MODEL_FLAGS = {"low_confidence_large_edge", "no_pick_extrapolation"}
WEAK_P = 0.526          # == Phi(1.0 / 15.5): below a 1-point gap, a "filler", not a signal

# The EPA-differential baseline's walk-forward hit rate, 2021-2025, current
# CFBD vintage (2026-10-15): 1840-1806 on 3,646 decided games = 50.47%.
#
# DELIBERATELY ONE POOLED NUMBER, not a per-edge-bucket table. The raw
# bucket hit rates are non-monotonic noise (|edge| 0-1: 46.3%, 1-2: 50.1%,
# 2-3: 48.5%, 3-5: 53.0%, 5-7: 53.9%, 7-10: 47.6%, 10+: 51.1%; n = 389-904
# each, standard error ~2-2.5 points). A chi-square test of "all buckets share
# one rate" gives chi2 ~ 9.0 on 6 df, p ~ 0.17: no evidence a larger edge means
# a higher hit rate. Using the raw buckets would let a 5-7pt model pick (53.9%)
# outrank a genuine 1.0-point market gap (52.6%) purely through sampling noise.
# Shrinking every bucket to the pooled rate is the statistically honest
# calibration, and it means model picks never outrank a market-gap signal.
# (lo, hi, hit, decided). Replace by re-running the calibration, not by hand.
MODEL_EDGE_CALIBRATION = [
    (0.0, None, 0.5047, 3646),
]
_DEFAULT_MODEL_P = 0.5047   # pooled baseline hit rate when no bucket matches


def model_pick_p(abs_edge):
    for lo, hi, hit, _n in MODEL_EDGE_CALIBRATION:
        if abs_edge >= lo and (hi is None or abs_edge < hi):
            return hit
    return _DEFAULT_MODEL_P


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------

def outcome_distribution(ps):
    """P(exactly k of the picks win), k = 0..len(ps), independent picks."""
    dist = [1.0]
    for p in ps:
        nxt = [0.0] * (len(dist) + 1)
        for k, v in enumerate(dist):
            nxt[k] += v * (1 - p)
            nxt[k + 1] += v * p
        dist = nxt
    return dist


def summarize_picks(picks):
    """picks: list of dicts with `p_win_est` and `points`."""
    ps = [x["p_win_est"] for x in picks]
    dist = outcome_distribution(ps)
    n = len(ps)
    return {
        "n_picks": n,
        "expected_correct": round(sum(ps), 3),
        "expected_points": round(sum(x["p_win_est"] * x["points"] for x in picks), 3),
        "max_points": sum(x["points"] for x in picks),
        "p_perfect": round(dist[n], 4) if n else None,
        "p_ge4": round(sum(dist[4:]), 4) if n >= 4 else None,
        "p_ge3": round(sum(dist[3:]), 4) if n >= 3 else None,
    }


def select_top(candidates, top_n=TOP_N, max_per_game=MAX_PER_GAME):
    """Greedy by probability with the per-game cap. Deterministic: ties break
    on the larger gap (or, for model fillers, the larger model edge -- which
    carries no skill, it just makes the order reproducible), then game_id,
    then market name."""
    ordered = sorted(candidates, key=lambda c: (-c["p_win_est"],
                                                -(c.get("abs_gap") or c.get("model_edge") or 0.0),
                                                c["game_id"], c["market"]))
    chosen, per_game = [], {}
    for c in ordered:
        if per_game.get(c["game_id"], 0) >= max_per_game:
            continue
        chosen.append(dict(c))
        per_game[c["game_id"]] = per_game.get(c["game_id"], 0) + 1
        if len(chosen) == top_n:
            break
    for i, c in enumerate(chosen):
        c["points"] = top_n - i
        c["rank"] = i + 1
    return chosen


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

def _snapshot_count(conn, game_id, market):
    """Distinct pre-kickoff pulls that carried this market for this game. A
    gap resting on ONE snapshot cannot be told apart from a permanent
    difference between the pool's source and ours (ARCHITECTURE.md s.34)."""
    col = "total" if market == "total" else "home_spread"
    cutoff = _kickoff_cutoff(conn, game_id)
    sql = (f"SELECT COUNT(DISTINCT fetched_at) FROM betting_lines WHERE game_id = ? "
           f"AND line_type = 'current' AND book != 'consensus' AND {col} IS NOT NULL")
    params = [game_id]
    if cutoff:
        sql += " AND substr(fetched_at, 1, 19) < ?"
        params.append(cutoff)
    return conn.execute(sql, params).fetchone()[0]


def build_candidates(conn, season, week, card=None, contest="pool", now=None):
    now_s = (now or datetime.utcnow()).isoformat()[:19]

    # 1. market-gap candidates (spread + total), upcoming games only
    gaps = [s for s in ms.build_signals(conn, season, week, contest, now) if s["actionable"]]
    cands = []
    spread_gap_games = set()
    for s in gaps:
        if s["market"] == "spread":
            spread_gap_games.add(s["game_id"])
        side = s["side"]
        pick_text = s["take"]
        cands.append({
            "game_id": s["game_id"], "away_team": s["away_team"], "home_team": s["home_team"],
            "market": s["market"], "side": side, "pick_text": pick_text,
            "pool_number": s["pool_number"], "market_number": s["market_number"],
            "gap": s["gap"], "abs_gap": s["abs_gap"], "p_win_est": s["p_win_est"],
            "source": "market_gap", "tier": "signal" if s["p_win_est"] >= WEAK_P else "weak",
            "book": s["book"], "kickoff": s["kickoff"], "model_side": None, "model_agrees": None,
            "single_snapshot": _snapshot_count(conn, s["game_id"], s["market"]) <= 1,
        })

    # 2. spread-model candidates against the POOL number
    if card:
        by_game = {g["game_id"]: g for g in card["games"]}
        rows = conn.execute(
            "SELECT pl.game_id, pl.home_team, pl.away_team, pl.pool_home_spread, g.start_date "
            "FROM pool_lines pl LEFT JOIN games g ON g.game_id = pl.game_id "
            "WHERE pl.contest = ? AND pl.season = ? AND pl.week = ?", (contest, season, week)).fetchall()
        by_cand = {(c["game_id"], c["market"]): c for c in cands}
        for gid, home, away, pool_spread, start in rows:
            g = by_game.get(gid)
            if g is None or g["confidence"] in EXCLUDED_MODEL_FLAGS:
                continue
            if not start or start[:19] <= now_s:
                continue
            # model's view of the HOME margin vs the pool's frozen number
            edge_home = g["predicted_home_margin"] + pool_spread
            if edge_home == 0:
                continue
            model_side = "home" if edge_home > 0 else "away"
            abs_edge = abs(edge_home)
            existing = by_cand.get((gid, "spread"))
            if existing is not None:
                existing["model_side"] = model_side
                existing["model_agrees"] = (model_side == existing["side"])
                continue                      # the market gap already decides this game's spread
            take = (f"{home} {pool_spread:+.1f}" if model_side == "home" else f"{away} {-pool_spread:+.1f}")
            cands.append({
                "game_id": gid, "away_team": away, "home_team": home, "market": "spread",
                "side": model_side, "pick_text": take, "pool_number": pool_spread,
                "market_number": None, "gap": 0.0, "abs_gap": 0.0,
                "p_win_est": round(model_pick_p(abs_edge), 4), "source": "spread_model",
                "tier": "weak", "book": None, "kickoff": start, "model_side": model_side,
                "model_agrees": True, "model_edge": round(abs_edge, 2), "single_snapshot": None,
            })
    return cands


# ---------------------------------------------------------------------------
# Report, suggestion CSV, log, grading
# ---------------------------------------------------------------------------

def build_blend(conn, season, week, card=None, contest="pool", now=None):
    cands = build_candidates(conn, season, week, card=card, contest=contest, now=now)
    picks = select_top(cands)
    by_source = {}
    for c in cands:
        by_source[c["source"]] = by_source.get(c["source"], 0) + 1
    warnings = []
    if not cands:
        warnings.append("No candidates: this week's pool slate has not been ingested, or every game has started.")
    elif len(picks) < TOP_N:
        warnings.append(f"Only {len(picks)} candidates available; fewer than {TOP_N} picks.")
    if any(p.get("single_snapshot") for p in picks):
        warnings.append("Some picks rest on a single market snapshot, so a gap there cannot yet be told apart "
                        "from a permanent source difference between the pool and our books.")
    weak = sum(1 for p in picks if p["tier"] == "weak")
    if weak:
        warnings.append(f"{weak} of {len(picks)} picks are weak (estimated win under {WEAK_P:.1%}): "
                        "effectively coin flips used to fill the slate.")
    return {
        "season": season, "week": week, "generated_at": (now or datetime.utcnow()).isoformat(),
        "picks": picks, "summary": summarize_picks(picks), "n_candidates": len(cands),
        "candidates_by_source": by_source, "warnings": warnings,
        "calibration_buckets": len(MODEL_EDGE_CALIBRATION),
    }


def suggestion_rows(picks):
    """Rows in the pool CSV format (pool_view.load_pool_entries). The file is
    named *_suggested_top5.csv on purpose: pool_view's default
    week_{N}_{season}.csv is AUTO-INGESTED as locked picks, and a suggestion
    must never become a locked entry by accident."""
    rows = []
    for p in picks:
        total = p["market"] == "total"
        rows.append({
            "game_id": p["game_id"], "home_team": p["home_team"], "away_team": p["away_team"],
            "market_type": p["market"],
            "pool_home_spread": "" if total else p["pool_number"],
            "locked_total": p["pool_number"] if total else "",
            "picked_side": p["side"] if total else (p["home_team"] if p["side"] == "home" else p["away_team"]),
            "rank": p["points"],
        })
    return rows


def write_suggestion_csv(report, path):
    import csv
    cols = ["game_id", "home_team", "away_team", "market_type", "pool_home_spread", "locked_total",
            "picked_side", "rank"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in suggestion_rows(report["picks"]):
            w.writerow(r)


def log_blend(conn, report):
    """Append this snapshot's picks to blend_log (append-only). Past
    suggestions are not reproducible later (the market keeps moving), so they
    are stored at the time they were made, for honest grading afterward."""
    now = report["generated_at"]
    for p in report["picks"]:
        conn.execute(
            "INSERT INTO blend_log (season, week, snapshot_at, game_id, market, side, points, pool_number, "
            "market_number, gap, p_win_est, source, tier, kickoff) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (report["season"], report["week"], now, p["game_id"], p["market"], p["side"], p["points"],
             p["pool_number"], p["market_number"], p["gap"], p["p_win_est"], p["source"], p["tier"], p["kickoff"]),
        )
    conn.commit()


def grade_blend(conn, season):
    """Grade the blend's logged suggestions. For each (week, game, market) the
    graded suggestion is the LAST one logged before that game's kickoff: what
    the blend was saying at the last moment a pick could still be made.
    Returns {"n", "w", "l", "p", "hit", "mean_p_est", "by_source": {...}}.
    mean_p_est next to hit is the calibration check: if the estimates are
    honest they track each other."""
    rows = conn.execute(
        "SELECT b.week, b.game_id, b.market, b.side, b.pool_number, b.p_win_est, b.source, b.snapshot_at, "
        "b.kickoff, g.completed, g.home_points, g.away_points "
        "FROM blend_log b LEFT JOIN games g ON g.game_id = b.game_id WHERE b.season = ?", (season,)
    ).fetchall()
    last = {}
    for wk, gid, mk, side, pool, p, src, snap, ko, comp, hp, ap in rows:
        if ko and snap[:19] >= ko[:19]:
            continue
        key = (wk, gid, mk)
        if key not in last or snap > last[key][0]:
            last[key] = (snap, side, pool, p, src, comp, hp, ap)
    out = {"n": 0, "w": 0, "l": 0, "p": 0, "by_source": {}}
    ps = []
    for (wk, gid, mk), (snap, side, pool, p, src, comp, hp, ap) in last.items():
        if not comp or hp is None or ap is None:
            continue
        res = (ms.grade_total(side, pool, hp + ap) if mk == "total"
               else ms.grade_spread(side, pool, hp, ap))
        out["n"] += 1
        out[{"win": "w", "loss": "l", "push": "p"}[res]] += 1
        b = out["by_source"].setdefault(src, {"w": 0, "l": 0, "p": 0})
        b[{"win": "w", "loss": "l", "push": "p"}[res]] += 1
        if res != "push":
            ps.append(p)
    decided = out["w"] + out["l"]
    out["hit"] = round(out["w"] / decided, 4) if decided else None
    out["mean_p_est"] = round(sum(ps) / len(ps), 4) if ps else None
    return out


def main():
    with db.log_run("blended_ranker") as run:
        week, season = fetch_stats.get_current_week()
        conn = db.get_connection()
        try:
            import card_generator
            card = card_generator.build_card(conn, season, week)
            report = build_blend(conn, season, week, card=card)
            report["track_record"] = grade_blend(conn, season)
            if report["picks"]:
                log_blend(conn, report)
        finally:
            conn.close()

        os.makedirs("data/line_views", exist_ok=True)
        with open(f"data/line_views/blend_week_{week}_{season}.json", "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
        if report["picks"]:
            write_suggestion_csv(report, f"data/pool_picks/week_{week}_{season}_suggested_top5.csv")

        run["rows_added"] = len(report["picks"])
        s = report["summary"]
        print(f"Season {season} Week {week}: blended top {len(report['picks'])} from "
              f"{report['n_candidates']} candidates {report['candidates_by_source']}.")
        for p in report["picks"]:
            print(f"  {p['points']} pts  {p['p_win_est']:.1%}  {p['away_team']} @ {p['home_team']}: "
                  f"{p['market']} {p['pick_text']}  [{p['source']}]")
        if s["n_picks"]:
            print(f"Expected {s['expected_correct']:.2f} correct / {s['expected_points']:.1f} of "
                  f"{s['max_points']} pts; P(5/5)={s['p_perfect']:.1%}, P(>=4)={s['p_ge4']:.1%}, "
                  f"P(>=3)={s['p_ge3']:.1%} (assumes independence and honest estimates).")
        for w in report["warnings"]:
            print("  NOTE:", w)


if __name__ == "__main__":
    main()
