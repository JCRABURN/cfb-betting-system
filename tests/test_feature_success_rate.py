import feature_success_rate as fsr


def make_package(home_off_epa, home_def_epa, home_off_sr, home_def_sr,
                  away_off_epa, away_def_epa, away_off_sr, away_def_sr):
    return {
        "home_stats": {
            "offense_epa_play": home_off_epa, "defense_epa_play": home_def_epa,
            "offense_success_rate": home_off_sr, "defense_success_rate": home_def_sr,
        },
        "away_stats": {
            "offense_epa_play": away_off_epa, "defense_epa_play": away_def_epa,
            "offense_success_rate": away_off_sr, "defense_success_rate": away_def_sr,
        },
    }


def test_features_returns_two_tuple():
    package = make_package(0.2, 0.0, 0.5, 0.3, 0.1, 0.0, 0.4, 0.35)
    result = fsr.features(package)
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_success_rate_diff_worse_defense_lowers_net_strength():
    # Higher defense_success_rate = worse defense (opponents succeed more
    # often against them) -- same convention as defense_epa_play. A team
    # with a worse defensive success rate allowed should show LOWER net
    # strength on that dimension.
    weak_defense = make_package(0.2, 0.0, 0.5, 0.50, 0.2, 0.0, 0.5, 0.30)
    strong_defense = make_package(0.2, 0.0, 0.5, 0.20, 0.2, 0.0, 0.5, 0.30)
    _, weak_sr_diff = fsr.features(weak_defense)
    _, strong_sr_diff = fsr.features(strong_defense)
    assert weak_sr_diff < strong_sr_diff


def test_symmetric_teams_both_diffs_zero():
    package = make_package(0.2, 0.05, 0.45, 0.35, 0.2, 0.05, 0.45, 0.35)
    epa_diff, sr_diff = fsr.features(package)
    assert epa_diff == 0
    assert sr_diff == 0


def test_predict_margin_applies_both_coefficients():
    package = make_package(0.2, 0.0, 0.10, 0.0, 0.0, 0.0, 0.0, 0.0)
    epa_diff, sr_diff = fsr.features(package)
    margin = fsr.predict_margin(package, intercept=2.5, coefs=(10.0, 5.0))
    assert margin == 10.0 * epa_diff + 5.0 * sr_diff + 2.5
