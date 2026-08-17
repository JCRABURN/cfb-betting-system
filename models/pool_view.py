"""
pool_view.py
Drift view for the pick'em pool: compares the LIVE line to the line the
user actually entered/locked from the pool's own Excel sheet -- a
DIFFERENT baseline than gambling_view.py's opening line, because the
pool's number isn't a real market opener, it's whatever number the pool's
sheet showed at whatever moment the user copied it in. Answers "has the
market moved enough since I locked this pick that it's worth reconsidering"
-- not "is this a good bet." No model signal here either, same reasoning
as gambling_view.py: EPA-only has no demonstrated edge (ARCHITECTURE.md
§19-20), so this stays a pure line-drift read.

Input (pool_entries): a list of dicts, one per pick already locked in the
pool --
    {"game_id": ..., "home_team": ..., "away_team": ...,
     "pool_home_spread": <float>, "picked_side": <team name>}
load_pool_entries() reads this shape from a CSV (exported from Excel, or
typed by hand) with the same column names -- kept as a separate function
from build_pool_view() on purpose, so the report logic never depends on
CSV being the input format.

The CSV lives at data/pool_picks/week_{week}_{season}.csv and is TRACKED
in git (not gitignored, unlike the other data/ scratch directories) --
the automated Thursday/Saturday pulls need it present in a fresh
checkout, so it has to be committed after you enter picks each week, not
just kept locally.
"""

import csv
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "data"))
import db
import fetch_stats
import fetch_odds
from line_utils import get_latest_line

DEFAULT_CONTEST = "pool"

# Confidence flags card_generator.py never treats as a real signal (large
# edge, prior-season fallback, or extrapolation past the model's reliable
# range -- ARCHITECTURE.md sect;19-20/sect;25). rank_pool_picks() excludes
# any pool pick whose matching card game carries one of these outright,
# not just denies it a tiebreaker -- these are exactly the model's
# least-reliable slices, so they shouldn't influence which pool picks get
# surfaced as strongest (external review's one accepted gap, closed this
# project's own way: drift confirmation over model edge, 2026-08-04).
POOL_RANKING_LOW_CONFIDENCE_FLAGS = {
    "low_confidence_large_edge", "low_confidence_prior_season_data", "no_pick_extrapolation",
}
POOL_RANKING_SIZE = 5


def build_pool_view(conn, pool_entries):
    """pool_entries: see module docstring. Returns every entry with the
    live line, drift, and whether the market's implied favorite has
    flipped since the pool's number -- sorted so picks the market has
    moved most AGAINST surface first (most negative signed_drift_vs_pick),
    since those are the ones most worth a second look."""
    entries = []
    skipped = []

    for pick in pool_entries:
        game_id = pick["game_id"]
        home_team = pick["home_team"]
        away_team = pick["away_team"]
        pool_spread = pick["pool_home_spread"]
        picked_side = pick["picked_side"]

        latest = get_latest_line(conn, game_id)
        if latest is None:
            skipped.append({
                "game_id": game_id, "home_team": home_team, "away_team": away_team,
                "reason": "no_live_line",
            })
            continue

        # home_spread convention: negative = home favored.
        drift = round(latest["home_spread"] - pool_spread, 2)
        pool_favorite = home_team if pool_spread < 0 else away_team
        live_favorite = home_team if latest["home_spread"] < 0 else away_team
        favorite_flipped = pool_favorite != live_favorite

        # Reframe drift relative to the side actually picked, not home/away:
        # for a home pick, drift<0 (home favored more) is movement TOWARD
        # the pick; for an away pick, it's the opposite sign.
        signed_drift_vs_pick = round(-drift if picked_side == home_team else drift, 2)
        if signed_drift_vs_pick > 0:
            movement_vs_pick = "toward_pick"
        elif signed_drift_vs_pick < 0:
            movement_vs_pick = "away_from_pick"
        else:
            movement_vs_pick = "flat"

        entries.append({
            "game_id": game_id,
            "home_team": home_team,
            "away_team": away_team,
            "picked_side": picked_side,
            "pool_home_spread": pool_spread,
            "live_home_spread": latest["home_spread"],
            "live_book": latest["book"],
            "live_line_type": latest["line_type"],
            "drift": drift,
            "signed_drift_vs_pick": signed_drift_vs_pick,
            "movement_vs_pick": movement_vs_pick,
            "favorite_flipped": favorite_flipped,
        })

    entries.sort(key=lambda e: (e["signed_drift_vs_pick"], e["game_id"]))

    return {
        "view": "pool_drift",
        "games": entries,
        "skipped": skipped,
    }


