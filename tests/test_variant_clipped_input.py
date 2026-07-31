import variant_clipped_input as vci


def make_package(home_off, home_def, away_off, away_def):
    return {
        "home_stats": {"offense_epa_play": home_off, "defense_epa_play": home_def},
        "away_stats": {"offense_epa_play": away_off, "defense_epa_play": away_def},
    }


def test_values_within_cap_unchanged():
    package = make_package(0.2, 0.0, 0.1, 0.0)  # epa_diff = 0.1
    assert vci.epa_differential(package) == (0.1,)


def test_value_exactly_at_cap_unchanged():
    package = make_package(0.40, 0.0, 0.0, 0.0)  # epa_diff = 0.40
    assert vci.epa_differential(package) == (0.40,)


def test_value_beyond_cap_is_clipped():
    package = make_package(0.80, 0.0, 0.0, 0.0)  # epa_diff = 0.80
    assert vci.epa_differential(package) == (0.40,)


def test_negative_value_beyond_cap_is_clipped_symmetrically():
    package = make_package(0.0, 0.0, 0.80, 0.0)  # epa_diff = -0.80
    assert vci.epa_differential(package) == (-0.40,)


def test_predict_margin_uses_clipped_value():
    package = make_package(0.80, 0.0, 0.0, 0.0)  # raw epa_diff 0.80, clipped to 0.40
    result = vci.predict_margin(package, intercept=1.0, coefs=(50.0,))
    assert result == 1.0 + 50.0 * 0.40  # not 50.0*0.80
