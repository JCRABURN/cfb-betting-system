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

## 14. Walk-forward backtest harness + EPA baseline (2026-07-30)

MODEL_DESIGN.md §4/§6, built in the order specified: the measuring instrument first, then the dumbest possible model run through it. Before writing either, confirmed the enforcement mechanism back to the owner (not just "the loop is in order" — three structural guarantees, agreed before any code):

1. **Sealed feature package, no DB handle for prediction code.** The harness builds `{home_stats, away_stats, opening_spread, opening_total}` before calling the prediction function; the function never sees a connection, a season, or a week. Whatever isn't in the package can't be used — this is what makes season-wide-aggregate leaks structurally impossible, not just discouraged.
2. **One accessor per concern.** `get_team_stats_as_of(team, season, week)` returns the most recent `cfbd_point_in_time` row strictly before `week`, or `None` — the only sanctioned read path into `team_game_stats` for features. `get_opening_line`/`get_closing_line` are separate functions so opening (model input) and closing (CLV only) can never be swapped by accident.
3. **Point-in-time discipline applies to training data too.** The v1 "retrain once per season" rule fits one slope+intercept per season Y using only games from seasons < Y — and each of *those* training games gets its own as-of-that-game's-week feature via the same accessor, not that game's season-final numbers. One code path serves both training and live prediction, so they can't drift into different (and differently leaky) implementations.

### Adversarial tests, run before the baseline (per instruction)

`tests/test_backtest_harness.py` — 30 tests, each trying to break a guarantee rather than just confirming the happy path:
- Plants a wildly-wrong stats row *at* the target week and confirms the accessor refuses it (only returns week N-1 or earlier).
- Confirms a season-final (`cfbd_historical_backfill`) row never satisfies the point-in-time accessor even as the only row present.
- Plants a "leak" game in the season about to be predicted with an absurd feature value, builds the training set for that season, and asserts the leak value never appears among the training data (`999.0 not in xs`).
- An end-to-end version of the same leak, run through the full `run_walk_forward` loop, not just the unit-level accessor.
- Confirms `get_opening_line` never falls back to a closing line when no opener exists (returns `None`, caller must skip).
- `fit_linear`, `grade_ats`, `unit_pl`, `calculate_clv` checked against known values and edge cases (push, insufficient training points, zero-variance feature).

All 30 passed before the baseline was run, per instruction, plus 4 more for `baseline_epa.py`'s feature logic (sign-correctness of the EPA differential, confirmed a worse defense lowers net team strength rather than raising it). 75 tests total across the whole suite.

### Two real bugs the adversarial tests didn't catch — real data did

Both were caught only once the harness ran against the actual archive, not the synthetic test fixtures — the same lesson as every other phase this session: a test can only be as honest as its assumptions.

1. **Training-set construction incorrectly required an opening line.** `build_feature_package` (designed for the predict step, which does need a line) was reused for training-set construction too, which only needs `(feature, actual_margin)` pairs — no line data at all. This made 2019 (0 opening lines in the archive at all — see next bug) show 0 training games, crashing `fit_linear`. Fixed by splitting out `get_pregame_stats()` (stats only) for training, keeping `build_feature_package()` (stats + line) for prediction. Added `test_training_set_does_not_require_an_opening_line`.

2. **CFBD's historical archive has no consensus opening line, ever, and consensus closing collapses after 2022.** Confirmed live: `SELECT COUNT(*) WHERE line_type='opening' AND book='consensus'` → 0 rows, every season. Individual books do have openers, but only from 2021 on (Bovada first, DraftKings/ESPN Bet added in later years); 2019/2020 have zero opening lines from any book at all. Separately, consensus *closing* coverage collapses too: 2019-2022 fine (700+ rows/season), 2023 drops to 29, 2024/2025 are 0 — while Bovada's closing coverage stays complete throughout. Fixed both accessors to prefer consensus, then fall back to a single book, flagged via a `book` field on the returned line (never silently relabeling a book's number as "consensus") — exactly the "fall back and FLAG it, never silently substitute" instruction already written into MODEL_DESIGN.md §4 before this was found. `get_closing_line` additionally takes the *same* book the opening came from, so CLV compares like-for-like rather than mixing books. 6 new tests cover both fallback chains and the "nothing at all exists" case.

### Full backtest run — usable history is 2021-2025, not 2020-2025

```
Total games considered: 4217
Graded (had both pregame stats and an opening line): 3479
Skipped: 738 (316 missing_pregame_stats, 422 missing_opening_line)
Bet-subset (edge >= 3.0 pts, a placeholder threshold, not yet calibrated): 2427
```

**Known fact, not a bug: 2020 contributes zero gradeable games.** Not partially degraded — literally 0 of 489 games could be predicted, because 2020 has no opening-line data in the archive at all (415 skipped for that reason, the remaining 74 for missing pregame stats, itself partly a COVID-schedule artifact of teams not having played yet). This is a distinct, more specific finding than "2020 is COVID-shortened" — it's a flat data-coverage hole, confirmed via direct query, separate from the schedule oddities COVID caused. **The usable backtest window is 2021-2025 (five seasons), not 2020-2025** — corrected here after initially reporting the wider range.

**Known fact, not a bug: opening-line coverage is thin and single-book-patched, so CLV should be read as noisier/less complete than the win-rate numbers.** CFBD's archive has no true consensus opener at all (0 rows, every season, confirmed live) and consensus closers collapse after 2022 (29 rows in 2023, 0 in 2024/2025). Every CLV figure below rests on a single-book proxy (`get_opening_line`/`get_closing_line`'s fallback), which is real, useful signal but a thinner foundation than the win-rate/ROI numbers, which don't depend on line data beyond the one opening spread used to grade ATS. Treat CLV as directional, not as tight as the ATS%/ROI columns next to it.

