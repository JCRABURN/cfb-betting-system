"""
repair_in_play_lines.py
One-time, idempotent repair (2026-10-14): relabel betting_lines rows that
were stored as line_type='current' from an odds pull made AFTER the game had
already kicked off.

Why this exists. Before fetch_odds.persist_lines_to_db() learned to skip
in-play games, a pull that landed after kickoff stored the live in-game price
as a normal 'current' line (e.g. Stanford @ Wake Forest -12.5 -> -13.5 ->
-33.5, the last one mid-game; Memphis @ Charlotte +20.5 -> +49.5). That
corrupted CLV and the market-movement view. line_utils.get_latest_line() now
excludes post-kickoff rows by comparing against games.start_date, but that is
only a backstop: the CFBD schedule is refreshed weekly, so a kickoff moved
after Tuesday leaves start_date stale (Charlotte's was 19:30 UTC in our table;
the Odds API itself reported 15:02 UTC).

How the rows are identified exactly, not heuristically. Every Odds API
response is archived whole in raw_payloads (external review, 2026-08-04),
including each event's own commence_time. For each archived payload, any
event whose commence_time is at or before that payload's request time was
in play; the betting_lines rows written by that same pull (same teams,
fetched_at within PULL_WINDOW_SECONDS after the request) are the in-play
rows.

Relabel, never delete: betting_lines is append-only history. Matching rows go
from line_type='current' to 'in_play'. Every reader that asks for
'current'/'closing'/'opening' then ignores them automatically, and the
original numbers stay queryable. Idempotent: an already-relabeled row no
longer matches line_type='current'.

Usage:
    python data/repair_in_play_lines.py            # apply
    python data/repair_in_play_lines.py --dry-run  # report only
"""

import gzip
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import fetch_odds

# persist_lines_to_db() stamps one `now` per call, taken after the HTTP
# response was archived; the gap is seconds. Generous but bounded, so a
# later pull of the same matchup can never be swept in.
PULL_WINDOW_SECONDS = 180


def _parse_ts(ts):
    return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")


def find_in_play_events(payload_games, requested_at):
    """[(raw_home, raw_away)] for events already in play at `requested_at`."""
    cutoff = _parse_ts(requested_at)
    out = []
    for g in payload_games:
        ct = g.get("commence_time")
        if not ct:
            continue
        try:
            if _parse_ts(ct) <= cutoff:
                out.append((g.get("home_team"), g.get("away_team")))
        except ValueError:
            continue
    return out


def repair(conn, dry_run=False):
    """Returns {"payloads": n, "events_in_play": n, "rows_relabeled": n}."""
    schools = fetch_odds.load_school_names(conn)
    payloads = conn.execute(
        "SELECT id, requested_at, body_gzip FROM raw_payloads "
        "WHERE provider = 'the_odds_api' AND http_status = 200 ORDER BY id"
    ).fetchall()

    events = 0
    relabeled = 0
    for _pid, requested_at, body in payloads:
        try:
            games = json.loads(gzip.decompress(body).decode("utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(games, list):
            continue
        window_end = (_parse_ts(requested_at) + timedelta(seconds=PULL_WINDOW_SECONDS)).isoformat()
        for raw_home, raw_away in find_in_play_events(games, requested_at):
            events += 1
            home = fetch_odds.resolve_school_name(schools, raw_home)
            away = fetch_odds.resolve_school_name(schools, raw_away)
            params = (home, away, requested_at, window_end)
            where = ("home_team = ? AND away_team = ? AND line_type = 'current' "
                     "AND fetched_at >= ? AND fetched_at <= ?")
            if dry_run:
                n = conn.execute(f"SELECT COUNT(*) FROM betting_lines WHERE {where}", params).fetchone()[0]
            else:
                n = conn.execute(
                    f"UPDATE betting_lines SET line_type = 'in_play' WHERE {where}", params
                ).rowcount
            relabeled += n
    if not dry_run:
        conn.commit()
    return {"payloads": len(payloads), "events_in_play": events, "rows_relabeled": relabeled}


def main():
    dry_run = "--dry-run" in sys.argv
    with db.log_run("repair_in_play_lines") as run:
        conn = db.get_connection()
        try:
            result = repair(conn, dry_run=dry_run)
        finally:
            conn.close()
        run["rows_added"] = 0 if dry_run else result["rows_relabeled"]
    print(("[dry run] " if dry_run else "") +
          f"{result['payloads']} archived payloads, {result['events_in_play']} in-play events, "
          f"{result['rows_relabeled']} betting_lines row(s) "
          f"{'would be ' if dry_run else ''}relabeled current -> in_play.")


if __name__ == "__main__":
    main()
