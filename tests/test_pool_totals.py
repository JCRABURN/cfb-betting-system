"""Pool totals: frozen over/under entries, drift vs the live total, grading,
the table rebuild, and the cross-market ranking (2026-10-14).

The pool lane is FROZEN at the Tuesday-noon line lock; these tests pin that
the locked number is never overwritten and is only ever compared to the
live, market-following total."""
import sqlite3

import pytest

import pool_view as pv
import post_game_audit as pga

KICKOFF = "2026-10-17T16:00:00.000Z"


def game(conn, gid=1, completed=0, hp=None, ap=None):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team, start_date, completed, "
        "home_points, away_points) VALUES (?, 2026, 7, 'Home', 'Away', ?, ?, ?, ?)",
        (gid, KICKOFF, completed, hp, ap))


def live_total(conn, total, gid=1, fetched="2026-10-16T12:00:00", book="consensus"):
    conn.execute(
        "INSERT INTO betting_lines (game_id, season, week, home_team, away_team, book, home_spread, total, "
        "line_type, source, fetched_at) VALUES (?, 2026, 7, 'Home', 'Away', ?, -3.0, ?, 'current', 't', ?)",
        (gid, book, total, fetched))


def write_csv(tmp_path, body):
    p = tmp_path / "week_7.csv"
    p.write_text(
        "game_id,home_team,away_team,pool_home_spread,picked_side,rank,market_type,locked_total\n" + body,
        encoding="utf-8")
    return str(p)


# ---- CSV parsing --------------------------------------------------------------

def test_total_row_parses_over_under_and_locked_total(tmp_path):
    path = write_csv(tmp_path, "1,Home,Away,,Over,5,total,51.5\n")
    e = pv.load_pool_entries(path)[0]
    assert (e["market_type"], e["locked_total"], e["picked_side"], e["pool_home_spread"]) == \
           ("total", 51.5, "over", None)


def test_spread_row_unchanged_when_market_type_blank(tmp_path):
    path = write_csv(tmp_path, "1,Home,Away,-3.5,Home,4,,\n")
    e = pv.load_pool_entries(path)[0]
    assert (e["market_type"], e["pool_home_spread"], e["locked_total"]) == ("spread", -3.5, None)


def test_old_csv_without_new_columns_still_loads(tmp_path):
    p = tmp_path / "old.csv"
    p.write_text("game_id,home_team,away_team,pool_home_spread,picked_side,rank\n1,Home,Away,-3.5,Home,3\n",
                 encoding="utf-8")
    assert pv.load_pool_entries(str(p))[0]["market_type"] == "spread"


@pytest.mark.parametrize("body", [
    "1,Home,Away,,Home,5,total,51.5\n",     # total picking a team, not over/under
    "1,Home,Away,,over,5,total,\n",         # total with no locked number
    "1,Home,Away,,over,5,moneyline,51.5\n",  # unknown market
    "1,Home,Away,,Home,5,spread,\n",        # spread with no spread
])
def test_malformed_rows_fail_loudly(tmp_path, body):
    with pytest.raises(ValueError):
        pv.load_pool_entries(write_csv(tmp_path, body))


# ---- ingestion + immutability ---------------------------------------------------

def test_spread_and_total_for_same_game_both_lock(temp_db, tmp_path):
    conn = temp_db.get_connection()
    game(conn)
    conn.commit()
    path = write_csv(tmp_path, "1,Home,Away,-3.5,Home,4,spread,\n1,Home,Away,,under,5,total,51.5\n")
    result = pv.ingest_contest_csv(conn, path, 2026, 7)
    n = conn.execute("SELECT COUNT(*) FROM contest_entries").fetchone()[0]
    conn.close()
    assert result["inserted"] == 2 and n == 2


def test_reingest_does_not_overwrite_a_locked_total(temp_db, tmp_path):
    conn = temp_db.get_connection()
    game(conn)
    conn.commit()
    pv.ingest_contest_csv(conn, write_csv(tmp_path, "1,Home,Away,,over,5,total,51.5\n"), 2026, 7)
    again = pv.ingest_contest_csv(conn, write_csv(tmp_path, "1,Home,Away,,over,5,total,54.0\n"), 2026, 7)
    locked = conn.execute("SELECT locked_total FROM contest_entries").fetchone()[0]
    conn.close()
    assert again["inserted"] == 0 and again["skipped"][0]["market_type"] == "total"
    assert locked == 51.5


def test_correct_total_entry_audits_original_and_stays_in_market(temp_db, tmp_path):
    conn = temp_db.get_connection()
    game(conn)
    conn.commit()
    pv.ingest_contest_csv(conn, write_csv(tmp_path, "1,Home,Away,,over,5,total,51.5\n"), 2026, 7)
    eid = conn.execute("SELECT id FROM contest_entries").fetchone()[0]

    with pytest.raises(ValueError):      # a spread number on a total entry is nonsense
        pv.correct_contest_entry(conn, eid, "typo", new_locked_home_spread=-3.0)
    with pytest.raises(ValueError):      # a team is not over/under
        pv.correct_contest_entry(conn, eid, "typo", new_picked_side="Home")

    pv.correct_contest_entry(conn, eid, "misread 51.5 as 52.5", new_locked_total=52.5)
    entry = conn.execute("SELECT locked_total FROM contest_entries WHERE id=?", (eid,)).fetchone()[0]
    audit = conn.execute(
        "SELECT original_locked_total, corrected_locked_total FROM contest_entry_corrections").fetchone()
    conn.close()
    assert entry == 52.5 and audit == (51.5, 52.5)


