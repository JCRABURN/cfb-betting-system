from fetch_odds import resolve_school_name, has_games_this_week, filter_by_week

SCHOOLS = [
    "TCU", "USC", "NC State", "Ohio", "Ohio State", "Miami", "Miami (OH)",
    "Albany", "Georgia", "Texas", "Hawai'i", "Massachusetts", "Southern Miss",
    "App State", "San José State",
]


def test_resolves_simple_mascot_suffix():
    assert resolve_school_name(SCHOOLS, "TCU Horned Frogs") == "TCU"
    assert resolve_school_name(SCHOOLS, "USC Trojans") == "USC"


def test_resolves_multiword_school_name():
    assert resolve_school_name(SCHOOLS, "NC State Wolfpack") == "NC State"


def test_exact_match_when_no_mascot_present():
    assert resolve_school_name(SCHOOLS, "Albany") == "Albany"


def test_disambiguates_prefix_collision_longer_wins():
    # Both "Ohio" and "Ohio State" are valid prefixes of "Ohio State Buckeyes";
    # the longer, more specific one must win.
    assert resolve_school_name(SCHOOLS, "Ohio State Buckeyes") == "Ohio State"
    assert resolve_school_name(SCHOOLS, "Ohio Bobcats") == "Ohio"


def test_disambiguates_miami_variants():
    assert resolve_school_name(SCHOOLS, "Miami Hurricanes") == "Miami"
    assert resolve_school_name(SCHOOLS, "Miami (OH) RedHawks") == "Miami (OH)"


def test_falls_back_to_original_name_when_unresolvable():
    assert resolve_school_name(SCHOOLS, "Some Unlisted Team Mascots") == "Some Unlisted Team Mascots"


def test_empty_school_list_falls_back():
    assert resolve_school_name([], "TCU Horned Frogs") == "TCU Horned Frogs"


def test_accent_normalization_resolves_san_jose_state():
    # Odds API spells it without the accent; CFBD's official name has "José".
    assert resolve_school_name(SCHOOLS, "San Jose State Spartans") == "San José State"


def test_apostrophe_normalization_resolves_hawaii():
    assert resolve_school_name(SCHOOLS, "Hawaii Rainbow Warriors") == "Hawai'i"


def test_known_aliases_with_no_shared_prefix():
    assert resolve_school_name(SCHOOLS, "Appalachian State Mountaineers") == "App State"
    assert resolve_school_name(SCHOOLS, "UMass Minutemen") == "Massachusetts"
    assert resolve_school_name(SCHOOLS, "Southern Mississippi Golden Eagles") == "Southern Miss"


def test_alias_not_used_if_canonical_name_missing_from_schools():
    # If the canonical alias target isn't actually in the teams table, don't force it --
    # fall through to normal matching (which will also fail here, so return unchanged).
    schools_without_umass = [s for s in SCHOOLS if s != "Massachusetts"]
    assert resolve_school_name(schools_without_umass, "UMass Minutemen") == "UMass Minutemen"


# ---------------------------------------------------------------------------
# has_games_this_week -- replaces the old ephemeral data/stats/week_*.json
# read, which never survived a GitHub Actions checkout (see module docstring)
# ---------------------------------------------------------------------------

def test_has_games_this_week_true_when_games_exist(temp_db):
    conn = temp_db.get_connection()
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team) VALUES (1, 2026, 3, 'A', 'B')"
    )
    conn.commit()
    assert has_games_this_week(conn, 2026, 3) is True
    conn.close()


def test_has_games_this_week_false_when_no_games(temp_db):
    conn = temp_db.get_connection()
    assert has_games_this_week(conn, 2026, 3) is False
    conn.close()


def test_has_games_this_week_is_specific_to_season_and_week(temp_db):
    conn = temp_db.get_connection()
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team) VALUES (1, 2026, 3, 'A', 'B')"
    )
    conn.commit()
    assert has_games_this_week(conn, 2026, 4) is False  # different week
    assert has_games_this_week(conn, 2025, 3) is False  # different season
    conn.close()


# ---------------------------------------------------------------------------
# filter_by_week -- keeps only games whose commence_time is inside the
# target week's actual date range (added 2026-08-04 after mislabeled
# future-game rows were found polluting week 1's betting_lines)
# ---------------------------------------------------------------------------

WEEK1_START = "2026-08-27T07:00:00Z"
WEEK1_END = "2026-09-02T06:59:00Z"


def make_game(commence_time, home="A", away="B"):
    return {"game_id": "x", "home_team": home, "away_team": away, "commence_time": commence_time}


def test_filter_by_week_keeps_games_inside_the_range():
    games = [make_game("2026-08-29T16:00:00Z")]
    result = filter_by_week(games, WEEK1_START, WEEK1_END)
    assert result == games


def test_filter_by_week_drops_games_months_out():
    games = [make_game("2026-11-28T17:00:00Z", "Ohio State", "Michigan")]
    result = filter_by_week(games, WEEK1_START, WEEK1_END)
    assert result == []


def test_filter_by_week_drops_games_before_the_range():
    games = [make_game("2026-08-01T00:00:00Z")]
    result = filter_by_week(games, WEEK1_START, WEEK1_END)
    assert result == []


def test_filter_by_week_keeps_games_exactly_at_the_boundaries():
    games = [make_game(WEEK1_START), make_game(WEEK1_END)]
    result = filter_by_week(games, WEEK1_START, WEEK1_END)
    assert len(result) == 2


def test_filter_by_week_drops_games_with_no_commence_time():
    games = [make_game(None)]
    result = filter_by_week(games, WEEK1_START, WEEK1_END)
    assert result == []


def test_filter_by_week_mixed_slate_keeps_only_the_current_week():
    games = [
        make_game("2026-08-29T16:00:00Z", "TCU", "UNC"),
        make_game("2026-11-28T17:00:00Z", "Ohio State", "Michigan"),
        make_game("2026-10-04T19:00:00Z", "Texas", "Oklahoma"),
    ]
    result = filter_by_week(games, WEEK1_START, WEEK1_END)
    assert len(result) == 1
    assert result[0]["home_team"] == "TCU"
