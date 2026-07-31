import feature_rest as fr


def make_package(home_off_epa, home_def_epa, home_rest, away_off_epa, away_def_epa, away_rest):
    return {
        "home_stats": {"offense_epa_play": home_off_epa, "defense_epa_play": home_def_epa},
        "away_stats": {"offense_epa_play": away_off_epa, "defense_epa_play": away_def_epa},
        "home_days_rest": home_rest,
        "away_days_rest": away_rest,
    }


def test_features_returns_three_tuple():
    package = make_package(0.2, 0.0, 7, 0.1, 0.0, 7)
    result = fr.features(package)
    assert isinstance(result, tuple)
    assert len(result) == 3


def test_rest_diff_positive_when_home_more_rested():
    package = make_package(0.2, 0.0, 14, 0.2, 0.0, 7)
    _, rest_diff, _ = fr.features(package)
    assert rest_diff == 7


def test_bye_diff_flags_home_coming_off_bye():
    package = make_package(0.2, 0.0, 14, 0.2, 0.0, 7)  # home had a bye (14 days), away didn't
    _, _, bye_diff = fr.features(package)
    assert bye_diff == 1


def test_bye_diff_zero_when_neither_had_a_bye():
    package = make_package(0.2, 0.0, 7, 0.2, 0.0, 6)
    _, _, bye_diff = fr.features(package)
    assert bye_diff == 0


def test_bye_diff_zero_when_both_had_byes():
    package = make_package(0.2, 0.0, 13, 0.2, 0.0, 14)
    _, _, bye_diff = fr.features(package)
    assert bye_diff == 0


def test_bye_threshold_boundary():
    # Exactly at the threshold counts as a bye; one day short does not.
    at_threshold = make_package(0.2, 0.0, fr.BYE_THRESHOLD_DAYS, 0.2, 0.0, fr.BYE_THRESHOLD_DAYS - 1)
    _, _, bye_diff = fr.features(at_threshold)
    assert bye_diff == 1


def test_features_returns_none_when_home_rest_missing():
    package = make_package(0.2, 0.0, None, 0.1, 0.0, 7)
    assert fr.features(package) is None


def test_features_returns_none_when_away_rest_missing():
    package = make_package(0.2, 0.0, 7, 0.1, 0.0, None)
    assert fr.features(package) is None


def test_predict_margin_applies_all_three_coefficients():
    package = make_package(0.2, 0.0, 14, 0.0, 0.0, 7)
    epa_diff, rest_diff, bye_diff = fr.features(package)
    margin = fr.predict_margin(package, intercept=2.5, coefs=(10.0, 0.1, 1.0))
    assert margin == 10.0 * epa_diff + 0.1 * rest_diff + 1.0 * bye_diff + 2.5


def test_predict_margin_returns_none_when_features_unavailable():
    package = make_package(0.2, 0.0, None, 0.1, 0.0, 7)
    assert fr.predict_margin(package, intercept=2.5, coefs=(10.0, 0.1, 1.0)) is None
