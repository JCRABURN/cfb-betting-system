"""
run_backtest.py
Runs the EPA-differential baseline (baseline_epa.py) through the walk-forward
harness (backtest_harness.py) across the full archive and reports ATS win %,
flat-stake ROI, and CLV -- split all-predictions vs. bet-subset (MODEL_DESIGN.md
§5), broken out by season.

SEASONS includes 2020 so the harness reports it explicitly (n=0), but usable
backtest history is 2021-2025 (five seasons), NOT 2020-2025: 2020 has zero
gradeable games due to a flat opening-line coverage hole (415/489 games
missing an opener from any book, confirmed live), separate from COVID-schedule
messiness. 2019 has no prior season to train on, so it's training data only,
never a predicted season.

Opening-line coverage is thin and single-book-patched throughout (CFBD's
archive has no true consensus opener at all, and consensus closers collapse
after 2022 -- see backtest_harness.py's get_opening_line/get_closing_line).
Since CLV depends on this line data, treat CLV as noisier/less complete than
the win-rate/ROI numbers, which don't depend on it beyond one opening spread.

EDGE_THRESHOLD is a placeholder starting assumption (matches the number found
in the pre-existing spread_model.py before this session), not a calibrated
value -- proper threshold calibration is future work per MODEL_DESIGN.md §8b.
As of the first baseline run, it is NOT demonstrably adding signal: the
bet-subset only beats the all-predictions ATS% in 1 of 5 seasons (2024);
2021/2023/2025 run at or below the all-predictions number. Don't treat that
one season as validation.

Usage: python models/run_backtest.py
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import backtest_harness as bh
import baseline_epa

SEASONS = [2020, 2021, 2022, 2023, 2024, 2025]
EDGE_THRESHOLD = 3.0


def aggregate(records):
    n = len(records)
    wins = sum(1 for r in records if r.result == "win")
    losses = sum(1 for r in records if r.result == "loss")
    pushes = sum(1 for r in records if r.result == "push")
    decided = wins + losses
    ats_pct = wins / decided if decided else None
    total_pl = sum(r.unit_pl for r in records)
    roi = total_pl / n if n else None
    clv_values = [r.clv for r in records if r.clv is not None]
    avg_clv = sum(clv_values) / len(clv_values) if clv_values else None
    return {
        "n": n, "wins": wins, "losses": losses, "pushes": pushes,
        "ats_pct": ats_pct, "total_pl": total_pl, "roi": roi,
        "avg_clv": avg_clv, "n_clv": len(clv_values),
    }


def fmt_pct(x):
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def fmt_row(label, agg):
    return (f"{label:<14} n={agg['n']:<5} W-L-P {agg['wins']}-{agg['losses']}-{agg['pushes']:<3}  "
            f"ATS {fmt_pct(agg['ats_pct']):<7} ROI {fmt_pct(agg['roi']):<7} "
            f"CLV {agg['avg_clv']:+.2f} (n={agg['n_clv']})" if agg['avg_clv'] is not None
            else f"{label:<14} n={agg['n']:<5} W-L-P {agg['wins']}-{agg['losses']}-{agg['pushes']:<3}  "
                 f"ATS {fmt_pct(agg['ats_pct']):<7} ROI {fmt_pct(agg['roi']):<7} CLV n/a")


def main():
    conn = db.get_connection()
    records = bh.run_walk_forward(conn, SEASONS, baseline_epa.epa_differential, baseline_epa.predict_margin)
    conn.close()

    graded = [r for r in records if r.skipped_reason is None]
    skipped = [r for r in records if r.skipped_reason is not None]
    bet_subset = [r for r in graded if r.edge >= EDGE_THRESHOLD]

    print(f"Total games considered: {len(records)}")
    print(f"Graded (had stats + opening line): {len(graded)}")
    print(f"Skipped: {len(skipped)}  {dict(Counter(r.skipped_reason for r in skipped))}")
    print(f"Bet-subset (edge >= {EDGE_THRESHOLD}): {len(bet_subset)}\n")

    print("Skip reasons by season (missing_opening_line here means CFBD has no opener at")
    print("all for that game, from any book -- confirmed real, not a bug, see report):")
    for season in SEASONS:
        season_all = [r for r in records if r.season == season]
        season_skipped = [r for r in season_all if r.skipped_reason is not None]
        reasons = dict(Counter(r.skipped_reason for r in season_skipped))
        print(f"  {season}: {len(season_all)} total, {len(season_skipped)} skipped {reasons}")

    print("\nOpening line source (consensus vs. single-book proxy, flagged per MODEL_DESIGN.md §4):")
    for season in SEASONS:
        season_graded = [r for r in graded if r.season == season]
        books = dict(Counter(r.opening_book for r in season_graded))
        print(f"  {season}: {books}")
    print()

    print("=" * 90)
    print("ALL PREDICTIONS (every graded game, calibration view -- per §5)")
    print("=" * 90)
    for season in SEASONS:
        season_records = [r for r in graded if r.season == season]
        print(fmt_row(str(season), aggregate(season_records)))
    print("-" * 90)
    print(fmt_row("2020 only", aggregate([r for r in graded if r.season == 2020])))
    print(fmt_row("2021-2025", aggregate([r for r in graded if r.season != 2020])))
    print(fmt_row("ALL SEASONS", aggregate(graded)))

    print("\n" + "=" * 90)
    print(f"BET-SUBSET ONLY (edge >= {EDGE_THRESHOLD} pts, the ROI/edge view -- per §5)")
    print("=" * 90)
    for season in SEASONS:
        season_records = [r for r in bet_subset if r.season == season]
        print(fmt_row(str(season), aggregate(season_records)))
    print("-" * 90)
    print(fmt_row("2020 only", aggregate([r for r in bet_subset if r.season == 2020])))
    print(fmt_row("2021-2025", aggregate([r for r in bet_subset if r.season != 2020])))
    print(fmt_row("ALL SEASONS", aggregate(bet_subset)))


if __name__ == "__main__":
    main()
