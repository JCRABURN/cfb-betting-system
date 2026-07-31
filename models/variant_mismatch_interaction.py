"""
variant_mismatch_interaction.py
Fix candidate #2 for the demonstrated 10+ edge bucket weakness
(ARCHITECTURE.md §19): let the model learn a DIFFERENT slope for large
EPA-gap games instead of extrapolating one global slope out into mismatches
it rarely sees near the middle of the training distribution.

Implemented as a single 2-feature OLS fit (epa_diff, epa_diff * indicator)
via the existing multivariate harness (fit_multilinear/run_walk_forward
already support N features -- no harness changes needed, same shape as
feature_success_rate.py/feature_havoc.py):

    predicted_margin = intercept + b1*epa_diff + b2*(epa_diff * indicator)

For a "normal" game (indicator=0): slope is b1.
For a "large gap" game (indicator=1): slope is b1+b2.
This is mathematically a two-slope piecewise-linear model with a shared
intercept, fit in one pass -- NOT two independently-fit models requiring a
custom walk-forward loop, and NOT a different train/predict code path.

MISMATCH_THRESHOLD=0.30 locked before running: confirmed live against the
full training archive (2019-2025, n=4,590 point-in-time feature rows)
before picking this number --
    |epa_diff| percentiles: p50=0.151, p75=0.269, p90=0.414, p95=0.532
-- 0.30 sits just above p75, roughly the most-mismatched ~20% of games,
close to but not equal to any single percentile (a round, pre-registered
number, not curve-fit to results). This is a FIXED global constant, not
computed per-season from a walk-forward-only slice -- it's a modeling
design choice made in advance from the full archive's shape, not a
data-derived split subject to lookahead (the split RULE was chosen before
any training/test division was applied, so it introduces no leak into the
walk-forward's own train/test separation).

Also confirmed live before locking this design: games.conference_game is
essentially unusable as a "mismatch proxy" split (only 49 of 4,952 games
flagged conference_game=1 -- clearly broken/near-empty historical
population of that field, not a real ~65% conference-game rate real CFB
schedules should show). This is why the split is on |epa_diff| magnitude,
not conference status as the user's second option suggested.
"""

MISMATCH_THRESHOLD = 0.30


def features(package):
    """(epa_diff, epa_diff * indicator[|epa_diff| > MISMATCH_THRESHOLD])."""
    home = package["home_stats"]
    away = package["away_stats"]
    home_net = home["offense_epa_play"] - home["defense_epa_play"]
    away_net = away["offense_epa_play"] - away["defense_epa_play"]
    epa_diff = home_net - away_net

    indicator = 1.0 if abs(epa_diff) > MISMATCH_THRESHOLD else 0.0
    return (epa_diff, epa_diff * indicator)


def predict_margin(package, intercept, coefs):
    """Predicted (home points - away points). Both coefficients fit by the
    harness via OLS over prior seasons -- neither hardcoded. For a
    large-gap game, the effective slope is epa_coef + interaction_coef."""
    epa_diff, interaction_term = features(package)
    epa_coef, interaction_coef = coefs
    return epa_coef * epa_diff + interaction_coef * interaction_term + intercept
