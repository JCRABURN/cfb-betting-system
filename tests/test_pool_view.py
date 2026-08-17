import pytest

import pool_view as pv


def insert_game(conn, game_id, season, week, home, away, completed=0,
                 start_date="2023-01-01T00:00:00.000Z"):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, completed, start_date) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (game_id, season, week, home, away, completed, start_date),
    )


def insert_line(conn, game_id, season, week, home, away, home_spread, line_type, book="consensus", total=45.0):
    insert_game(conn, game_id, season, week, home, away)
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'test', 'now')",
        (game_id, season, week, home, away, book, home_spread, total, line_type),
    )


def test_market_moves_toward_a_home_pick(temp_db):
    """Pool had home favored by 3; live line now favors home by 6 --
    good news for a home pick (market agrees more, not less)."""
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-6.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    conn.close()

    game = view["games"][0]
    assert game["drift"] == -3.0
    assert game["signed_drift_vs_pick"] == 3.0
    assert game["movement_vs_pick"] == "toward_pick"
    assert game["favorite_flipped"] is False


def test_market_moves_away_from_a_home_pick(temp_db):
    """Pool had home favored by 6; live line now only favors home by 1 --
    bad news for a home pick (market likes them less than at pool-lock)."""
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-1.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -6.0, "picked_side": "X"},
    ])
    conn.close()

    game = view["games"][0]
    assert game["signed_drift_vs_pick"] == -5.0
    assert game["movement_vs_pick"] == "away_from_pick"


def test_market_moves_toward_an_away_pick(temp_db):
    """Pool had home favored by 3 (away +3); live line now has away
    favored by 2 (home +2) -- good news for an away pick, and the
    favorite has flipped."""
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=2.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "Y"},
    ])
    conn.close()

    game = view["games"][0]
    assert game["signed_drift_vs_pick"] == 5.0
    assert game["movement_vs_pick"] == "toward_pick"
    assert game["favorite_flipped"] is True  # pool favored X, live favors Y


def test_favorite_flip_against_the_pick(temp_db):
    """Pool had home favored by 3, user picked home; live line now has
    away favored by 1 -- the favorite flipped against the pick."""
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=1.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    conn.close()

    game = view["games"][0]
    assert game["favorite_flipped"] is True
    assert game["movement_vs_pick"] == "away_from_pick"


def test_flat_when_no_movement(temp_db):
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "X", "Y", home_spread=-3.0, line_type="current")
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    conn.close()

    assert view["games"][0]["movement_vs_pick"] == "flat"
    assert view["games"][0]["favorite_flipped"] is False


def test_sorted_worst_for_pick_first(temp_db):
    conn = temp_db.get_connection()
    insert_line(conn, 1, 2023, 5, "A", "B", home_spread=-3.0, line_type="current")  # unchanged, flat
    insert_line(conn, 2, 2023, 5, "C", "D", home_spread=2.0, line_type="current")   # flipped against pick
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "A", "away_team": "B", "pool_home_spread": -3.0, "picked_side": "A"},
        {"game_id": 2, "home_team": "C", "away_team": "D", "pool_home_spread": -3.0, "picked_side": "C"},
    ])
    conn.close()

    assert view["games"][0]["game_id"] == 2  # worst (moved against pick) first
    assert view["games"][1]["game_id"] == 1


