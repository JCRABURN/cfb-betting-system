"""
migrate_to_sqlite.py
One-time Phase 1 migration: create data/cfb.db and load whatever real
production data currently lives in docs/data/all_picks.json into the
`picks` table (pick_type='live' — this is real settled/pending output,
never demo data). Safe to re-run: skips picks already present for a
given (game_id, week, year).

Run once from the repo root: python migrate_to_sqlite.py
"""

import json
import os
from datetime import datetime

import db

PICKS_PATH = "docs/data/all_picks.json"


def load_existing_picks():
    if not os.path.exists(PICKS_PATH):
        return []
    with open(PICKS_PATH, encoding="utf-8") as f:
        return json.load(f).get("picks", [])


def migrate():
    db.init_db()
    picks = load_existing_picks()
    before_json = len(picks)

    conn = db.get_connection()
    try:
        before_db = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]

        now = datetime.utcnow().isoformat()
        migrated = 0
        for pick in picks:
            exists = conn.execute(
                "SELECT 1 FROM picks WHERE game_id = ? AND week = ? AND year = ? AND pick_type = 'live'",
                (pick.get("game_id"), pick.get("week"), pick.get("year")),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                """
                INSERT INTO picks (
                    game_id, week, year, home_team, away_team,
                    consensus_spread, projected_spread, edge, recommended_side, units,
                    confidence_signals, key_factors, line_movement, weather, risk_flags,
                    qualifies, status, result, clv, unit_pl, pick_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?)
                """,
                (
                    pick.get("game_id"), pick.get("week"), pick.get("year"),
                    pick.get("home_team"), pick.get("away_team"),
                    pick.get("consensus_spread"), pick.get("projected_spread"), pick.get("edge"),
                    pick.get("recommended_side"), pick.get("units"),
                    json.dumps(pick.get("confidence_signals", [])),
                    json.dumps(pick.get("key_factors", [])),
                    pick.get("line_movement"),
                    json.dumps(pick.get("weather", {})),
                    json.dumps(pick.get("risk_flags", [])),
                    int(bool(pick.get("qualifies"))),
                    pick.get("status", "pending"),
                    pick.get("result"), pick.get("clv"), pick.get("unit_pl"),
                    now,
                ),
            )
            migrated += 1

        conn.commit()
        after_db = conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0]
    finally:
        conn.close()

    print(f"docs/data/all_picks.json picks (before): {before_json}")
    print(f"picks table row count (before migration): {before_db}")
    print(f"picks table row count (after migration):  {after_db}")
    print(f"Rows migrated this run: {migrated}")

    with db.log_run("migration_json_to_sqlite") as run:
        run["rows_added"] = migrated


if __name__ == "__main__":
    migrate()
