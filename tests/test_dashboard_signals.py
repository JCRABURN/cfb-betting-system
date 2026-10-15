"""Dashboard 'Stale Pool Numbers' panel (2026-10-15)."""
import build_dashboard as bd


def healths():
    ok = {"state": "ok", "finished_at": "2026-10-15T12:00:00", "error": None}
    return {k: dict(ok) for k in
            ("cfbd_stats", "odds_api", "card_generator", "gambling_view", "pool_view", "post_game_audit")}


def bucket(w=0, l=0, p=0):
    d = w + l
    return {"n": d + p, "w": w, "l": l, "p": p, "decided": d,
            "hit": (w / d) if d else None, "lo": 0.3 if d else None, "hi": 0.8 if d else None}


def report(current=None, primary_n=0, met=False):
    empty = {k: bucket() for k in ("<1", "1-2", "2-3", ">=3")}
    return {
        "current": current or [], "weeks_in_record": [1, 2, 7],
        "record": {"all": {**empty, "2-3": bucket(w=4, l=2)}, "spread": empty, "total": empty},
        "primary": {"n": primary_n, "w": 4, "l": 2, "p": 0, "hit": 0.67, "lo": 0.3, "hi": 0.9,
                    "actionable_threshold_met": met, "min_n_to_act": 60},
    }


SIG = {"away_team": "Rutgers", "home_team": "Maryland", "market": "spread", "pool_number": -5.5,
       "market_number": -8.0, "abs_gap": 2.5, "gap": -2.5, "take": "Maryland -5.5", "tier": "strong",
       "p_win_est": 0.564, "actionable": True}


def render(signals):
    return bd.render_dashboard(2026, 7, None, None, None, None, None, healths(),
                               "2026-10-15T12:00:00", signals=signals)


def test_panel_lists_the_take_and_the_pool_number():
    html = render(report([SIG]))
    assert "Stale Pool Numbers" in html
    assert "Maryland -5.5" in html and "-5.5" in html and "-8.0" in html
    assert "56%" in html and "upcoming" in html


def test_started_game_is_shown_but_marked_started():
    html = render(report([{**SIG, "actionable": False}]))
    assert "started" in html and "Maryland -5.5" in html


def test_empty_state_when_no_gaps_or_no_slate():
    assert "No pool number differs" in render(report([]))
    assert "No pool number differs" in render(None)


def test_small_sample_is_labelled_not_evidence():
    html = render(report([SIG], primary_n=6, met=False))
    assert "Not yet evidence" in html and "need 60" in html


def test_graded_table_shows_only_buckets_with_decided_games():
    html = render(report([SIG]))
    assert "4&ndash;2&ndash;0" in html
    assert "<td class=\"mono\">&gt;=3</td>" not in html and ">=3</td>" not in html


def test_meets_bar_wording():
    assert "Meets the pre-registered bar" in render(report([SIG], primary_n=80, met=True))


def test_dashboard_without_signals_argument_still_renders():
    """Existing callers pass 9 positional args; the new one is optional."""
    html = bd.render_dashboard(2026, 7, None, None, None, None, None, healths(), "2026-10-15T12:00:00")
    assert "Stale Pool Numbers" in html