def test_missing_live_line_is_skipped_not_crashed(temp_db):
    conn = temp_db.get_connection()
    conn.commit()

    view = pv.build_pool_view(conn, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    conn.close()

    assert view["games"] == []
    assert view["skipped"][0]["reason"] == "no_live_line"


def test_load_pool_entries_reads_csv(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side\n"
        "401636915,Oklahoma State,Arizona State,-1.0,Oklahoma State\n"
        "401628409,Mississippi State,Massachusetts,-17.5,Mississippi State\n",
        encoding="utf-8",
    )

    entries = pv.load_pool_entries(str(csv_path))

    assert len(entries) == 2
    assert entries[0] == {
        "game_id": 401636915, "home_team": "Oklahoma State", "away_team": "Arizona State",
        "pool_home_spread": -1.0, "picked_side": "Oklahoma State", "rank": None,
    }
    assert entries[1]["pool_home_spread"] == -17.5


def test_load_pool_entries_strips_whitespace(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side\n"
        "1, Home Team , Away Team ,-3.0, Home Team \n",
        encoding="utf-8",
    )

    entries = pv.load_pool_entries(str(csv_path))

    assert entries[0]["home_team"] == "Home Team"
    assert entries[0]["picked_side"] == "Home Team"


def test_load_pool_entries_ignores_extra_columns(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side,notes\n"
        "1,A,B,-3.0,A,some note\n",
        encoding="utf-8",
    )

    entries = pv.load_pool_entries(str(csv_path))

    assert entries[0]["game_id"] == 1
    assert "notes" not in entries[0]


# ---------------------------------------------------------------------------
# Optional confidence rank, 1-5 nullable (added 2026-08-13)
# ---------------------------------------------------------------------------

def test_load_pool_entries_reads_rank(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side,rank\n"
        "1,A,B,-3.0,A,5\n",
        encoding="utf-8",
    )
    entries = pv.load_pool_entries(str(csv_path))
    assert entries[0]["rank"] == 5


def test_load_pool_entries_blank_rank_is_none(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side,rank\n"
        "1,A,B,-3.0,A,\n",
        encoding="utf-8",
    )
    entries = pv.load_pool_entries(str(csv_path))
    assert entries[0]["rank"] is None


def test_load_pool_entries_missing_rank_column_entirely_is_none(tmp_path):
    """Backward compatibility: a CSV written before this column existed
    (no 'rank' header at all) must still load, not raise KeyError."""
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side\n"
        "1,A,B,-3.0,A\n",
        encoding="utf-8",
    )
    entries = pv.load_pool_entries(str(csv_path))
    assert entries[0]["rank"] is None


def test_load_pool_entries_rejects_rank_out_of_range(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side,rank\n"
        "1,A,B,-3.0,A,6\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        pv.load_pool_entries(str(csv_path))


def test_load_pool_entries_rejects_rank_zero(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side,rank\n"
        "1,A,B,-3.0,A,0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        pv.load_pool_entries(str(csv_path))


def test_ingest_contest_csv_stores_rank(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0,
         "picked_side": "X", "rank": 4},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)

    row = conn.execute("SELECT rank FROM contest_entries").fetchone()
    conn.close()
    assert row == (4,)


def test_ingest_contest_csv_rank_defaults_to_null(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)

    row = conn.execute("SELECT rank FROM contest_entries").fetchone()
    conn.close()
    assert row == (None,)


def test_load_pool_entries_from_db_returns_rank(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0,
         "picked_side": "X", "rank": 2},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)

    entries = pv.load_pool_entries_from_db(conn, season=2026, week=1)
    conn.close()
    assert entries[0]["rank"] == 2


def test_correct_contest_entry_can_change_rank_preserving_original(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0,
         "picked_side": "X", "rank": 3},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    entry_id = conn.execute("SELECT id FROM contest_entries").fetchone()[0]

    pv.correct_contest_entry(conn, entry_id, reason="Misjudged confidence, actually my top pick",
                              new_rank=5)

    updated_rank = conn.execute("SELECT rank FROM contest_entries WHERE id = ?", (entry_id,)).fetchone()[0]
    correction = conn.execute(
        "SELECT original_rank, corrected_rank FROM contest_entry_corrections"
    ).fetchone()
    conn.close()

    assert updated_rank == 5
    assert correction == (3, 5)  # original preserved, corrected recorded