| | All predictions (calibration, §5) | Bet-subset only (edge ≥ 3, §5) |
|---|---|---|
| 2020 | n=0 (see above — no opening-line coverage, not scored) | n=0 |
| 2021 | 682, ATS 48.7%, ROI -7.0%, CLV +0.10 | 494, ATS 49.2%, ROI -6.0%, CLV +0.07 |
| 2022 | 674, ATS 53.0%, ROI +1.2%, CLV -0.04 | 478, ATS 52.6%, ROI +0.3%, CLV +0.01 |
| 2023 | 698, ATS 50.7%, ROI -3.2%, CLV +0.36 | 492, ATS 50.5%, ROI -3.5%, CLV +0.52 |
| 2024 | 712, ATS 52.1%, ROI -0.6%, CLV +0.09 | 480, ATS 55.2%, ROI +5.3%, CLV +0.13 |
| 2025 | 713, ATS 50.9%, ROI -2.7%, CLV +0.11 | 483, ATS 49.4%, ROI -5.7%, CLV +0.12 |
| **2021-2025 combined** | **3479, ATS 51.1%, ROI -2.5%, CLV +0.13** | **2427, ATS 51.3%, ROI -2.0%, CLV +0.17** |

**This is the correct, expected result per §6, not a failure.** ATS sits in a 48.7%-55.2% band around 50% across 5 seasons × 2 subsets (10 cells) — exactly the spread of noise expected from a single-feature model with no real edge against an efficient market. Combined ROI is *negative* (-2.0% to -2.5%), consistent with a ~50% true hit rate losing to standard -110 juice, not a profitable result that would need explaining away. Checked per instruction ("a profitable baseline would be suspicious") — nothing here is profitable enough, or consistently enough, to suspect a lookahead leak.

**The edge≥3 filter is not demonstrably adding signal yet — don't read the 2024 cell as proof it works.** 2024's bet-subset (55.2% ATS, +5.3% ROI) is the one green cell in the table, but the same filter runs *cold* in 2021 (49.2%, -6.0%), 2023 (50.5%, -3.5%), and 2025 (49.4%, -5.7%), and roughly flat in 2022 (52.6% vs. 53.0% all-predictions) — four of the five seasons with data show no improvement, one shows a lot. A filter that helped would show up consistently across seasons, not in one out of five. The honest read is the aggregate (~51% ATS both ways, bet-subset barely different from all-predictions), consistent with no real edge from this threshold on this baseline yet. `EDGE_THRESHOLD = 3.0` remains an unvalidated placeholder (§8b already says as much for staking; this extends that caution to the threshold itself).

### New files

`models/backtest_harness.py` (the harness), `models/baseline_epa.py` (the one-feature model), `models/run_backtest.py` (CLI runner + season/subset reporting), `tests/test_backtest_harness.py` (30 tests), `tests/test_baseline_epa.py` (4 tests).

Committed as `3a39812`.

## 15. First one-at-a-time feature test: success rate — REJECTED (2026-07-30)

Per MODEL_DESIGN.md's "features, one at a time, each measured against the baseline" plan (§2 step 4), tested point-in-time success rate (already sitting in `team_game_stats` from the Phase 1 point-in-time backfill, unused until now) as a second feature alongside EPA. Methodology confirmed with the owner *before* writing any code:

1. **McNemar's test on disagreement games** (games where the two models pick opposite sides of the same opening line) — significant at p<0.05, required.
2. **Directional improvement in ≥4 of 5 graded seasons** (2021-2025) — a feature that helps in one lucky season is noise, not signal (same lesson as the edge≥3 threshold finding from the baseline run).
3. **Coefficient sign stability** across every season's walk-forward fit.

All three required, decided before results were seen, so a partial pass couldn't be used to argue the bar down after the fact.

### Harness generalized from one feature to N, verified as a no-op first

