"""
variant_damped_output.py
Fix candidate #1 for the demonstrated 10+ edge bucket weakness
(ARCHITECTURE.md §19: that bucket is the WORST of the three, 50.7% ATS
aggregate, below 50% in 2 of 5 seasons -- consistent with the linear EPA
model overclaiming in blowout/mismatch games). This compresses the model's
OUTPUT when it predicts an extreme margin, rather than touching the fit.

feature_fn is IDENTICAL to baseline_epa.epa_differential -- same training
rows go into fit_multilinear, so the fitted intercept/coef are numerically
identical to the baseline's for the same season. Only predict_fn changes:
it computes the SAME raw prediction the baseline would, then damps it.

DAMP_CAP=10.0, DAMP_SHRINK=0.5 locked before running against real data (per
the pre-registered design): a raw prediction beyond +/-10 points has its
excess beyond the cap HALVED, not clipped outright and not left alone --
e.g. a raw 30-point prediction becomes 10 + 0.5*(30-10) = 20.
"""

DAMP_CAP = 10.0
DAMP_SHRINK = 0.5


def epa_differential(package):
    """Same as baseline_epa.epa_differential -- duplicated rather than
    imported, matching this project's convention of each feature module
    being self-contained (see feature_success_rate.py, feature_havoc.py)."""
    home = package["home_stats"]
    away = package["away_stats"]
    home_net = home["offense_epa_play"] - home["defense_epa_play"]
    away_net = away["offense_epa_play"] - away["defense_epa_play"]
    return (home_net - away_net,)


def damp(margin, cap=DAMP_CAP, shrink=DAMP_SHRINK):
    """Compress a raw predicted margin beyond +/-cap by `shrink`. Symmetric,
    continuous at the cap boundary (no jump), and never flips sign."""
    if margin > cap:
        return cap + shrink * (margin - cap)
    if margin < -cap:
        return -cap + shrink * (margin + cap)
    return margin


def predict_margin(package, intercept, coefs):
    """Predicted (home points - away points), damped. intercept/coefs are
    fit the same way as the baseline (same feature_fn, same OLS) -- the
    damping is applied strictly after the linear prediction, never inside
    the fit itself."""
    (epa_diff,) = epa_differential(package)
    (epa_coef,) = coefs
    raw = epa_coef * epa_diff + intercept
    return damp(raw)
