from fetch_odds import resolve_school_name

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