RANK_MIN, RANK_MAX = 1, 5


def load_pool_entries(path):
    """Load pool picks from a CSV with header row:
        game_id,home_team,away_team,pool_home_spread,picked_side,rank
    game_id is OPTIONAL (added 2026-08-13) -- a blank cell is stored as
    None here and resolved later, in ingest_contest_csv(), via the same
    team-name-to-game_id join fetch_odds.py's own ingestion already uses
    (looking a game_id up by hand for every pick each week was real
    friction). Left as a pure, DB-free CSV parser on purpose (see module
    docstring) -- resolving a blank game_id needs a database connection
    this function deliberately doesn't have, so that job belongs to
    ingest_contest_csv(), not here.

    rank is OPTIONAL and nullable (added 2026-08-13): a 1-5 confidence
    ranking for the pick, recorded at lock time so post_game_audit.py can
    report whether higher-confidence picks actually perform better, over
    a season, than lower-confidence ones -- rather than that question
    being answered from memory after the fact. A blank cell is None. Read
    with .get() rather than indexing, so a CSV written before this column
    existed (no 'rank' header at all) still loads instead of raising
    KeyError -- every row just gets rank=None. Raises ValueError for a
    present-but-out-of-[1,5]-range value here, at load time, rather than
    letting a malformed CSV fail later with a less legible DB-level error.

    No other schema beyond these six columns; extra columns are ignored."""
    entries = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw_game_id = row["game_id"].strip()
            raw_rank = (row.get("rank") or "").strip()
            rank = int(raw_rank) if raw_rank else None
            if rank is not None and not (RANK_MIN <= rank <= RANK_MAX):
                raise ValueError(
                    f"rank must be {RANK_MIN}-{RANK_MAX} or blank, got {rank!r} "
                    f"for {row.get('home_team')} vs {row.get('away_team')} in {path}"
                )
            entries.append({
                "game_id": int(raw_game_id) if raw_game_id else None,
                "home_team": row["home_team"].strip(),
                "away_team": row["away_team"].strip(),
                "pool_home_spread": float(row["pool_home_spread"]),
                "picked_side": row["picked_side"].strip(),
                "rank": rank,
            })
    return entries


def default_csv_path(week, season):
    return f"data/pool_picks/week_{week}_{season}.csv"


