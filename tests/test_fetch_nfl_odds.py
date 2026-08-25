import fetch_nfl_odds as fno


def insert_nfl_game(conn, game_id, season, week, home, away):
    conn.execute(
        "INSERT INTO nfl_games (game_id, season, week, game_type, gameday, home_team, away_team, "
        "completed, source, fetched_at) VALUES (?, ?, ?, 'REG', '2026-09-13', ?, ?, 0, 'test', 'now')",
        (game_id, season, week, home, away),
    )


def make_game(home_name, away_name, spreads):
    """spreads: {book_key: (home_point, away_point)}"""
    bookmakers = []
    for book, (home_pt, away_pt) in spreads.items():
        bookmakers.append({
            "key": book,
            "markets": [{"key": "spreads", "outcomes": [
                {"name": home_name, "point": home_pt},
                {"name": away_name, "point": away_pt},
            ]}],
        })
    return {"id": "oddsapi123", "home_team": home_name, "away_team": away_name, "bookmakers": bookmakers}


def test_resolve_and_match_real_game(temp_db):
    conn = temp_db.get_connection()
    insert_nfl_game(conn, "2026_02_BUF_MIA", 2026, 2, "MIA", "BUF")
    conn.commit()
    result = fno.resolve_and_match(conn, 2026, "Miami Dolphins", "Buffalo Bills")
    conn.close()
    assert result == ("2026_02_BUF_MIA", 2, "MIA", "BUF")


def test_resolve_and_match_unknown_team_name(temp_db):
    conn = temp_db.get_connection()
    result = fno.resolve_and_match(conn, 2026, "Made Up Team", "Buffalo Bills")
    conn.close()
    assert result is None


def test_resolve_and_match_no_scheduled_game(temp_db):
    conn = temp_db.get_connection()
    result = fno.resolve_and_match(conn, 2026, "Miami Dolphins", "Buffalo Bills")
    conn.close()
    assert result is None


def test_persist_lines_writes_league_nfl_and_resolved_teams(temp_db):
    conn = temp_db.get_connection()
    insert_nfl_game(conn, "2026_02_BUF_MIA", 2026, 2, "MIA", "BUF")
    conn.commit()

    game = make_game("Miami Dolphins", "Buffalo Bills", {
        "draftkings": (-3.0, 3.0), "fanduel": (-2.5, 2.5),
    })
    rows_added = fno.persist_lines_to_db(conn, [game], 2026)

    rows = conn.execute(
        "SELECT book, home_spread, league, line_type, game_id, home_team, away_team FROM betting_lines "
        "WHERE game_id = '2026_02_BUF_MIA' ORDER BY book"
    ).fetchall()
    conn.close()

    assert rows_added == 3  # 2 books + 1 consensus
    by_book = {r[0]: r for r in rows}
    assert by_book["draftkings"] == ("draftkings", -3.0, "nfl", "current", "2026_02_BUF_MIA", "MIA", "BUF")
    assert by_book["consensus"][1] == -2.8  # round(average(-3.0, -2.5), 1)


def test_persist_lines_unmatched_game_gets_null_game_id(temp_db):
    conn = temp_db.get_connection()
    game = make_game("Made Up Team", "Buffalo Bills", {"draftkings": (-3.0, 3.0)})
    fno.persist_lines_to_db(conn, [game], 2026)
    row = conn.execute(
        "SELECT game_id, league FROM betting_lines WHERE book = 'draftkings'"
    ).fetchone()
    conn.close()
    assert row == (None, "nfl")
