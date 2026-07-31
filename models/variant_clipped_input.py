"""
variant_clipped_input.py
Fix candidate #3 for the demonstrated 10+ edge bucket weakness
(ARCHITECTURE.md §19). Where variant_damped_output.py compresses the
model's OUTPUT after fitting normally, this compresses the INPUT feature
before it ever reaches the OLS fit -- the fit itself never sees a raw
epa_diff beyond the cap, at training time OR prediction time, so it can't
learn (or extrapolate) a slope calibrated to differences it's clipped away.
This is a genuinely different mechanism from variant_damped_output.py, not
a restatement of it: damping changes what the SAME fitted line outputs;
clipping changes what the fit itself is allowed to learn from.

CLIP_CAP=0.40 locked before running: confirmed live against the full
training archive (2019-2025, n=4,590) before picking this number --
|epa_diff| p90=0.414, p95=0.532 -- 0.40 sits at essentially the 90th
percentile, so roughly the most extreme 10% of games have their feature
value capped before fitting. Symmetric (+/-0.40), applied identically at
training-row construction and at prediction time (the same feature_fn
serves both, per backtest_harness.py's build_training_set/
build_feature_package contract), so there's no train/predict skew.
"""

CLIP_CAP = 0.40


def epa_differential(package):
    """Clipped epa_diff, home minus away net EPA/play. Unlike
    variant_damped_output.epa_differential (identical to the baseline's),
    this one is the actual point of the variant: the raw value is clipped
    to +/-CLIP_CAP before it's returned, so fit_multilinear never sees (and
    therefore never fits a slope calibrated to) anything beyond the cap."""
    home = package["home_stats"]
    away = package["away_stats"]
    home_net = home["offense_epa_play"] - home["defense_epa_play"]
    away_net = away["offense_epa_play"] - away["defense_epa_play"]
    raw_diff = home_net - away_net
    clipped = max(-CLIP_CAP, min(CLIP_CAP, raw_diff))
    return (clipped,)


def predict_margin(package, intercept, coefs):
    """Predicted (home points - away points), using the CLIPPED feature.
    intercept/coefs are fit fresh on the clipped training rows -- this fit
    is numerically different from the baseline's, since the inputs differ
    for every game beyond the cap."""
    (clipped_diff,) = epa_differential(package)
    (epa_coef,) = coefs
    return epa_coef * clipped_diff + intercept
