"""
nfl_teams.py
Team-identity resolution shared by every NFL ingestion script. nflverse's
own 2-3 letter codes (ARI, ATL, ..., WAS) are this project's canonical NFL
team identifier -- nfl_games/nfl_team_stats/betting_lines(league='nfl') all
key on these, matching whatever data/backfill_nfl_games.py wrote from
nflverse's own games.csv.

Confirmed live 2026-08-24 against the real games.csv (1999-2025, every
season): 35 distinct codes total -- the 32 current franchises plus 3
retired codes for teams that relocated during the years this project's
historical archive covers (STL/SD/OAK). nflverse keeps a team's code
STABLE across most of a relocation era and only changes it the season the
franchise actually moved, confirmed by checking the real season-by-season
codes:
    Rams:      STL through 2015, LA from 2016 on
    Chargers:  SD  through 2016, LAC from 2017 on
    Raiders:   OAK through 2019, LV  from 2020 on
No other franchise changed codes in this window.
"""

NFLVERSE_CODES = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LA", "LAC", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "OAK", "PHI", "PIT", "SD", "SEA",
    "SF", "STL", "TB", "TEN", "WAS",
}

# The Odds API's NFL team names, confirmed to follow its standard
# "City Nickname" convention on every other sport this project already
# integrates (CFB) -- NOT live-verified against a real NFL response as of
# 2026-08-24: api.the-odds-api.com specifically was unreachable from this
# environment (TLS reset) while CFBD worked fine from the same machine,
# moments apart -- the exact network-level block already documented in
# ARCHITECTURE.md §11, resolved there by switching off a work WiFi. Treat
# this mapping as unverified until fetch_nfl_odds.py's first real run is
# checked against actual response team names.
ODDS_API_NFL_TEAM_TO_CODE = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Los Angeles Rams": "LA", "Los Angeles Chargers": "LAC",
    "Las Vegas Raiders": "LV", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "Seattle Seahawks": "SEA", "San Francisco 49ers": "SF", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}

def _normalize(name):
    """Strip spaces and periods before matching -- confirmed live
    2026-08-24 against the real archive that this alone (no fuzzy
    matching, no per-year special-casing) is enough: the SAME 15 committed
    files spell team names two different ways depending on which era of
    the source site produced them ("Kansas City" in the 2008-09 file,
    "KansasCity" in 2007-08/2016-17/2021-22 -- found only by actually
    diffing every unique raw name across all 15 files against the alias
    table, not assumed from a few samples). Both collapse to the same
    normalized key, so the alias tables below only need ONE spelling per
    team/era, not both."""
    return name.replace(" ", "").replace(".", "")


# sportsbookreviewsonline.com's NFL archive (2007-2022) spells team names
# inconsistently across its own history (see _normalize) -- confirmed
# against the real files, including real data-entry variants found by
# inspecting every unique name across all 15 committed seasons (not
# guessed): "BuffaloBills" (2013-14 only), "HoustonTexans" (2008-09 and
# 2013-14), "KCChiefs"/"Kansas" (2020-21 only, alongside the normal
# "KansasCity"), "LVRaiders" (2020-21 only, alongside "LasVegas"), "Tampa"
# (2020-21 only, alongside "TampaBay"), "Washingtom" (2020-21 only -- a
# literal typo for "Washington", left as-is in the source). Static,
# season-invariant part first; the 3 relocated franchises are resolved
# separately below since their SBR spelling depends on the season. Keys
# here are the PRE-NORMALIZED spelling (spaces/periods as commonly
# written) -- normalized at lookup time, see _SBR_STATIC_ALIASES_NORM.
_SBR_STATIC_ALIASES = {
    "Arizona": "ARI", "Atlanta": "ATL", "Baltimore": "BAL",
    "Buffalo": "BUF", "BuffaloBills": "BUF",
    "Carolina": "CAR", "Chicago": "CHI", "Cincinnati": "CIN", "Cleveland": "CLE",
    "Dallas": "DAL", "Denver": "DEN", "Detroit": "DET", "Green Bay": "GB",
    "Houston": "HOU", "Houston Texans": "HOU",
    "Indianapolis": "IND", "Jacksonville": "JAX",
    "Kansas City": "KC", "KC Chiefs": "KC", "Kansas": "KC",
    "Miami": "MIA", "Minnesota": "MIN",
    "NY Giants": "NYG", "NY Jets": "NYJ",
    "New England": "NE", "New Orleans": "NO",
    "Philadelphia": "PHI", "Pittsburgh": "PIT",
    "San Francisco": "SF", "Seattle": "SEA",
    "Tampa": "TB", "Tampa Bay": "TB",
    "Tennessee": "TEN", "Washington": "WAS", "Washingtom": "WAS",
}
_SBR_STATIC_ALIASES_NORM = {_normalize(k): v for k, v in _SBR_STATIC_ALIASES.items()}

# Relocated-franchise SBR spellings, by the seasons they actually appear in
# the real files (confirmed live against 2007-08/2008-09/2016-17/2021-22,
# not guessed): Rams spelled "St. Louis"/"St.Louis" through the 2015 file,
# bare "LosAngeles" ONLY in the 2016 file (the one season after moving but
# before the Chargers also relocated, so no disambiguation was needed
# yet), then "LARams" from 2017 on. Chargers "San Diego"/"SanDiego"
# through 2016, "LAChargers" from 2017 on. Raiders "Oakland" through 2019,
# "LasVegas" (and the "LVRaiders" variant, 2020-21 only) from 2020 on.
_SBR_RELOCATION_ALIASES = [
    # (sbr_name, nflverse_code, min_season_inclusive, max_season_inclusive)
    ("St. Louis", "STL", None, 2015),
    ("LosAngeles", "LA", 2016, 2016),
    ("LARams", "LA", 2017, None),
    ("San Diego", "SD", None, 2016),
    ("LAChargers", "LAC", 2017, None),
    ("Oakland", "OAK", None, 2019),
    ("LasVegas", "LV", 2020, None),
    ("LVRaiders", "LV", 2020, None),
]
_SBR_RELOCATION_ALIASES_NORM = [
    (_normalize(sbr_name), code, min_s, max_s)
    for sbr_name, code, min_s, max_s in _SBR_RELOCATION_ALIASES
]


def resolve_sbr_team(name, season):
    """SBR's raw team-name string (from the committed .xlsx archive) ->
    nflverse's 2-3 letter code for the given season, or None if it can't be
    resolved -- callers must treat None as an unresolvable row (skip and
    report), never guess. Deliberately season-aware only for the 3
    franchises that actually relocated during 2007-2022; every other name
    is a fixed, unconditional mapping. Normalized before matching (see
    _normalize) so both of the archive's two spelling conventions resolve
    without needing every name listed twice."""
    key = _normalize(name)
    if key in _SBR_STATIC_ALIASES_NORM:
        return _SBR_STATIC_ALIASES_NORM[key]
    for sbr_key, code, min_season, max_season in _SBR_RELOCATION_ALIASES_NORM:
        if key != sbr_key:
            continue
        if min_season is not None and season < min_season:
            continue
        if max_season is not None and season > max_season:
            continue
        return code
    return None
