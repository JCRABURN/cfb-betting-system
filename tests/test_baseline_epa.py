import baseline_epa as bepa


def make_package(home_off, home_def, away_off, away_def):
    return {
        "home_stats": {"offense_epa_play": home_off, "defense_epa_play": home_def},
        "away_stats": {"offense_epa_play": away_off, "defense_epa_play": away_def},
    }


def test_epa_differential_favors_better_offense():
    package = make_package(0.30, 0.0, 0.10, 0.0)
    assert bepa.epa_differential(package) > 0


def test_epa_differential_worse_defense_lowers_net_strength():
    # Higher defense_epa_play = worse defense (PPA opponents generate against
    # them) -- a team with a worse defense should have LOWER net strength,
    # confirming the subtraction sign is correct, not a bug.
    weak_defense = make_package(0.20, 0.20, 0.20, 0.0)
    strong_defense = make_package(0.20, -0.10, 0.20, 0.0)
    assert bepa.epa_differential(weak_defense) < bepa.epa_differential(strong_defense)


def test_epa_differential_symmetric_teams_is_zero():
    package = make_package(0.20, 0.05, 0.20, 0.05)
    assert bepa.epa_differential(package) == 0


def test_predict_margin_applies_fitted_slope_and_intercept():
    package = make_package(0.20, 0.0, 0.0, 0.0)  # differential = 0.20
    margin = bepa.predict_margin(package, slope=10.0, intercept=2.5)
    assert margin == 10.0 * 0.20 + 2.5