def ingest_contest_csv(conn, csv_path, season, week, contest=DEFAULT_CONTEST):
    """Loads pool_entries from csv_path (see load_pool_entries) and inserts
    each into contest_entries -- the source of truth for locked pool lines
    (external review, accepted 2026-08-04). INSERT OR IGNORE against
    UNIQUE(contest, season, week, game_id): a row already locked for this
    (contest, season, week, game_id) is left untouched even if the CSV's
    own numbers have since changed -- re-ingesting the same week's CSV is
    idempotent, never a silent overwrite. correct_contest_entry() is the
    only sanctioned way to change an already-locked value.

    Raw team names come straight from the CSV; normalized names resolve
    against `teams` via fetch_odds.resolve_school_name() -- the same
    resolution fetch_odds.py itself uses for the live odds feed -- so
    build_pool_view's join against betting_lines/games behaves the same
    regardless of which spelling convention the pool's own sheet used.
    picked_side is normalized the same way (matched against raw_home/away
    first, since that's the exact string the CSV commits to; resolved
    independently only if it matches neither).

    game_id is OPTIONAL (added 2026-08-13): a row with a blank game_id
    (see load_pool_entries) is resolved via fetch_odds.find_game_id()
    against the NORMALIZED team names for this (season, week) -- the same
    join fetch_odds.py's own live-odds ingestion already relies on, so a
    pick can be entered from team names alone instead of requiring a
    manual game_id lookup every week. A row WITH an explicit game_id skips
    this resolution entirely and behaves exactly as before. A row whose
    game_id can't be resolved this way is reported in `unmatched`, never
    silently dropped -- a pool pick that never makes it into
    contest_entries because of a team-name typo is exactly the kind of
    gap that must be loud, not quiet.

    Returns {"inserted": <count of NEW rows>, "skipped": [{"game_id",
    "home_team", "away_team"}, ...], "unmatched": [{"raw_home_team",
    "raw_away_team", "normalized_home_team", "normalized_away_team"}, ...]}.
    `skipped` names, explicitly, every row whose (contest, season, week,
    game_id) was already locked from an earlier ingest and therefore left
    untouched. This is NOT an error -- it's the correct, intended behavior
    for e.g. a workflow re-run -- but it needs to be visible, not silent:
    a caller who edited the CSV to fix a typo and re-ran this expecting
    the fix to take needs to see that nothing changed, not just an
    unremarkable inserted=0 (external review follow-up, accepted
    2026-08-05). Use correct_contest_entry() to actually change one of
    these rows. `unmatched` names every blank-game_id row that couldn't be
    resolved to a real game_id at all (see above) -- distinct from
    `skipped`, since these rows were never inserted in the first place,
    not already-locked. rank (see load_pool_entries) is inserted as-is,
    None or otherwise."""
    pool_entries = load_pool_entries(csv_path)
    schools = fetch_odds.load_school_names(conn)
    locked_at = datetime.utcnow().isoformat()
    source = f"csv:{csv_path}"

    inserted = 0
    skipped = []
    unmatched = []
    for entry in pool_entries:
        raw_home = entry["home_team"]
        raw_away = entry["away_team"]
        norm_home = fetch_odds.resolve_school_name(schools, raw_home)
        norm_away = fetch_odds.resolve_school_name(schools, raw_away)

        raw_side = entry["picked_side"]
        if raw_side == raw_home:
            norm_side = norm_home
        elif raw_side == raw_away:
            norm_side = norm_away
        else:
            norm_side = fetch_odds.resolve_school_name(schools, raw_side)

        game_id = entry["game_id"]
        if game_id is None:
            game_id = fetch_odds.find_game_id(conn, week, season, norm_home, norm_away)
            if game_id is None:
                unmatched.append({
                    "raw_home_team": raw_home, "raw_away_team": raw_away,
                    "normalized_home_team": norm_home, "normalized_away_team": norm_away,
                })
                continue

        cur = conn.execute(
            "INSERT OR IGNORE INTO contest_entries ("
            "contest, season, week, game_id, raw_home_team, raw_away_team, "
            "normalized_home_team, normalized_away_team, locked_home_spread, "
            "picked_side, rank, locked_at, source"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (contest, season, week, game_id, raw_home, raw_away,
             norm_home, norm_away, entry["pool_home_spread"], norm_side,
             entry["rank"], locked_at, source),
        )
        if cur.rowcount:
            inserted += 1
        else:
            skipped.append({"game_id": game_id, "home_team": raw_home, "away_team": raw_away})
    conn.commit()
    return {"inserted": inserted, "skipped": skipped, "unmatched": unmatched}


def load_pool_entries_from_db(conn, season, week, contest=DEFAULT_CONTEST):
    """The DB-backed replacement for reading pool_entries straight off the
    CSV (external review, accepted 2026-08-04): build_pool_view() gets its
    input from contest_entries -- the locked-at-ingest source of truth --
    not from re-reading the CSV, which could have been hand-edited after
    the fact. Same dict shape load_pool_entries() returns, plus `entry_id`
    (contest_entries.id) for callers that need to target
    correct_contest_entry()."""
    rows = conn.execute(
        "SELECT id, game_id, normalized_home_team, normalized_away_team, "
        "locked_home_spread, picked_side, rank FROM contest_entries "
        "WHERE contest = ? AND season = ? AND week = ? ORDER BY game_id",
        (contest, season, week),
    ).fetchall()
    return [
        {
            "entry_id": r[0], "game_id": r[1], "home_team": r[2], "away_team": r[3],
            "pool_home_spread": r[4], "picked_side": r[5], "rank": r[6],
        }
        for r in rows
    ]


