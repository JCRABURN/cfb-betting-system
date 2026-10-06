from datetime import datetime, timezone

import watchdog as wd


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# Monday 2026-10-05 is a real Monday (verified: 2026-09-07 was a Monday).

def test_audit_slot_on_monday_before_11_is_previous_monday():
    assert wd.most_recent_audit_slot(utc(2026, 10, 5, 5, 0)) == utc(2026, 9, 28, 11, 0)


def test_audit_slot_on_monday_after_11_is_today():
    assert wd.most_recent_audit_slot(utc(2026, 10, 5, 15, 0)) == utc(2026, 10, 5, 11, 0)


def test_audit_slot_midweek_is_this_weeks_monday():
    assert wd.most_recent_audit_slot(utc(2026, 10, 7, 12, 0)) == utc(2026, 10, 5, 11, 0)


def test_no_dispatch_inside_grace_window():
    # Monday 15:00 UTC: only 4h past slot, normal late cron still plausible.
    assert wd.should_dispatch_audit(utc(2026, 10, 5, 15, 0), None, 0) is False


def test_dispatch_when_past_grace_and_no_success_since_slot():
    # Monday 23:00 UTC, no success since Monday 11:00 slot -> dispatch.
    assert wd.should_dispatch_audit(utc(2026, 10, 5, 23, 0), utc(2026, 9, 28, 19, 0), 0) is True


def test_no_dispatch_if_this_weeks_audit_already_succeeded():
    assert wd.should_dispatch_audit(
        utc(2026, 10, 6, 14, 0), utc(2026, 10, 5, 19, 31), 0
    ) is False


def test_no_dispatch_while_an_audit_is_already_active():
    assert wd.should_dispatch_audit(utc(2026, 10, 5, 23, 0), None, 1) is False


def test_dispatch_when_never_succeeded():
    assert wd.should_dispatch_audit(utc(2026, 10, 5, 23, 0), None, 0) is True


def run(id, status, conclusion):
    return {"id": id, "status": status, "conclusion": conclusion}


def test_pages_reruns_latest_cancelled_deploy():
    runs = [run(2, "completed", "cancelled"), run(1, "completed", "success")]
    assert wd.pages_run_to_rerun(runs) == 2


def test_pages_reruns_latest_failed_deploy():
    assert wd.pages_run_to_rerun([run(9, "completed", "failure")]) == 9


def test_pages_leaves_healthy_site_alone():
    assert wd.pages_run_to_rerun([run(3, "completed", "success")]) is None


def test_pages_ignores_older_failure_superseded_by_newer_success():
    runs = [run(5, "completed", "success"), run(4, "completed", "failure")]
    assert wd.pages_run_to_rerun(runs) is None


def test_pages_does_not_touch_in_progress_deploy():
    assert wd.pages_run_to_rerun([run(6, "in_progress", None), run(5, "completed", "failure")]) is None


def test_pages_no_runs_is_noop():
    assert wd.pages_run_to_rerun([]) is None
