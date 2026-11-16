"""Audited correction of stored pool_lines (2026-11-16)."""
import pytest

import ingest_pool_slate as ips


def seed(conn, gid=10, home="Home", away="Away", week=1):
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team) VALUES (?, 2026, ?, ?, ?)",
                 (gid, week, home, away))
    conn.execute("INSERT INTO pool_lines (contest, season, week, game_id, home_team, away_team, pool_home_spread, "
                 "pool_total, source, ingested_at) VALUES ('pool', 2026, ?, ?, ?, ?, -8.5, 55.5, 'orig', 'n')",
                 (week, gid, home, away))
    conn.commit()


def G(spread, total):
    return [{"away": "Away", "home": "Home", "home_spread": spread, "total": total}]


def stored(conn):
    return conn.execute("SELECT pool_home_spread, pool_total FROM pool_lines").fetchone()


def test_correction_updates_and_archives_the_original(temp_db):
    conn = temp_db.get_connection()
    seed(conn)
    r = ips.correct_slate(conn, G(-6.5, 54.5), 2026, 1, "hand transcription error", "slate:pdf")
    arch = conn.execute("SELECT old_pool_home_spread, old_pool_total, new_pool_home_spread, new_pool_total, reason, "
                        "source FROM pool_lines_corrections").fetchall()
    now = stored(conn)
    conn.close()
    assert r["corrected"] == 1 and now == (-6.5, 54.5)
    assert arch == [(-8.5, 55.5, -6.5, 54.5, "hand transcription error", "slate:pdf")]


def test_reason_is_required_and_nothing_changes_without_it(temp_db):
    conn = temp_db.get_connection()
    seed(conn)
    for bad in ("", "  ", None):
        with pytest.raises(ValueError):
            ips.correct_slate(conn, G(-6.5, 54.5), 2026, 1, bad, "s")
    assert stored(conn) == (-8.5, 55.5)
    conn.close()


def test_identical_numbers_are_left_alone_and_idempotent(temp_db):
    conn = temp_db.get_connection()
    seed(conn)
    ips.correct_slate(conn, G(-6.5, 54.5), 2026, 1, "fix", "s")
    again = ips.correct_slate(conn, G(-6.5, 54.5), 2026, 1, "fix", "s")
    n = conn.execute("SELECT COUNT(*) FROM pool_lines_corrections").fetchone()[0]
    conn.close()
    assert again["corrected"] == 0 and again["unchanged"] == 1 and n == 1


def test_a_correction_never_inserts_a_game_that_was_not_stored(temp_db):
    conn = temp_db.get_connection()
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team) VALUES (11, 2026, 1, 'Home', 'Away')")
    conn.commit()
    r = ips.correct_slate(conn, G(-6.5, 54.5), 2026, 1, "fix", "s")
    n = conn.execute("SELECT COUNT(*) FROM pool_lines").fetchone()[0]
    conn.close()
    assert r["not_stored"][0]["game_id"] == 11 and n == 0


def test_unresolvable_game_is_reported(temp_db):
    conn = temp_db.get_connection()
    r = ips.correct_slate(conn, G(-6.5, 54.5), 2026, 1, "fix", "s")
    conn.close()
    assert r["unmatched"][0]["away"] == "Away" and r["corrected"] == 0


def test_normal_ingest_still_refuses_to_overwrite(temp_db):
    """The correction path must not weaken the default immutability."""
    conn = temp_db.get_connection()
    seed(conn)
    r = ips.ingest_slate(conn, G(-6.5, 54.5), 2026, 1, "t")
    assert r["inserted"] == 0 and len(r["conflicts"]) == 1 and stored(conn) == (-8.5, 55.5)
    conn.close()
