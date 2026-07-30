"""
feature_havoc.py
Second one-at-a-time feature test (MODEL_DESIGN.md's "Features, one at a
time" plan; first was success rate, REJECTED -- see ARCHITECTURE.md §15).
Tested independently against the same EPA-only baseline, not stacked on the
rejected success-rate feature.

CFBD only exposes havoc as a single (defensive) stat per team -- there's no
offense_havoc_rate the way EPA/success rate have an offense/defense split
(confirmed live during the Phase 2 point-in-time backfill). So the feature is
simply home_havoc_rate - away_havoc_rate: whichever team's defense generates
more disruptive plays (TFLs, forced fumbles, PBUs, etc.) gets credit. A
higher home_havoc_rate should push the predicted margin toward home, so the
theoretically expected coefficient sign is positive -- same direction as EPA
and as success rate's (rejected) coefficient.

A-priori case for this one (not a prediction, doesn't move the bar): havoc
measures defensive disruption specifically, a different mechanism than EPA's
general play-efficiency measure. Success rate failed because it's highly
correlated with EPA and had little independent information left to give.
Havoc has a more plausible case for carrying something EPA doesn't already
capture. The three criteria decide, same as last time.
"""


def features(package):
    """(epa_diff, havoc_diff), home minus away for both. Returns None if
    either team's havoc_rate is unavailable (confirmed live: 17 of 13,290
    point-in-time rows, 0.13%, concentrated in 2020 -- presumably too few
    defensive snaps recorded yet for havoc to be computed, even though EPA is
    present). The harness treats a None feature row as "skip this game," the
    same way it already handles a missing pregame-stats row entirely."""
    home = package["home_stats"]
    away = package["away_stats"]

    if home["havoc_rate"] is None or away["havoc_rate"] is None:
        return None

    home_epa_net = home["offense_epa_play"] - home["defense_epa_play"]
    away_epa_net = away["offense_epa_play"] - away["defense_epa_play"]
    epa_diff = home_epa_net - away_epa_net

    havoc_diff = home["havoc_rate"] - away["havoc_rate"]

    return (epa_diff, havoc_diff)


def predict_margin(package, intercept, coefs):
    """Predicted (home points - away points), or None if features() can't be
    computed for this game. Both coefficients are fit by the harness via OLS
    over prior seasons -- neither hardcoded here."""
    result = features(package)
    if result is None:
        return None
    epa_diff, havoc_diff = result
    epa_coef, havoc_coef = coefs
    return epa_coef * epa_diff + havoc_coef * havoc_diff + intercept
