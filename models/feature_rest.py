"""
feature_rest.py
Third one-at-a-time feature test (MODEL_DESIGN.md's "Features, one at a
time" plan; success rate and havoc both REJECTED -- see ARCHITECTURE.md §15,
§16). Tested independently against the EPA-only baseline, not stacked on
either rejected feature.

Owner's framing going in: success rate and havoc both failed because
they're on-field performance summaries that overlap what EPA already
captures. Rest/schedule is structurally different -- EPA cannot see that a
team is on a short week or coming off a bye, since that information isn't
in any performance stat at all, only in the schedule itself.

Two situational features, bundled as one "rest/schedule" test (matching
MODEL_DESIGN.md's "Later features" framing of "Rest / schedule spots" as one
theme, not two separate one-at-a-time tests): days-of-rest differential, and
a bye-week-flag differential. Both computed from games.start_date via
backtest_harness.get_days_rest() -- point-in-time correct by construction
(strictly the most recent PRIOR game this season, before the target game's
own date; never reaches into the previous season).

BYE_THRESHOLD_DAYS=10 chosen from the real rest-day distribution, checked
before building anything: a clean bimodal split between a ~6-8 day normal-
week cluster (6,746 of 8,981 observations) and a ~13-14 day bye cluster
(1,418), with only a thin 9-12 day zone between them (357 observations). 10
days cleanly separates the two humps.
"""

BYE_THRESHOLD_DAYS = 10


def features(package):
    """(epa_diff, rest_diff, bye_diff), home minus away for all three.
    Returns None if either team has no rest value available (week 1, no
    prior same-season game) -- the harness treats that as "skip this game,"
    never a fabricated default rest value."""
    home = package["home_stats"]
    away = package["away_stats"]

    home_rest = package["home_days_rest"]
    away_rest = package["away_days_rest"]
    if home_rest is None or away_rest is None:
        return None

    home_epa_net = home["offense_epa_play"] - home["defense_epa_play"]
    away_epa_net = away["offense_epa_play"] - away["defense_epa_play"]
    epa_diff = home_epa_net - away_epa_net

    rest_diff = home_rest - away_rest

    home_bye = 1 if home_rest >= BYE_THRESHOLD_DAYS else 0
    away_bye = 1 if away_rest >= BYE_THRESHOLD_DAYS else 0
    bye_diff = home_bye - away_bye

    return (epa_diff, rest_diff, bye_diff)


def predict_margin(package, intercept, coefs):
    """Predicted (home points - away points), or None if features() can't be
    computed for this game. All three coefficients are fit by the harness
    via OLS over prior seasons -- none hardcoded here."""
    result = features(package)
    if result is None:
        return None
    epa_diff, rest_diff, bye_diff = result
    epa_coef, rest_coef, bye_coef = coefs
    return epa_coef * epa_diff + rest_coef * rest_diff + bye_coef * bye_diff + intercept
