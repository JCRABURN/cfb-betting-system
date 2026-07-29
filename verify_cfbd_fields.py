"""
verify_cfbd_fields.py
Makes exactly one real call to each CFBD endpoint that fetch_stats.py and
backfill_historical_stats.py depend on, then checks whether the field
paths those two scripts currently assume actually exist in the live
response. This checklist tracks the CURRENT code, not the original
guesses -- when a live run (2026-07-29) found /games fields are camelCase
(homeTeam, not home_team) and the EPA field is called "ppa", not
"epa_per_play", both fetch_stats.py and backfill_historical_stats.py were
corrected and this file's checklist was updated to match. successRate and
havoc.total were confirmed correct on that same run.

Requires CFBD_API_KEY, either already in the environment or in a local
.env file (never committed -- see .gitignore).

Usage:
    python verify_cfbd_fields.py
    python verify_cfbd_fields.py --year 2025 --week 10
    make verify-cfbd
    make verify-cfbd YEAR=2024 WEEK=8
"""

import os
import sys
import argparse

import requests


def load_dotenv(path=".env"):
    """Minimal .env loader -- no new dependency for something this small."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


load_dotenv()

CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "")
CFBD_BASE = "https://api.collegefootballdata.com"
HEADERS = {"Authorization": f"Bearer {CFBD_API_KEY}", "Content-Type": "application/json"}


def check_path(obj, path, label, results):
    """Walk a list of nested dict keys; record whether the full path resolves."""
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            available = list(cur.keys()) if isinstance(cur, dict) else f"<{type(cur).__name__}: {cur!r}>"
            results.append((label, ".".join(path), False, available))
            return
        cur = cur[key]
    results.append((label, ".".join(path), True, cur))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--week", type=int, default=10)
    args = parser.parse_args()

    if not CFBD_API_KEY:
        print("CFBD_API_KEY not set (checked environment and ./.env). Add it to .env and re-run.")
        sys.exit(1)

    print(f"Verifying live CFBD field shapes against {args.year} week {args.week}...\n")
    results = []

    # 1. /games -- fields persist_to_db() writes into the games table.
    # "classification", not "division" -- verified live 2026-07-29 that "division"
    # is silently ignored (returned 304 mixed-division games instead of 52 FBS ones).
    resp = requests.get(f"{CFBD_BASE}/games", headers=HEADERS,
                         params={"year": args.year, "week": args.week, "classification": "fbs"})
    resp.raise_for_status()
    games = resp.json()
    if not games:
        print(f"No games returned for {args.year} week {args.week} -- pick a different --week.")
        sys.exit(1)
    print(f"/games with classification=fbs returned {len(games)} games "
          f"(sanity check: should be roughly 50-70 for a full FBS week, not 300+)")
    game = games[0]
    print(f"Sample game: {game.get('awayTeam')} @ {game.get('homeTeam')}")
    for path in (["id"], ["season"], ["week"], ["seasonType"], ["startDate"],
                 ["homeTeam"], ["awayTeam"], ["venue"], ["neutralSite"], ["conferenceGame"]):
        check_path(game, path, "games", results)
    # homePoints/awayPoints -- what update_results.py reads to grade picks. Only
    # meaningful on a completed game, so fall back to whichever game in the sample
    # actually has scores rather than failing on game[0] if it hasn't kicked off.
    scored_game = next((g for g in games if g.get("homePoints") is not None), game)
    check_path(scored_game, ["homePoints"], "games", results)
    check_path(scored_game, ["awayPoints"], "games", results)
    # Known, permanent gap (not a pass/fail check): CFBD's /games has no lat/long,
    # only venue name + venueId. A /venues lookup would be needed (Phase 4 weather
    # work), so this is reported separately rather than failing the whole script.
    has_coords = "venue_latitude" in game or "venue_longitude" in game
    print(f"  (informational) venue_latitude/venue_longitude present in /games: {has_coords} "
          f"-- expected False; weather needs a separate /venues join, tracked for Phase 4")

    # 2. /ratings/sp -- fetch_sp_ratings()
    resp = requests.get(f"{CFBD_BASE}/ratings/sp", headers=HEADERS, params={"year": args.year})
    resp.raise_for_status()
    sp = resp.json()
    if sp:
        check_path(sp[0], ["team"], "ratings/sp", results)
        check_path(sp[0], ["rating"], "ratings/sp", results)

    # 3. /stats/season/advanced -- the field paths in question (successRate, havoc.total)
    resp = requests.get(f"{CFBD_BASE}/stats/season/advanced", headers=HEADERS,
                         params={"year": args.year, "excludeGarbageTime": True})
    resp.raise_for_status()
    adv = resp.json()
    if adv:
        sample = adv[0]
        print(f"\nSample advanced-stats entry (team={sample.get('team')}):")
        print(f"  top-level keys: {list(sample.keys())}")
        if isinstance(sample.get("offense"), dict):
            print(f"  offense keys: {list(sample['offense'].keys())}")
        if isinstance(sample.get("defense"), dict):
            print(f"  defense keys: {list(sample['defense'].keys())}")
            if isinstance(sample["defense"].get("havoc"), dict):
                print(f"  defense.havoc keys: {list(sample['defense']['havoc'].keys())}")
        for path in (["team"], ["offense", "ppa"], ["defense", "ppa"],
                     ["offense", "successRate"], ["defense", "successRate"],
                     ["defense", "havoc", "total"]):
            check_path(sample, path, "stats/season/advanced", results)

    # 4. /records -- fetch_team_records()
    resp = requests.get(f"{CFBD_BASE}/records", headers=HEADERS, params={"year": args.year})
    resp.raise_for_status()
    records = resp.json()
    if records:
        check_path(records[0], ["team"], "records", results)
        check_path(records[0], ["total", "wins"], "records", results)
        check_path(records[0], ["total", "losses"], "records", results)

    # 5. /teams -- backfill_historical_stats.fetch_teams()
    resp = requests.get(f"{CFBD_BASE}/teams", headers=HEADERS, params={"year": args.year})
    resp.raise_for_status()
    teams = resp.json()
    if teams:
        for path in (["id"], ["school"], ["conference"], ["classification"], ["division"]):
            check_path(teams[0], path, "teams", results)

    print("\n" + "=" * 72)
    print(f"{'ENDPOINT':<25}{'FIELD PATH':<30}STATUS")
    print("-" * 72)
    all_ok = True
    for label, path, ok, value in results:
        status = "OK" if ok else "MISSING"
        if not ok:
            all_ok = False
        print(f"{label:<25}{path:<30}{status}")
        if not ok:
            print(f"  -> available at that point: {value}")
    print("=" * 72)

    if all_ok:
        print("\nAll field-name assumptions in fetch_stats.py / backfill_historical_stats.py "
              "match the live CFBD response.")
        sys.exit(0)
    else:
        print("\nSome field-name assumptions DO NOT match the live response -- "
              "fix fetch_stats.py / backfill_historical_stats.py before trusting cfb.db.")
        sys.exit(1)


if __name__ == "__main__":
    main()
