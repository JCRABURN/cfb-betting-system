"""
ingest_pool_slate.py
Stores the pool's FULL slate of frozen lines (spread + total for every game),
not just the picks you made. `contest_entries` only ever holds picks; the
stale-number signal (models/move_signal.py) needs every game the pool lists,
because the games worth picking are exactly the ones you haven't picked yet.

The pool freezes its lines Tuesday noon, so these numbers are immutable the
same way contest_entries are: INSERT OR IGNORE keyed on (contest, season,
week, game_id). Re-ingesting a file is idempotent. If a re-sent slate carries
a DIFFERENT number for a game already stored, that is reported as a conflict
and NOT applied -- a pool's printed number at lock is a historical fact, and a
silent overwrite would corrupt every signal graded against it.

Accepted layouts (detected from the header row, not by position):
  * xlsx with 'Away Team' / 'Home Team' and 'Home Line' | 'Home Spread', and
    'Total (O/U)' | 'Total'   (the Week 1 and Week 7 sheets)
  * csv with away_team / home_team / home_spread / locked_total  (Week 2)
Sign convention is the project's: negative home spread = home favored. Both
layouts already use it; the parser asserts away spread == -home spread where
both are present, so a sign-flipped sheet fails loudly instead of ingesting.

File naming (week/season are read from it):
    data/pool_picks/raw/week_{N}_{season}_splashsports_slate.{csv|xlsx}

Usage:
    python data/ingest_pool_slate.py            # every matching file, idempotent
    python data/ingest_pool_slate.py path.xlsx --week 7 --season 2026
"""

import csv
import glob
import os
import re
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "data"))
import db
import fetch_odds

DEFAULT_CONTEST = "pool"
RAW_DIR = os.path.join(_ROOT, "data", "pool_picks", "raw")
_NAME_RE = re.compile(r"week_(\d+)_(\d{4})_splashsports_slate\.(csv|xlsx)$", re.IGNORECASE)

_HOME_SPREAD_HEADERS = ("home line", "home spread", "home_spread")
_AWAY_SPREAD_HEADERS = ("away line", "away spread", "away_spread")
_TOTAL_HEADERS = ("total (o/u)", "total", "locked_total")
_AWAY_HEADERS = ("away team", "away_team")
_HOME_HEADERS = ("home team", "home_team")


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _find(header, candidates):
    for i, h in enumerate(header):
        if h is not None and str(h).strip().lower() in candidates:
            return i
    return None


def _rows_to_games(rows, label):
    """rows: iterable of row tuples/lists including a header row somewhere
    near the top. Returns [{"away", "home", "home_spread", "total"}]."""
    rows = list(rows)
    header_idx = None
    for i, row in enumerate(rows[:15]):
        cells = [str(c).strip().lower() if c is not None else "" for c in row]
        if any(c in _AWAY_HEADERS for c in cells) and any(c in _HOME_HEADERS for c in cells):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"{label}: no header row with away/home team columns found")

    header = rows[header_idx]
    ia, ih = _find(header, _AWAY_HEADERS), _find(header, _HOME_HEADERS)
    ihs, ias = _find(header, _HOME_SPREAD_HEADERS), _find(header, _AWAY_SPREAD_HEADERS)
    it = _find(header, _TOTAL_HEADERS)
    if ihs is None:
        raise ValueError(f"{label}: no home spread column ('Home Line'/'Home Spread'/'home_spread')")

    games = []
    for row in rows[header_idx + 1:]:
        if len(row) <= max(ia, ih, ihs) or not row[ia] or not row[ih]:
            continue
        away, home = str(row[ia]).strip(), str(row[ih]).strip()
        hs = _num(row[ihs])
        if hs is None:
            continue
        if ias is not None and len(row) > ias:
            as_ = _num(row[ias])
            if as_ is not None and abs(as_ + hs) > 1e-9:
                raise ValueError(
                    f"{label}: {away} @ {home}: away spread {as_} and home spread {hs} are not "
                    f"opposites -- sheet may be sign-flipped; refusing to ingest")
        games.append({"away": away, "home": home, "home_spread": hs,
                      "total": _num(row[it]) if it is not None and len(row) > it else None})
    return games


