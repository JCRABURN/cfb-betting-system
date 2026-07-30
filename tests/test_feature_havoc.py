import feature_havoc as fh


def make_package(home_off_epa, home_def_epa, home_havoc, away_off_epa, away_def_epa, away_havoc):
    return {
        "home_stats": {
            "offense_epa_play": home_off_epa, "defense_epa_play": home_def_epa,
            "havoc_rate": home_havoc,
        },
        "away_stats": {
            "offense_epa_play": away_off_epa, "defense_epa_play": away_def_epa,
            "havoc_rate": away_havoc,
        },
    }


def test_features_returns_two_tuple():
    package = make_package(0.2, 0.0, 0.18, 0.1, 0.0, 0.15)
    result = fh.features(package)
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_havoc_diff_favors_more_disruptive_home_defense():
    package = make_package(0.2, 0.0, 0.20, 0.2, 0.0, 0.12)
    _, havoc_diff = fh.features(package)
    assert havoc_diff > 0  # home's defense is more disruptive than away's


def test_symmetric_teams_both_diffs_zero():
    package = make_package(0.2, 0.05, 0.17, 0.2, 0.05, 0.17)
    epa_diff, havoc_diff = fh.features(package)
    assert epa_diff == 0
    assert havoc_diff == 0


def test_predict_margin_applies_both_coefficients():
    package = make_package(0.2, 0.0, 0.10, 0.0, 0.0, 0.0)
    epa_diff, havoc_diff = fh.features(package)
    margin = fh.predict_margin(package, intercept=2.5, coefs=(10.0, 5.0))
    assert margin == 10.0 * epa_diff + 5.0 * havoc_diff + 2.5


def test_features_returns_none_when_home_havoc_missing():
    package = make_package(0.2, 0.0, None, 0.1, 0.0, 0.15)
    assert fh.features(package) is None


def test_features_returns_none_when_away_havoc_missing():
    package = make_package(0.2, 0.0, 0.18, 0.1, 0.0, None)
    assert fh.features(package) is None


def test_predict_margin_returns_none_when_features_unavailable():
    package = make_package(0.2, 0.0, None, 0.1, 0.0, 0.15)
    assert fh.predict_margin(package, intercept=2.5, coefs=(10.0, 5.0)) is None
