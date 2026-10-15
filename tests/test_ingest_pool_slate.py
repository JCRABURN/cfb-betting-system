"""Pool slate ingestion: three real layouts, immutability, conflict reporting."""
import csv

import openpyxl
import pytest

import ingest_pool_slate as ips


def seed_game(conn, gid, home, away, week=7):
    conn.execute("INSERT INTO games (game_id, season, week, home_team, away_team) VALUES (?, 2026, ?, ?, ?)",
                 (gid, week, home, away))


# ---- parsing the three real layouts ----------------------------------------------

def test_parses_week7_style_xlsx(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Game #", "Day / Date", "Away Team", "Away Spread", "Home Team", "Home Spread", "Total"])
    ws.append([1, "Tue Oct 13", "Delaware", -1.5, "Middle Tennessee", 1.5, 47.5])
    ws.append([2, "Tue Oct 13", "FIU", 2.5, "Jacksonville State", -2.5, 43.5])
    p = tmp_path / "week_7_2026_splashsports_slate.xlsx"
    wb.save(p)
    g = ips.parse_slate_file(str(p))
    assert g[0] == {"away": "Delaware", "home": "Middle Tennessee", "home_spread": 1.5, "total": 47.5}
    assert g[1]["home_spread"] == -2.5 and g[1]["away"] == "FIU"


def test_parses_week1_style_xlsx_with_banner_rows_and_extra_columns(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Slate"
    ws.append(["College Football Pick'em - Week of Sep 3-7, 2026"])
    ws.append(["Lines and totals transcribed from pool screenshots."])
    ws.append([])
    ws.append(["Game #", "Date", "Day", "Kickoff (CT)", "Away Team", "Away", "Away Rank", "Away Line",
               "Home Team", "Home", "Home Rank", "Home Line", "Favorite", "Fav Line", "Total (O/U)"])
    ws.append([1, "2026-09-03", "Thu", "5:00 PM", "UMass", "MASS", None, 29.5,
               "Rutgers", "RUTG", None, -29.5, "RUTG", -29.5, 51.5])
    ws.append(["TIEBREAKER", None, "SMU at Florida State ..."])       # non-game row must be skipped
    p = tmp_path / "week_1_2026_splashsports_slate.xlsx"
    wb.save(p)
    g = ips.parse_slate_file(str(p))
    assert g == [{"away": "UMass", "home": "Rutgers", "home_spread": -29.5, "total": 51.5}]


def test_parses_week2_style_csv(tmp_path):
    p = tmp_path / "week_2_2026_splashsports_slate.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["game_id", "season", "week", "away_team", "home_team", "away_spread", "home_spread",
                    "locked_total", "lock_status"])
        w.writerow(["2026_W02_01", 2026, 2, "Rutgers", "Boston College", 3.5, -3.5, 54.5, "LOCKED"])
    assert ips.parse_slate_file(str(p)) == [
        {"away": "Rutgers", "home": "Boston College", "home_spread": -3.5, "total": 54.5}]


def test_total_may_be_absent(tmp_path):
    p = tmp_path / "week_3_2026_splashsports_slate.csv"
    p.write_text("away_team,home_team,home_spread\nA,B,-3.0\n", encoding="utf-8")
    assert ips.parse_slate_file(str(p))[0]["total"] is None


def test_sign_flipped_sheet_fails_loudly(tmp_path):
    """Away and home spreads that are not opposites mean one column is
    mislabeled; ingesting would invert every signal."""
    p = tmp_path / "week_3_2026_splashsports_slate.csv"
    p.write_text("away_team,home_team,away_spread,home_spread\nA,B,-3.0,-3.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not opposites"):
        ips.parse_slate_file(str(p))


def test_missing_header_fails_loudly(tmp_path):
    p = tmp_path / "week_3_2026_splashsports_slate.csv"
    p.write_text("foo,bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ips.parse_slate_file(str(p))


# ---- ingestion -----------------------------------------------------------------------

G = [{"away": "Delaware", "home": "Middle Tennessee", "home_spread": 1.5, "total": 47.5}]


def test_ingest_stores_and_is_idempotent(temp_db):
    conn = temp_db.get_connection()
    seed_game(conn, 10, "Middle Tennessee", "Delaware")
    conn.commit()
    first = ips.ingest_slate(conn, G, 2026, 7, "t")
    again = ips.ingest_slate(conn, G, 2026, 7, "t")
    n = conn.execute("SELECT COUNT(*) FROM pool_lines").fetchone()[0]
    conn.close()
    assert first["inserted"] == 1 and again["inserted"] == 0 and again["skipped"] == 1 and n == 1


def test_differing_renumber_is_a_reported_conflict_not_an_overwrite(temp_db):
    conn = temp_db.get_connection()
    seed_game(conn, 10, "Middle Tennessee", "Delaware")
    conn.commit()
    ips.ingest_slate(conn, G, 2026, 7, "t")
    changed = [{**G[0], "home_spread": 3.0}]
    r = ips.ingest_slate(conn, changed, 2026, 7, "t")
    stored = conn.execute("SELECT pool_home_spread FROM pool_lines").fetchone()[0]
    conn.close()
    assert r["inserted"] == 0 and len(r["conflicts"]) == 1
    assert r["conflicts"][0]["stored"]["home_spread"] == 1.5 and r["conflicts"][0]["incoming"]["home_spread"] == 3.0
    assert stored == 1.5


def test_unresolvable_game_is_reported_not_dropped(temp_db):
    conn = temp_db.get_connection()
    r = ips.ingest_slate(conn, G, 2026, 7, "t")
    conn.close()
    assert r["inserted"] == 0 and r["unmatched"][0]["away"] == "Delaware"


def test_discover_files_reads_week_and_season_from_name(tmp_path):
    for n in ("week_7_2026_splashsports_slate.xlsx", "week_2_2026_splashsports_slate.csv", "notes.txt",
              "week_1_2026_model_read.csv"):
        (tmp_path / n).write_text("x")
    found = sorted((w, s) for _, w, s in ips.discover_files(str(tmp_path)))
    assert found == [(2, 2026), (7, 2026)]
