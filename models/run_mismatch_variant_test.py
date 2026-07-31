"""
run_mismatch_variant_test.py
Tests candidate fixes for the model's demonstrated weakness (ARCHITECTURE.md
§19): within the edge>=3 bet-subset, the edge>=10 bucket is the WORST of the
three edge buckets (3-6/6-10/10+), not the best -- 50.7% ATS aggregate,
below 50% in 2 of 5 seasons -- consistent with linear extrapolation in
blowout/mismatch games. This is a DIFFERENT question than the one-at-a-time
feature tests in run_feature_test.py: it isn't looking for a new feature
that adds signal, it's checking whether a structural change to how the
EXISTING EPA information is used fixes a known, specific failure mode.

Three criteria, ALL required, locked before any variant was run or its
results seen:

  1. McNemar's test on disagreement games vs. the EPA-only baseline,
     p < 0.05 -- the same sharp instrument as the feature tests: most
     games, both models agree, so only the games a variant actually
     changes are informative.
  2. The variant's OWN 10+ bucket (recomputed using the variant's own edge
     values -- edge depends on predicted_margin, which every variant here
     changes, so which bucket a given game falls into can shift) must NOT
     be the worst of its own three buckets: its ATS% must be >= both the
     3-6 and 6-10 buckets, aggregate across 2021-2025. This is the actual
     target -- fixing the specific demonstrated weakness, not general
     aggregate improvement.
  3. The variant's 10+ bucket ATS% must beat the BASELINE's 10+ bucket
     ATS% in at least 4 of the 5 graded seasons (2021-2025) -- so a fix
     that only works in one lucky season doesn't count, the same
     season-consistency lesson every prior test in this project has
     applied (most recently: the edge-bucket check itself, which is what
     caught the 6-10 bucket's 60.7%/41.5% hot/cold swing).

Deliberately does NOT reuse run_feature_test.py's bar (aggregate ATS
improvement across all edges + coefficient sign stability) -- that bar
answers "does this feature add signal," a different question from "does
this fix the specific bucket that's broken." Fit details are still printed
for transparency, just not gated on.

Not a standalone CLI -- called from a small per-variant entry-point script
(e.g. run_mismatch_test_damped.py) that supplies the challenger's
feature/predict functions.
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
EDGE_THRESHOLD = 3.0
BUCKETS = [(3.0, 6.0), (6.0, 10.0), (10.0, float("inf"))]


def run_variant_test(challenger_feature_fn, challenger_predict_fn, challenger_label):
    conn = db.get_connection()
    baseline_records, baseline_fits = bh.run_walk_forward(
        conn, SEASONS, baseline_epa.epa_differential, baseline_epa.predict_margin)
    challenger_records, challenger_fits = bh.run_walk_forward(
        conn, SEASONS, challenger_feature_fn, challenger_predict_fn)
    conn.close()

    baseline_by_id = {r.game_id: r for r in baseline_records}
    challenger_by_id = {r.game_id: r for r in challenger_records}

    baseline_graded_ids = {r.game_id for r in baseline_records if r.skipped_reason is None}
    challenger_graded_ids = {r.game_id for r in challenger_records if r.skipped_reason is None}
    common_ids = baseline_graded_ids & challenger_graded_ids
    mismatch = (baseline_graded_ids | challenger_graded_ids) - common_ids
    if mismatch:
        pct = len(mismatch) / len(baseline_graded_ids | challenger_graded_ids) * 100
        if pct > 1.0:
            raise AssertionError(
                f"Baseline and challenger graded substantially different game sets ({pct:.2f}%) "
                f"-- comparison invalid.")
        print(f"NOTE: {len(mismatch)} game(s) ({pct:.2f}%) graded by only one model -- "
              f"comparing on the intersection.\n")

    baseline_graded = [r for r in baseline_records if r.game_id in common_ids and r.season in GRADED_SEASONS]
    challenger_graded = [r for r in challenger_records if r.game_id in common_ids and r.season in GRADED_SEASONS]

    baseline_bet_subset = [r for r in baseline_graded if r.edge >= EDGE_THRESHOLD]
    challenger_bet_subset = [r for r in challenger_graded if r.edge >= EDGE_THRESHOLD]

    print(f"Fitted coefficients by season ({challenger_label}):")
    for season in SEASONS:
        if season in challenger_fits:
            intercept, coefs = challenger_fits[season]
            print(f"  {season}: intercept={intercept:.4f}, coefs={tuple(round(c, 4) for c in coefs)}")
    print()

    print("=== BASELINE (EPA only) edge buckets, bet-subset, 2021-2025 ===")
    baseline_buckets = report.bucket_by_edge(baseline_bet_subset, BUCKETS)
    for label, agg in baseline_buckets:
        print(f"  edge {label:<6} " + report.fmt_row("", agg))
    print()

    print(f"=== CHALLENGER ({challenger_label}) edge buckets, bet-subset, 2021-2025 ===")
    challenger_buckets = report.bucket_by_edge(challenger_bet_subset, BUCKETS)
    for label, agg in challenger_buckets:
        print(f"  edge {label:<6} " + report.fmt_row("", agg))
    print()

    # --- Criterion 2: challenger's own 10+ bucket must not be the worst of its three ---
    print("=" * 90)
    print("CRITERION 2 -- challenger's 10+ bucket must NOT be the worst of its own three buckets")
    print("=" * 90)
    c_36, c_610, c_10p = (agg["ats_pct"] for _, agg in challenger_buckets)
    criterion_2_pass = (
        c_10p is not None and c_36 is not None and c_610 is not None
        and c_10p >= c_36 and c_10p >= c_610
    )
    print(f"  3-6: {report.fmt_pct(c_36)}   6-10: {report.fmt_pct(c_610)}   10+: {report.fmt_pct(c_10p)}")
    print(f"  {'PASS' if criterion_2_pass else 'FAIL'}\n")

    # --- Criterion 3: challenger's 10+ bucket beats baseline's 10+ bucket, >=4/5 seasons ---
    print("=" * 90)
    print("CRITERION 3 -- challenger 10+ bucket ATS% beats baseline 10+ bucket ATS%, >=4/5 seasons")
    print("=" * 90)
    seasons_improved = 0
    for season in GRADED_SEASONS:
        b_10p_season = [r for r in baseline_bet_subset if r.season == season and r.edge >= 10.0]
        c_10p_season = [r for r in challenger_bet_subset if r.season == season and r.edge >= 10.0]
        b_agg = report.aggregate(b_10p_season)
        c_agg = report.aggregate(c_10p_season)
        b_ats, c_ats = b_agg["ats_pct"], c_agg["ats_pct"]
        improved = c_ats is not None and b_ats is not None and c_ats > b_ats
        seasons_improved += improved
        print(f"  {season}: baseline 10+ {report.fmt_pct(b_ats)} (n={b_agg['n']}) -> "
              f"challenger 10+ {report.fmt_pct(c_ats)} (n={c_agg['n']})  "
              f"{'IMPROVED' if improved else 'no improvement'}")
    criterion_3_pass = seasons_improved >= 4
    print(f"\n  Improved in {seasons_improved}/5 seasons -- {'PASS' if criterion_3_pass else 'FAIL'}\n")

    # --- Criterion 1: McNemar on disagreements ---
    print("=" * 90)
    print("CRITERION 1 -- McNemar's test on disagreement games (need p<0.05)")
    print("=" * 90)
    disagreements = [
        (baseline_by_id[gid], challenger_by_id[gid])
        for gid in common_ids
        if baseline_by_id[gid].season in GRADED_SEASONS
        and baseline_by_id[gid].side != challenger_by_id[gid].side
        and baseline_by_id[gid].result in ("win", "loss")
        and challenger_by_id[gid].result in ("win", "loss")
    ]
    challenger_right = sum(1 for b, c in disagreements if c.result == "win")
    baseline_right = sum(1 for b, c in disagreements if b.result == "win")
    print(f"  Disagreement games (different side, decided both ways): {len(disagreements)}")
    print(f"  2x2 table: Challenger right (baseline wrong)={challenger_right}  "
          f"Baseline right (challenger wrong)={baseline_right}")
    chi2, p_value = bh.mcnemar_test(challenger_right, baseline_right)
    print(f"  McNemar chi2 = {chi2:.3f}, p = {p_value:.4f}")
    criterion_1_pass = p_value < 0.05
    print(f"  {'PASS' if criterion_1_pass else 'FAIL'}\n")

    all_pass = criterion_1_pass and criterion_2_pass and criterion_3_pass
    print("=" * 90)
    print("VERDICT")
    print("=" * 90)
    print(f"  Criterion 1 (McNemar p<0.05):                 {'PASS' if criterion_1_pass else 'FAIL'}")
    print(f"  Criterion 2 (10+ not the worst bucket):        {'PASS' if criterion_2_pass else 'FAIL'}")
    print(f"  Criterion 3 (10+ beats baseline, >=4/5 szns):  {'PASS' if criterion_3_pass else 'FAIL'}")
    verdict = "KEEP" if all_pass else "DO NOT KEEP"
    print(f"\n  OVERALL: {verdict} -- {challenger_label} "
          f"{'fixes the demonstrated 10+ bucket weakness' if all_pass else 'does not clear the pre-registered bar'}")

    return {
        "criterion_1_pass": criterion_1_pass, "criterion_2_pass": criterion_2_pass,
        "criterion_3_pass": criterion_3_pass, "all_pass": all_pass,
        "mcnemar_chi2": chi2, "mcnemar_p": p_value,
        "challenger_right": challenger_right, "baseline_right": baseline_right,
        "n_disagreements": len(disagreements), "seasons_improved": seasons_improved,
        "baseline_buckets": baseline_buckets, "challenger_buckets": challenger_buckets,
    }