Extended `backtest_harness.py` from single-variable OLS (`fit_linear`) to a general `fit_multilinear` (closed-form normal equations, pure Python — a 2-4 feature regression doesn't need numpy). `feature_fn` now returns a tuple (a 1-tuple for the existing EPA-only baseline) rather than a bare scalar, and `run_walk_forward` now also returns `season_fits` (`{season: (intercept, coefs)}`) so coefficient stability can be checked directly. Also added `mcnemar_test(challenger_right, baseline_right)` as a general, reusable utility (stdlib `math.erf` for the chi-square(1 df) p-value — no scipy needed for a single degree of freedom), since every future feature test needs it, not just this one.

**Before trusting any comparison against the 51.1% floor, re-ran the EPA baseline through the refactored (now-multivariate) harness and confirmed it reproduces the exact committed numbers** (51.1% ATS / -2.5% ROI all-predictions, 51.3%/-2.0% bet-subset, identical to the last commit, digit for digit) — the refactor is a verified no-op for the single-feature case, not just an assumed one. Also factored `run_backtest.py`'s reporting (`aggregate`/`fmt_row`/season-table printing) into a new shared `models/backtest_report.py`, since every future feature test needs the same tables.

### The test

`models/feature_success_rate.py`: EPA differential (unchanged) + success-rate differential (`offense_success_rate - defense_success_rate`, home minus away — same subtraction convention as EPA, since `defense_success_rate` is the success rate *opposing offenses* achieve against that defense, confirmed directionally sane against Georgia's known-elite 2023 defense sitting well below the ~42-45% national average). `models/run_feature_test.py` (generic, reusable for every future feature) runs baseline and challenger through the identical harness, confirms both graded the *exact same* 3,479 games (a sanity check that would have caught any NULL-coverage mismatch between EPA and success rate before trusting anything downstream), then applies the three criteria.

### Result: FAIL on 2 of 3 — do not keep

```
McNemar 2x2 table (301 disagreement games, decided both ways):
  Challenger right (baseline wrong): 153
  Baseline right (challenger wrong): 148
  chi2 = 0.053, p = 0.8177                              -> FAIL (need p<0.05)

Per-season ATS, baseline -> challenger:
  2021: 48.7% -> 49.6%  (+0.9pp)  improved
  2022: 53.0% -> 51.2%  (-1.8pp)  regressed
  2023: 50.7% -> 50.2%  (-0.4pp)  regressed
  2024: 52.1% -> 54.1%  (+2.0pp)  improved
  2025: 50.9% -> 50.9%  ( 0.0pp)  no change
  Improved in 2/5 seasons                               -> FAIL (need >=4/5)

Coefficient sign (success-rate term), all 6 season fits: always positive
  (29.0 to 35.9 range)                                  -> PASS
```

**153 vs. 148 on the disagreement games is as close to a coin flip as this sample size gets** — the McNemar p-value of 0.82 says so plainly. Combined ATS moved from 51.1% to 51.2% (essentially unchanged), and that flat aggregate was hiding two seasons up, two down, one flat — exactly what the season-by-season criterion exists to catch, since the aggregate alone would have looked like a shrug rather than a clear miss.

**Plausible reason, not proven, flagged as a hypothesis:** success rate and EPA are both derived from largely the same underlying play-by-play efficiency data and are likely highly correlated at the team level. Added on top of a model that already has EPA, its independent marginal information may simply be small — consistent with (though not proof of) what both criterion 1 and criterion 2 show.

**Verdict: DO NOT KEEP, per the pre-registered bar.** Not rescued by relaxing the threshold or cherry-picking the two improved seasons — the bar was set before results were seen and stays set. Havoc is next, tested independently against the same EPA-only baseline (not stacked on top of the rejected success-rate feature).

### A real performance bug found and fixed along the way

The first run took much longer than expected (single-model baseline runs had taken roughly a minute; this two-model run took over 8 minutes, not the ~2x estimate). Confirmed via CPU-time sampling (growing slowly — 32s to 44s CPU time across several minutes of wall clock) that it was making genuine but very slow progress, not hung. Root cause: `team_game_stats` had no index on `(source, team, season, week)`, so every single `get_team_stats_as_of()` call — tens of thousands of them across a full walk-forward run, and this is called for every training row and every prediction, for every model tested — was a full table scan.

Added `idx_team_game_stats_lookup` (on `source, team, season, week`), plus `idx_betting_lines_lookup` and `idx_games_season_week` for the same reason (both queried in the same hot loop). `CREATE INDEX IF NOT EXISTS` applies cleanly to the already-populated committed database, no migration dance needed. Re-ran the exact same feature test after adding the indexes: **2.4 seconds, down from 8+ minutes — a ~200x speedup** — and confirmed the results are byte-for-byte identical to the pre-index run (see the McNemar table above), so this was purely a performance fix, not a behavior change. This matters beyond just today's run: havoc and every feature tested after it hits the same hot path, so this was worth fixing now rather than paying it on every future comparison.

### New files

`models/backtest_report.py` (shared reporting, extracted from `run_backtest.py`), `models/feature_success_rate.py`, `models/run_feature_test.py` (generic, reusable), `models/run_feature_test_success_rate.py` (entry point), `tests/test_feature_success_rate.py` (4 tests), plus updates to `backtest_harness.py`/`baseline_epa.py`/their tests for the multivariate generalization, and three new indexes in `db.py`. 85 tests pass across the whole suite.

Committed as `59e3bbb`.

## 16. Second one-at-a-time feature test: havoc rate — REJECTED (2026-07-30)

Same three pre-registered criteria as §15, same bar, tested independently against the EPA-only baseline (not stacked on the rejected success-rate feature). Before running: the owner noted havoc measures defensive disruption specifically (TFLs, forced fumbles, PBUs) rather than EPA's general efficiency — a more plausible a-priori mechanism for independent signal than success rate had, explicitly not a prediction and not a change to the bar.

### A real gap the previous feature test never hit

`havoc_rate` is the only stat CFBD exposes as a single (defense-only) value per team, with no offense/defense split the way EPA and success rate have. Building `feature_havoc.py`'s `home_havoc_rate - away_havoc_rate` crashed on the first run: `TypeError: unsupported operand type(s) for -: 'float' and 'NoneType'`. Checked the scope before deciding on a fix: **17 of 13,290 point-in-time rows (0.13%) have `havoc_rate IS NULL` while `offense_epa_play` is populated** — mostly 2020 (13 rows, presumably too few defensive snaps recorded yet in the COVID-disrupted early schedule), 2 each in 2023/2024. Success rate never hit this because its own NULL count (checked at the time) was exactly zero — pure luck of which field was tested first, not evidence the gap didn't exist.

**Fix, not a workaround:** extended the harness's contract rather than special-casing havoc. `feature_fn`/`predict_fn` may now return `None` to signal "cannot compute for this game" (in addition to always being allowed to return a tuple/margin) — `build_training_set` silently excludes such training rows (same treatment as a game with no pregame stats at all), and `run_walk_forward`'s prediction loop records a skip with `skipped_reason='missing_feature_data'` rather than crashing or substituting a default. Added tests for both paths in `test_backtest_harness.py`, plus `feature_havoc.py`'s own None-returning behavior in `test_feature_havoc.py`.

This in turn surfaced a real gap in `run_feature_test.py`'s own sanity check: it originally *required* baseline and challenger to grade the byte-identical game set, which is correct in spirit but too strict in practice — havoc's 3 affected games (out of 3,479) tripped a hard `AssertionError` even though the discrepancy is small, understood, and doesn't threaten the comparison's validity. Changed the check to compute the game-ID intersection, print an explicit note when it's non-empty (count and which side), and proceed comparing both models on the intersection — but still raise if the mismatch exceeds 1% of games, preserving the original protection against a genuine bug silently comparing two different populations. Re-verified the success-rate test still reports the exact same numbers as before this change (0 mismatch there, so the intersection equals the full 3,479-game set) before trusting the havoc run.

### Result

```
McNemar 2x2 table (119 disagreement games, decided both ways):
  Challenger right (baseline wrong): 60
  Baseline right (challenger wrong): 59
  chi2 = 0.000, p = 1.0000                              -> FAIL (need p<0.05)

Per-season ATS, baseline -> challenger (3,476 games, 3 excluded per above):
  2021: 48.7% -> 49.1%  (+0.4pp)  improved
  2022: 53.0% -> 52.7%  (-0.3pp)  regressed
  2023: 50.7% -> 50.8%  (+0.1pp)  improved
  2024: 52.0% -> 52.0%  ( 0.0pp)  no change
  2025: 50.9% -> 50.8%  (-0.1pp)  regressed
  Improved in 2/5 seasons                               -> FAIL (need >=4/5)

Coefficient sign (havoc term), all 6 season fits: always positive
  (16.7 to 24.9 range)                                  -> PASS
```

**60 vs. 59 is as close to an exact coin flip as a paired test gets** — even more decisive a non-result than success rate's 153-vs-148 (p=0.82). Combined ATS barely moved (51.1% either way, rounding), and again the flat aggregate masked a real split: two seasons up marginally, two down marginally, one flat.

**Verdict: DO NOT KEEP, per the same pre-registered bar.** The owner's a-priori case — havoc measuring a mechanism distinct from EPA's general efficiency — did not pan out. Noted explicitly at the time as "no prediction either way, the data will decide," and it decided against it. Two independent candidate features derived from the same CFBD advanced-stats call have now both failed to clear the bar; EPA alone remains the only feature that has survived testing.

### New files

`models/feature_havoc.py`, `models/run_feature_test_havoc.py` (entry point), `tests/test_feature_havoc.py` (7 tests), plus the `None`-contract extension to `backtest_harness.py` (2 new tests) and the intersection-based sanity check in `run_feature_test.py`. 94 tests pass across the whole suite.

Committed as `2616aea`.

## 17. Third one-at-a-time feature test: rest/schedule — REJECTED (2026-07-31)

Owner's framing going in: success rate and havoc both failed because they're on-field performance summaries overlapping what EPA already captures. Rest/schedule is structurally different — EPA cannot see a short week or a bye, since that's not in any performance stat, only in the schedule itself. Same three pre-registered criteria, same bar, tested independently against the EPA-only baseline.

### Verified date reliability before building anything (per instruction)

`games.start_date`: 0 NULLs across all 4,952 games, every season 2019-2025, clean ISO-8601 UTC format (`2024-11-02T19:30:00.000Z`), confirmed parseable with `datetime.fromisoformat` after swapping the trailing `Z` for an explicit offset. Also computed the real rest-day distribution across 8,981 team-game observations before picking a bye threshold, rather than guessing: a clean bimodal split, 6-8 days (normal week, ~7,000 observations) vs. 13-14 days (bye, ~1,418), with only a thin 9-12 day zone between (357). `BYE_THRESHOLD_DAYS=10` cleanly separates the two humps.

### Two situational features, one test

`models/feature_rest.py`: days-of-rest differential (home minus away) and a bye-week-flag differential, both bundled as one "rest/schedule" test per MODEL_DESIGN.md's "Later features" framing (one theme, not two separate one-at-a-time tests). Both computed via a new harness accessor, `get_days_rest(conn, team, season, game_date)` — the most recent prior completed game for that team, strictly before `game_date`, scoped to the same season only (reaching into the previous season's bowl game would call the offseason "rest," not a meaningful in-season comparison). Threaded `start_date` through `list_games`/`get_pregame_stats`/`build_feature_package` to support this, extending (not replacing) the existing point-in-time accessor pattern.

### A structural data gap found and fixed, not worked around

First run: 161 games (4.63%) had no computable rest for one team — well above the 1% threshold that auto-proceeds on the intersection (see §16), so it correctly hard-stopped rather than silently comparing a smaller, differently-composed sample. Investigated the actual case (Georgia vs. UAB, 2021 week 2): **UAB's actual week-1 2021 game (vs. Jacksonville State, an FCS opponent) simply isn't in our `games` table at all**, because `games` is intentionally FBS-only (a Phase 3 design decision, not a bug). This meant `get_days_rest` correctly found no prior FBS game — but the *real* prior game exists, just not in our archive.

This is a fundamentally different kind of gap than havoc's coincidental 0.13% NULL rate: it's systematic (concentrated in weeks 2-3, when FCS buy games cluster) and, as the owner flagged before authorizing any fix, **non-random with respect to what the feature measures** — excluding these games would have biased the test toward "does rest help when it's cleanly computable" rather than the real question. Per instruction, time-boxed a check before deciding how to handle it: confirmed live that CFBD's `/games` endpoint returns FBS-vs-FCS games (with dates) when not filtered by classification — one call, immediate confirmation.

Identified the exact scope before fetching anything: 204 unique `(team, season, week)` gaps, concentrated in just 7 distinct `(season, week)` combinations. Fetched those 7 weeks unfiltered by classification (each returning the full multi-division slate) and resolved 201 of 204 immediately; the remaining 3 (e.g. Purdue 2024 week 3) needed one week further back, since those teams had a **second**, genuine bye immediately after their FCS game — the data for those was already in the same 7 fetches, just needed a "most recent match across all fetched weeks" lookup instead of an exact "week - 1" assumption. All 204 resolved with zero additional API calls.

Built this as a proper, reusable, idempotent script rather than a one-off patch: `data/backfill_rest_dates.py` finds gaps generically (any `(team, season, week)` where both teams have EPA available but rest doesn't compute — not hardcoded to the 204 already found, so it'll pick up new gaps automatically for future seasons), searches back up to 4 weeks per gap with a per-`(season, week)` fetch cache to bound API calls, and stores results in a new `supplemental_game_dates` table — **dates only, not full game rows** (no score, no FK to `games`, no opponent-as-tracked-entity), keeping `games`' FBS-only scope untouched. `get_days_rest` now checks both tables and takes whichever date is more recent. Logged to `ingestion_runs` like every other ingestion script.

Ran it for real: 204/204 gaps resolved, 0 unresolved, 0 API calls beyond the 7 needed. Re-ran the full test suite (110 tests) and the feature test with full coverage confirmed: **3,479 games compared, no exclusion note** — the complete, unbiased sample the owner asked for.

### Result

```
McNemar 2x2 table (87 disagreement games, decided both ways):
  Challenger right (baseline wrong): 50
  Baseline right (challenger wrong): 37
  chi2 = 1.655, p = 0.1983                              -> FAIL (need p<0.05)

Per-season ATS, baseline -> challenger:
  2021: 48.7% -> 49.3%  (+0.6pp)  improved
  2022: 53.0% -> 52.9%  (-0.2pp)  no improvement
  2023: 50.7% -> 51.1%  (+0.4pp)  improved
  2024: 52.1% -> 53.1%  (+1.0pp)  improved
  2025: 50.9% -> 50.9%  ( 0.0pp)  no change
  Improved in 3/5 seasons                               -> FAIL (need >=4/5)

Coefficient signs (rest_diff, bye_diff), all 6 season fits:
  2020: (-0.80, +4.93)   2021: (-0.09, +1.21)   2022: (-0.01, -0.53)
  2023: (+0.02, -0.80)   2024: (-0.03, -0.44)   2025: (-0.07, -0.27)
  Observed sign patterns: {(-1,+1), (+1,-1), (-1,-1)}    -> FAIL (flips)
```

**This is the first feature to fail all three criteria, not two.** Notably, it's also the *closest* on two of them — p=0.20 (vs. 0.82 and 1.00 for success rate/havoc) and 3/5 seasons improved (vs. 2/5 for both prior features) — but "closest to the bar" is not "clears the bar," and the pre-registered bar doesn't move for a plausible-sounding feature that came closer. The coefficient-sign instability is the clearest tell: both `rest_diff` and `bye_diff` flip sign repeatedly across the six season fits, meaning whatever small effect the model finds isn't a consistent, learnable relationship — it's noise the fit is chasing differently each time it's re-estimated on a different training window.

**Verdict: DO NOT KEEP, per the same pre-registered bar as the other two.** Three independent feature candidates now tested, three rejected. EPA alone remains the only feature that has survived testing.

### New files

`models/feature_rest.py`, `models/run_feature_test_rest.py` (entry point), `data/backfill_rest_dates.py` (reusable, idempotent supplemental backfill), `tests/test_feature_rest.py` (10 tests), plus `get_days_rest()` + the `supplemental_game_dates` table + 6 new tests in `test_backtest_harness.py`, and the `start_date` threading through `list_games`/`get_pregame_stats`/`build_feature_package`. 110 tests pass across the whole suite.

## 18. Live in-season path audit + fixes (2026-07-31)

EPA-only is the model (three feature candidates tested and rejected, §15-17). Before building the weekly card generator, audited `data/fetch_stats.py`/`data/fetch_odds.py`'s in-season fetch path — it has only ever run in offseason fallback mode, never against a real in-season API response. Same discipline as every other path this session: don't trust an assumed shape, verify or harden before building on top of it.

### Three real bugs found by code inspection, fixed without needing live season data

1. **`get_current_week()` silently defaulted to week 1 on ANY calendar API failure**, including a mid-season one. Week 1 always has real, completed games once the season starts, so `main()`'s `if not games` guard doesn't catch this case — a mid-season `/calendar` blip would silently persist stale week-1 data as if it were the current week, with only a console print nothing is watching in a scheduled Action. Fixed: removed the try/except around the calendar call entirely, so the exception now propagates through `db.log_run()`, which logs it to `ingestion_runs` with `status='error'` and re-raises, failing the GitHub Actions step visibly. The genuinely-benign case ("no active week matches `now`" — true offseason or between seasons) is unaffected and still defaults to week 1 softly, since that's a different, legitimate branch, not an API failure. Verified live: pointed `CFBD_BASE` at a bad path and confirmed `get_current_week()` raises, and that `fetch_stats.main()` now crashes (instead of completing silently) with the failure logged to `ingestion_runs`.
2. **`fetch_odds.py` picked the wrong week's stats file under multiple candidates.** `sorted(glob.glob("data/stats/week_*.json"))[-1]` sorts filenames lexicographically, and they aren't zero-padded, so `week_10_2026.json` sorts *before* `week_2_2026.json`. Harmless in CI today (fresh checkout → exactly one file per run) but exactly the failure mode manual Week 0/1 testing would hit if `data/stats/` isn't cleared between local runs. Fixed: load every candidate file's own `year`/`week` fields and pick by `max(metas, key=lambda m: (m["year"], m["week"]))` instead of sorting paths.
3. **Opening-line tracking used a file that can't survive a re-run.** `is_opening_pull` was `not os.path.exists("data/spreads/opening_week_{week}_{year}.json")` — but `data/spreads/` is gitignored and GitHub Actions checks out fresh every run, so this was *always* True. Harmless only because the workflow currently runs exactly once per week; a manual `workflow_dispatch` re-run mid-week would have inserted a second, later line into `betting_lines` mislabeled `line_type='opening'`, corrupting the CLV baseline for every game that week. Fixed: added `opening_line_recorded(conn, week, year)`, which checks `betting_lines` (the persistent store) directly for an existing `line_type='opening'` row that season/week, and drives `is_opening_pull` off that instead of the ephemeral file.

### Documented, not fixed — genuinely needs live in-season data

- **`get_current_week()`'s successful-match branch has never executed.** Every run to date has hit either the hardcoded-test-value path or the exception fallback (now removed per fix #1). The loop that matches `firstGameStart <= now <= lastGameStart` against a real live calendar, at a real moment in time, has zero execution history. Field names are confirmed correct (verified live against 2025's `/calendar`, and week windows are contiguous with no gap), but the naive-datetime-vs-`Z`-suffixed-string comparison itself has only ever been checked against one static sample, not exercised at a real day/week boundary. **Action: the first real run of `fetch_stats.py` once Week 0/1 is live should have its `get_current_week()` return value manually checked against the actual calendar**, not just trusted.
- **Bookmaker coverage per game is unconfirmed.** `BOOKMAKERS = "draftkings,fanduel,betmgm,caesars"` is confirmed to return data successfully (~126 games, verified earlier this session), but it's never been confirmed that all four books actually appear *per game* in a live in-season response, versus some being silently absent for certain games. **Action: spot-check a real in-season Odds API response and confirm book coverage, not just that the request succeeds.**

