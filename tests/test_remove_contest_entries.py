"""Audited removal of contest_entries rows (2026-11-16)."""
import json

import pytest

import pool_view as pv


def seed(conn, n=3):
    for i in range(1, n + 1):
        conn.execute(
            "INSERT INTO contest_entries (contest, season, week, game_id, raw_home_team, raw_away_team, "
            "normalized_home_team, normalized_away_team, locked_home_spread, picked_side, rank, locked_at, source) "
            "VALUES ('pool', 2026, 1, ?, 'H', 'A', 'H', 'A', -3.5, 'H', ?, 't', 'demo')", (100 + i, i))
    conn.commit()


def ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM contest_entries ORDER BY id")]


def test_removal_archives_the_full_row_then_deletes_it(temp_db):
    conn = temp_db.get_connection()
    seed(conn)
    n = pv.remove_contest_entries(conn, [1, 2], "Week 1 demo rows, never real picks")
    left = ids(conn)
    arch = conn.execute("SELECT original_entry_id, payload, reason FROM contest_entries_removed ORDER BY id").fetchall()
    conn.close()
    assert n == 2 and left == [3]
    assert [a[0] for a in arch] == [1, 2]
    p = json.loads(arch[0][1])
    assert p["picked_side"] == "H" and p["locked_home_spread"] == -3.5 and p["source"] == "demo" and p["rank"] == 1
    assert arch[0][2] == "Week 1 demo rows, never real picks"


def test_a_missing_id_removes_nothing_at_all(temp_db):
    conn = temp_db.get_connection()
    seed(conn)
    with pytest.raises(ValueError, match="nothing removed"):
        pv.remove_contest_entries(conn, [1, 999], "reason")
    assert ids(conn) == [1, 2, 3]
    assert conn.execute("SELECT COUNT(*) FROM contest_entries_removed").fetchone()[0] == 0
    conn.close()


def test_reason_is_required(temp_db):
    conn = temp_db.get_connection()
    seed(conn)
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            pv.remove_contest_entries(conn, [1], bad)
    assert ids(conn) == [1, 2, 3]
    conn.close()


def test_empty_id_list_is_refused(temp_db):
    conn = temp_db.get_connection()
    with pytest.raises(ValueError):
        pv.remove_contest_entries(conn, [], "reason")
    conn.close()


def test_entry_with_a_correction_on_file_cannot_be_removed(temp_db):
    conn = temp_db.get_connection()
    seed(conn)
    pv.correct_contest_entry(conn, 1, "typo", new_locked_home_spread=-4.0)
    with pytest.raises(ValueError, match="corrections on file"):
        pv.remove_contest_entries(conn, [1, 2], "reason")
    assert ids(conn) == [1, 2, 3]                       # all-or-nothing: id 2 not removed either
    assert conn.execute("SELECT COUNT(*) FROM contest_entries_removed").fetchone()[0] == 0
    conn.close()


def test_removed_rows_no_longer_count_in_grading(temp_db):
    import post_game_audit as pga
    conn = temp_db.get_connection()
    seed(conn, n=1)
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team, completed, home_points, away_points) "
                 "VALUES (101, 2026, 1, 'H', 'A', 1, 30, 10)")
    conn.commit()
    assert pga.grade_contest_entries(conn, 2026)["overall"]["n"] == 1
    pv.remove_contest_entries(conn, [1], "demo")
    assert pga.grade_contest_entries(conn, 2026)["overall"]["n"] == 0
    conn.close()
