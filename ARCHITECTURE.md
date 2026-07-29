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
