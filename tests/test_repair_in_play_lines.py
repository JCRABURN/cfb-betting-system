"""repair_in_play_lines: relabel post-kickoff 'current' rows exactly, never
sweep in a legitimate pre-game row, and be idempotent (2026-10-14)."""
import json

import repair_in_play_lines as rep

REQUESTED = "2026-10-03T17:51:25.558203"
PULL_AT = "2026-10-03T17:51:26.129837"        # persist stamps `now` a moment after the request


def archive(conn, games, requested_at=REQUESTED):
    conn.execute(
        "INSERT INTO raw_payloads (provider, endpoint, request_params, requested_at, http_status, "
        "checksum, body_gzip, parser_version, rows_accepted, rows_rejected) "
        "VALUES ('the_odds_api', '/odds', '{}', ?, 200, 'x', ?, 'v', 0, 0)",
        (requested_at, __import__("gzip").compress(json.dumps(games).encode())))


def game(conn, gid, home, away):
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team, start_date) "
                 "VALUES (?, 2026, 5, ?, ?, '2026-10-03T19:30:00.000Z')", (gid, home, away))


def row(conn, gid, home, away, fetched_at, spread, line_type="current"):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, "
        "line_type, source, fetched_at) VALUES (?, 2026, 5, ?, ?, 'draftkings', ?, ?, 't', ?)",
        (gid, home, away, spread, line_type, fetched_at))


def types(conn):
    return [r[0] for r in conn.execute("SELECT line_type FROM betting_lines ORDER BY id")]


def test_find_in_play_events_uses_the_feeds_own_commence_time():
    games = [
        {"home_team": "A", "away_team": "B", "commence_time": "2026-10-03T15:02:25Z"},   # started
        {"home_team": "C", "away_team": "D", "commence_time": "2026-10-03T19:30:00Z"},   # upcoming
        {"home_team": "E", "away_team": "F"},                                             # no time
    ]
    assert rep.find_in_play_events(games, REQUESTED) == [("A", "B")]


def test_relabels_only_the_post_kickoff_snapshot(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, "Charlotte", "Memphis")
    row(conn, 1, "Charlotte", "Memphis", "2026-10-01T19:19:59", 20.5)        # genuine pre-game
    row(conn, 1, "Charlotte", "Memphis", PULL_AT, 49.5)                       # in-play snapshot
    archive(conn, [{"home_team": "Charlotte 49ers", "away_team": "Memphis Tigers",
                    "commence_time": "2026-10-03T15:02:25Z"}])
    conn.commit()
    # Charlotte 49ers -> "Charlotte" resolves only if the school is known
    conn.execute("INSERT INTO teams (school) VALUES ('Charlotte')")
    conn.execute("INSERT INTO teams (school) VALUES ('Memphis')")
    conn.commit()

    result = rep.repair(conn)
    out = types(conn)
    conn.close()
    assert result["rows_relabeled"] == 1
    assert out == ["current", "in_play"]


def test_a_later_pull_of_the_same_matchup_is_not_swept_in(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, "Charlotte", "Memphis")
    row(conn, 1, "Charlotte", "Memphis", PULL_AT, 49.5)
    row(conn, 1, "Charlotte", "Memphis", "2026-10-03T20:30:00", 55.5)        # an hour later, separate pull
    archive(conn, [{"home_team": "Charlotte 49ers", "away_team": "Memphis Tigers",
                    "commence_time": "2026-10-03T15:02:25Z"}])
    conn.execute("INSERT INTO teams (school) VALUES ('Charlotte')")
    conn.execute("INSERT INTO teams (school) VALUES ('Memphis')")
    conn.commit()
    rep.repair(conn)
    out = types(conn)
    conn.close()
    assert out == ["in_play", "current"]


def test_upcoming_game_in_the_same_payload_is_untouched(temp_db):
    conn = temp_db.get_connection()
    game(conn, 2, "Duke", "Tulane")
    row(conn, 2, "Duke", "Tulane", PULL_AT, -7.5)
    archive(conn, [{"home_team": "Duke Blue Devils", "away_team": "Tulane Green Wave",
                    "commence_time": "2026-10-03T23:00:00Z"}])
    conn.execute("INSERT INTO teams (school) VALUES ('Duke')")
    conn.execute("INSERT INTO teams (school) VALUES ('Tulane')")
    conn.commit()
    assert rep.repair(conn)["rows_relabeled"] == 0
    assert types(conn) == ["current"]
    conn.close()


def test_idempotent_and_dry_run_changes_nothing(temp_db):
    conn = temp_db.get_connection()
    game(conn, 1, "Charlotte", "Memphis")
    row(conn, 1, "Charlotte", "Memphis", PULL_AT, 49.5)
    archive(conn, [{"home_team": "Charlotte 49ers", "away_team": "Memphis Tigers",
                    "commence_time": "2026-10-03T15:02:25Z"}])
    conn.execute("INSERT INTO teams (school) VALUES ('Charlotte')")
    conn.execute("INSERT INTO teams (school) VALUES ('Memphis')")
    conn.commit()

    dry = rep.repair(conn, dry_run=True)
    assert dry["rows_relabeled"] == 1 and types(conn) == ["current"]     # reported, not applied
    assert rep.repair(conn)["rows_relabeled"] == 1
    assert rep.repair(conn)["rows_relabeled"] == 0                       # second run: nothing left
    conn.close()


def test_relabeled_row_no_longer_reaches_get_latest_line(temp_db):
    """End to end: the point of the repair. Even with a stale start_date that
    the read-side filter can't catch, the in-play number stops being 'latest'."""
    import line_utils as lu
    conn = temp_db.get_connection()
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team, start_date) "
                 "VALUES (1, 2026, 5, 'Charlotte', 'Memphis', '2026-10-03T23:59:00.000Z')")  # stale: far too late
    row(conn, 1, "Charlotte", "Memphis", "2026-10-01T19:19:59", 20.5)
    row(conn, 1, "Charlotte", "Memphis", PULL_AT, 49.5)
    archive(conn, [{"home_team": "Charlotte 49ers", "away_team": "Memphis Tigers",
                    "commence_time": "2026-10-03T15:02:25Z"}])
    conn.execute("INSERT INTO teams (school) VALUES ('Charlotte')")
    conn.execute("INSERT INTO teams (school) VALUES ('Memphis')")
    conn.commit()

    assert lu.get_latest_line(conn, 1, prefer_book="draftkings")["home_spread"] == 49.5   # the bug
    rep.repair(conn)
    assert lu.get_latest_line(conn, 1, prefer_book="draftkings")["home_spread"] == 20.5   # fixed
    conn.close()
