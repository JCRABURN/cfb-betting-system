from verify_cfbd_fields import check_path


def test_check_path_resolves_nested_match():
    results = []
    check_path({"defense": {"havoc": {"total": 0.18}}}, ["defense", "havoc", "total"], "adv", results)
    assert results == [("adv", "defense.havoc.total", True, 0.18)]


def test_check_path_reports_missing_key_and_available_siblings():
    results = []
    check_path({"defense": {"havocRate": 0.18}}, ["defense", "havoc", "total"], "adv", results)
    label, path, ok, available = results[0]
    assert not ok
    assert path == "defense.havoc.total"
    assert available == ["havocRate"]  # shows what's actually there instead


def test_check_path_handles_non_dict_at_break_point():
    results = []
    check_path({"offense": "not-a-dict"}, ["offense", "successRate"], "adv", results)
    label, path, ok, available = results[0]
    assert not ok
    assert "not-a-dict" in available