def test_correct_contest_entry_rejects_rank_out_of_range(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()
    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    entry_id = conn.execute("SELECT id FROM contest_entries").fetchone()[0]

    with pytest.raises(ValueError):
        pv.correct_contest_entry(conn, entry_id, reason="oops", new_rank=7)
    conn.close()


def test_no_model_fields_present():
    """build_pool_view() itself must never carry a model prediction/side/
    edge field -- it's a pure line-drift read (ARCHITECTURE.md §19-20: no
    demonstrated edge). Scoped to build_pool_view's own source, not the
    whole module: rank_pool_picks() (external review, accepted 2026-08-04)
    legitimately uses model edge, but only as an authorized tiebreaker
    behind drift confirmation, in a separate function with its own
    extensive doc-comment explaining why that's still safe."""
    import inspect
    source = inspect.getsource(pv.build_pool_view)
    for forbidden in ("predicted_margin", "recommended_side", '"edge":', '"confidence":'):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# contest_entries ingestion + correction (external review, accepted 2026-08-04)
# ---------------------------------------------------------------------------

def insert_team(conn, school):
    conn.execute("INSERT OR IGNORE INTO teams (school) VALUES (?)", (school,))


def write_pool_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write("game_id,home_team,away_team,pool_home_spread,picked_side,rank\n")
        for r in rows:
            f.write(f'{r["game_id"]},{r["home_team"]},{r["away_team"]},{r["pool_home_spread"]},'
                     f'{r["picked_side"]},{r.get("rank", "")}\n')


# ---------------------------------------------------------------------------
# Optional game_id, resolved via team names (added 2026-08-13)
# ---------------------------------------------------------------------------

def test_load_pool_entries_blank_game_id_is_none(tmp_path):
    csv_path = tmp_path / "picks.csv"
    csv_path.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side\n"
        ",Oklahoma State,Arizona State,-1.0,Oklahoma State\n",
        encoding="utf-8",
    )
    entries = pv.load_pool_entries(str(csv_path))
    assert entries[0]["game_id"] is None


def test_ingest_contest_csv_resolves_blank_game_id_from_team_names(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    insert_game(conn, 555, 2026, 1, "X", "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": "", "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])

    result = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    row = conn.execute("SELECT game_id FROM contest_entries").fetchone()
    conn.close()

    assert result == {"inserted": 1, "skipped": [], "unmatched": []}
    assert row == (555,)


def test_ingest_contest_csv_resolves_blank_game_id_via_alias(temp_db, tmp_path):
    """Resolution goes through fetch_odds.resolve_school_name() first, same
    as the explicit-game_id path -- "Appalachian State" (the Odds-API-style
    alias key) in the CSV must still find the game keyed on
    games.home_team='App State' (the CFBD-canonical form, the alias's
    value)."""
    conn = temp_db.get_connection()
    insert_team(conn, "App State")
    insert_team(conn, "Georgia Southern")
    insert_game(conn, 777, 2026, 1, "App State", "Georgia Southern")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": "", "home_team": "Appalachian State", "away_team": "Georgia Southern",
         "pool_home_spread": -3.0, "picked_side": "Appalachian State"},
    ])

    result = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    row = conn.execute("SELECT game_id FROM contest_entries").fetchone()
    conn.close()

    assert result["inserted"] == 1
    assert row == (777,)


def test_ingest_contest_csv_reports_unmatched_blank_game_id_rows(temp_db, tmp_path):
    """No game exists for these team names/week/season -- must be reported
    in `unmatched`, NOT silently dropped, and NOT inserted at all."""
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    # Deliberately no matching row in `games` for (2026, week 1, X, Y).
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": "", "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])

    result = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    count = conn.execute("SELECT COUNT(*) FROM contest_entries").fetchone()[0]
    conn.close()

    assert result["inserted"] == 0
    assert result["skipped"] == []
    assert result["unmatched"] == [
        {"raw_home_team": "X", "raw_away_team": "Y", "normalized_home_team": "X", "normalized_away_team": "Y"},
    ]
    assert count == 0


