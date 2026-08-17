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

import gzip
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_ROOT, "data", "cfb.db")


def _load_dotenv(path=None):
    """Minimal .env loader so local runs pick up CFBD_API_KEY/ODDS_API_KEY without
    the caller having to export them manually. No-op if .env doesn't exist (e.g. in
    GitHub Actions, where secrets are already env vars). Never overwrites a var
    that's already set, so real env vars always win over .env.

    Every ingestion script does `import db` before reading its own API key
    constants from os.environ, so this runs early enough to matter.
    """
    path = path or os.path.join(_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

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

-- Season-to-date snapshot (SP+, EPA, success rate, havoc rate, records), one
-- row per team per capture. week/game_id are NULL for season-level snapshots
-- (e.g. the historical backfill), populated for in-season weekly captures.
CREATE TABLE IF NOT EXISTS team_game_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER,
    season INTEGER NOT NULL,
    week INTEGER,
    team TEXT NOT NULL,
    sp_rating REAL,
    offense_epa_play REAL,
    defense_epa_play REAL,
    offense_success_rate REAL,
    defense_success_rate REAL,
    havoc_rate REAL,           -- CFBD only exposes havoc as a defensive stat
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

-- Added 2026-07-30: the backtest harness's get_team_stats_as_of() runs this
-- exact WHERE/ORDER BY on every single team-game prediction and every
-- training-set row (tens of thousands of calls per walk-forward run,
-- multiplied by every model tested against the baseline) with no index --
-- a full table scan every time. A two-model feature-test run took several
-- minutes as a result. CREATE INDEX IF NOT EXISTS is safe to run against an
-- already-populated table (unlike ALTER TABLE ADD COLUMN, no separate
-- migration dance needed).
CREATE INDEX IF NOT EXISTS idx_team_game_stats_lookup
    ON team_game_stats (source, team, season, week);

CREATE INDEX IF NOT EXISTS idx_betting_lines_lookup
    ON betting_lines (game_id, line_type, book);

CREATE INDEX IF NOT EXISTS idx_games_season_week
    ON games (season, week);

-- Added 2026-07-31, for the rest/schedule feature test (MODEL_DESIGN.md
-- "Later features"): `games` is intentionally FBS-only (see Phase 3), so a
-- team's rest calculation breaks when their actual most recent game was an
-- FBS-vs-FCS buy game not in that table. This does NOT duplicate `games`'
-- scope -- it stores only the one field backtest_harness.get_days_rest()
-- needs (the date), never a full game row (no score, no opponent-as-a-
-- tracked-entity, no FK to games). Populated by
-- data/backfill_rest_dates.py, which only ever fills in gaps found in the
-- FBS-only archive, on demand.
CREATE TABLE IF NOT EXISTS supplemental_game_dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    opponent_classification TEXT,
    source TEXT NOT NULL DEFAULT 'cfbd_supplemental_dates',
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supplemental_game_dates_lookup
    ON supplemental_game_dates (team, season, week);

-- Raw API response archival (external review, accepted 2026-08-04): every
-- CFBD/Odds API request's raw response body, gzip-compressed (SQLite has
-- no native compression). Purpose: when the next parser bug surfaces
-- (every historical path in this project has had one), REPLAY the stored
-- response through the fixed parser instead of re-spending API calls
-- (Odds API's free tier is capped at 500/month) and guessing which rows a
-- bad parse polluted. request_params must NEVER include an API key --
-- both fetch_stats.py and fetch_odds.py redact it before storing.
CREATE TABLE IF NOT EXISTS raw_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,            -- 'cfbd' | 'the_odds_api'
    endpoint TEXT NOT NULL,            -- e.g. '/games', '/ratings/sp'
    request_params TEXT,               -- JSON-encoded query params, API keys redacted
    requested_at TEXT NOT NULL,
    http_status INTEGER,
    checksum TEXT NOT NULL,            -- sha256 hex of the raw (uncompressed) response body
    body_gzip BLOB NOT NULL,           -- gzip-compressed raw response body
    parser_version TEXT NOT NULL,      -- which fetch module parser logic produced downstream rows from this
    rows_accepted INTEGER DEFAULT 0,
    rows_rejected INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_raw_payloads_lookup
    ON raw_payloads (provider, endpoint, requested_at);

-- Immutable locked contest lines (external review, accepted 2026-08-04):
-- the pool's own displayed number at the moment a pick was entered,
-- inserted ONCE per (contest, season, week, game_id) and never updated in
-- place by normal ingestion -- a pool's printed number is a historical
-- fact about what was seen at lock time, not something that should
-- silently drift if the CSV is re-ingested. correct_contest_entry() in
-- pool_view.py is the ONLY sanctioned way to change a locked value.
CREATE TABLE IF NOT EXISTS contest_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    game_id INTEGER NOT NULL,
    raw_home_team TEXT NOT NULL,         -- exactly as typed/exported into the CSV
    raw_away_team TEXT NOT NULL,
    normalized_home_team TEXT NOT NULL,  -- resolved against `teams`, may equal raw_*
    normalized_away_team TEXT NOT NULL,
    locked_home_spread REAL NOT NULL,
    picked_side TEXT NOT NULL,
    rank INTEGER,                        -- confidence rank 1-5, nullable; range enforced in
                                          -- pool_view.py (app-level), not a DB CHECK constraint,
                                          -- matching this schema's existing style
    locked_at TEXT NOT NULL,
    source TEXT NOT NULL,                -- e.g. 'csv:data/pool_picks/week_1_2026.csv'
    corrected_at TEXT,                   -- NULL unless correct_contest_entry() has touched this row
    UNIQUE(contest, season, week, game_id)
);