def correct_contest_entry(conn, entry_id, reason, new_locked_home_spread=None, new_picked_side=None,
                           new_rank=None):
    """The ONLY sanctioned way to change an already-locked contest_entries
    row (external review, accepted 2026-08-04). Writes the ORIGINAL values
    to contest_entry_corrections BEFORE touching contest_entries, so a
    correction is always an audited supersession, never a silent overwrite
    -- the number a pool showed at lock time is a historical fact, not
    something that becomes unrecoverable just because it turned out to be
    a typo.

    new_rank (added 2026-08-13) gets the same immutability treatment as
    locked_home_spread/picked_side, not looser handling just because it's
    the user's own subjective confidence call: post_game_audit.py reports
    performance BY rank over a season, and freely rewriting a rank after
    the game is decided (hindsight "I knew it all along") would quietly
    invalidate that report. RANK_MIN/RANK_MAX-checked here the same way
    load_pool_entries() checks it, since this is a second entry point that
    can set rank.

    At least one of new_locked_home_spread/new_picked_side/new_rank must
    be given; the others stay unchanged. `reason` is required -- a
    correction with no stated reason is exactly the kind of silent drift
    this exists to prevent."""
    if not reason or not reason.strip():
        raise ValueError("correct_contest_entry requires a non-empty reason")
    if new_locked_home_spread is None and new_picked_side is None and new_rank is None:
        raise ValueError("correct_contest_entry requires at least one corrected value")
    if new_rank is not None and not (RANK_MIN <= new_rank <= RANK_MAX):
        raise ValueError(f"new_rank must be {RANK_MIN}-{RANK_MAX}, got {new_rank!r}")

    row = conn.execute(
        "SELECT locked_home_spread, picked_side, rank FROM contest_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No contest_entries row with id {entry_id}")
    original_spread, original_side, original_rank = row

    corrected_at = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO contest_entry_corrections ("
        "original_entry_id, original_locked_home_spread, original_picked_side, original_rank, "
        "corrected_locked_home_spread, corrected_picked_side, corrected_rank, reason, corrected_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (entry_id, original_spread, original_side, original_rank, new_locked_home_spread,
         new_picked_side, new_rank, reason, corrected_at),
    )
    conn.execute(
        "UPDATE contest_entries SET locked_home_spread = COALESCE(?, locked_home_spread), "
        "picked_side = COALESCE(?, picked_side), rank = COALESCE(?, rank), corrected_at = ? WHERE id = ?",
        (new_locked_home_spread, new_picked_side, new_rank, corrected_at, entry_id),
    )
    conn.commit()