def test_ingest_contest_csv_mixes_explicit_and_blank_game_id_in_one_file(temp_db, tmp_path):
    """Explicit game_id rows must behave exactly as before, unaffected by
    other rows in the same file needing resolution."""
    conn = temp_db.get_connection()
    insert_team(conn, "A")
    insert_team(conn, "B")
    insert_team(conn, "C")
    insert_team(conn, "D")
    insert_game(conn, 2, 2026, 1, "C", "D")  # only the blank-game_id row needs this
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "A", "away_team": "B", "pool_home_spread": -3.0, "picked_side": "A"},
        {"game_id": "", "home_team": "C", "away_team": "D", "pool_home_spread": -6.0, "picked_side": "D"},
    ])

    result = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    game_ids = {r[0] for r in conn.execute("SELECT game_id FROM contest_entries").fetchall()}
    conn.close()

    assert result["inserted"] == 2
    assert result["unmatched"] == []
    assert game_ids == {1, 2}


def test_ingest_contest_csv_inserts_rows(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "Oklahoma State")
    insert_team(conn, "Arizona State")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "Oklahoma State", "away_team": "Arizona State",
         "pool_home_spread": -3.0, "picked_side": "Oklahoma State"},
    ])

    result = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    conn.close()

    assert result == {"inserted": 1, "skipped": [], "unmatched": []}
    conn = temp_db.get_connection()
    row = conn.execute(
        "SELECT contest, season, week, game_id, raw_home_team, normalized_home_team, "
        "locked_home_spread, picked_side, source FROM contest_entries"
    ).fetchone()
    conn.close()
    assert row == (
        "pool", 2026, 1, 1, "Oklahoma State", "Oklahoma State", -3.0,
        "Oklahoma State", f"csv:{csv_path}",
    )


def test_ingest_contest_csv_is_idempotent(temp_db, tmp_path):
    """Re-ingesting the same CSV (e.g. the workflow re-running, or the pool
    file being read again with no changes) must not duplicate or update
    the already-locked row -- inserted once, per the schema's UNIQUE
    constraint and INSERT OR IGNORE."""
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])

    first = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    second = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)

    count = conn.execute("SELECT COUNT(*) FROM contest_entries").fetchone()[0]
    conn.close()

    assert first == {"inserted": 1, "skipped": [], "unmatched": []}
    assert second == {"inserted": 0, "skipped": [{"game_id": 1, "home_team": "X", "away_team": "Y"}],
                       "unmatched": []}
    assert count == 1


def test_ingest_contest_csv_reports_skipped_rows_explicitly(temp_db, tmp_path):
    """Explicitly required (external review follow-up, accepted
    2026-08-05): a caller re-running ingest after fixing a CSV typo must
    be able to see WHICH games were skipped as already-locked, not just an
    unremarkable inserted=0 that could be mistaken for "nothing to do"."""
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    insert_team(conn, "A")
    insert_team(conn, "B")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)

    # Re-ingest with game 1 unchanged (already locked) plus a brand-new
    # game 2 -- the result must distinguish the two, not lump them together.
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -9.0, "picked_side": "X"},
        {"game_id": 2, "home_team": "A", "away_team": "B", "pool_home_spread": -1.0, "picked_side": "A"},
    ])
    result = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    conn.close()

    assert result["inserted"] == 1
    assert result["skipped"] == [{"game_id": 1, "home_team": "X", "away_team": "Y"}]


def test_reingesting_a_changed_csv_does_not_alter_the_locked_spread(temp_db, tmp_path):
    """The property the contest audit actually depends on: once a
    (contest, season, week, game_id) is locked, NOTHING but
    correct_contest_entry() can change its locked_home_spread/picked_side
    -- not even re-ingesting a CSV that now disagrees with it (a hand
    edit, a re-run against a "corrected" file, a typo fix someone made
    directly in the sheet instead of going through the correction path).
    ingest_contest_csv() must refuse the new value, not silently apply it."""
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    entry_id = conn.execute("SELECT id FROM contest_entries").fetchone()[0]

    # Overwrite the CSV in place with a DIFFERENT spread and a DIFFERENT
    # picked_side for the SAME game_id/season/week -- simulating either an
    # accidental re-run or someone hand-editing the committed file after
    # the fact instead of using correct_contest_entry().
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -7.5, "picked_side": "Y"},
    ])
    result = pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)

    row = conn.execute(
        "SELECT id, locked_home_spread, picked_side, corrected_at FROM contest_entries WHERE game_id = 1"
    ).fetchone()
    count = conn.execute("SELECT COUNT(*) FROM contest_entries").fetchone()[0]
    conn.close()

    assert result["inserted"] == 0  # nothing new inserted -- the row was refused, not overwritten
    assert result["skipped"] == [{"game_id": 1, "home_team": "X", "away_team": "Y"}]
    assert count == 1     # no duplicate row created either
    assert row[0] == entry_id
    assert row[1] == -3.0   # ORIGINAL locked spread, untouched by the re-ingest
    assert row[2] == "X"    # ORIGINAL picked_side, untouched by the re-ingest
    assert row[3] is None   # corrected_at still NULL -- only correct_contest_entry() sets it


