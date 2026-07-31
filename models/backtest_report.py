"""
backtest_report.py
Shared reporting utilities for backtest_harness.py runs. Used by
run_backtest.py (the baseline) and every one-at-a-time feature test script
after it, so every run is reported in the same shape and results from
different scripts can be compared on sight rather than re-derived.
"""

from collections import Counter


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
    base = (f"{label:<14} n={agg['n']:<5} W-L-P {agg['wins']}-{agg['losses']}-{agg['pushes']:<3}  "
            f"ATS {fmt_pct(agg['ats_pct']):<7} ROI {fmt_pct(agg['roi']):<7} ")
    if agg["avg_clv"] is not None:
        return base + f"CLV {agg['avg_clv']:+.2f} (n={agg['n_clv']})"
    return base + "CLV n/a"


def bucket_by_edge(records, buckets):
    """records: PredictionRecord list, already filtered to whatever subset
    matters (e.g. edge >= 3.0). buckets: ordered list of (lo, hi) tuples,
    e.g. [(3.0, 6.0), (6.0, 10.0), (10.0, float('inf'))] -- lo <= edge < hi.
    Returns a list of (label, aggregate_dict) pairs in the same order as
    `buckets`, using each record's OWN `edge` field -- so this reflects
    whatever model produced `records`, not a fixed baseline bucketing
    reapplied to a different model's numbers (see ARCHITECTURE.md §19/§20)."""
    results = []
    for lo, hi in buckets:
        subset = [r for r in records if lo <= r.edge < hi]
        label = f"{lo:.0f}-{hi:.0f}" if hi != float("inf") else f"{lo:.0f}+"
        results.append((label, aggregate(subset)))
    return results


def print_diagnostics(records, seasons):
    graded = [r for r in records if r.skipped_reason is None]
    skipped = [r for r in records if r.skipped_reason is not None]

    print(f"Total games considered: {len(records)}")
    print(f"Graded (had stats + opening line): {len(graded)}")
    print(f"Skipped: {len(skipped)}  {dict(Counter(r.skipped_reason for r in skipped))}\n")

    print("Skip reasons by season:")
    for season in seasons:
        season_all = [r for r in records if r.season == season]
        season_skipped = [r for r in season_all if r.skipped_reason is not None]
        reasons = dict(Counter(r.skipped_reason for r in season_skipped))
        print(f"  {season}: {len(season_all)} total, {len(season_skipped)} skipped {reasons}")
    print()


def print_opening_line_sources(graded_records, seasons):
    print("Opening line source (consensus vs. single-book proxy, flagged per MODEL_DESIGN.md §4):")
    for season in seasons:
        season_graded = [r for r in graded_records if r.season == season]
        books = dict(Counter(r.opening_book for r in season_graded))
        print(f"  {season}: {books}")
    print()


def print_season_table(title, records, seasons, exclude_2020_label="2021-2025"):
    print("=" * 90)
    print(title)
    print("=" * 90)
    for season in seasons:
        season_records = [r for r in records if r.season == season]
        print(fmt_row(str(season), aggregate(season_records)))
    print("-" * 90)
    if 2020 in seasons:
        print(fmt_row("2020 only", aggregate([r for r in records if r.season == 2020])))
        print(fmt_row(exclude_2020_label, aggregate([r for r in records if r.season != 2020])))
    print(fmt_row("ALL SEASONS", aggregate(records)))
