import json
from datetime import datetime, timedelta

import build_dashboard as bd


def insert_run(conn, source, status, finished_at, error=None):
    conn.execute(
        "INSERT INTO ingestion_runs (source, started_at, finished_at, rows_added, status, error) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (source, finished_at, finished_at, status, error),
    )


def insert_pick(conn, game_id, season, result, unit_pl, clv=None, key_factors="[]"):
    conn.execute(
        "INSERT INTO games (game_id, season, week, home_team, away_team) VALUES (?, ?, 5, 'A', 'B')",
        (game_id, season),
    )
    conn.execute(
        "INSERT INTO picks (game_id, week, year, home_team, away_team, status, result, unit_pl, clv, "
        "key_factors, pick_type, created_at) VALUES (?, 5, ?, 'A', 'B', 'settled', ?, ?, ?, ?, 'live', 'now')",
        (game_id, season, result, unit_pl, clv, key_factors),
    )


# ---------------------------------------------------------------------------
# get_source_health
# ---------------------------------------------------------------------------

def test_pending_when_no_run_exists(temp_db):
    conn = temp_db.get_connection()
    health = bd.get_source_health(conn, "card_generator")
    conn.close()
    assert health["state"] == "pending"


def test_stale_when_latest_run_errored(temp_db):
    conn = temp_db.get_connection()
    insert_run(conn, "odds_api", "error", datetime.utcnow().isoformat(), error="Calendar API unavailable")
    conn.commit()
    health = bd.get_source_health(conn, "odds_api")
    conn.close()
    assert health["state"] == "stale"
    assert "Calendar" in health["error"]


def test_ok_when_recent_success(temp_db):
    conn = temp_db.get_connection()
    insert_run(conn, "card_generator", "success", datetime.utcnow().isoformat())
    conn.commit()
    health = bd.get_source_health(conn, "card_generator")
    conn.close()
    assert health["state"] == "ok"


def test_stale_when_last_success_too_old(temp_db):
    conn = temp_db.get_connection()
    old = (datetime.utcnow() - timedelta(days=30)).isoformat()
    insert_run(conn, "gambling_view", "success", old)
    conn.commit()
    health = bd.get_source_health(conn, "gambling_view")
    conn.close()
    assert health["state"] == "stale"


def test_ok_when_within_that_sources_own_window(temp_db):
    conn = temp_db.get_connection()
    recent = (datetime.utcnow() - timedelta(days=2)).isoformat()
    insert_run(conn, "gambling_view", "success", recent)  # window is 4 days
    conn.commit()
    health = bd.get_source_health(conn, "gambling_view")
    conn.close()
    assert health["state"] == "ok"


# ---------------------------------------------------------------------------
# build_season_ledger
# ---------------------------------------------------------------------------

def test_ledger_is_none_when_nothing_graded(temp_db):
    conn = temp_db.get_connection()
    ledger = bd.build_season_ledger(conn, 2026)
    conn.close()
    assert ledger is None


def test_ledger_aggregates_settled_picks_honestly(temp_db):
    conn = temp_db.get_connection()
    insert_pick(conn, 1, 2026, "win", 0.909, clv=1.0)
    insert_pick(conn, 2, 2026, "loss", -1.0, clv=-0.5)
    insert_pick(conn, 3, 2026, "push", 0.0)
    conn.commit()

    ledger = bd.build_season_ledger(conn, 2026)
    conn.close()

    assert ledger["wins"] == 1
    assert ledger["losses"] == 1
    assert ledger["pushes"] == 1
    assert ledger["n"] == 3
    assert ledger["ats_pct"] == 0.5
    assert round(ledger["roi"], 3) == round((0.909 - 1.0 + 0.0) / 3, 3)


def test_ledger_counts_hooks(temp_db):
    conn = temp_db.get_connection()
    insert_pick(conn, 1, 2026, "win", 0.909, key_factors='["hook"]')
    insert_pick(conn, 2, 2026, "loss", -1.0, key_factors="[]")
    conn.commit()

    ledger = bd.build_season_ledger(conn, 2026)
    conn.close()

    assert ledger["hooks"] == 1


def test_ledger_only_counts_current_season(temp_db):
    conn = temp_db.get_connection()
    insert_pick(conn, 1, 2025, "win", 0.909)
    insert_pick(conn, 2, 2026, "loss", -1.0)
    conn.commit()

    ledger = bd.build_season_ledger(conn, 2026)
    conn.close()

    assert ledger["n"] == 1
    assert ledger["losses"] == 1


# ---------------------------------------------------------------------------
# render_dashboard -- smoke tests on the HTML shape, not a full DOM parse
# ---------------------------------------------------------------------------

def _healths(state="ok"):
    h = {"state": state, "finished_at": "2026-08-04T14:00:00", "error": None if state != "stale" else "boom"}
    return {k: dict(h) for k in ("cfbd_stats", "odds_api", "card_generator", "gambling_view", "pool_view", "post_game_audit")}


def test_render_dashboard_shows_empty_states_when_nothing_available():
    html = bd.render_dashboard(2026, 1, None, None, None, None, _healths(), "2026-08-04T14:00:00")
    assert "No card generated yet" in html
    assert "No pool picks entered" in html
    assert "No games graded yet this season" in html
    assert "1224" not in html  # no backtest numbers leaking into the real page


def test_render_dashboard_shows_stale_banner_when_source_is_stale():
    healths = _healths("ok")
    healths["gambling_view"] = {"state": "stale", "finished_at": None, "error": "Odds API unavailable"}
    html = bd.render_dashboard(2026, 1, None, None, None, None, healths, "2026-08-04T14:00:00")
    assert "Market Movement may be out of date" in html
    assert "Odds API unavailable" in html


def test_render_dashboard_no_disclaimer_missing_even_with_full_slate():
    card = {
        "games": [
            {"away_team": "A", "home_team": "B", "side": "B", "edge": 5.0, "confidence": "standard"},
            {"away_team": "C", "home_team": "D", "side": "C", "edge": 22.0, "confidence": "low_confidence_large_edge"},
        ]
    }
    html = bd.render_dashboard(2026, 1, card, None, None, None, _healths(), "2026-08-04T14:00:00")
    assert "has not demonstrated a betting edge" in html
    assert "low confidence" in html


def test_render_dashboard_shows_prior_season_banner_and_row_pill():
    card = {
        "games": [
            {"away_team": "A", "home_team": "B", "side": "B", "edge": 4.0,
             "confidence": "low_confidence_prior_season_data", "uses_prior_season_data": True},
            {"away_team": "C", "home_team": "D", "side": "C", "edge": 5.0,
             "confidence": "standard", "uses_prior_season_data": False},
        ],
        "flagged_prior_season_data": [
            {"away_team": "A", "home_team": "B", "side": "B", "edge": 4.0,
             "confidence": "low_confidence_prior_season_data", "uses_prior_season_data": True},
        ],
    }
    html = bd.render_dashboard(2026, 1, card, None, None, None, _healths(), "2026-08-04T14:00:00")
    assert "1 pick this week uses prior-season EPA" in html
    assert "prior-season data" in html  # the per-row pill label
    assert "MODEL_DESIGN.md &sect;6" in html


def test_render_dashboard_no_prior_season_banner_when_nothing_flagged():
    card = {
        "games": [{"away_team": "A", "home_team": "B", "side": "B", "edge": 4.0,
                    "confidence": "standard", "uses_prior_season_data": False}],
        "flagged_prior_season_data": [],
    }
    html = bd.render_dashboard(2026, 1, card, None, None, None, _healths(), "2026-08-04T14:00:00")
    assert "prior-season EPA" not in html
