# CFB Betting System — Architecture Audit

_Last updated: 2026-07-28 (Phase 0 audit + Phase 1 foundation + Phase 2 historical stats)_

> **Phase 1 status: done** (committed `a6463de`). **Phase 2 status: done, pending your
> review.** See §7 for Phase 1 and §8 for Phase 2. Sections 1–6 below are the original
> Phase 0 audit, kept as-is for the historical record of what things looked like before
> the SQLite migration.

## 1. Repo map

```
data/
  fetch_stats.py      # CFBD: games, SP+, EPA, records + Open-Meteo weather
  fetch_odds.py        # The Odds API: spreads, consensus line, line movement
models/
  spread_model.py      # projects a spread, scores confidence signals, sizes units
  generate_report.py    # renders markdown report + dashboard JSON + picks log
  update_results.py     # grades picks, computes CLV, retrains weights
docs/data/              # JSON consumed by a dashboard (dashboard front-end itself is NOT in this repo)
outputs/                # generated markdown reports
.github/workflows/
  weekly_report.yml     # Tue 9am CT: fetch_stats -> fetch_odds -> spread_model -> generate_report -> commit
  results_updater.yml   # Mon 6am CT: update_results -> commit
```

No `requirements.txt`, `Makefile`, `.env.example`, or `tests/` directory exists anywhere in the repo.

## 2. Data sources currently ingested

| Source | Script | What | Auth |
|---|---|---|---|
| CollegeFootballData (CFBD) | `fetch_stats.py` | current-week games/schedule, SP+ ratings, advanced (EPA/success rate) stats, W-L records | `CFBD_API_KEY` env var, Bearer header |
| Open-Meteo | `fetch_stats.py::fetch_weather` | 7-day hourly forecast at venue lat/long, takes hour-0 temp/wind/precip | none (free) |
| The Odds API | `fetch_odds.py` | current spreads across 4 books (DK/FD/BetMGM/Caesars), consensus = simple average | `ODDS_API_KEY` env var, query param |
| CFBD (again) | `update_results.py::fetch_game_results` | final scores for grading | `CFBD_API_KEY` |

**Not yet ingested, despite being in the CLAUDE.md target list:** historical betting lines/closing lines, historical SP+/EPA back to 2019, injuries/roster/transfer portal data. Nothing in the repo fetches anything before "this week."

## 3. Storage — this is the biggest gap

**There is no database.** Everything is flat JSON files written by `json.dump`, and critically:

- `fetch_stats.py` → `data/stats/week_{w}_{y}.json`
- `fetch_odds.py` → `data/spreads/{opening|current}_week_{w}_{y}.json`
- `spread_model.py` → `data/analysis/week_{w}_{y}.json`

**None of `data/` is ever committed.** Both GitHub Actions workflows only `git add docs/data/ outputs/` (and `results_updater.yml` also adds `models/weights.json tracker/`). Since each Action run starts from a fresh `actions/checkout`, everything under `data/` is **built and thrown away in the same job** — there is no persistent history of stats, EPA, SP+, or line movement anywhere. This directly blocks the CLAUDE.md goals of backtesting against closing lines and tracking line movement over time: the raw material for that doesn't survive past a single Tuesday run.

It gets worse for `update_results.py`: `fetch_closing_lines()` reads `data/spreads/current_week_{w}_{y}.json` expecting it to still be on disk from the prior Tuesday's run. On a fresh Monday checkout **that file never exists**, so `closing_lines` is always `{}` and **CLV is silently always `None`**. This isn't a hypothetical — it's guaranteed by how the workflows are wired, and I confirmed it by re-running the pipeline locally (see §4).

Similarly, `generate_report.py::load_performance_log()` reads `tracker/performance_log.csv`, and `results_updater.yml` stages `tracker/` for commit — but **nothing in the codebase ever writes that file**. It's a dead code path on both ends.

What *is* persisted (via `docs/data/`) is only the final derived output per week: the report JSON, the flattened `all_picks.json` log, and `performance_stats.json`. That's presentation data, not raw ingested data — you can't rebuild or re-backtest a model from it.

**One more thing worth flagging directly:** your working tree currently has staged/untracked changes to `docs/data/all_picks.json`, `weeks_index.json`, and a new `week_14_2024.json`, containing what looks like hand-authored or test-generated picks for 2024 weeks 11–14 (narrative-style `key_factors` like "sharp action Tuesday", results already marked `"settled"`, `generated_at: 2024-12-03`). `weeks_index.json` also now references `week_11_2024.json`, `week_12_2024.json`, `week_13_2024.json` — **none of which exist as files**, so the dashboard would 404 on three of the four weeks it lists. I haven't touched or committed any of this — flagging so you can tell me whether it's demo/test data to discard or real work in progress to keep and fix.

### Migration plan → SQLite (Phase 1 proposal)

Single file `data/cfb.db`. Proposed tables:

- `teams` (team_id/name, conference, division)
- `games` (game_id, season, week, home/away team, venue, kickoff, neutral_site)
- `betting_lines` (game_id, book, market, spread, total, moneyline, **captured_at timestamp**, source) — append-only so line movement is just a query, not a bolt-on field
- `team_game_stats` (game_id, team_id, side, epa_off, epa_def, success_rate, havoc_rate, sp_rating, source)
- `weather` (game_id, captured_at, temp_f, wind_mph, precip_pct, is_forecast_or_actual)
- `injuries` (team_id, player, status, report_date, source)
- `picks` (replaces `all_picks.json`: game_id, projected_spread, edge, units, result, clv, settled_at)
- `ingestion_runs` (id, source, started_at, finished_at, rows_added, status, error) — the run-log CLAUDE.md asks for

