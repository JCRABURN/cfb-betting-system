import variant_damped_output as vd


def make_package(home_off, home_def, away_off, away_def):
    return {
        "home_stats": {"offense_epa_play": home_off, "defense_epa_play": home_def},
        "away_stats": {"offense_epa_play": away_off, "defense_epa_play": away_def},
    }


def test_epa_differential_matches_baseline_shape():
    package = make_package(0.2, 0.0, 0.1, 0.0)
    result = vd.epa_differential(package)
    assert result == (0.1,)


def test_damp_leaves_values_within_cap_unchanged():
    assert vd.damp(5.0) == 5.0
    assert vd.damp(-5.0) == -5.0
    assert vd.damp(10.0) == 10.0  # exactly at cap, unchanged
    assert vd.damp(-10.0) == -10.0


def test_damp_compresses_excess_beyond_cap_by_shrink_factor():
    # 30 -> 10 + 0.5*(30-10) = 20
    assert vd.damp(30.0) == 20.0
    assert vd.damp(-30.0) == -20.0


def test_damp_never_flips_sign():
    assert vd.damp(1000.0) > 0
    assert vd.damp(-1000.0) < 0


def test_predict_margin_applies_damping_to_the_linear_prediction():
    package = make_package(0.5, 0.0, -0.5, 0.0)  # epa_diff = 1.0
    # raw = intercept(0) + coef(30)*1.0 = 30 -> damped to 10 + 0.5*20 = 20
    result = vd.predict_margin(package, intercept=0.0, coefs=(30.0,))
    assert result == 20.0


def test_predict_margin_leaves_small_predictions_unchanged():
    package = make_package(0.1, 0.0, 0.0, 0.0)  # epa_diff = 0.1
    # raw = 0 + 10*0.1 = 1.0, within cap
    result = vd.predict_margin(package, intercept=0.0, coefs=(10.0,))
    assert result == 1.0
