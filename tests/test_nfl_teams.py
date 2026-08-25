import nfl_teams as nt


def test_static_alias_resolves_regardless_of_spacing():
    """The real archive spells the same team two ways across different
    years (e.g. 'Kansas City' vs 'KansasCity') -- both must resolve."""
    assert nt.resolve_sbr_team("KansasCity", 2007) == "KC"
    assert nt.resolve_sbr_team("Kansas City", 2008) == "KC"


def test_static_alias_data_entry_variants():
    assert nt.resolve_sbr_team("BuffaloBills", 2013) == "BUF"
    assert nt.resolve_sbr_team("Washingtom", 2020) == "WAS"
    assert nt.resolve_sbr_team("KC Chiefs", 2020) == "KC"


def test_unknown_name_returns_none_not_a_guess():
    assert nt.resolve_sbr_team("NewYork", 2013) is None
    assert nt.resolve_sbr_team("TotallyMadeUpTeam", 2015) is None


def test_rams_relocation_by_season():
    assert nt.resolve_sbr_team("St. Louis", 2015) == "STL"
    assert nt.resolve_sbr_team("St.Louis", 2010) == "STL"
    assert nt.resolve_sbr_team("LosAngeles", 2016) == "LA"
    assert nt.resolve_sbr_team("LARams", 2017) == "LA"
    assert nt.resolve_sbr_team("LARams", 2021) == "LA"
    # Wrong-season spellings must NOT resolve -- e.g. "St. Louis" doesn't
    # exist as a franchise identity by 2017, it should stay unresolved
    # under that name rather than silently mapping to STL anyway.
    assert nt.resolve_sbr_team("St. Louis", 2017) is None
    assert nt.resolve_sbr_team("LARams", 2015) is None


def test_chargers_relocation_by_season():
    assert nt.resolve_sbr_team("SanDiego", 2016) == "SD"
    assert nt.resolve_sbr_team("San Diego", 2010) == "SD"
    assert nt.resolve_sbr_team("LAChargers", 2017) == "LAC"
    assert nt.resolve_sbr_team("SanDiego", 2017) is None


def test_raiders_relocation_by_season():
    assert nt.resolve_sbr_team("Oakland", 2019) == "OAK"
    assert nt.resolve_sbr_team("LasVegas", 2020) == "LV"
    assert nt.resolve_sbr_team("LVRaiders", 2020) == "LV"
    assert nt.resolve_sbr_team("Oakland", 2020) is None


def test_odds_api_map_covers_all_32_current_teams_with_real_codes():
    assert len(nt.ODDS_API_NFL_TEAM_TO_CODE) == 32
    for code in nt.ODDS_API_NFL_TEAM_TO_CODE.values():
        assert code in nt.NFLVERSE_CODES


def test_nflverse_codes_include_relocated_franchise_codes():
    assert {"STL", "SD", "OAK"}.issubset(nt.NFLVERSE_CODES)