def test_ingest_contest_csv_normalizes_picked_side(temp_db, tmp_path):
    """Uses fetch_odds.KNOWN_TEAM_ALIASES ("Appalachian State" -> "App
    State", the CFBD-canonical form) to prove picked_side, stored raw in
    the CSV, resolves to the same normalized name as its matching
    home/away team -- not a coincidentally-equal string."""
    conn = temp_db.get_connection()
    insert_team(conn, "App State")
    insert_team(conn, "Georgia Southern")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "Appalachian State", "away_team": "Georgia Southern",
         "pool_home_spread": -3.0, "picked_side": "Appalachian State"},
    ])

    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    row = conn.execute(
        "SELECT normalized_home_team, picked_side FROM contest_entries"
    ).fetchone()
    conn.close()

    assert row == ("App State", "App State")


def test_load_pool_entries_from_db_matches_locked_values(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)

    entries = pv.load_pool_entries_from_db(conn, season=2026, week=1)
    conn.close()

    assert len(entries) == 1
    assert entries[0]["game_id"] == 1
    assert entries[0]["home_team"] == "X"
    assert entries[0]["pool_home_spread"] == -3.0
    assert entries[0]["picked_side"] == "X"


def test_correct_contest_entry_preserves_original_values(temp_db, tmp_path):
    """The explicitly required proof: correcting a locked entry must leave
    an audit row carrying the ORIGINAL locked_home_spread/picked_side --
    not the corrected ones -- and must not lose them even though
    contest_entries itself is updated in place."""
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()

    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    entry_id = conn.execute("SELECT id FROM contest_entries").fetchone()[0]

    pv.correct_contest_entry(
        conn, entry_id, reason="Typo in pool sheet -- actual locked line was -3.5",
        new_locked_home_spread=-3.5,
    )

    updated = conn.execute(
        "SELECT locked_home_spread, picked_side, corrected_at FROM contest_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    correction = conn.execute(
        "SELECT original_entry_id, original_locked_home_spread, original_picked_side, "
        "corrected_locked_home_spread, corrected_picked_side, reason FROM contest_entry_corrections"
    ).fetchone()
    conn.close()

    # contest_entries reflects the correction...
    assert updated[0] == -3.5
    assert updated[1] == "X"  # unchanged field stays as-is
    assert updated[2] is not None

    # ...but the ledger preserves the ORIGINAL row's values, not the new ones.
    assert correction[0] == entry_id
    assert correction[1] == -3.0  # original spread, preserved
    assert correction[2] == "X"   # original picked_side, preserved
    assert correction[3] == -3.5  # corrected spread
    assert correction[4] is None  # picked_side wasn't corrected
    assert "Typo" in correction[5]


