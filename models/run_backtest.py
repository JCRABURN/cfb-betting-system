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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import backtest_harness as bh
import backtest_report as report
import baseline_epa

SEASONS = [2020, 2021, 2022, 2023, 2024, 2025]
EDGE_THRESHOLD = 3.0


def main():
    conn = db.get_connection()
    records, season_fits = bh.run_walk_forward(
        conn, SEASONS, baseline_epa.epa_differential, baseline_epa.predict_margin)
    conn.close()

    print("Fitted coefficients by season (intercept, epa_coef):")
    for season in SEASONS:
        if season in season_fits:
            intercept, coefs = season_fits[season]
            print(f"  {season}: intercept={intercept:.3f}, epa_coef={coefs[0]:.3f}")
    print()

    graded = [r for r in records if r.skipped_reason is None]
    bet_subset = [r for r in graded if r.edge >= EDGE_THRESHOLD]

    report.print_diagnostics(records, SEASONS)
    print(f"Bet-subset (edge >= {EDGE_THRESHOLD}): {len(bet_subset)}\n")
    report.print_opening_line_sources(graded, SEASONS)

    report.print_season_table("ALL PREDICTIONS (every graded game, calibration view -- per §5)", graded, SEASONS)
    print()
    report.print_season_table(f"BET-SUBSET ONLY (edge >= {EDGE_THRESHOLD} pts, the ROI/edge view -- per §5)",
                               bet_subset, SEASONS)


if __name__ == "__main__":
    main()
