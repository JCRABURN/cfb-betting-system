"""
watchdog.py
Self-healing check for the two failure modes that left the live site stale
on 2026-10-05 (see ARCHITECTURE.md §30):

1. Post-Game Audit cron fires late. GitHub's scheduler has drifted from a
   ~6am CT target to 8.5+ hours late. If no successful audit has run since
   the most recent scheduled Monday 11:00 UTC slot AND AUDIT_GRACE_HOURS have
   passed since that slot, dispatch the audit workflow manually. The audit
   is idempotent (grades only pending picks, skips already-ingested
   point-in-time weeks), so a dispatch that races a late cron is harmless.

2. GitHub Pages deployment fails silently. The auto-generated
   pages-build-deployment workflow can end `cancelled`/`failure` with nothing
   afterward to retry it, leaving the public site stuck on an old build. If
   the most recent Pages run failed or was cancelled, re-run it.

Both checks are read-then-act and no-op when everything is healthy. In a
local run without GITHUB_TOKEN, reads still work (public repo) and the
decisions are printed without acting.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "JCRABURN/cfb-betting-system")
AUDIT_WORKFLOW = "post_game_audit.yml"
PAGES_WORKFLOW_NAME = "pages-build-deployment"
AUDIT_GRACE_HOURS = 10  # observed cron delay ran up to 8.5h; 10h leaves margin


def parse_utc(ts):
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def most_recent_audit_slot(now):
    """Latest Monday 11:00 UTC at or before `now` (the audit's cron target)."""
    days_back = now.weekday()  # Monday == 0
    slot = (now - timedelta(days=days_back)).replace(hour=11, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=7)
    return slot


def should_dispatch_audit(now, last_success_at, active_run_count):
    """True if the audit should be dispatched manually right now."""
    if active_run_count > 0:
        return False  # already queued or running; don't stack a second one
    slot = most_recent_audit_slot(now)
    if now - slot < timedelta(hours=AUDIT_GRACE_HOURS):
        return False  # still inside the normal (possibly late) cron window
    if last_success_at is not None and last_success_at >= slot:
        return False  # this week's audit already succeeded
    return True


def pages_run_to_rerun(runs):
    """`runs`: pages-build-deployment runs, newest first. Returns the id of the
    run to re-run, or None. Only the most recent run is considered: an older
    failure is superseded by any newer success, and an in-progress deploy is
    left alone."""
    if not runs:
        return None
    latest = runs[0]
    if latest["status"] != "completed":
        return None
    if latest["conclusion"] in ("failure", "cancelled"):
        return latest["id"]
    return None


def _headers(token):
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _list_runs(workflow, params, token):
    resp = requests.get(
        f"{API}/repos/{REPO}/actions/workflows/{workflow}/runs",
        params=params, headers=_headers(token), timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["workflow_runs"]


def _pages_workflow_id(token):
    resp = requests.get(f"{API}/repos/{REPO}/actions/workflows", headers=_headers(token), timeout=30)
    resp.raise_for_status()
    for wf in resp.json()["workflows"]:
        if wf["name"] == PAGES_WORKFLOW_NAME:
            return wf["id"]
    return None


def main():
    token = os.environ.get("GITHUB_TOKEN")
    dry_run = token is None
    now = datetime.now(timezone.utc)

    # 1. Post-Game Audit
    success_runs = _list_runs(AUDIT_WORKFLOW, {"status": "success", "per_page": 1}, token)
    last_success = parse_utc(success_runs[0]["created_at"]) if success_runs else None
    active = sum(
        len(_list_runs(AUDIT_WORKFLOW, {"status": s, "per_page": 5}, token))
        for s in ("queued", "in_progress")
    )
    if should_dispatch_audit(now, last_success, active):
        if dry_run:
            print("[dry run] would dispatch Post-Game Audit (no success since "
                  f"{most_recent_audit_slot(now).isoformat()})")
        else:
            resp = requests.post(
                f"{API}/repos/{REPO}/actions/workflows/{AUDIT_WORKFLOW}/dispatches",
                json={"ref": "main"}, headers=_headers(token), timeout=30,
            )
            resp.raise_for_status()
            print("Dispatched Post-Game Audit (no success since the Monday slot).")
    else:
        print(f"Post-Game Audit OK: last success {last_success.isoformat() if last_success else 'never'}, "
              f"active runs {active}.")

    # 2. GitHub Pages
    pages_id = _pages_workflow_id(token)
    if pages_id is None:
        print("No pages-build-deployment workflow found; skipping Pages check.")
        return
    pages_runs = _list_runs(pages_id, {"per_page": 3}, token)
    run_id = pages_run_to_rerun(pages_runs)
    if run_id is None:
        print("Pages deployment OK.")
    elif dry_run:
        print(f"[dry run] would re-run Pages deployment {run_id}.")
    else:
        resp = requests.post(
            f"{API}/repos/{REPO}/actions/runs/{run_id}/rerun",
            headers=_headers(token), timeout=30,
        )
        resp.raise_for_status()
        print(f"Re-ran failed Pages deployment {run_id}.")


if __name__ == "__main__":
    sys.exit(main())