def rank_pool_picks(pool_view, card=None):
    """Ranks the pool's OWN candidate picks -- not model recommendations --
    by drift confirmation: has the market moved TOWARD this pick since it
    was locked (signed_drift_vs_pick from build_pool_view(), positive =
    toward). That's the PRIMARY sort key, since it's a real, observed
    market signal. Model edge from card_generator.build_card() is a
    TIEBREAKER ONLY when drift ties -- ARCHITECTURE.md sect;19-20 found no
    demonstrated standalone edge for the model, so it never overrides the
    market-based primary signal, only nudges between two picks the market
    likes equally (external review's one accepted gap, closed this
    project's own way, 2026-08-04).

    A pool pick whose matching card game is flagged low-confidence (large
    edge, prior-season fallback, or no-pick-extrapolation --
    POOL_RANKING_LOW_CONFIDENCE_FLAGS) is EXCLUDED entirely, not just
    denied its tiebreaker. A pool pick with no matching card game at all
    (no model output this week, or a game the model didn't line) is still
    eligible -- it simply ranks on drift alone, no edge tiebreak.

    `card`, if given: card_generator.build_card()'s return value. None
    (or no matching flagged games) is fine -- ranking degrades to
    drift-only for every pick.

    Returns up to POOL_RANKING_SIZE entries -- each pool_view game dict
    plus `edge` (None if no usable model signal) and `rank` (1 = strongest)
    -- best drift-confirmation first."""
    confidence_by_game = {}
    edge_by_game = {}
    if card:
        for g in card["games"]:
            confidence_by_game[g["game_id"]] = g["confidence"]
            edge_by_game[g["game_id"]] = g["edge"]

    eligible = []
    for g in pool_view["games"]:
        if confidence_by_game.get(g["game_id"]) in POOL_RANKING_LOW_CONFIDENCE_FLAGS:
            continue
        eligible.append({**g, "edge": edge_by_game.get(g["game_id"])})

    eligible.sort(
        key=lambda g: (g["signed_drift_vs_pick"], g["edge"] if g["edge"] is not None else float("-inf")),
        reverse=True,
    )

    ranked = eligible[:POOL_RANKING_SIZE]
    for i, g in enumerate(ranked):
        g["rank"] = i + 1
    return ranked


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=None,
                         help="Defaults to data/pool_picks/week_{week}_{season}.csv for the "
                              "current week (see default_csv_path/fetch_stats.get_current_week)")
    parser.add_argument("--contest", default=DEFAULT_CONTEST,
                         help=f"Contest name recorded in contest_entries (default: {DEFAULT_CONTEST!r})")
    args = parser.parse_args()

    with db.log_run("pool_view") as run:
        week, season = fetch_stats.get_current_week()
        csv_path = args.csv or default_csv_path(week, season)

        # The CSV stays the entry mechanism, but it ingests INTO
        # contest_entries; the view is always built from what's locked in
        # the DB, not from re-reading the CSV directly (external review,
        # accepted 2026-08-04) -- so a hand-edit to an already-committed
        # CSV can't silently redefine an already-locked pool number.
        conn = db.get_connection()
        try:
            if os.path.exists(csv_path):
                result = ingest_contest_csv(conn, csv_path, season, week, contest=args.contest)
                print(f"Ingested {result['inserted']} new contest_entries row(s) from {csv_path}.")
                if result["skipped"]:
                    games = ", ".join(
                        f'{g["away_team"]} @ {g["home_team"]} (game_id={g["game_id"]})'
                        for g in result["skipped"]
                    )
                    print(
                        f"{len(result['skipped'])} row(s) already locked, not modified -- "
                        f"use correct_contest_entry() to change a locked line: {games}"
                    )
                if result["unmatched"]:
                    games = ", ".join(
                        f'{u["raw_away_team"]} @ {u["raw_home_team"]} '
                        f'(resolved to "{u["normalized_away_team"]}" @ "{u["normalized_home_team"]}", '
                        f'no matching game_id found)'
                        for u in result["unmatched"]
                    )
                    print(
                        f"WARNING: {len(result['unmatched'])} row(s) had no game_id and couldn't be "
                        f"matched to a game for season={season} week={week} -- NOT ingested, fix the "
                        f"team names or provide game_id directly: {games}"
                    )
            else:
                print(f"No pool-picks file at {csv_path} yet -- reading whatever's already locked.")

            pool_entries = load_pool_entries_from_db(conn, season, week, contest=args.contest)
            view = build_pool_view(conn, pool_entries) if pool_entries else None
        finally:
            conn.close()

        if view is None:
            print("No contest_entries locked for this week yet -- nothing to do this run.")
            return

        os.makedirs("data/line_views", exist_ok=True)
        out_path = "data/line_views/pool_drift_latest.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(view, f, indent=2)

        run["rows_added"] = len(view["games"])
        print(f"Pool view: {len(view['games'])} picks, {len(view['skipped'])} skipped. Saved to {out_path}")


if __name__ == "__main__":
    main()
