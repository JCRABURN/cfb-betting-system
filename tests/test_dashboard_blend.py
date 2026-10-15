"""Dashboard 'Blended Top 5' panel (2026-10-15)."""
import build_dashboard as bd


def healths():
    ok = {"state": "ok", "finished_at": "2026-10-15T12:00:00", "error": None}
    return {k: dict(ok) for k in
            ("cfbd_stats", "odds_api", "card_generator", "gambling_view", "pool_view", "post_game_audit")}


def pick(points, text="Under 57.5", market="total", p=0.551, source="market_gap", tier="signal",
         agrees=None, snap=True):
    return {"points": points, "away_team": "Georgia Tech", "home_team": "Virginia Tech", "market": market,
            "pick_text": text, "p_win_est": p, "source": source, "tier": tier,
            "model_agrees": agrees, "single_snapshot": snap}


def blend(picks, warnings=None, track=None):
    ps = [x["p_win_est"] for x in picks]
    return {"picks": picks, "warnings": warnings or [], "track_record": track or {"n": 0},
            "summary": {"n_picks": len(picks), "expected_correct": round(sum(ps), 2),
                        "expected_points": 8.2, "max_points": 15, "p_perfect": 0.048,
                        "p_ge4": 0.248, "p_ge3": 0.583}}


def render(b):
    return bd.render_dashboard(2026, 7, None, None, None, None, None, healths(), "2026-10-15T12:00:00", blend=b)


def test_panel_shows_points_pick_and_probability():
    html = render(blend([pick(5), pick(4, "Over 45.5", p=0.543)]))
    assert "Blended Top 5" in html
    assert "Under 57.5" in html and "Over 45.5" in html
    assert "55.1%" in html and "54.3%" in html


def test_summary_states_the_coin_flip_baseline_and_assumptions():
    html = render(blend([pick(5)]))
    assert "coin flip is 7.5" in html
    assert "independent" in html and "4.8%" in html


def test_weak_filler_and_single_snapshot_are_labelled():
    html = render(blend([pick(1, "Home -3.5", market="spread", p=0.5047, source="spread_model", tier="weak",
                              snap=False), pick(5)]))
    assert ">weak<" in html and "1 snapshot" in html
    assert "spread model only" in html


def test_model_disagreement_is_surfaced():
    html = render(blend([pick(5, agrees=False)]))
    assert "model disagrees" in html


def test_warnings_are_rendered():
    html = render(blend([pick(5)], warnings=["Some picks rest on a single market snapshot"]))
    assert "single market snapshot" in html


def test_track_record_only_when_games_have_been_graded():
    assert "Logged track record" not in render(blend([pick(5)]))
    html = render(blend([pick(5)], track={"n": 12, "w": 7, "l": 5, "p": 0, "hit": 0.583, "mean_p_est": 0.54}))
    assert "Logged track record" in html and "7&ndash;5&ndash;0" in html and "58%" in html


def test_empty_state_when_no_picks():
    assert "No blended picks yet" in render(blend([]))
    assert "No blended picks yet" in render(None)


def test_dashboard_without_blend_argument_still_renders():
    html = bd.render_dashboard(2026, 7, None, None, None, None, None, healths(), "2026-10-15T12:00:00")
    assert "Blended Top 5" in html
