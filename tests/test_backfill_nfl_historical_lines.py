import openpyxl
import pytest

import backfill_nfl_historical_lines as bhl

HEADER = ["Date", "Rot", "VH", "Team", "1st", "2nd", "3rd", "4th", "Final", "Open", "Close", "ML", "2H"]


def write_sbr_xlsx(path, games):
    """games: list of (date, [row1_dict, row2_dict]) where each row dict
    has vh/team/open/close/ml -- matches the real archive's shape."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER)
    for date, rows in games:
        for r in rows:
            ws.append([date, r.get("rot", 100), r["vh"], r["team"], 0, 0, 0, 0, 0,
                       r["open"], r["close"], r.get("ml", -110)])
    wb.save(path)


def insert_nfl_game(conn, game_id, season, week, home, away):
    conn.execute(
        "INSERT INTO nfl_games (game_id, season, week, game_type, gameday, home_team, away_team, "
        "completed, source, fetched_at) VALUES (?, ?, ?, 'REG', '2021-09-09', ?, ?, 0, 'test', 'now')",
        (game_id, season, week, home, away),
    )


# Season-INVARIANT teams only (excludes the 3 relocated franchises --
# St.Louis/LosAngeles/LARams, SanDiego/LAChargers, Oakland/LasVegas/
# LVRaiders -- whose SBR spelling depends on the season, so a fixed test
# fixture can't use them safely across an arbitrary `season` param).
_PADDING_TEAMS = ["Arizona", "Atlanta", "Baltimore", "Buffalo", "Carolina", "Chicago",
                  "Cincinnati", "Cleveland", "Denver", "Detroit", "GreenBay", "Indianapolis",
                  "Jacksonville", "Miami", "Minnesota", "NewOrleans", "Philadelphia", "Seattle",
                  "SanFrancisco", "Tennessee", "Washington", "Pittsburgh", "NewEngland", "Houston",
                  "Dallas", "NYGiants", "NYJets", "KansasCity", "TampaBay"]  # 29 teams -> 14 pairs


def padding_games(conn, season, n):
    """N extra clean, resolvable games -- keeps a test fixture's overall
    skip rate under SKIP_RATE_ALARM_THRESHOLD so a single deliberately
    broken game can be tested in isolation without tripping the
    hard-fail meant for a genuinely structural problem. Cycles through
    weeks to reuse the 16 real team pairs without a team facing itself.
    nfl_games rows use the RESOLVED code (what ingest_season will look
    up); the .xlsx fixture uses the raw SBR-style name (what a real
    archive row has)."""
    games = []
    n_pairs = len(_PADDING_TEAMS) // 2
    for i in range(n):
        pair_idx = i % n_pairs
        week = i // n_pairs + 1
        home_raw, away_raw = _PADDING_TEAMS[2 * pair_idx], _PADDING_TEAMS[2 * pair_idx + 1]
        home_code = bhl.resolve_sbr_team(home_raw, season)
        away_code = bhl.resolve_sbr_team(away_raw, season)
        game_id = f"pad_{season}_{i}"
        insert_nfl_game(conn, game_id, season, week, home_code, away_code)
        games.append((900 + i, [
            {"vh": "V", "team": away_raw, "open": 44.5, "close": 45.0},
            {"vh": "H", "team": home_raw, "open": 3.0, "close": 3.5},
        ]))
    return games


# ---------------------------------------------------------------------------
# _to_num / _classify_cell / _resolve_column -- the validated pairing algorithm
# ---------------------------------------------------------------------------

def test_to_num_handles_pick_em():
    assert bhl._to_num("pk") == 0.0
    assert bhl._to_num("PK") == 0.0


def test_to_num_handles_numeric_and_blank():
    assert bhl._to_num(7.5) == 7.5
    assert bhl._to_num("7.5") == 7.5
    assert bhl._to_num(None) is None
    assert bhl._to_num("garbage") is None


def test_classify_cell_spread_and_total():
    assert bhl._classify_cell(7.0) == "spread"
    assert bhl._classify_cell(45.5) == "total"


def test_classify_cell_gap_zone_is_ambiguous():
    assert bhl._classify_cell(27.0) is None  # between SPREAD_MAX and TOTAL_MIN


def test_resolve_column_requires_exactly_one_of_each():
    assert bhl._resolve_column(7.0, 45.5) == ("spread", "total")
    assert bhl._resolve_column(45.5, 7.0) == ("total", "spread")


def test_resolve_column_unresolvable_when_both_same_class():
    assert bhl._resolve_column(7.0, 3.5) is None  # both spread-shaped
    assert bhl._resolve_column(45.5, 48.0) is None  # both total-shaped


def test_resolve_column_unresolvable_on_gap_value():
    assert bhl._resolve_column(27.0, 7.0) is None


# ---------------------------------------------------------------------------
# ingest_season -- end to end against synthetic .xlsx fixtures
# ---------------------------------------------------------------------------

def test_ingest_season_normal_game(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_nfl_game(conn, "2021_01_TB_DAL", 2021, 1, "TB", "DAL")
    conn.commit()

    path = tmp_path / "nfl_odds_2021.xlsx"
    write_sbr_xlsx(path, [
        (909, [
            {"vh": "V", "team": "Dallas", "open": 52.5, "close": 52.5},
            {"vh": "H", "team": "TampaBay", "open": 7, "close": 10},
        ]),
    ])
    bhl.ODDS_DIR = str(tmp_path)

    result = bhl.ingest_season(conn, 2021)
    rows = conn.execute(
        "SELECT line_type, home_spread, total FROM betting_lines "
        "WHERE game_id='2021_01_TB_DAL' ORDER BY line_type"
    ).fetchall()
    conn.close()

    assert result["inserted"] == 2
    assert result["unresolved"] == []
    assert set(rows) == {("closing", -10.0, 52.5), ("opening", -7.0, 52.5)}


def test_ingest_season_favorite_flip_between_open_and_close(temp_db, tmp_path):
    """The real phenomenon that broke the naive per-row model: the
    favorite (and therefore which row's cell holds the spread) can
    genuinely differ between open and close."""
    conn = temp_db.get_connection()
    insert_nfl_game(conn, "2013_10_HOU_KC", 2013, 10, "KC", "HOU")
    conn.commit()

    path = tmp_path / "nfl_odds_2013.xlsx"
    write_sbr_xlsx(path, [
        (1110, [
            {"vh": "V", "team": "HoustonTexans", "open": 38.5, "close": 3},
            {"vh": "H", "team": "KansasCity", "open": "pk", "close": 37.5},
        ]),
    ])
    bhl.ODDS_DIR = str(tmp_path)

    result = bhl.ingest_season(conn, 2013)
    rows = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT line_type, home_spread, total FROM betting_lines WHERE game_id='2013_10_HOU_KC'"
    ).fetchall()}
    conn.close()

    assert result["unresolved"] == []
    # At open, KC (home) held the spread cell (pk=0) -> home_spread=0.0, total=38.5
    assert rows["opening"] == (0.0, 38.5)
    # At close, HOU (away) held the spread cell (3, favorite) -> home is the
    # underdog now -> home_spread=+3.0, total=37.5
    assert rows["closing"] == (3.0, 37.5)


def test_ingest_season_neutral_site_resolves_via_nfl_games(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_nfl_game(conn, "2013_SB_SEA_DEN", 2013, 21, "DEN", "SEA")
    conn.commit()

    path = tmp_path / "nfl_odds_2013.xlsx"
    write_sbr_xlsx(path, [
        (202, [
            {"vh": "N", "team": "Seattle", "open": 2.5, "close": 2.5},
            {"vh": "N", "team": "Denver", "open": 43.5, "close": 47},
        ]),
    ])
    bhl.ODDS_DIR = str(tmp_path)

    result = bhl.ingest_season(conn, 2013)
    row = conn.execute(
        "SELECT home_team, away_team, home_spread FROM betting_lines "
        "WHERE game_id='2013_SB_SEA_DEN' AND line_type='opening'"
    ).fetchone()
    conn.close()

    assert result["unresolved"] == []
    # nfl_games designates DEN home -- Seattle's row (2.5) held the spread
    # cell, Seattle is away, so home (DEN) is the underdog: home_spread=+2.5
    assert row == ("DEN", "SEA", 2.5)


def test_ingest_season_unresolved_team_name_is_skipped_and_reported(temp_db, tmp_path):
    conn = temp_db.get_connection()
    games = padding_games(conn, 2013, 25)
    conn.commit()
    games.append((1110, [
        {"vh": "V", "team": "Oakland", "open": 3, "close": 3},
        {"vh": "H", "team": "NewYork", "open": 44, "close": 45},
    ]))
    path = tmp_path / "nfl_odds_2013.xlsx"
    write_sbr_xlsx(path, games)
    bhl.ODDS_DIR = str(tmp_path)

    result = bhl.ingest_season(conn, 2013)
    count = conn.execute(
        "SELECT COUNT(*) FROM betting_lines WHERE game_id LIKE 'pad_%'"
    ).fetchone()[0]
    conn.close()

    assert result["inserted"] == 50  # the 25 padding games, 2 rows each
    assert count == 50
    assert len(result["unresolved"]) == 1
    assert "NewYork" in result["unresolved"][0]["reason"]


def test_ingest_season_ambiguous_spread_total_is_skipped_and_reported(temp_db, tmp_path):
    conn = temp_db.get_connection()
    games = padding_games(conn, 2021, 25)
    insert_nfl_game(conn, "2021_01_ATL_ARI", 2021, 1, "ATL", "ARI")
    conn.commit()
    games.append((909, [
        {"vh": "V", "team": "Arizona", "open": 3.5, "close": 3.5},
        {"vh": "H", "team": "Atlanta", "open": 7.0, "close": 7.0},
    ]))
    path = tmp_path / "nfl_odds_2021.xlsx"
    write_sbr_xlsx(path, games)
    bhl.ODDS_DIR = str(tmp_path)

    result = bhl.ingest_season(conn, 2021)
    conn.close()

    assert result["inserted"] == 50
    assert len(result["unresolved"]) == 1
    assert "ambiguous" in result["unresolved"][0]["reason"]


def test_ingest_season_no_matching_nfl_games_row_is_skipped_and_reported(temp_db, tmp_path):
    """nfl_games not populated for THIS game -- must report why, not
    silently drop or crash (see module docstring's stated dependency).
    Padding games ARE in nfl_games, so this isolates the one missing
    game specifically."""
    conn = temp_db.get_connection()
    games = padding_games(conn, 2021, 25)
    conn.commit()
    games.append((909, [
        {"vh": "V", "team": "Dallas", "open": 52.5, "close": 52.5},
        {"vh": "H", "team": "TampaBay", "open": 7, "close": 10},
    ]))
    path = tmp_path / "nfl_odds_2021.xlsx"
    write_sbr_xlsx(path, games)
    bhl.ODDS_DIR = str(tmp_path)

    result = bhl.ingest_season(conn, 2021)
    conn.close()

    assert result["inserted"] == 50
    assert len(result["unresolved"]) == 1
    assert "no matching nfl_games row" in result["unresolved"][0]["reason"]


def test_ingest_season_hard_fails_over_the_skip_rate_threshold(temp_db, tmp_path):
    """More than SKIP_RATE_ALARM_THRESHOLD unresolved -> a structural
    problem, not the small expected residual -- must raise, not silently
    complete a mostly-broken ingest."""
    conn = temp_db.get_connection()
    conn.commit()
    path = tmp_path / "nfl_odds_2021.xlsx"
    # Every game uses unresolvable team names -- 100% skip rate.
    write_sbr_xlsx(path, [
        (909, [
            {"vh": "V", "team": "TotallyFakeTeamA", "open": 3, "close": 3},
            {"vh": "H", "team": "TotallyFakeTeamB", "open": 44, "close": 45},
        ]),
    ])
    bhl.ODDS_DIR = str(tmp_path)

    with pytest.raises(RuntimeError, match="unresolved"):
        bhl.ingest_season(conn, 2021)
    conn.close()


def test_ingest_season_idempotent_without_force(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_nfl_game(conn, "2021_01_TB_DAL", 2021, 1, "TB", "DAL")
    conn.commit()
    path = tmp_path / "nfl_odds_2021.xlsx"
    write_sbr_xlsx(path, [
        (909, [
            {"vh": "V", "team": "Dallas", "open": 52.5, "close": 52.5},
            {"vh": "H", "team": "TampaBay", "open": 7, "close": 10},
        ]),
    ])
    bhl.ODDS_DIR = str(tmp_path)

    first = bhl.ingest_season(conn, 2021)
    second = bhl.ingest_season(conn, 2021)
    count = conn.execute("SELECT COUNT(*) FROM betting_lines").fetchone()[0]
    conn.close()

    assert first["inserted"] == 2
    assert second["inserted"] == 0
    assert count == 2
