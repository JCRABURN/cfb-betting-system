import variant_mismatch_interaction as vmi


def make_package(home_off, home_def, away_off, away_def):
    return {
        "home_stats": {"offense_epa_play": home_off, "defense_epa_play": home_def},
        "away_stats": {"offense_epa_play": away_off, "defense_epa_play": away_def},
    }


def test_features_returns_two_tuple():
    package = make_package(0.2, 0.0, 0.1, 0.0)
    result = vmi.features(package)
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_indicator_is_zero_below_threshold():
    # epa_diff = 0.1, below MISMATCH_THRESHOLD (0.30)
    package = make_package(0.1, 0.0, 0.0, 0.0)
    epa_diff, interaction = vmi.features(package)
    assert epa_diff == 0.1
    assert interaction == 0.0  # indicator=0 -> interaction term is 0


def test_indicator_is_one_above_threshold():
    # epa_diff = 0.5, above MISMATCH_THRESHOLD (0.30)
    package = make_package(0.5, 0.0, 0.0, 0.0)
    epa_diff, interaction = vmi.features(package)
    assert epa_diff == 0.5
    assert interaction == 0.5  # indicator=1 -> interaction term equals epa_diff


def test_indicator_triggers_on_large_negative_epa_diff_too():
    package = make_package(0.0, 0.0, 0.5, 0.0)  # epa_diff = -0.5
    epa_diff, interaction = vmi.features(package)
    assert epa_diff == -0.5
    assert interaction == -0.5  # |epa_diff| > threshold, indicator=1


def test_exactly_at_threshold_is_not_flagged():
    package = make_package(0.30, 0.0, 0.0, 0.0)  # epa_diff exactly 0.30
    _, interaction = vmi.features(package)
    assert interaction == 0.0  # strict > , not >=


def test_predict_margin_effective_slope_is_epa_coef_plus_interaction_coef_for_large_gap():
    package = make_package(0.5, 0.0, 0.0, 0.0)  # epa_diff = 0.5, large gap
    # predicted = intercept + epa_coef*0.5 + interaction_coef*0.5
    result = vmi.predict_margin(package, intercept=0.0, coefs=(20.0, 10.0))
    assert result == 15.0  # (20+10)*0.5


def test_predict_margin_uses_only_epa_coef_for_normal_gap():
    package = make_package(0.1, 0.0, 0.0, 0.0)  # epa_diff = 0.1, normal gap
    result = vmi.predict_margin(package, intercept=0.0, coefs=(20.0, 10.0))
    assert result == 2.0  # 20*0.1, interaction term is 0