This also fixes the CI persistence problem for free: SQLite is one file, so `git add data/cfb.db` in the workflow keeps full history across runs the same way `docs/data/*.json` does today, instead of needing a separate "don't lose the raw data" mechanism.

## 4. What runs, what's broken — verified by executing the pipeline

Ran all 5 scripts locally end-to-end (offseason, no API keys set, isolated from your working tree so your uncommitted `docs/data/` changes weren't touched):

| Step | Result |
|---|---|
| `fetch_stats.py` | Runs. CFBD calendar/games calls 401 without a key → caught, falls back to "offseason" placeholder correctly. Graceful. |
| `fetch_odds.py` | Runs. Detects offseason flag from stats file, writes empty opening/current placeholders. Graceful. |
| `spread_model.py` | Runs, 0 qualified bets (expected, no data). |
| `generate_report.py` | **Crashes on Windows**: `UnicodeEncodeError` — the file is opened with the default `cp1252` encoding and the report contains emoji (🏈, 🔥, ⚠️, ❌). Every `open(path, "w")` in this file (and the other scripts) needs `encoding="utf-8"` explicitly. GitHub Actions' `ubuntu-latest` runner defaults to UTF-8 so this hasn't surfaced in CI, but it'll bite anyone testing locally on Windows, which is your environment. |
| `update_results.py` | Exits early ("No picks file found") since no `docs/data/all_picks.json` existed in the isolated test dir — expected given the test setup, not a bug in isolation, but see the CLV issue in §3 which *is* a real bug. |

## 5. Code quality issues

1. **No SQLite, no run logging, no smoke tests, no `.env.example`** — all explicitly required by CLAUDE.md's engineering rules, none present yet.
2. **Windows encoding crash** — every `open(..., "w")` writing JSON/markdown with emoji needs `encoding="utf-8"`. Affects `generate_report.py` today; latent risk anywhere else text is written.
3. **Raw ingested data isn't durable** (§3) — the single biggest issue. Backtesting and line-movement analysis are impossible right now because the inputs vanish after each CI run.
4. **CLV is silently always `None`** in production due to the missing `data/spreads/current_week_*.json` file on Monday runs — no error, no log, just a null field that looks intentional.
5. **Dead code path**: `tracker/performance_log.csv` is read but never written; `results_updater.yml` stages a directory that will never exist.
6. **Fragile team-name join key**: `spread_model.py` and `fetch_odds.py` match games between CFBD and The Odds API by concatenating `home_team + away_team` strings. Any naming mismatch between the two providers (e.g., "Ohio State" vs "Ohio St.") silently drops the odds for that game — `odds_entry` becomes `None`, the game just doesn't qualify, with no warning logged. Worth a canonical team-name mapping table once lines ingestion is built out (Phase 3).
7. **`models/weights.json` has never been committed** — no history in git, meaning the "self-learning" retrain step in `update_results.py` has not yet run to completion in production (or its commit step never fired). Worth confirming whether `update_results.py` has actually executed successfully via Actions yet.
8. **Dangling dashboard file references** — see the `weeks_index.json` note in §3.

## 6. Summary for a non-Python-author owner

Plain-English version: you have three real, working pieces — stat fetching, an odds fetcher, and a projection/report generator — glued together by two scheduled GitHub Actions. They run gracefully in the offseason (i.e., right now) and don't crash the CI job. But the system currently has no memory: every Tuesday it fetches fresh stats, projects spreads, writes a report, and throws away all the raw numbers it used to get there. Only the final picks list survives, as JSON files on the dashboard branch. That's fine for "what did we bet last week" but not enough to ever backtest the model or study line movement, which is explicitly a goal. The fix is Phase 1: move to a single SQLite file that persists in the repo, so every ingestion adds to history instead of overwriting it.

## 7. Phase 1 — what was built

**Discarded first:** the staged 2024 weeks 11–14 demo/test data in `docs/data/` (confirmed synthetic — narrative `key_factors`, pre-settled results, and `weeks_index.json` pointing at 3 files that didn't exist on disk). Reset to the real HEAD state (empty picks, one pending Week 1 2026 summary) before building anything.

### Schema (`db.py`, `data/cfb.db`)

Built the schema proposed in §3 with two additions per your review:
- **`picks.pick_type`** — `'live' | 'backfilled' | 'synthetic'`, defaults to `'live'`. Every insert path in this phase (`generate_report.py`, `migrate_to_sqlite.py`) tags rows `'live'` explicitly, so real production picks can never silently mix with test/backfilled data in a backtest.
- **`source` + `fetched_at`** on `betting_lines` and `team_game_stats` — every row now carries where it came from and when it was captured, in addition to the append-only design (rows are never updated in place, so line movement is a query over `fetched_at`, not a bolt-on field).

`teams` and `injuries` tables exist per the target schema but stay empty — nothing ingests a canonical team list or injury data yet (that's Phase 2/4 work, not invented here).

### Windows encoding bug — fixed

All 22 `open()` calls across the 5 pipeline scripts now pass `encoding="utf-8"` explicitly. Verified by re-running `generate_report.py` locally on Windows — it no longer raises `UnicodeEncodeError` on the emoji in the report.

### Persistence wired into the actual scripts, not just the workflows

- `data/fetch_stats.py` → writes `games` + `team_game_stats` (one row per team per capture) + `weather` into `data/cfb.db`, in addition to its existing `data/stats/*.json` output.
- `data/fetch_odds.py` → writes `betting_lines` (one row per book + a `consensus` row), best-effort joined to `games` by season/week/team name. When the join fails (verified with a deliberate "Ohio St." vs "Ohio State" mismatch — see the fragile-join issue in §5.6), the row is still saved with `game_id = NULL` and a warning is printed, instead of silently dropping data.
- `models/generate_report.py` → inserts newly-created pending picks into `picks` (`pick_type='live'`).
- `models/update_results.py` → **`fetch_closing_lines()` now queries `data/cfb.db` instead of reading `data/spreads/current_week_*.json`.** This is the fix for the CLV-always-`None` bug from §5.4: that JSON file never survived to Monday's run, so CLV always came back `None`; the DB persists across runs, so it now resolves correctly. Verified end-to-end with a fixture: opening line -6.5, simulated closing line -8.0 → CLV computed as `1.5`, and the pick correctly settled as a `win` with `unit_pl = 1.818` in both `picks` and `games`.
- Every ingestion entry point (`cfbd_stats`, `odds_api`, `report_picks`, `results_update`, plus the one-time `migration_json_to_sqlite`) logs a row to `ingestion_runs` — the run-log table CLAUDE.md's engineering rules ask for.

### A second real bug found while wiring the workflow, not just the one asked about

`results_updater.yml` committed `git add docs/data/ models/weights.json tracker/`. Since `tracker/` has never existed (nothing writes it — see §5.5), `git add` with multiple pathspecs where any one doesn't match fails with `fatal: pathspec 'tracker/' did not match any files` (exit 128) — **before `git commit` ever runs**. Verified this directly. This is why `models/weights.json` has no git history despite the retrain logic existing: the commit step has been silently failing every single week. Fixed by dropping `tracker/` from the `git add` line (and removed the now-fully-dead `generate_report.py::load_performance_log()` function that read from it, since nothing ever wrote to it either).

### Workflow changes — explicit diff of intent

- **`weekly_report.yml`**: added `data/cfb.db` to the commit step. This is the actual fix for "stop discarding raw data" — `fetch_stats.py`/`fetch_odds.py` now write into that file during the job, and this is what makes it survive to the next run instead of being scratch.
- **`results_updater.yml`**: added `data/cfb.db` (now also written by `update_results.py` — final scores into `games`, settled picks into `picks`); removed the broken `tracker/` pathspec that was making the commit step fail outright.
- **`.gitignore`**: added `data/stats/`, `data/spreads/`, `data/analysis/` — these remain intentional per-run scratch (the enriched JSON is still written for the current run's own use), now explicitly marked as such instead of just happening to never be committed by omission.

### Migration — row counts

Ran `migrate_to_sqlite.py` against the real (post-demo-data-discard) `docs/data/all_picks.json`:

```
docs/data/all_picks.json picks (before): 0
picks table row count (before migration): 0
picks table row count (after migration):  0
Rows migrated this run: 0
```

There was nothing to migrate — the real production log is genuinely empty right now (offseason, week 1 2026 has 0 picks). The script is idempotent and safe to re-run; it's a no-op today by design, not a shortcut. `ingestion_runs` shows 1 row logging that this migration ran.

### End-to-end verification

Ran all 5 scripts via their real `main()` entry points back-to-back (offseason path, no API keys — the only path testable without live keys), then separately exercised the season-with-real-games code paths (`persist_to_db`, `persist_lines_to_db`, `persist_picks_to_db`, `fetch_closing_lines`, `persist_results_to_db`) directly against fixture data simulating 2 games, since real API keys aren't available in this environment. Both passes completed with exit code 0 and the expected row counts in every table (2 games → 4 `team_game_stats` + 2 `weather` + 8 total from `fetch_stats`; 2 games × up-to-2-books + 1 consensus each → 5 `betting_lines` rows from `fetch_odds`, with the deliberate name-mismatch game correctly landing as `game_id=NULL` plus a warning rather than being dropped).

### New files

- `db.py` (repo root) — schema + connection helper + `log_run()` context manager, imported by the 4 scripts above via a `sys.path` shim (each script's own directory is what Python puts on `sys.path`, not the repo root, since they're invoked as `python data/fetch_stats.py` etc.)
- `migrate_to_sqlite.py` (repo root) — one-time/rerunnable backfill from `docs/data/all_picks.json` into `picks`
- `data/cfb.db` — the actual database, now committed

### ⚠️ Outstanding before Phase 1 is truly closed

Everything in this phase was verified against **synthetic fixtures**, not live APIs — there's no `CFBD_API_KEY` or `ODDS_API_KEY` available in this environment, so the season-with-real-games code paths (`persist_to_db`, `persist_lines_to_db`, `fetch_closing_lines`, `persist_results_to_db`) were exercised directly with hand-built fixture data standing in for CFBD/The Odds API responses, not an actual end-to-end run through `weekly_report.yml` / `results_updater.yml` against the real APIs. The offseason path (no games) *was* verified for real, since that's what actually runs today.

**Before Week 0, run the real pipeline once against live CFBD + Odds API data** (either locally with real keys, or by watching the first live `workflow_dispatch` run in Actions) and check `data/cfb.db` row counts land as expected. Field-name assumptions from the CFBD/Odds API docs (e.g. exact JSON shape of `/stats/season/advanced`) are untested against the real response — a fixture can't catch a wrong key name.

## 8. Phase 2 — historical SP+/EPA/success rate/havoc rate (2019+)

### Does Phase 1's persistence layer give this somewhere to live?

Yes, with one gap that's now closed. `team_game_stats` already had nullable `game_id`/`week` and a required `season` — exactly the shape a season-level snapshot needs (one row per team per season, no game attached). But it was missing success rate and havoc rate entirely. Added via an `ALTER TABLE` migration (`db._migrate_schema()`, runs automatically inside `init_db()` so the already-committed `data/cfb.db` picks up the new columns without a manual step):

- `offense_success_rate`, `defense_success_rate`
- `havoc_rate` — singular, not split offense/defense. CFBD's advanced-stats model only exposes havoc as a defensive stat (havoc *generated*), so an "offense_havoc_rate" column would be fabricating a metric that doesn't exist in the source data.

Also backfilled `offense_success_rate`/`defense_success_rate`/`havoc_rate` into `fetch_stats.py`'s **weekly** in-season write path (`persist_to_db`), not just the historical script — the same CFBD `/stats/season/advanced` call it already makes contains these fields, so leaving them `NULL` for the current season while historical seasons have them would create an inconsistent dataset once modeling starts. This only touches what gets written to `data/cfb.db`; the JSON/report/model-facing `enriched_games` shape that `spread_model.py` and `generate_report.py` read is untouched.

### What was built

- **`data/backfill_historical_stats.py`** — loops seasons (default 2019 through last calendar year; the current season is left to the weekly `fetch_stats.py` run rather than double-ingested), fetching SP+, advanced stats, and records by reusing `fetch_stats.py`'s existing fetch functions (same directory, imported directly — no duplicated request logic). Also fetches `/teams` (FBS only) and upserts into the previously-empty `teams` table.
- **Idempotent**: a season already present with `source='cfbd_historical_backfill'` is skipped with zero API calls unless `--force` is passed; a per-team existence check underneath also protects against a run that got interrupted partway through a season.
- **Incremental**: `--start-year`/`--end-year` mean re-running next year with a wider range only fetches the new season — verified directly (see below).
- Every run logs to `ingestion_runs` (`source='cfbd_historical_backfill'`).

### Smoke tests — new `tests/` directory, `requirements.txt`, `Makefile`, `.env.example`

None of these existed before (flagged as a gap in §5.1 of the original audit). Added:
- `requirements.txt` (`requests`, `pytest`) and `.env.example` (`CFBD_API_KEY`, `ODDS_API_KEY`) — both explicitly called for by CLAUDE.md's engineering rules, neither previously present in any phase.
- `Makefile` with a `test` target. **Note:** this machine has no `make` installed (verified — `make: command not found`), so `pytest -q` is what was actually run here; `make test` will work in CI (`ubuntu-latest` has `make`) or on a dev machine that has it, but wasn't exercised on this box.
- `tests/` — 11 tests, all passing (`pytest -q` → `11 passed in 0.67s`):
  - `test_db_schema.py`: schema creates all 8 tables, `team_game_stats` has the 3 new columns, `picks.pick_type` defaults to `'live'`, `log_run()` records both success and error/re-raise cases correctly.
  - `test_backfill_historical_stats.py`: mocks `requests.get` with fixture CFBD payloads (2 fake teams) and verifies — row values land correctly including the new success/havoc columns; re-running the same season without `--force` doesn't duplicate rows; re-running **does** skip all API calls for an already-ingested season (asserted via call-count, not just row count); `--force` does re-fetch; `teams` upsert is update-not-duplicate under simulated conference realignment.

### End-to-end verification (fixtures, not live API — same caveat as Phase 1)

Ran the actual `backfill_historical_stats.py` CLI (`main()`, not just the internal functions the pytest suite covers) twice against 2 fixture seasons in an isolated copy:

```
First run  (--start-year 2019 --end-year 2020): 7 API calls, 4 rows added (2 teams × 2 seasons)
Second run (same args, no --force):             1 API call (teams list only), 0 rows added
                                                 both seasons correctly report "already ingested, skipping"
```

Final `data/cfb.db` in that test: `teams` = 2, `team_game_stats` = 4, `ingestion_runs` shows both runs logged (`rows_added` 4 and 0 respectively). This confirms idempotency and incrementality end-to-end, not just at the unit level.

**Same outstanding item as Phase 1**: no live `CFBD_API_KEY` in this environment, so field-name assumptions for `/stats/season/advanced` (`successRate`, `havoc.total`) and `/teams` (`classification`, `division`) are best-guess based on the CFBD docs, unverified against a real response. Fold this into the same pre-Week-0 live-API verification pass already flagged in §7 — run `backfill_historical_stats.py --start-year 2019 --end-year 2025` for real and spot-check a few known teams' numbers before trusting the table.

## 9. Live CFBD verification (2026-07-29, `verify_cfbd_fields.py` against 2025 week 10)

Ran `make verify-cfbd`-equivalent against real CFBD data once the owner added `CFBD_API_KEY`/`ODDS_API_KEY` to a local `.env`. Confirmed two things were right, found four things wrong (two more than the two we set out to check), fixed all four in both `fetch_stats.py` and `update_results.py`, and re-verified.

### 1. What CFBD actually returned

**Confirmed correct** (no change needed): `offense.successRate` / `defense.successRate` and `defense.havoc.total` — exactly what `backfill_historical_stats.py` and Phase 2's addition to `fetch_stats.py` assumed. Also confirmed correct: `/records`' `total.wins`/`total.losses`, and every field on `/teams` (`id`, `school`, `conference`, `classification`, `division`).

**Wrong, fixed:**

| Assumed | Actual | Where it was wrong |
|---|---|---|
| `epa_per_play` (under `offense`/`defense`) | `ppa` | `fetch_stats.py` (weekly path) and `backfill_historical_stats.py` (historical path) — both fixed. Verified `ppa == totalPPA / plays` exactly, confirming it's the correct per-play average. |
| `home_team` / `away_team` (and `season_type`, `start_date`, `neutral_site`, `conference_game`) | `homeTeam` / `awayTeam` (`seasonType`, `startDate`, `neutralSite`, `conferenceGame`) | `/games` is camelCase throughout. This bug **predates every phase in this project** — it was in the original `fetch_stats.py` before Phase 0. Its effect: `home`/`away` were always `""` for any real (non-offseason) week, meaning SP+/EPA/record lookups (`sp_ratings.get(home, ...)`) always missed and returned defaults. **The pipeline has never correctly enriched a real game with real stats until this fix.** |
| `"division": "fbs"` param on `/games` | `"classification": "fbs"` | Silently ignored by CFBD — verified: `division=fbs` returned 304 games (FBS+FCS+D-II+D-III mixed) for a week that has 52 actual FBS games; `classification=fbs` returned exactly 52, all `homeClassification`/`awayClassification` = `"fbs"`. Fixed in `fetch_stats.py::fetch_games()` and `update_results.py::fetch_game_results()` (both called `/games` with the same wrong param). |
| `home_points` / `away_points` | `homePoints` / `awayPoints` | `update_results.py` — `determine_ats_result()`'s early-return (`if actual_home_score is None: return "pending"`) means **every pick has always settled as `"pending"` forever**, never actually graded W/L/Push. Found and fixed alongside the `division` bug since both live in the same `/games` response. |

**Confirmed absent, not a bug** — `venue_latitude`/`venue_longitude` genuinely don't exist anywhere in `/games` (only a `venue` name string and `venueId`). Getting coordinates needs a separate `/venues` lookup joined by `venueId` — this was always going to be Phase 4 work ("build a stadium lat/long reference table"), and now we know precisely why weather has never populated: `fetch_weather()`'s guard clause (`if not venue_lat or not venue_lon: return {}`) has been correctly returning empty every time, exactly as designed for missing data, just on a field that was never going to be there without a `/venues` join. Not fixed here — flagged for Phase 4, where it belongs.

### 2. Did it land in `cfb.db` correctly?

Ran a real one-week ingestion (2025 week 10 — a completed week from last season, not the full 2019+ backfill, per instruction) using the fixed code, after cleaning out the one batch written with the pre-fix `division` bug (304 games / 912 rows — deleted `team_game_stats` before `games` to satisfy the FK constraint, then re-ran clean):

```
games:            52   (matches classification=fbs sanity check)
team_game_stats: 104   (2 per game: home + away)
```

Sample row, real data: `Kennesaw State` — `sp_rating=-5.4`, `offense_epa_play=0.170`, `defense_epa_play=0.157`, `offense_success_rate=0.413`, `defense_success_rate=0.421`, `havoc_rate=0.168`. All in plausible ranges. Team names are real (`UTEP @ Kennesaw State`, `Florida @ Georgia`, etc.), not the empty strings the pre-fix code would have produced.

Also separately verified the `update_results.py` write path: fetched real final scores for the same 52 games via the fixed `fetch_game_results()`, ran `persist_results_to_db()` against them (no picks existed for this week to settle — this was plumbing verification, not a real betting week), and confirmed the `games` table updated correctly: e.g. `Arkansas 35 – Mississippi State 38, completed=1`.

`ingestion_runs` now shows the full trail, including the dirty run that got cleaned up (kept as an honest audit log rather than deleted):
```
migration_json_to_sqlite            success    0
cfbd_stats_live_verification        success    912   <- pre-fix, data since deleted
cfbd_stats_live_verification_fixed  success    156   <- post-fix, real data kept (52+104)
results_update_live_verification    success    52
```

### 3. Fixture vs. reality — what Phases 1 and 2's assumptions got wrong

Every fixture used in the Phase 1/2 pytest suite and manual verification used **snake_case field names** (`home_team`, `epa_per_play`, `home_points`, etc.) because that's what the pre-existing code (and my extensions to it) assumed. Fixtures, by construction, can only validate that code does what it's written to do — they can't catch a wrong assumption baked into both the code and the fixture. That's exactly what happened here on four separate fields, and it's why this live check was worth doing before Phase 3 rather than after: Phase 3 (`betting_lines`) will lean on `games.game_id`/`home_team`/`away_team` being correct to join odds to games, which they are now, but silently weren't before.

`verify_cfbd_fields.py`'s own checklist has been updated to check the corrected field names (`ppa`, `homeTeam`, `classification`, `homePoints`, etc.) rather than the original guesses, so re-running `make verify-cfbd` in the future is checking "does the code's current assumption still hold," not "was the original guess right."

All 14 pytest tests still pass (fixtures updated to use `ppa` instead of `epa_per_play` in `test_backfill_historical_stats.py`). Committed as `5cc566e`.

## 10. `division` → `classification`, `homePoints`/`awayPoints`, and the historical backfill (2026-07-29, same session)

Two more real bugs surfaced by the same live-verification exercise, both in the shared `/games` endpoint, both fixed in every file that called it:

- **`"division": "fbs"` is silently ignored by CFBD.** Verified: it returned 304 games (FBS+FCS+D-II+D-III all mixed) for a week that has 52 real FBS games; `"classification": "fbs"` returns exactly 52, all confirmed `homeClassification`/`awayClassification` = `"fbs"`. Fixed in `fetch_stats.py::fetch_games()` and `update_results.py::fetch_game_results()`.
- **`homePoints`/`awayPoints`, not `home_points`/`away_points`.** Effect: `determine_ats_result()`'s null-check on the wrong key name meant **every pick has always settled as `"pending"` forever** — the grading system has never actually graded a single pick. Fixed in `update_results.py` (both the read and the `persist_results_to_db` write path). Verified by fetching real final scores for 2025 week 10 and confirming the `games` table update lands correctly (e.g. `Arkansas 35 – Mississippi State 38, completed=1`).

Also found: every production script (`fetch_stats.py`, `fetch_odds.py`, `update_results.py`, `backfill_historical_stats.py`) reads `CFBD_API_KEY`/`ODDS_API_KEY` from `os.environ` but never loaded `.env` itself — only `verify_cfbd_fields.py` did. Moved a small `.env` loader into `db.py` (every script already does `import db` before reading its own key), so local runs now pick up `.env` automatically; GitHub Actions is unaffected since it sets real env vars and `.env` won't exist there.

### Full 2019–2025 historical backfill — run for real

With field names confirmed, ran `python data/backfill_historical_stats.py --start-year 2019 --end-year 2025` for real (per the owner's Q1: independent of Phase 3, no reason to wait). First pass revealed a data-quality issue: the team universe was built as `sp_ratings ∪ epa_stats ∪ records`, but `records` covers ~668 schools across every division while `sp_ratings`/`epa_stats` are inherently FBS-only (~136 teams) — 74% of the resulting rows (2,877 of 3,912) were non-FBS noise with `sp_rating IS NULL`. Fixed by dropping `records` from the team-universe union (still used as a wins/losses lookup, just not to decide which teams get a row), wiped the bad rows, re-ran clean:

```
teams: 136 (upserted from /teams, classification=fbs)
team_game_stats (source=cfbd_historical_backfill): 931 rows across 2019-2025
  (2019: 131, 2020: 131, 2021: 131, 2022: 132, 2023: 134, 2024: 135, 2025: 137 -- FBS expansion over time)
  0 rows with NULL sp_rating
```

Spot-checked Georgia's row across all 7 seasons against known real history (15-0 in 2022, 14-1 in 2021, 12-2 in 2019, etc.) — all correct.

## 11. Odds path verification and the team-name join fix (2026-07-29, same session)

Owner asked whether `fetch_odds.py`'s field mapping had ever been checked against a live Odds API response, given it carries the same "fixtures share the code's own wrong assumption" risk just found on the CFBD side.

**First attempt blocked at the network level** — `api.the-odds-api.com` returned `ConnectionResetError [WinError 10054]` on every attempt (Python `requests` and raw `curl` alike), even with a garbage API key, while CFBD worked fine from the same machine minutes earlier. 8 retries across two rounds all failed identically. Resolved once the owner switched off their work WiFi — confirms it was a network-level block (firewall/proxy) on that connection, not a code or API problem.

**Field names: all correct, no fix needed.** Unlike CFBD, The Odds API's response matches `fetch_odds.py`'s assumptions exactly: `id`, `home_team`, `away_team`, `commence_time` at the top level; `bookmakers[].key`/`.markets`; `markets[].key` (confirmed `"spreads"`) /`.outcomes`; `outcomes[].name`/`.point`/`.price`. Quota check: 495/500 remaining.

**But the live response surfaced something bigger than a field name: the team-name join was completely broken.** The Odds API includes the mascot in team names (`"TCU Horned Frogs"`, `"NC State Wolfpack"`); CFBD uses bare school names (`"TCU"`, `"NC State"`). Every single one of 126 real games had this mismatch. The existing join (`fetch_odds.py`'s `find_game_id()`, matching on exact `home_team`/`away_team` strings) would have failed for essentially every game in production — this is the "fragile team-name join" flagged as finding #6 in the original Phase 0 audit, now confirmed to be a near-total failure rather than an edge case.

**Fix:** added `resolve_school_name()` to `fetch_odds.py` — longest-known-school-name-prefix match against the `teams` table (populated by the historical backfill above), applied before the `games` lookup and before storing `home_team`/`away_team` on `betting_lines` rows. Handles the general case (`"TCU Horned Frogs"` → `"TCU"`) and disambiguates real collisions correctly by preferring the longer match (`teams` has both `"Ohio"`/`"Ohio State"` and `"Miami"`/`"Miami (OH)"`).

Spot-checking the real 126-game response surfaced further genuine mismatches that pure prefix-matching can't solve:
- **Accent/apostrophe differences** (generic fix, no hardcoding needed): `"Hawai'i"` vs `"Hawaii"`, `"San José State"` vs `"San Jose State"`. Added `_normalize()` (strip accents via `unicodedata`, strip apostrophes) applied to both sides before comparing.
- **Genuinely different abbreviations** (no shared prefix at all, needs an explicit mapping): `"App State"` vs `"Appalachian State"`, `"Massachusetts"` vs `"UMass"`, `"Southern Miss"` vs `"Southern Mississippi"`. Added `KNOWN_TEAM_ALIASES`, checked before prefix-matching. This is an open-ended, long-tail problem — more pairs will likely surface over time as more of the season's games are seen; the dict is meant to grow, not be exhaustive today.

**Verified against the real 126-game response**: 208/252 team slots (82.5%) now resolve. Manually confirmed every one of the remaining 44 unresolved names is a genuine FCS/D-II opponent in a buy game (Abilene Christian, Alcorn State, Citadel, Furman, etc.) — none of them exist in the FBS-only `teams` table, so correctly falling through unresolved is the right behavior, not a bug. 11 new unit tests added in `tests/test_fetch_odds.py` covering the mascot-suffix case, the alias cases, the normalization cases, the Ohio/Miami disambiguation, and the fallback-when-unresolvable case. All 25 tests pass.

Committed as `89e2ef6`.

## 12. Phase 3 — historical lines backfill and the full 2019–2025 run (2026-07-29, same session)

### Live field check on `/lines` before writing the backfill

Checked `/lines` for 2025 week 10 before building anything. Key findings:
- Uses CFBD's own team naming throughout (`homeTeam: "Florida"`, `awayTeam: "Georgia"` — bare school names, matching `games` exactly), and the **same numeric `id`** as `/games` for the same game. Unlike The Odds API, there's no mascot-name mismatch here by construction. The team-name resolver is still applied before every insert per instruction — a no-op (exact match) in the normal case, a safety net if a provider-level name ever disagrees with CFBD's own.
- Spread convention: `spread` is the home team's line (positive = home underdog, negative = home favored) — confirmed via `homeTeam=Florida, awayTeam=Georgia, spread=7, formattedSpread="Georgia -7"`. Matches our existing `home_spread` column with no sign flip needed.
- Coverage confirmed back through 2019 (48/48, 61/61, 118/118 games had at least one provider across three spot-checked years).
- Providers vary by year (`numberfire`/`teamrankings`/`Caesars`/`Bovada` in 2019; `DraftKings`/`ESPN Bet`/`Bovada` in 2023) — CFBD also returns its own pre-computed `"consensus"` line directly, so the historical script stores that rather than re-deriving an average itself.

### The single fully-assembled example (per instruction, before the full run)

Ingested one week (2025 week 10) as a preview and assembled one real game end-to-end — **Georgia @ Florida** (`game_id=401752755`, final Georgia 24–Florida 20): Florida (3.5 SP+, 4-8) vs. Georgia (24.1 SP+, 12-2), opening lines Georgia -7.5 to -8 across books, closing -6.5 to -7. Coherent story: the ~20-point SP+ gap matches Georgia being favored by 7-8, the line moved slightly toward Florida over the week, and the 4-point final margin means Florida covered both the opening and closing numbers. Owner reviewed and confirmed this as sufficient proof before the full run.

This preview also caught a real bug: `/lines`' `classification` param is silently ignored too (same pattern as `/games`' `division` — see §10), returning FCS-vs-FCS games alongside FBS ones. Initial handling (checking whether `game_id` already existed in `games`) crashed with a `FOREIGN KEY constraint failed`, which led to a bigger discovery below.

### Bigger discovery: `games` was never populated for historical weeks

The FK-constraint crash revealed that `games` only had 52 rows total — the one week `fetch_stats.py` had ever been pointed at manually. Nothing had ever backfilled the **schedule/results** themselves for historical weeks; `backfill_historical_stats.py` is season-level only (no `game_id`), and `fetch_stats.py` only ever writes the current week. Fixed by having `backfill_historical_lines.py` upsert into `games` directly from `/lines`' own game-level fields (`id`, `season`, `week`, `seasonType`, `startDate`, `homeTeam`, `awayTeam`, `homeScore`, `awayScore` — note `homeScore`/`awayScore` here, a *third* naming variant for the same concept as `/games`' `homePoints`/`awayPoints`), filtering FBS-vs-FBS client-side since the query param doesn't work. The `ON CONFLICT` only touches `home_points`/`away_points`/`completed`, so a richer row already written by `fetch_stats.py` (venue, lat/long, neutral_site) is never clobbered — verified with a dedicated test.

### Full 2019–2025 × weeks 1–15 run

Ran for real, no `--force` (idempotency handled resuming from the two already-ingested weeks automatically, exactly as intended):

```
25,678 rows in betting_lines (18,413 closing + 7,265 opening)
4,952 rows in games (across all 7 seasons combined)
1,035 rows in team_game_stats (104 live + 931 historical backfill, from Phase 2)
0 rows with a NULL game_id in betting_lines
0 rows with both home_spread AND total NULL (no junk rows)
```

**Team-name resolver match rate: 100% exact match, 9,710/9,710 lookups, 0 fallback needed, 0 unresolved.** Confirms the live-check finding: CFBD's `/lines` genuinely uses consistent naming with `/games`, unlike The Odds API.

**Rate limits:** never triggered. All 105 weekly requests succeeded on the first attempt; the retry/backoff logic (429-aware, exponential, up to 5 attempts) built for this run never had to fire. `empty_weeks`/`failed_weeks` were both empty at the end of the run.

**Anomalies checked and confirmed as real, not bugs:**
- **2020 season is genuinely smaller**: 489 games vs. ~730-760 in every other year; week 1 has only 5 games vs. 45-48 in other years, ramping up gradually through week 8 (44 games) — this is the real COVID-shortened schedule (Big Ten, Pac-12, and others delayed or canceled early games), not a data gap.
- **Week 15 is sparse most years** (1-37 games depending on year) — correctly reflects that week 15 is conference championship week plus a handful of makeup games, not every year having a full slate.
- **Provider name drift across years** (`"DraftKings"` vs `"Draft Kings"`, `"Caesars"` vs `"Caesars (Pennsylvania)"` vs `"Caesars Sportsbook (Colorado)"`) — same real-world sportsbook, different label per year/state license in CFBD's own `provider` field. Not a bug, but worth knowing before doing any "track DraftKings' line over time" analysis — that will need a book-name normalization pass, not built here since it wasn't asked for and doesn't block anything today.

### Tests

8 new tests in `tests/test_backfill_historical_lines.py`: games-row creation from `/lines`' own fields, FCS-vs-FCS client-side filtering, opening/closing row correctness (including the case where a provider has no opening value at all), idempotent rerun (no duplication), the games-upsert non-clobbering behavior, already-ingested skip (no wasted API call), and the 429-retry/give-up-after-max-retries paths. All 33 tests across the whole suite pass.

### Foundation status

With this, `betting_lines` and `team_game_stats` are both archived and joining correctly across all 7 seasons (2019-2025), verified end-to-end with a real assembled example before the full run and comprehensive row-count/anomaly checks after. This closes out the "stats + lines archived and joining correctly" milestone. Feature engineering and the prediction model are the next conversation.

Committed as `6aa1fa9`.

## 13. Point-in-time weekly stats backfill (2026-07-30)

A separate design conversation produced `MODEL_DESIGN.md`, the full spec for the prediction-model phase. Its §1 flagged a critical, session-consistent-style finding: `team_game_stats` stores **season-final** SP+/EPA (one row per team per season) — confirmed via `SELECT COUNT(DISTINCT sp_rating) ... WHERE season=2023 AND team='Georgia'` returning 1. Using Georgia's final 2023 SP+ to predict their week 3 2023 game means the rating already reflects how the whole season turned out — fatal for an honest backtest. Full rationale, the walk-forward harness design, and the rest of the model architecture live in `MODEL_DESIGN.md`; this section covers only the data-layer fix.

### Live verification before building anything

Per the same discipline as every ingestion path this session: checked CFBD's actual behavior before writing code, assuming it was broken until proven otherwise.

**SP+ (`/ratings/sp`) — backfill-blocked, not deferred.** Direct A/B test, Georgia 2023: `week=3` → rating 31.2, `week=8` → 31.2, `week=13` → 31.2, no `week` param at all → 31.2. Identical in every field. Checked for alternates (`/ratings/sp/conferences` — conference-level; `/ratings/srs` — a different rating system, not SP+; `/rankings` — poll rankings, not SP+). **None serve historical point-in-time SP+.** CFBD only retains the season-final SP+ for a completed season — there is nothing to backfill, this isn't a "defer to later."

**EPA/success rate/havoc (`/stats/season/advanced` via `endWeek`) — genuinely point-in-time.** Same team/season, values actually move: `endWeek=3` → offense.ppa 0.311, `endWeek=8` → 0.377, `endWeek=13` → 0.395, full season → 0.400. Success rate and havoc from the same call move too. Confirmed `endWeek` alone (no `startWeek`) correctly defaults to season start (173 plays either way).

This split — SP+ blocked, everything else from `/stats/season/advanced` fine — is different from `MODEL_DESIGN.md` §3's original plan (SP++EPA now, success rate/havoc deferred to save a second verification pass). Since all three non-SP+ metrics come from one already-verified call, deferring them saved nothing; backfilled all three together. Owner reviewed the full comparison and made this call explicitly.

### Built: `data/backfill_point_in_time_stats.py`

One row per team per **week** (not per season) in `team_game_stats`, tagged `source='cfbd_point_in_time'`, `sp_rating` always `NULL`. Idempotent (skips an ingested week with zero API calls unless `--force`) — and `--force` was fixed during review to **replace** rather than duplicate a snapshot, since (unlike `betting_lines`' genuine append-only design) consumers need exactly one canonical row per team/week; caught this before it shipped as a gap. Same 429-aware retry/backoff as the lines backfill. 8 new tests (mirroring the established pattern): row insertion, idempotency, force-replace-not-duplicate, `sp_rating` always null, and the retry/give-up paths. 41 tests passed before the full run.

**Documented explicitly for whoever builds feature engineering next:** a row with `week=N` holds stats cumulative *through* week N's games. Predicting week N's games must join against `week=N-1` (or the latest available prior week), never `week=N` itself.

### Full 2019–2025 × weeks 1–15 run — verified

```
13,290 rows total (source=cfbd_point_in_time)
0 duplicate (season, week, team) combinations
0 rows with non-NULL sp_rating
```

Georgia 2023, weeks 3/6/9/12 — proof the numbers genuinely move, not frozen like SP+ was:

| week | offense_epa_play | defense_epa_play | off_success_rate | def_success_rate | havoc_rate |
|---|---|---|---|---|---|
| 3 | 0.311 | -0.016 | 0.520 | 0.299 | 0.204 |
| 6 | 0.378 | 0.024 | 0.538 | 0.347 | 0.183 |
| 9 | 0.383 | 0.056 | 0.521 | 0.344 | 0.186 |
| 12 | 0.403 | 0.098 | 0.516 | 0.361 | 0.172 |

2020's team-count ramp-up (14 teams at week 1 → 128 by week 15) reflects the same real COVID-delayed schedule already confirmed in §12 — not a new anomaly. No rate limits triggered; 0 empty/failed weeks.

### `MODEL_DESIGN.md` updated to match

§1 and §3 revised to state the actual finding (SP+ blocked, not deferred) rather than the original plan (SP++EPA now, success rate/havoc later). Added an explicit honesty caveat: **the 2019-2025 backtest has no SP+ feature at all; live predictions eventually will, once enough live-forward SP+ history accumulates.** These are not the same feature set and must never be silently compared as if they were — any future "does adding SP+ help" comparison needs its own controlled test. §2 and §6's baseline-definition references to "SP+/EPA only" were also flagged as outdated given SP+'s absence.

Also fixed while opening `MODEL_DESIGN.md`: the committed file had every line wrapped in a spurious leading `# ` (turning every paragraph into an H1 heading) plus backslash-escaped markdown (`\*\*`, `\#`, `\---`) and literal `&#x20;` space entities — an artifact from whatever export process produced it, not intentional formatting. Cleaned mechanically (stripped the artifact prefix, unescaped the backslash sequences, replaced the entities, collapsed doubled blank lines) and diffed the result against a manual read of the original to confirm no content changed, only its renderability.