### Tests

110 tests pass (no new tests added — these are hardening fixes to orchestration code that was never exercised as written, not new behavior with its own test surface; the existing `tests/test_fetch_odds.py` suite already covers the functions touched).

## 19. Card generator, and the edge-bucket check that disproved its first confidence design (2026-08-01)

Built `models/card_generator.py`: the shared weekly-card output (every lined game, model-predicted side, edge, confidence) that both eventual contest consumers (SplashSports, pick'em pool) will read from. Reuses the validated EPA-only walk-forward model exactly (`backtest_harness.py` + `baseline_epa.py`) — no new fitting logic. Two departures from the backtest harness, since a card and a backtest answer different questions: games aren't filtered to `completed=1` (a card covers games not yet played), and edge is computed against the latest available line (`current`, falling back to `closing` for the historical archive's vocabulary) rather than the opening line the backtest uses for grading integrity. Verified against 2024 week 10 (48 games, 0 skipped) — this validates card *logic* against known data, not current-season trustworthiness (still gated on §18's Week 0/1 items).

**The first version ranked confidence by raw edge size** (quintiles of the week's own edge distribution, biggest edge = confidence 5). That encodes an assumption — bigger model/market disagreement = stronger pick — that had never actually been tested.

### The check: bucket the bet-subset by edge size, 2021-2025

Reused `run_walk_forward` (same EPA-only fit, same 2021-2025 graded seasons) to bucket the existing `edge >= 3.0` bet-subset (2,427 picks) into 3-6 / 6-10 / 10+ point buckets and compare ATS%:

```
edge 3-6    n=868   W-L-P 437-412-19   ATS 51.5%   ROI -1.7%
edge 6-10   n=723   W-L-P 368-340-15   ATS 52.0%   ROI -0.8%
edge 10+    n=836   W-L-P 419-408-9    ATS 50.7%   ROI -3.2%
```

Aggregate across all three (1224-1160 decided, 51.3%) matches the previously-reported bet-subset number exactly — same data, no new bug. Per-season breakdown, checking for a hidden hot/cold cell (the same trap the `edge>=3` threshold itself fell into originally):

| Season | 3-6 | 6-10 | 10+ |
|---|---|---|---|
| 2021 | 50.6% | 50.7% | 46.6% |
| 2022 | 55.3% | 53.3% | 49.4% |
| 2023 | 46.6% | 52.8% | 52.2% |
| 2024 | 52.5% | 60.7% | 53.3% |
| 2025 | 52.2% | 41.5% | 52.4% |

**Result: no bucket reliably beats the ~52.4% breakeven line, and edge magnitude does not predict hit rate.** The middle bucket (6-10) has the best aggregate number, but it's mostly one hot season (2024, 60.7%) offset by one cold one (2025, 41.5%) — high variance, not a stable edge. The 10+ bucket — the biggest, most eye-catching edges, like the Massachusetts +22 pick that led off the first sample card — is the *worst* of the three in aggregate and sits below 50% in 2 of 5 seasons. Ranking confidence by raw edge was therefore backwards: it surfaced the model's least reliable picks as its most confident ones.

### The fix: no graduated ranking; one negative flag, not five positive tiers

Capping or normalizing edge to rescue a 1-5 ranking was considered and rejected — that would be engineering around a model that hasn't demonstrated an edge signal, not fixing a bug. Since neither remaining distinction (3-6 vs 6-10, 51.5% vs 52.0%) is big enough to trust over season-to-season noise, there's no basis for a positive graduated scale at all. The only thing the data supports is the one negative signal: edge >= 10 is the demonstrably worst bucket. `_assign_confidence()` now assigns exactly two states — `"standard"`, or `"low_confidence_large_edge"` for edge >= 10 (the exact bucket boundary already tested, not a new number) — and does **not** reorder games by edge, since edge size is not a quality ranking. `build_card()`'s `top5` key is gone entirely (it was the same "biggest edge = best" framing in a different field); a `flagged_large_edge` list surfaces which games hit the low-confidence threshold, purely as a transparency flag, not a "top picks" list. Re-verified against 2024 week 10: the Massachusetts @ Mississippi State game (edge 22.0) that led the old top-5 is now correctly flagged `low_confidence_large_edge`, alongside 4 other games; 43 of 48 games are `"standard"`.

### Bottom line on the model itself

Every honest slice of this backtest — all-predictions (51.1%), the edge>=3 bet-subset (51.3%), and now every edge bucket within that subset (51.5% / 52.0% / 50.7%) — sits at or below the 52.4% breakeven line, with no slice showing a stable, season-consistent edge. Three independent candidate features (success rate, havoc rate, rest/schedule — §15-17) were tested against a pre-registered bar specifically to find something EPA doesn't already know, and all three were rejected. **EPA-only has not demonstrated a betting edge over the closing line, and the feature search that came up empty three times running was not optional groundwork — it was the actual test of whether this model has anything real to contribute.** The infrastructure (data pipeline, walk-forward harness, live-path hardening, card generator) is solid and reusable, but the model itself is not validated for use in either contest as of this entry. Running it now would mean staking real contest entries on a model indistinguishable from a coin flip against the market, which is exactly the outcome the backtest discipline in this project exists to catch before it happens, not after.

### Tests

10 new/updated tests in `tests/test_card_generator.py` for the confidence redesign (exact-threshold boundary, flagged-list contents, no reordering). 120 tests pass across the whole suite.

## 20. Three structural fixes for the 10+ edge bucket — all rejected (2026-08-01)

§19 found the demonstrated weakness: the 10+ edge bucket is the worst-performing of the three, not the best, consistent with the linear EPA model extrapolating too aggressively in mismatch games. Rather than another new feature, tested three structural changes to how the EXISTING EPA information is used — one at a time, against a pre-registered bar, locked before any variant ran or its results were seen.

### The bar (locked before running)

Three criteria, ALL required — deliberately different from `run_feature_test.py`'s bar (aggregate ATS improvement + coefficient sign stability), because the question here isn't "does this add signal," it's "does this fix the specific broken bucket":

1. **McNemar's test on disagreement games vs. the EPA-only baseline, p < 0.05.** Same sharp instrument as every feature test — only the games a variant actually changes the pick on are informative.
2. **The variant's own 10+ bucket must not be the worst of its own three buckets** (ATS% >= both the 3-6 and 6-10 buckets, aggregate 2021-2025). Recomputed using the variant's *own* edge values, since edge depends on predicted_margin, which every variant here changes — a game can shift buckets between models.
3. **The variant's 10+ bucket ATS% must beat the baseline's 10+ bucket ATS% in at least 4 of 5 graded seasons** — a fix that only works in one lucky season doesn't count, the same lesson the edge-bucket check itself just taught (§19's 6-10 bucket: 60.7%/41.5% hot/cold swing hiding inside a decent aggregate).

Two design choices confirmed live before locking numbers, not guessed: the full training archive's `|epa_diff|` percentiles (p50=0.151, p75=0.269, p90=0.414, p95=0.532, n=4,590) set the magnitude thresholds below; and `games.conference_game` was checked as a candidate split for variant 2 and found unusable — only 49 of 4,952 games are flagged `conference_game=1`, clearly broken/near-empty for a sport where most games are conference games. That's why variant 2 splits on `|epa_diff|` magnitude instead, and it's a new, previously-undocumented data quality gap in that column, noted here rather than silently relied on.

### Three variants tested

1. **Damped output** (`models/variant_damped_output.py`): same fit as the baseline (identical feature, identical training rows), but any raw prediction beyond ±10 points has its excess beyond the cap halved (30 → 20). Locked: cap=10.0, shrink=0.5.
2. **Mismatch interaction term** (`models/variant_mismatch_interaction.py`): a single 2-feature OLS fit — `(epa_diff, epa_diff × indicator[|epa_diff| > 0.30])` — giving mismatch games (top ~20% by EPA gap) a different effective slope (`epa_coef + interaction_coef`) than normal games, fit in one pass via the existing multivariate harness. Locked: threshold=0.30.
3. **Clipped input** (`models/variant_clipped_input.py`): `epa_diff` itself is clipped to ±0.40 (≈90th percentile) before it ever reaches the OLS fit, at both training and prediction time — the fit never learns from (or extrapolates into) differences beyond the cap. Different mechanism from #1: damping changes what an unchanged fit outputs; clipping changes what the fit is allowed to learn from in the first place.

### Results

```
                    BASELINE            DAMPED OUTPUT        INTERACTION TERM      CLIPPED INPUT
edge 3-6     51.5%  (n=868)      51.8%  (n=852)       50.9%  (n=871)        51.0%  (n=873)
edge 6-10    52.0%  (n=723)      52.5%  (n=736)       50.7%  (n=703)        49.6%  (n=707)
edge 10+     50.7%  (n=836)      51.1%  (n=859)       52.1%  (n=819)        52.4%  (n=772)
```

| Criterion | Damped output | Interaction term | Clipped input |
|---|---|---|---|
| 1. McNemar p<0.05 | p=0.909 — **FAIL** | p=0.499 — **FAIL** | p=0.260 — **FAIL** |
| 2. 10+ not the worst bucket | 51.1% < 51.8%/52.5% — **FAIL** | 52.1% ≥ 50.9%/50.7% — **PASS** | 52.4% ≥ 51.0%/49.6% — **PASS** |
| 3. 10+ beats baseline, ≥4/5 seasons | 3/5 (2023, 2025 no) — **FAIL** | 4/5 (2023 no) — **PASS** | 3/5 (2023, 2024 no) — **FAIL** |
| **Verdict** | **DO NOT KEEP** | **DO NOT KEEP** | **DO NOT KEEP** |

**Damped output** barely moves anything: only 76 disagreement games total (damping compresses magnitude, not sign, so it rarely flips a pick), split exactly 38-38 — indistinguishable from a coin flip. It fails all three criteria outright.

**Mismatch interaction term is the most interesting failure.** Its bucket table looks like a real fix — 10+ becomes the *best* bucket (52.1%), and it beats the baseline's 10+ number in 4 of 5 seasons. If the bar had been buckets-only, this would look like a win. But McNemar on the 315 disagreement games it actually produces is a near coin flip (151-164, p=0.499) — meaning the games where this variant disagrees with the baseline aren't won at a rate distinguishable from noise. The aggregate bucket improvement is consistent with games shifting between buckets (edge changes when predicted_margin changes) as much as with better picks. This is exactly why all three criteria were required together: the bucket table alone would have been misleading.

**Clipped input** shows the same pattern — bucket table looks fixed (10+ becomes best at 52.4%), but McNemar (p=0.260) and season-consistency (3/5, not 4/5) both fail.

### Verdict: none of the three fixes the demonstrated weakness

All three rejected against the pre-registered bar, per the same discipline as §15-17 — no criterion relaxed, no bar renegotiated after seeing results. This doesn't just fail to improve the model; it specifically fails to answer the question this round of testing was built to answer (is the 10+ bucket fixable with a structural change to EPA usage, without new data). The honest answer, for these three candidates, is no.

Combined with §19's finding (no edge bucket in the unmodified baseline beats breakeven either) and §15-17's three rejected feature candidates, EPA-only — in every variant tested so far, including these three structural fixes — has not demonstrated a betting edge over the market in any slice examined. That conclusion has not changed; if anything it's more load-bearing now that a plausible, mechanistically-motivated class of fixes has also come up empty.

### Tests

18 new unit tests (`tests/test_variant_damped_output.py`, `tests/test_variant_mismatch_interaction.py`, `tests/test_variant_clipped_input.py`) verifying each variant's feature/predict functions in isolation (boundary behavior at the cap/threshold, sign preservation, effective-slope arithmetic). `models/run_mismatch_variant_test.py` is the shared bucket-focused test runner (analogous to `run_feature_test.py` but with this round's different bar); `backtest_report.bucket_by_edge()` is the new shared bucketing helper. 138 tests pass across the whole suite.