def parse_slate_file(path):
    ext = os.path.splitext(path)[1].lower()
    label = os.path.basename(path)
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            return _rows_to_games(list(csv.reader(f)), label)
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                try:
                    return _rows_to_games(list(ws.iter_rows(values_only=True)), f"{label}:{ws.title}")
                except ValueError:
                    continue
        finally:
            wb.close()
        raise ValueError(f"{label}: no sheet with a recognisable slate header")
    raise ValueError(f"{label}: unsupported file type {ext!r}")


def ingest_slate(conn, games, season, week, source, contest=DEFAULT_CONTEST):
    """Returns {"inserted", "skipped", "conflicts", "unmatched"}.
    skipped   = already stored with the SAME numbers (idempotent re-ingest)
    conflicts = already stored with DIFFERENT numbers; NOT applied
    unmatched = could not be resolved to a game_id; never silently dropped"""
    schools = fetch_odds.load_school_names(conn)
    now = datetime.utcnow().isoformat()
    out = {"inserted": 0, "skipped": 0, "conflicts": [], "unmatched": []}
    for g in games:
        home = fetch_odds.resolve_school_name(schools, g["home"])
        away = fetch_odds.resolve_school_name(schools, g["away"])
        game_id = fetch_odds.find_game_id(conn, week, season, home, away)
        if game_id is None:
            out["unmatched"].append({"away": g["away"], "home": g["home"],
                                     "resolved_away": away, "resolved_home": home})
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO pool_lines (contest, season, week, game_id, home_team, away_team, "
            "pool_home_spread, pool_total, source, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (contest, season, week, game_id, home, away, g["home_spread"], g["total"], source, now),
        )
        if cur.rowcount:
            out["inserted"] += 1
            continue
        stored = conn.execute(
            "SELECT pool_home_spread, pool_total FROM pool_lines "
            "WHERE contest = ? AND season = ? AND week = ? AND game_id = ?",
            (contest, season, week, game_id),
        ).fetchone()
        if stored == (g["home_spread"], g["total"]):
            out["skipped"] += 1
        else:
            out["conflicts"].append({
                "game_id": game_id, "away": away, "home": home,
                "stored": {"home_spread": stored[0], "total": stored[1]},
                "incoming": {"home_spread": g["home_spread"], "total": g["total"]},
            })
    conn.commit()
    return out


def discover_files(raw_dir=RAW_DIR):
    found = []
    for path in sorted(glob.glob(os.path.join(raw_dir, "week_*_slate.*"))):
        m = _NAME_RE.search(os.path.basename(path))
        if m:
            found.append((path, int(m.group(1)), int(m.group(2))))
    return found


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?")
    parser.add_argument("--week", type=int)
    parser.add_argument("--season", type=int)
    args = parser.parse_args()

    if args.path:
        if args.week is None or args.season is None:
            parser.error("--week and --season are required with an explicit path")
        targets = [(args.path, args.week, args.season)]
    else:
        targets = discover_files()

    with db.log_run("pool_slate") as run:
        conn = db.get_connection()
        try:
            total = 0
            for path, week, season in targets:
                result = ingest_slate(conn, parse_slate_file(path), season, week,
                                      source=f"slate:{os.path.basename(path)}")
                total += result["inserted"]
                print(f"{os.path.basename(path)}: {result['inserted']} new, {result['skipped']} already stored, "
                      f"{len(result['conflicts'])} conflict(s), {len(result['unmatched'])} unmatched")
                for c in result["conflicts"]:
                    print(f"  CONFLICT (not applied): {c['away']} @ {c['home']}  "
                          f"stored {c['stored']} vs incoming {c['incoming']}")
                for u in result["unmatched"]:
                    print(f"  UNMATCHED (not stored): {u['away']} @ {u['home']} "
                          f"-> {u['resolved_away']} @ {u['resolved_home']}")
            run["rows_added"] = total
        finally:
            conn.close()


if __name__ == "__main__":
    main()
