import csv
import gzip
import io

import backfill_nfl_pbp_stats as bps

PBP_FIELDS = ["season_type", "week", "posteam", "defteam", "play_type", "epa", "success"]


def make_gz(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=PBP_FIELDS)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return gzip.compress(buf.getvalue().encode("utf-8"))


def play(week, posteam, defteam, epa, success, season_type="REG", play_type="pass"):
    return {"season_type": season_type, "week": week, "posteam": posteam, "defteam": defteam,
            "play_type": play_type, "epa": epa, "success": success}


# ---------------------------------------------------------------------------
# _weekly_totals / cumulative_point_in_time -- pure aggregation
# ---------------------------------------------------------------------------

def test_weekly_totals_only_counts_scrimmage_plays():
    gz = make_gz([
        play(1, "A", "B", 0.5, 1, play_type="pass"),
        play(1, "A", "B", 0.3, 1, play_type="run"),
        play(1, "A", "B", 0.9, 1, play_type="punt"),  # excluded
        play(1, "A", "B", 0.9, 1, play_type="kickoff"),  # excluded
    ])
    weekly = bps._weekly_totals(gz)
    off = weekly[("A", 1)]
    assert off[bps._OFF_EPA_N] == 2  # only the pass+run plays


def test_weekly_totals_excludes_postseason():
    gz = make_gz([
        play(1, "A", "B", 0.5, 1, season_type="REG"),
        play(19, "A", "B", 0.9, 1, season_type="POST"),
    ])
    weekly = bps._weekly_totals(gz)
    assert ("A", 19) not in weekly
    assert ("A", 1) in weekly


def test_weekly_totals_offense_and_defense_both_credited():
    gz = make_gz([play(1, "A", "B", 1.0, 1)])
    weekly = bps._weekly_totals(gz)
    assert weekly[("A", 1)][bps._OFF_EPA_SUM] == 1.0
    assert weekly[("A", 1)][bps._OFF_EPA_N] == 1
    assert weekly[("B", 1)][bps._DEF_EPA_SUM] == 1.0
    assert weekly[("B", 1)][bps._DEF_EPA_N] == 1


def test_cumulative_averages_correctly_and_moves_week_to_week():
    weekly = {
        # [off_epa_sum, off_epa_n, def_epa_sum, def_epa_n, off_succ_sum, off_succ_n, def_succ_sum, def_succ_n]
        ("A", 1): [2.0, 2, 0.0, 0, 1.0, 2, 0.0, 0],   # week 1 alone: off_epa 2.0/2=1.0, off_succ 1.0/2=0.5
        ("A", 2): [1.0, 2, 0.0, 0, 1.0, 2, 0.0, 0],   # week 2 alone: off_epa 1.0/2=0.5, off_succ 1.0/2=0.5
    }
    rows = bps.cumulative_point_in_time(weekly)
    by_week = {w: (off_epa, off_succ) for team, w, off_epa, def_epa, off_succ, def_succ in rows if team == "A"}
    assert by_week[1] == (1.0, 0.5)  # 2.0/2, 1.0/2
    assert by_week[2] == (0.75, 0.5)  # cumulative (2.0+1.0)/(2+2)=0.75, (1.0+1.0)/(2+2)=0.5


def test_cumulative_carries_forward_through_a_bye_week():
    """A week with no new plays for a team (a bye) still gets a row,
    identical to the prior week's cumulative numbers -- see Detroit 2024
    weeks 4/5 in the live verification."""
    weekly = {("A", 1): [4.0, 2, 0.0, 0, 2.0, 2, 0.0, 0]}  # only week 1 has plays; team appears again week 3
    weekly[("A", 3)] = [0.0, 0, 0.0, 0, 0.0, 0, 0.0, 0]
    # simulate: week 2 is a bye (no entry at all), week 3 also has 0 new plays recorded
    rows = bps.cumulative_point_in_time(weekly)
    by_week = {w: off_epa for team, w, off_epa, *_ in rows if team == "A"}
    assert by_week[1] == 2.0
    assert by_week[2] == 2.0  # bye -- no entry for week 2 at all, carried forward
    assert by_week[3] == 2.0  # week 3 entry added zero new plays, unchanged


def test_cumulative_no_row_before_teams_first_game():
    weekly = {("A", 3): [1.0, 1, 0.0, 0, 1.0, 1, 0.0, 0]}
    rows = bps.cumulative_point_in_time(weekly)
    weeks = [w for team, w, *_ in rows if team == "A"]
    assert weeks == [3]  # no rows for weeks 1-2, before the team's first game


# ---------------------------------------------------------------------------
# backfill_season -- DB write path
# ---------------------------------------------------------------------------

def test_backfill_season_writes_nfl_team_stats(temp_db, monkeypatch):
    gz = make_gz([
        play(1, "A", "B", 1.0, 1),
        play(2, "A", "B", 0.5, 0),
    ])
    monkeypatch.setattr(bps, "fetch_pbp_gzip", lambda season: gz)

    conn = temp_db.get_connection()
    rows_added, status = bps.backfill_season(conn, 2024)
    row_wk1 = conn.execute(
        "SELECT offense_epa_play, offense_success_rate FROM nfl_team_stats "
        "WHERE season=2024 AND team='A' AND week=1"
    ).fetchone()
    row_wk2 = conn.execute(
        "SELECT offense_epa_play, offense_success_rate FROM nfl_team_stats "
        "WHERE season=2024 AND team='A' AND week=2"
    ).fetchone()
    conn.close()

    assert status == "ok"
    assert row_wk1 == (1.0, 1.0)
    assert row_wk2 == (0.75, 0.5)  # cumulative (1.0+0.5)/2, (1+0)/2


def test_backfill_season_idempotent_without_force(temp_db, monkeypatch):
    gz = make_gz([play(1, "A", "B", 1.0, 1)])
    monkeypatch.setattr(bps, "fetch_pbp_gzip", lambda season: gz)

    conn = temp_db.get_connection()
    bps.backfill_season(conn, 2024)
    rows_added, status = bps.backfill_season(conn, 2024)
    conn.close()
    assert status == "skipped"
    assert rows_added == 0


def test_backfill_season_force_replaces_not_duplicates(temp_db, monkeypatch):
    gz = make_gz([play(1, "A", "B", 1.0, 1)])
    monkeypatch.setattr(bps, "fetch_pbp_gzip", lambda season: gz)

    conn = temp_db.get_connection()
    bps.backfill_season(conn, 2024)
    bps.backfill_season(conn, 2024, force=True)
    count = conn.execute(
        "SELECT COUNT(*) FROM nfl_team_stats WHERE season=2024 AND team='A' AND week=1"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_backfill_season_failed_fetch_reported(temp_db, monkeypatch):
    monkeypatch.setattr(bps, "fetch_pbp_gzip", lambda season: None)
    conn = temp_db.get_connection()
    rows_added, status = bps.backfill_season(conn, 2024)
    conn.close()
    assert status == "failed"
    assert rows_added == 0
