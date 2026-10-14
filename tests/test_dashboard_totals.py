"""Dashboard renders total-based pool entries and the totals panel without
the spread-only keys they don't have (2026-10-14)."""
import build_dashboard as bd


def healths():
    ok = {"state": "ok", "finished_at": "2026-10-14T12:00:00", "error": None}
    return {k: dict(ok) for k in
            ("cfbd_stats", "odds_api", "card_generator", "gambling_view", "pool_view", "post_game_audit")}


TOTAL_ENTRY = {
    "market_type": "total", "game_id": 1, "home_team": "Home", "away_team": "Away",
    "picked_side": "over", "locked_total": 51.5, "live_total": 54.0, "live_book": "consensus",
    "drift": 2.5, "signed_drift_vs_pick": 2.5, "movement_vs_pick": "toward_pick",
    "favorite_flipped": False,
}
SPREAD_ENTRY = {
    "market_type": "spread", "game_id": 2, "home_team": "H2", "away_team": "A2", "picked_side": "H2",
    "pool_home_spread": -3.5, "live_home_spread": -5.0, "drift": -1.5, "signed_drift_vs_pick": 1.5,
    "movement_vs_pick": "toward_pick", "favorite_flipped": False, "live_book": "consensus",
}


def render(pool=None, ranked=None, gambling=None):
    return bd.render_dashboard(2026, 7, None, gambling, pool, ranked, None, healths(), "2026-10-14T12:00:00")


def test_pool_drift_renders_a_total_entry_without_spread_keys():
    html = render(pool={"games": [TOTAL_ENTRY, SPREAD_ENTRY], "skipped": []})
    assert "Over (total)" in html
    assert "51.5" in html and "54.0" in html
    assert "-3.5" in html                      # spread entry unaffected


def test_ranked_board_shows_points_and_labels_the_total():
    ranked = [{**TOTAL_ENTRY, "rank": 1, "suggested_points": 5, "edge": None, "confidence": None}]
    html = render(ranked=ranked)
    assert "Pool pts" in html
    assert "Over 51.5" in html and ">total<" in html


def test_totals_panel_renders_movement():
    totals = {"games": [{
        "game_id": 1, "home_team": "Home", "away_team": "Away", "first_seen_total": 50.5,
        "latest_total": 53.0, "latest_book": "draftkings", "same_book_match": True,
        "movement": 2.5, "direction": "up", "magnitude": 2.5,
    }], "skipped": []}
    html = render(gambling={"games": [], "skipped": [], "totals": totals})
    assert "Totals Movement" in html
    assert "50.5" in html and "53.0" in html and "toward over" in html


def test_totals_panel_empty_state_when_no_totals_yet():
    html = render(gambling={"games": [], "skipped": [], "totals": {"games": [], "skipped": []}})
    assert "No totals recorded yet" in html


def test_dashboard_still_renders_when_gambling_view_failed():
    assert "Totals Movement" in render(gambling=None)
