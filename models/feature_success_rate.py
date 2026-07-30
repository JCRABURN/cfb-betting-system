"""
feature_success_rate.py
First candidate feature per MODEL_DESIGN.md's "Features, one at a time" plan
(baseline: EPA differential only, see baseline_epa.py). This adds
point-in-time success rate as a SECOND feature alongside EPA -- not a
replacement -- so the comparison isolates what success rate specifically
contributes on top of what EPA already captures.

Tested against the 51.1% ATS / -2.5% ROI (2021-2025) baseline via
run_feature_test.py using three acceptance criteria locked in before this was
built or run: McNemar's test on disagreement games (p<0.05), directional
improvement in >=4 of 5 seasons, and coefficient sign stability across every
season's fit. All three required, not any one alone.
"""


def features(package):
    """(epa_diff, success_rate_diff), home minus away for both.

    defense_success_rate follows the exact same convention as
    defense_epa_play (confirmed live during the point-in-time backfill
    phase): it's the success rate OPPOSING offenses achieve against that
    team's defense, so a HIGHER number means a WORSE defense. Subtracting it
    (same as defense_epa_play) is therefore correct, not a sign error --
    spot-checked against Georgia's 2023 defense (a known-elite unit):
    defense_success_rate sat in the 0.30-0.38 range, well below the
    national ~42-45% average, consistent with "lower = better defense."
    """
    home = package["home_stats"]
    away = package["away_stats"]

    home_epa_net = home["offense_epa_play"] - home["defense_epa_play"]
    away_epa_net = away["offense_epa_play"] - away["defense_epa_play"]
    epa_diff = home_epa_net - away_epa_net

    home_sr_net = home["offense_success_rate"] - home["defense_success_rate"]
    away_sr_net = away["offense_success_rate"] - away["defense_success_rate"]
    sr_diff = home_sr_net - away_sr_net

    return (epa_diff, sr_diff)


def predict_margin(package, intercept, coefs):
    """Predicted (home points - away points). Both coefficients are fit by
    the harness via OLS over prior seasons -- neither is hardcoded."""
    epa_diff, sr_diff = features(package)
    epa_coef, sr_coef = coefs
    return epa_coef * epa_diff + sr_coef * sr_diff + intercept