-- Every correction to a contest_entries row: the ORIGINAL value, the new
-- value, and why -- contest_entries itself is only ever UPDATEd by
-- pool_view.correct_contest_entry(), and only after this ledger row is
-- written first, so the original locked value is never lost, only
-- superseded with an audit trail.
CREATE TABLE IF NOT EXISTS contest_entry_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_entry_id INTEGER NOT NULL,
    original_locked_home_spread REAL NOT NULL,
    original_picked_side TEXT NOT NULL,
    original_rank INTEGER,
    corrected_locked_home_spread REAL,
    corrected_picked_side TEXT,
    corrected_rank INTEGER,
    reason TEXT NOT NULL,
    corrected_at TEXT NOT NULL,
    FOREIGN KEY (original_entry_id) REFERENCES contest_entries(id)
);
"""


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the table already existed in committed DBs.
# CREATE TABLE IF NOT EXISTS won't retroactively add these, so init_db()
# patches them in via ALTER TABLE when missing.
_ADDED_COLUMNS = {
    "team_game_stats": [
        ("offense_success_rate", "REAL"),
        ("defense_success_rate", "REAL"),
        ("havoc_rate", "REAL"),
    ],
    "contest_entries": [
        ("rank", "INTEGER"),
    ],
    "contest_entry_corrections": [
        ("original_rank", "INTEGER"),
        ("corrected_rank", "INTEGER"),
    ],
}


def _migrate_schema(conn):
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, col_type in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()


_SECRET_PARAM_MARKERS = ("key", "token", "secret", "password", "authorization")


def _redact_params(params):
    """Strips anything key/token/secret/password/authorization-shaped from
    a params dict before it's stored. Applied unconditionally in
    archive_raw_payload() regardless of provider -- CFBD's key lives in a
    header, never in params, but the Odds API's `apiKey` IS a query param,
    so this makes the safety a property of the archival function itself
    rather than something every caller has to remember to do."""
    if not params:
        return {}
    return {
        k: ("<redacted>" if any(m in k.lower() for m in _SECRET_PARAM_MARKERS) else v)
        for k, v in params.items()
    }


def archive_raw_payload(conn, provider, endpoint, params, response_text, http_status,
                         parser_version, rows_accepted=0, rows_rejected=0):
    """Stores one raw API response for later replay through a fixed parser
    (external review, accepted 2026-08-04) -- see raw_payloads' schema
    comment for the full rationale. Does NOT commit -- the caller's own
    transaction/commit flow controls when this becomes durable, same as
    every other write helper in this module.

    response_text: the raw response body as a str (caller's own
    `resp.text`, not `resp.json()` -- storing the exact bytes CFBD/the Odds
    API actually sent, not a re-serialized version of the parsed object,
    is the whole point of an archive meant for replay).

    rows_accepted/rows_rejected are usually unknown at call time (the
    caller archives BEFORE parsing, so an error response gets captured
    too, not just successful ones) -- pass 0/0 here and call
    update_raw_payload_counts() with the returned id once parsing
    actually happens. Returns the new row's id."""
    body_bytes = response_text.encode("utf-8")
    checksum = hashlib.sha256(body_bytes).hexdigest()
    body_gzip = gzip.compress(body_bytes)
    cur = conn.execute(
        """
        INSERT INTO raw_payloads (
            provider, endpoint, request_params, requested_at, http_status,
            checksum, body_gzip, parser_version, rows_accepted, rows_rejected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            provider, endpoint, json.dumps(_redact_params(params)),
            datetime.utcnow().isoformat(), http_status, checksum, body_gzip,
            parser_version, rows_accepted, rows_rejected,
        ),
    )
    return cur.lastrowid


def update_raw_payload_counts(conn, payload_id, rows_accepted=0, rows_rejected=0):
    """Fills in accepted/rejected counts after parsing actually happens --
    see archive_raw_payload()'s docstring for why these are usually
    unknown at archival time."""
    conn.execute(
        "UPDATE raw_payloads SET rows_accepted = ?, rows_rejected = ? WHERE id = ?",
        (rows_accepted, rows_rejected, payload_id),
    )


def integrity_check():
    """Runs SQLite's own PRAGMA integrity_check against the committed DB.
    Returns True iff the result is exactly ['ok'] -- any corruption message
    (there can be several rows describing different problems) returns
    False. Called before every commit of data/cfb.db in each workflow
    (external review, accepted 2026-08-04): a corrupted DB must never be
    pushed, since git history would then have the corruption baked in as
    the new baseline for every future checkout."""
    conn = get_connection()
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return len(rows) == 1 and rows[0][0] == "ok"


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