def test_cannot_set_a_total_on_a_spread_entry(temp_db, tmp_path):
    conn = temp_db.get_connection()
    game(conn)
    conn.commit()
    pv.ingest_contest_csv(conn, write_csv(tmp_path, "1,Home,Away,-3.5,Home,4,spread,\n"), 2026, 7)
    eid = conn.execute("SELECT id FROM contest_entries").fetchone()[0]
    with pytest.raises(ValueError):
        pv.correct_contest_entry(conn, eid, "x", new_locked_total=51.5)
    conn.close()


# ---- drift vs the LIVE total ------------------------------------------------------

def _entry(side, locked):
    return {"game_id": 1, "home_team": "Home", "away_team": "Away", "market_type": "total",
            "picked_side": side, "locked_total": locked, "pool_home_spread": None}


def test_over_confirmed_when_market_total_rises(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    live_total(conn, 54.0)
    conn.commit()
    g = pv.build_pool_view(conn, [_entry("over", 51.5)])["games"][0]
    conn.close()
    assert (g["drift"], g["signed_drift_vs_pick"], g["movement_vs_pick"]) == (2.5, 2.5, "toward_pick")


def test_under_confirmed_when_market_total_falls(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    live_total(conn, 49.0)
    conn.commit()
    g = pv.build_pool_view(conn, [_entry("under", 51.5)])["games"][0]
    conn.close()
    assert g["signed_drift_vs_pick"] == 2.5


def test_over_hurt_when_market_total_falls(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    live_total(conn, 49.0)
    conn.commit()
    g = pv.build_pool_view(conn, [_entry("over", 51.5)])["games"][0]
    conn.close()
    assert (g["signed_drift_vs_pick"], g["movement_vs_pick"]) == (-2.5, "away_from_pick")


def test_in_play_total_is_never_used_as_the_live_number(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    live_total(conn, 52.0, fetched="2026-10-16T12:00:00")
    live_total(conn, 78.5, fetched="2026-10-17T17:30:00")     # after kickoff
    conn.commit()
    g = pv.build_pool_view(conn, [_entry("over", 51.5)])["games"][0]
    conn.close()
    assert g["live_total"] == 52.0


def test_total_entry_with_no_live_total_is_reported_not_dropped(temp_db):
    conn = temp_db.get_connection()
    game(conn)
    conn.commit()
    v = pv.build_pool_view(conn, [_entry("over", 51.5)])
    conn.close()
    assert v["games"] == [] and v["skipped"][0]["reason"] == "no_live_total"


# ---- grading ----------------------------------------------------------------------

@pytest.mark.parametrize("side,locked,pts,expected", [
    ("over", 51.5, 55, "win"), ("over", 51.5, 48, "loss"),
    ("under", 51.5, 48, "win"), ("under", 51.5, 55, "loss"),
    ("over", 52.0, 52, "push"), ("under", 52.0, 52, "push"),
])
def test_grade_total(side, locked, pts, expected):
    assert pga.grade_total(side, locked, pts) == expected


def test_contest_grading_uses_the_frozen_total_not_the_live_one(temp_db, tmp_path):
    conn = temp_db.get_connection()
    game(conn, completed=1, hp=30, ap=24)          # 54 combined
    live_total(conn, 60.0)                          # market later moved well past the lock
    conn.commit()
    pv.ingest_contest_csv(conn, write_csv(tmp_path, "1,Home,Away,,over,5,total,51.5\n"), 2026, 7)
    report = pga.grade_contest_entries(conn, 2026)
    conn.close()
    assert report["overall"]["win"] == 1                       # 54 > frozen 51.5
    assert report["by_market"]["total"]["win"] == 1
    assert report["by_rank"][5]["win"] == 1


def test_spread_and_total_grade_independently_in_one_game(temp_db, tmp_path):
    conn = temp_db.get_connection()
    game(conn, completed=1, hp=30, ap=24)          # Home wins by 6, 54 total
    conn.commit()
    pv.ingest_contest_csv(
        conn, write_csv(tmp_path, "1,Home,Away,-3.5,Home,4,spread,\n1,Home,Away,,under,3,total,51.5\n"),
        2026, 7)
    r = pga.grade_contest_entries(conn, 2026)
    conn.close()
    assert r["by_market"]["spread"]["win"] == 1                # Home -3.5 covers by 6
    assert r["by_market"]["total"]["loss"] == 1                # 54 is not under 51.5


# ---- cross-market ranking -----------------------------------------------------------

def test_heavily_moved_total_outranks_a_flat_spread():
    pool = {"games": [
        {"game_id": 1, "market_type": "spread", "signed_drift_vs_pick": 0.0},
        {"game_id": 2, "market_type": "total", "signed_drift_vs_pick": 4.0},
    ]}
    ranked = pv.rank_pool_picks(pool)
    assert [g["game_id"] for g in ranked] == [2, 1]


def test_points_follow_board_position_five_highest():
    pool = {"games": [{"game_id": i, "market_type": "spread", "signed_drift_vs_pick": float(i)}
                      for i in range(1, 8)]}
    ranked = pv.rank_pool_picks(pool)
    assert [g["suggested_points"] for g in ranked] == [5, 4, 3, 2, 1]
    assert [g["game_id"] for g in ranked] == [7, 6, 5, 4, 3]


def test_total_entry_never_borrows_the_spread_edge_or_card_exclusion():
    """The card's confidence/edge belong to the game's SPREAD pick. A large-
    edge spread flag on game 1 must not exclude, or hand an edge to, a
    total entry on the same game."""
    pool = {"games": [{"game_id": 1, "market_type": "total", "signed_drift_vs_pick": 3.0}]}
    card = {"games": [{"game_id": 1, "confidence": "low_confidence_large_edge", "edge": 15.0}]}
    ranked = pv.rank_pool_picks(pool, card)
    assert len(ranked) == 1 and ranked[0]["edge"] is None and ranked[0]["confidence"] is None


# ---- the table rebuild -----------------------------------------------------------------

def test_rebuild_preserves_rows_ids_and_corrections_from_the_old_shape(tmp_path):
    """Build the PRE-migration schema by hand, put real-looking rows and a
    correction in it, then run init_db() and check nothing was lost."""
    import db
    old = tmp_path / "old.db"
    c = sqlite3.connect(old)
    c.executescript("""
        CREATE TABLE contest_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, contest TEXT NOT NULL, season INTEGER NOT NULL,
            week INTEGER NOT NULL, game_id INTEGER NOT NULL, raw_home_team TEXT NOT NULL,
            raw_away_team TEXT NOT NULL, normalized_home_team TEXT NOT NULL,
            normalized_away_team TEXT NOT NULL, locked_home_spread REAL NOT NULL,
            picked_side TEXT NOT NULL, rank INTEGER, locked_at TEXT NOT NULL, source TEXT NOT NULL,
            corrected_at TEXT, UNIQUE(contest, season, week, game_id));
        CREATE TABLE contest_entry_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, original_entry_id INTEGER NOT NULL,
            original_locked_home_spread REAL NOT NULL, original_picked_side TEXT NOT NULL,
            original_rank INTEGER, corrected_locked_home_spread REAL, corrected_picked_side TEXT,
            corrected_rank INTEGER, reason TEXT NOT NULL, corrected_at TEXT NOT NULL,
            FOREIGN KEY (original_entry_id) REFERENCES contest_entries(id));
        INSERT INTO contest_entries VALUES (7,'pool',2026,1,100,'A','B','A','B',-3.5,'A',4,'t','src',NULL);
        INSERT INTO contest_entries VALUES (9,'pool',2026,1,101,'C','D','C','D',6.0,'D',2,'t','src',NULL);
        INSERT INTO contest_entry_corrections VALUES (1,7,-4.0,'A',3,-3.5,NULL,4,'typo','t');
    """)
    c.commit()
    c.close()

    old_path = db.DB_PATH
    try:
        db.DB_PATH = str(old)
        db.init_db()
        db.init_db()                                   # idempotent
        c = sqlite3.connect(old)
        rows = c.execute("SELECT id, game_id, market_type, locked_home_spread, locked_total, picked_side, rank "
                         "FROM contest_entries ORDER BY id").fetchall()
        corr = c.execute("SELECT original_entry_id, original_locked_home_spread, original_locked_total "
                         "FROM contest_entry_corrections").fetchall()
        fk = c.execute("PRAGMA foreign_key_check").fetchall()
        # a total and a spread on the same game are now both allowed...
        c.execute("INSERT INTO contest_entries (contest, season, week, game_id, market_type, raw_home_team, "
                  "raw_away_team, normalized_home_team, normalized_away_team, locked_total, picked_side, "
                  "locked_at, source) VALUES ('pool',2026,1,100,'total','A','B','A','B',50.5,'over','t','s')")
        # ...but two spreads on one game are still not
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO contest_entries (contest, season, week, game_id, market_type, raw_home_team, "
                      "raw_away_team, normalized_home_team, normalized_away_team, locked_home_spread, "
                      "picked_side, locked_at, source) VALUES ('pool',2026,1,100,'spread','A','B','A','B',"
                      "-9.0,'A','t','s')")
        c.close()
    finally:
        db.DB_PATH = old_path

    assert rows == [(7, 100, "spread", -3.5, None, "A", 4), (9, 101, "spread", 6.0, None, "D", 2)]
    assert corr == [(7, -4.0, None)]
    assert fk == []
