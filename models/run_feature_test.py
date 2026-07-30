"""
run_feature_test.py
Generic one-at-a-time feature test (MODEL_DESIGN.md's "Features, one at a
time, each measured against the baseline" plan). Runs the baseline (EPA
only) and a challenger (EPA + one new feature) through the SAME walk-forward
harness, then applies three acceptance criteria that were locked in BEFORE
any feature was tested or its results seen:

  1. McNemar's test on disagreement games (games where the two models pick
     different sides) is significant at p < 0.05. This is the sharp
     instrument: most games, both models agree, which says nothing about
     whether the new feature helps -- only the games it actually changes
     the pick on are informative.
  2. The challenger improves ATS% over the baseline in at least 4 of the 5
     graded seasons (2021-2025), in the same direction. A feature that only
     helps in one lucky season is noise, not signal (this is the same
     lesson as the edge>=3 threshold finding from the baseline run).
  3. The new feature's coefficient sign is stable across every season's fit
     (never flips). A flipping sign is a tell that the fit is chasing
     sample-specific noise rather than a real, consistent relationship.

ALL THREE required, not any one alone -- the bar doesn't get renegotiated
after seeing partial results.

Not a standalone CLI -- called from a small per-feature script (e.g.
run_feature_test_success_rate.py) that supplies the challenger's feature/
predict functions.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
import backtest_harness as bh
import backtest_report as report
import baseline_epa

SEASONS = [2020, 2021, 2022, 2023, 2024, 2025]
GRADED_SEASONS = [2021, 2022, 2023, 2024, 2025]  # 2020 has 0 gradeable games, see ARCHITECTURE.md §14


def run_feature_test(challenger_feature_fn, challenger_predict_fn, num_new_features, challenger_label):
    """num_new_features: how many of the challenger's trailing coefficients
    are the NEW feature(s) being tested (vs. inherited from the baseline) --
    used only to report which coefficients matter for criterion 3."""
    conn = db.get_connection()
    baseline_records, baseline_fits = bh.run_walk_forward(
        conn, SEASONS, baseline_epa.epa_differential, baseline_epa.predict_margin)
    challenger_records, challenger_fits = bh.run_walk_forward(
        conn, SEASONS, challenger_feature_fn, challenger_predict_fn)
    conn.close()

    baseline_by_id = {r.game_id: r for r in baseline_records}
    challenger_by_id = {r.game_id: r for r in challenger_records}

    # Sanity check before anything else: both models must see the identical
    # graded/skipped game set, since the same point-in-time accessor decides
    # availability for both. A mismatch would mean the new feature has
    # different NULL coverage than EPA -- worth investigating, not silently
    # comparing two different game populations.
    baseline_graded_ids = {r.game_id for r in baseline_records if r.skipped_reason is None}
    challenger_graded_ids = {r.game_id for r in challenger_records if r.skipped_reason is None}
    if baseline_graded_ids != challenger_graded_ids:
        raise AssertionError(
            f"Baseline and challenger graded DIFFERENT game sets -- comparison invalid. "
            f"{len(baseline_graded_ids - challenger_graded_ids)} graded only by baseline, "
            f"{len(challenger_graded_ids - baseline_graded_ids)} only by challenger. "
            f"Investigate the new feature's NULL coverage before trusting anything below."
        )
    print(f"Both models graded the identical {len(baseline_graded_ids)} games -- comparison is apples-to-apples.\n")

    baseline_graded = [r for r in baseline_records if r.skipped_reason is None]
    challenger_graded = [r for r in challenger_records if r.skipped_reason is None]

    print("=== BASELINE (EPA only) ===")
    report.print_season_table("ALL PREDICTIONS", baseline_graded, GRADED_SEASONS)
    print(f"\n=== CHALLENGER ({challenger_label}) ===")
    report.print_season_table("ALL PREDICTIONS", challenger_graded, GRADED_SEASONS)
    print()

    # --- Criterion 2: per-season directional improvement ---
    print("=" * 90)
    print("CRITERION 2 -- directional improvement, season by season (need >=4/5)")
    print("=" * 90)
    seasons_improved = 0
    for season in GRADED_SEASONS:
        b_agg = report.aggregate([r for r in baseline_graded if r.season == season])
        c_agg = report.aggregate([r for r in challenger_graded if r.season == season])
        delta = c_agg["ats_pct"] - b_agg["ats_pct"]
        improved = delta > 0
        seasons_improved += improved
        print(f"  {season}: baseline {report.fmt_pct(b_agg['ats_pct'])} -> challenger "
              f"{report.fmt_pct(c_agg['ats_pct'])}  (delta {delta * 100:+.1f}pp)  "
              f"{'IMPROVED' if improved else 'no improvement'}")
    criterion_2_pass = seasons_improved >= 4
    print(f"\n  Improved in {seasons_improved}/5 seasons -- {'PASS' if criterion_2_pass else 'FAIL'}\n")

    # --- Criterion 1: disagreement analysis + McNemar ---
    print("=" * 90)
    print("CRITERION 1 -- McNemar's test on disagreement games (need p<0.05)")
    print("=" * 90)
    disagreements = [
        (baseline_by_id[gid], challenger_by_id[gid])
        for gid in baseline_graded_ids
        if baseline_by_id[gid].side != challenger_by_id[gid].side
        and baseline_by_id[gid].result in ("win", "loss")
        and challenger_by_id[gid].result in ("win", "loss")
    ]
    challenger_right = sum(1 for b, c in disagreements if c.result == "win")
    baseline_right = sum(1 for b, c in disagreements if b.result == "win")

    print(f"  Disagreement games (different side picked, decided both ways): {len(disagreements)}")
    print(f"  2x2 table:")
    print(f"    Challenger right (baseline therefore wrong): {challenger_right}")
    print(f"    Baseline right (challenger therefore wrong): {baseline_right}")
    chi2, p_value = bh.mcnemar_test(challenger_right, baseline_right)
    print(f"  McNemar chi2 = {chi2:.3f}, p = {p_value:.4f}")
    criterion_1_pass = p_value < 0.05
    print(f"  {'PASS' if criterion_1_pass else 'FAIL'}\n")

    # --- Criterion 3: coefficient sign stability ---
    print("=" * 90)
    print("CRITERION 3 -- new feature's coefficient sign stable across every season's fit")
    print("=" * 90)
    signs = set()
    for season in SEASONS:
        if season not in challenger_fits:
            continue
        intercept, coefs = challenger_fits[season]
        new_coefs = coefs[-num_new_features:]
        print(f"  {season}: intercept={intercept:.3f}, coefs={tuple(round(c, 4) for c in coefs)}")
        signs.add(tuple(1 if c > 0 else -1 for c in new_coefs))
    criterion_3_pass = len(signs) == 1
    print(f"\n  Sign pattern(s) observed: {signs} -- {'PASS' if criterion_3_pass else 'FAIL'}\n")

    all_pass = criterion_1_pass and criterion_2_pass and criterion_3_pass
    print("=" * 90)
    print("VERDICT")
    print("=" * 90)
    print(f"  Criterion 1 (McNemar p<0.05):         {'PASS' if criterion_1_pass else 'FAIL'}")
    print(f"  Criterion 2 (>=4/5 seasons improved):  {'PASS' if criterion_2_pass else 'FAIL'}")
    print(f"  Criterion 3 (coefficient sign stable): {'PASS' if criterion_3_pass else 'FAIL'}")
    verdict = "KEEP" if all_pass else "DO NOT KEEP"
    print(f"\n  OVERALL: {verdict} -- {challenger_label} "
          f"{'demonstrably adds signal over the baseline' if all_pass else 'does not clear the pre-registered bar'}")

    return {
        "criterion_1_pass": criterion_1_pass, "criterion_2_pass": criterion_2_pass,
        "criterion_3_pass": criterion_3_pass, "all_pass": all_pass,
        "mcnemar_chi2": chi2, "mcnemar_p": p_value,
        "challenger_right": challenger_right, "baseline_right": baseline_right,
        "n_disagreements": len(disagreements), "seasons_improved": seasons_improved,
    }