def test_correct_contest_entry_requires_reason(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()
    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    entry_id = conn.execute("SELECT id FROM contest_entries").fetchone()[0]

    with pytest.raises(ValueError):
        pv.correct_contest_entry(conn, entry_id, reason="", new_locked_home_spread=-3.5)
    conn.close()


def test_correct_contest_entry_requires_a_new_value(temp_db, tmp_path):
    conn = temp_db.get_connection()
    insert_team(conn, "X")
    insert_team(conn, "Y")
    conn.commit()
    csv_path = tmp_path / "picks.csv"
    write_pool_csv(csv_path, [
        {"game_id": 1, "home_team": "X", "away_team": "Y", "pool_home_spread": -3.0, "picked_side": "X"},
    ])
    pv.ingest_contest_csv(conn, str(csv_path), season=2026, week=1)
    entry_id = conn.execute("SELECT id FROM contest_entries").fetchone()[0]

    with pytest.raises(ValueError):
        pv.correct_contest_entry(conn, entry_id, reason="no-op attempt")
    conn.close()


# ---------------------------------------------------------------------------
# rank_pool_picks (external review's one accepted gap, 2026-08-04)
# ---------------------------------------------------------------------------

def make_card(games):
    return {"games": games}


def test_rank_pool_picks_sorts_by_drift_confirmation():
    pool = {"games": [
        {"game_id": 1, "signed_drift_vs_pick": 1.0},
        {"game_id": 2, "signed_drift_vs_pick": 5.0},
        {"game_id": 3, "signed_drift_vs_pick": -2.0},
    ]}
    ranked = pv.rank_pool_picks(pool)
    assert [g["game_id"] for g in ranked] == [2, 1, 3]
    assert [g["rank"] for g in ranked] == [1, 2, 3]


def test_rank_pool_picks_uses_edge_only_as_tiebreaker():
    pool = {"games": [
        {"game_id": 1, "signed_drift_vs_pick": 3.0},
        {"game_id": 2, "signed_drift_vs_pick": 3.0},
    ]}
    card = make_card([
        {"game_id": 1, "confidence": "standard", "edge": 2.0},
        {"game_id": 2, "confidence": "standard", "edge": 7.0},
    ])
    ranked = pv.rank_pool_picks(pool, card)
    # Drift tied at 3.0 for both -- higher edge (game 2) wins the tiebreak.
    assert [g["game_id"] for g in ranked] == [2, 1]

    # But a bigger edge NEVER outranks better drift confirmation.
    pool2 = {"games": [
        {"game_id": 1, "signed_drift_vs_pick": 3.0},
        {"game_id": 2, "signed_drift_vs_pick": 1.0},
    ]}
    card2 = make_card([
        {"game_id": 1, "confidence": "standard", "edge": 0.5},
        {"game_id": 2, "confidence": "standard", "edge": 9.0},
    ])
    ranked2 = pv.rank_pool_picks(pool2, card2)
    assert [g["game_id"] for g in ranked2] == [1, 2]


def test_rank_pool_picks_excludes_low_confidence_flagged_games():
    pool = {"games": [
        {"game_id": 1, "signed_drift_vs_pick": 10.0},  # best drift, but flagged
        {"game_id": 2, "signed_drift_vs_pick": 1.0},
    ]}
    card = make_card([
        {"game_id": 1, "confidence": "low_confidence_large_edge", "edge": 15.0},
        {"game_id": 2, "confidence": "standard", "edge": 2.0},
    ])
    ranked = pv.rank_pool_picks(pool, card)
    assert [g["game_id"] for g in ranked] == [2]


def test_rank_pool_picks_keeps_games_with_no_card_match():
    """A pool pick the model didn't line at all this week (no matching
    card game_id) isn't a flagged game -- it's simply unscored by the
    model, so it stays eligible, ranked on drift alone."""
    pool = {"games": [
        {"game_id": 1, "signed_drift_vs_pick": 4.0},
    ]}
    card = make_card([
        {"game_id": 999, "confidence": "standard", "edge": 3.0},
    ])
    ranked = pv.rank_pool_picks(pool, card)
    assert len(ranked) == 1
    assert ranked[0]["game_id"] == 1
    assert ranked[0]["edge"] is None


def test_rank_pool_picks_caps_at_five():
    pool = {"games": [{"game_id": i, "signed_drift_vs_pick": float(i)} for i in range(8)]}
    ranked = pv.rank_pool_picks(pool)
    assert len(ranked) == 5
    assert [g["game_id"] for g in ranked] == [7, 6, 5, 4, 3]
