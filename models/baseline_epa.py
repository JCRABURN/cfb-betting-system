"""
baseline_epa.py
The "dead-simple baseline" from MODEL_DESIGN.md §2/§6 -- the reference every
future feature must beat. Single feature: each team's net EPA/play (offense
minus defense), home minus away. No success rate, no havoc, no home-field
constant hardcoded -- the harness fits one slope + intercept per season via
OLS on strictly-prior seasons (the intercept absorbs home-field advantage).

Deliberately not fancier than this. A model this simple losing to closed line
movement, or landing near ~50% ATS, is the CORRECT and expected result against
an efficient market (§6) -- not a bug to chase away.
"""


def epa_differential(package):
    """net_epa(team) = offense_epa_play - defense_epa_play. Subtracting
    defense_epa_play is correct, not a sign error: it's the PPA an opposing
    OFFENSE generated against that team's defense (confirmed live during the
    point-in-time backfill), so a bigger number means a WORSE defense --
    subtracting it lowers a team's net strength, as it should."""
    home = package["home_stats"]
    away = package["away_stats"]
    home_net = home["offense_epa_play"] - home["defense_epa_play"]
    away_net = away["offense_epa_play"] - away["defense_epa_play"]
    return home_net - away_net


def predict_margin(package, slope, intercept):
    """Predicted (home points - away points). slope/intercept are fit by the
    harness via OLS on actual_margin ~ epa_differential over prior seasons --
    not hardcoded here, so home-field advantage and the EPA-to-points
    conversion are both learned from data, never guessed."""
    return slope * epa_differential(package) + intercept
