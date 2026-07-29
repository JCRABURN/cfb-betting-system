"""
db.py
Shared SQLite access layer for the CFB betting system.
Single file DB at data/cfb.db, committed to git so ingestion history
persists across GitHub Actions runs instead of being discarded each week.

Scripts in data/ and models/ import this via:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import db
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cfb.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY,
    school TEXT NOT NULL UNIQUE,
    conference TEXT,
    division TEXT
);

CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    season_type TEXT,
    start_date TEXT,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    venue TEXT,
    venue_latitude REAL,
    venue_longitude REAL,
    neutral_site INTEGER DEFAULT 0,
    conference_game INTEGER DEFAULT 0,
    home_points INTEGER,
    away_points INTEGER,
    completed INTEGER DEFAULT 0
);

-- One row per book per line pull. Never updated in place, only appended,
-- so line movement is a query (ORDER BY fetched_at) instead of a bolt-on field.
CREATE TABLE IF NOT EXISTS betting_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER,              -- best-effort FK to games.game_id; nullable if the
                                    -- team-name join between odds and CFBD data failed
    season INTEGER,
    week INTEGER,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    book TEXT NOT NULL,
    home_spread REAL,
    total REAL,
    home_moneyline INTEGER,
    away_moneyline INTEGER,
    line_type TEXT NOT NULL,       -- opening | current
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

-- Season-to-date snapshot (SP+, EPA, records), one row per team per capture.
CREATE TABLE IF NOT EXISTS team_game_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER,
    season INTEGER NOT NULL,
    week INTEGER,
    team TEXT NOT NULL,
    sp_rating REAL,
    offense_epa_play REAL,
    defense_epa_play REAL,
    wins INTEGER,
    losses INTEGER,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE TABLE IF NOT EXISTS weather (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER,
    captured_at TEXT NOT NULL,
    temp_f REAL,
    wind_mph REAL,
    precip_pct REAL,
    is_forecast INTEGER DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'open-meteo',
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE TABLE IF NOT EXISTS injuries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team TEXT NOT NULL,
    player TEXT,
    position TEXT,
    status TEXT,
    report_date TEXT,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER,
    week INTEGER NOT NULL,
    year INTEGER NOT NULL,
    home_team TEXT,
    away_team TEXT,
    consensus_spread REAL,
    projected_spread REAL,
    edge REAL,
    recommended_side TEXT,
    units INTEGER,
    confidence_signals TEXT,   -- JSON-encoded list
    key_factors TEXT,          -- JSON-encoded list
    line_movement REAL,
    weather TEXT,              -- JSON-encoded dict
    risk_flags TEXT,           -- JSON-encoded list
    qualifies INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT,
    clv REAL,
    unit_pl REAL,
    pick_type TEXT NOT NULL DEFAULT 'live',  -- live | backfilled | synthetic
    created_at TEXT NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(game_id)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    rows_added INTEGER DEFAULT 0,
    status TEXT NOT NULL,      -- success | error
    error TEXT
);
"""


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def log_run(source):
    """
    Wraps an ingestion step and records it in ingestion_runs.
    Usage:
        with log_run("cfbd_stats") as run:
            ... do work ...
            run["rows_added"] = 42
    """
    init_db()
    conn = get_connection()
    started_at = datetime.utcnow().isoformat()
    run = {"rows_added": 0}
    try:
        yield run
        conn.execute(
            "INSERT INTO ingestion_runs (source, started_at, finished_at, rows_added, status) "
            "VALUES (?, ?, ?, ?, 'success')",
            (source, started_at, datetime.utcnow().isoformat(), run["rows_added"]),
        )
        conn.commit()
    except Exception as e:
        conn.execute(
            "INSERT INTO ingestion_runs (source, started_at, finished_at, rows_added, status, error) "
            "VALUES (?, ?, ?, ?, 'error', ?)",
            (source, started_at, datetime.utcnow().isoformat(), run["rows_added"], str(e)),
        )
        conn.commit()
        raise
    finally:
        conn.close()
