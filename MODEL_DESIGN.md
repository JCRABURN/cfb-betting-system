# CFB Betting Model — Design Doc (in progress)

> Purpose: this is the design spec for the prediction-model phase of the

> cfb-betting-system. It is being written during a design conversation

> and will be handed to Claude Code as a build spec once complete.

> **Design first, build second.** Nothing here gets built until the

> section is marked APPROVED.

---

## 0. Where the project stands (context for Claude Code)

Data foundation is complete and committed:

- SQLite DB at `data/cfb.db`, 7 seasons (2019–2025), joining cleanly by `game_id`.

- Tables: `games` (4,952 rows), `team_game_stats` (SP+, EPA, success rate,

  havoc rate), `betting_lines` (25,678 rows — timestamped opening + closing

  spreads/totals across multiple books), plus `weather`, `injuries`, `picks`,

  `ingestion_runs`.

- Team-name resolver in place; CFBD sources join at 100%, Odds API needs the

  mascot-stripping resolver (already built).

- `CLAUDE.md` and `ARCHITECTURE.md` document the whole system.

Full history of how this was built and verified is in `ARCHITECTURE.md`.

---

## 1. THE CRITICAL PROBLEM: lookahead bias in stored stats  ⚠️ BLOCKS EVERYTHING

**Finding (verified via query):** `team_game_stats` currently stores

**season-final** SP+/EPA — one row per team per season (`week`/`game_id`

NULL), from `backfill_historical_stats.py`. Confirmed:

`SELECT ... COUNT(DISTINCT sp_rating) FROM team_game_stats

WHERE season=2023 AND team='Georgia'` → `distinct_sp = 1`.

**Why this is fatal for a backtest:** SP+ is a season-long rating that reflects

the *entire* season, including the games we'd be trying to predict. Using

Georgia's final 2023 SP+ to predict their week 3 2023 game means the rating

already "knows" how week 3 (and every later week) turned out. A backtest built

on this looks excellent and is entirely fake — the edge vanishes the moment it

meets live games, where only point-in-time data exists.

This is the same failure mode as the field-name / join bugs caught earlier this

session: great-looking output that is silently, fundamentally wrong.

**The fix, as originally scoped (data collection, not redesign):** the plan was

that CFBD serves *historical point-in-time* ratings — "what was each team's

SP+ as of week N of season Y" — so weekly snapshots could replace the

season-final row. **Live verification (2026-07-30) found this is only true for**

**EPA/success rate/havoc, not SP+.** See §3 for the full finding: `/ratings/sp`'s

`week` param is silently ignored (same rating returned for week 3, 8, 13, and no

week param at all), while `/stats/season/advanced`'s `endWeek` param genuinely

does restrict the aggregate to games through that week. SP+ cannot be backfilled

point-in-time from CFBD's API at all, for any past season — deferred to

live-forward capture only (§3). EPA/success rate/havoc **were** backfilled this

way, point-in-time, all 7 seasons — done, not just planned.

`team_game_stats` already had nullable `week`, so it supported this directly.

Same idempotent-backfill pattern already built twice this session before this,

now built a third time (`data/backfill_point_in_time_stats.py`).

**Known wrinkle (design decision, not a bug):** SP+ does not publish meaningful

weekly ratings for the first few weeks of a season — early on it leans on

preseason priors (returning production, recruiting) until real results

accumulate. How the model handles early weeks (use the prior? skip weeks 1–3?

treat separately?) is an OPEN decision — see §5. This is now moot for the

*backtest* (which has no SP+ at all, see §3), but still applies to *live*

predictions once SP+ capture starts.

---

## 2. BUILD ORDER (revised after the §1 finding)

The point-in-time backfill moves ahead of the harness — the harness has nothing

honest to measure until the underlying data is point-in-time.

1. **Point-in-time weekly stats backfill** ← real first build step

2. **Honest backtest harness** (lookahead-safe; CLV as first-class metric)

3. **Dead-simple baseline** (originally scoped as SP+/EPA only — revised per §3:

   no SP+ exists historically, so the baseline is EPA/success rate/havoc only)

   — the reference every feature must beat

4. **Features, one at a time**, each measured against the baseline

Rationale (consistent all session): get the foundation truly right before

building on top, because a wrong foundation silently poisons everything

downstream.

---

## 3. DECISION — point-in-time backfill scope  ✅ DONE (revised from original plan, see below)

**Original plan (this section, before verification): pull SP+ and EPA weekly,**

**all 7 seasons, point-in-time, defer success rate/havoc.** Live verification

(2026-07-30) overturned half of that plan and simplified the other half. What

actually happened:

**SP+ — backfill-blocked, not deferred. Structurally impossible via CFBD's API.**

Direct A/B test, same team/season, `/ratings/sp?year=2023`: `week=3` → rating

31.2, `week=8` → 31.2, `week=13` → 31.2, no `week` param at all → 31.2.

Identical in every field, not just `rating`. Checked for alternates

(`/ratings/sp/conferences` — conference-level, not team; `/ratings/srs` — a

different rating system entirely, not SP+; `/rankings` — AP/Coaches poll, not

SP+). **None serve historical point-in-time SP+.** CFBD only retains/serves the

season-final SP+ once a season completes. This is not a "defer to later" —

there is nothing to backfill. **Resolution: SP+ is captured live-forward only,**

starting now, each week naturally point-in-time during an in-progress season.

The 2019-2025 backtest has **no SP+ feature** — see the honesty caveat below.

**EPA + success rate + havoc — backfilled together, point-in-time, all 7**

**seasons.** The original plan deferred success rate/havoc to save a second

live-verification pass on a different endpoint — moot, since all three come

from the exact same `/stats/season/advanced` call as EPA, verified together in

one shot. Direct proof it's real (not silently frozen like SP+): Georgia 2023,

`endWeek=3` → offense.ppa 0.311, `endWeek=8` → 0.377, full season → 0.400 —

genuinely different at every cutoff. Same for successRate and havoc.total.

**Built:** `data/backfill_point_in_time_stats.py` — one row per team per week

(not per season) in `team_game_stats`, `source='cfbd_point_in_time'`,

`sp_rating` always `NULL`. Idempotent (skips an ingested week with zero API

calls unless `--force`, which replaces rather than duplicates — `team_game_stats`

needs exactly one canonical row per team/week, unlike `betting_lines`' genuine

append-only design). Ran for real: **13,290 rows**, all 7 seasons, weeks 1-15.

Verified post-run: Georgia 2023 weeks 3/6/9/12 show EPA/success rate/havoc

genuinely moving (e.g. offense_epa_play 0.311 → 0.378 → 0.383 → 0.403); 0

duplicate (season, week, team) rows; 0 rows with non-NULL `sp_rating`.

**⚠️ HONESTY CAVEAT — backtest vs. live feature sets differ, do not compare as**

**identical:** the 2019-2025 backtest (§4) will train/evaluate on

EPA/success rate/havoc only — no SP+ exists for any historical week. Live

predictions (once SP+ capture starts running week-to-week from here forward)

will eventually have SP+ available too. **These are not the same feature set.**

A model version trained/backtested without SP+ is not directly comparable to

a hypothetical future version with SP+ added — that comparison would need its

own controlled test once enough live-forward SP+ history accumulates. Do not

silently blend backtest results (no SP+) with live performance claims (SP+

eventually included) as if they measured the same thing.

**Consumption note for whoever builds feature engineering:** a `team_game_stats`

row with `week=N` (`source='cfbd_point_in_time'`) holds stats CUMULATIVE

THROUGH week N's games. Predicting week N's games must join against `week=N-1`

(or the latest available prior week), never `week=N` itself — that row already

includes week N's own results.

**All 7 seasons**, not a partial-year sample — a backtest needs full history to

be meaningful. Rate limits never triggered (same as the lines backfill before

it) — 0 empty/failed weeks across the full run.

**Verification discipline (carried forward, and it caught something real**

**again):** confirmed the point-in-time CFBD field names/shape against real

responses before building anything, exactly as done for every other data path

this session. Every checked path so far has had a hidden bug — this one did

too (SP+'s ignored `week` param), assume the next one does as well until a

live call proves otherwise.

---

## 4. DECISION — honest backtest harness design  ✅ LOCKED

The most important piece: it determines whether every number the model ever

produces is trustworthy or fake.

### Structure: WALK-FORWARD validation (time-ordered)

Test the model the way it would actually be used — moving forward through time,

only ever looking backward. To predict week N of season Y, the model may learn

ONLY from games before that point (all prior seasons + weeks 1..N-1 of Y),

never after. Step forward week by week / season by season through all history.

**Explicitly NOT** random-shuffle 80/20 train/test. Random shuffling mixes the

timeline and lets the future leak into the past (train on a December game, then

"predict" a September game from the same season). That produces fake accuracy

that dies on live games. Walk-forward makes the leak *structurally impossible* —

time only moves one direction. Do not trust care; enforce it with structure.

### v1 simplification: retrain once per SEASON (not per week)

Train on all prior completed seasons, predict the upcoming season week by week,

then roll forward one season. Still fully lookahead-safe (only past seasons

used), much simpler to build/debug. Finer week-by-week retraining can come later

if it proves worth it.

### Line timing: predict against the OPENING line, measure CLV vs. CLOSE  ✅

- **Model input = opening line.** Earliest, softest, least-efficient number —

  where real edge lives and where CLV is maximized.

- **CLV = (opening line we'd bet) vs. (closing line market settled on).** That

  gap is the core edge signal, reported from day one.

- **Do NOT feed the closing line to the model as an input** — it reflects sharp

  money/news up to kickoff and would leak information the model shouldn't know.

**Honesty caveat (document in results, don't "fix"):** opening lines are the

hardest to actually bet live — odd hours, low early limits, fast movement. So

backtested opening-line CLV is closer to a *ceiling* than a guarantee: it proves

the model finds real edge; how much is captured live depends on execution.

**Data caveat (harness must handle explicitly):** historical opener coverage may

be spotty. If no true opener exists for a game, the harness must skip it or fall

back to the earliest available line AND FLAG it — never silently substitute a

later line and call it the opener (that quietly inflates the backtest).

### The three lookahead leaks the harness must structurally prevent

1. **Stats** — every feature strictly as-of-kickoff (this is why §3's

   point-in-time backfill exists). Walk-forward structure + point-in-time data

   are two halves of one guarantee; neither alone is sufficient.

2. **Line timing** — use opening (input) vs. closing (CLV only), per above.

3. **Season-wide aggregates** — any feature averaged/counted over the whole

   season (season averages, games-played counts, full-season coach records)

   leaks. The harness treats "is every feature strictly as-of-kickoff?" as an

   ENFORCED checklist, not a hope. This is the leak that survives even a correct

   walk-forward structure.

### Metrics reported from day one

ATS win %, flat-stake ROI, **and CLV** — all three, every run. CLV is

first-class (the timestamped opening/closing lines were stored specifically to

enable it), not a later add-on.

---

## 5. DECISION — prediction target + predict-vs-bet architecture  ✅ LOCKED

Driven by the owner's stated goals (bet the spread not winners; evaluate every

lined FBS game; professional edge-measured process; continuous improvement).

### Predict on EVERY lined FBS game — always. Separate PREDICTION from BET.

A prediction ("Georgia covers, projected margin X") and a bet ("the edge vs. the

market is big enough to risk money") are two different acts. The model makes a

graded prediction on every lined FBS game — no cherry-picking, no exceptions —

and a separate threshold decides whether that prediction is a *recommended bet*.

**Why predict on all games even when not betting (the learning argument):**

scoring only bet games trains the model on a biased slice — only games it

already thought it had edge on. It would never learn whether its "no bet"

judgments were correct, and could never discover a whole category of games it

*should* be betting (goal #9's systematic-weakness detection is impossible on

games you refuse to evaluate). Grading a prediction on every game gives a

complete report card across the entire game space.

### Prediction target

- Predict a **margin** (e.g. "Georgia by 6.2"), then compare to the locked

  spread to derive the side. Margin is richer than a bare cover/no-cover

  classification: the *size* of (predicted margin − spread) IS the edge signal

  that drives confidence and the bet/no-bet threshold. Straight-up winner is a

  byproduct, reported but secondary (goal #1).

### Confidence = edge size, not vibes

Confidence (1–5) is driven by how far the predicted margin sits from the locked

contest spread. Project Georgia −9 vs. line −3 → big edge → high confidence.

Project −3.5 vs. line −3 → rounding error → minimal edge → low confidence /

no-bet.

### Threshold splits recommendation from prediction

- Edge above cutoff → recommended bet, with confidence tier.

- Edge below cutoff → officially **"no bet"** (or lowest tier), **but the

  prediction is still recorded and graded.** Same prediction, different staking

  decision.

### This resolves the #2-vs-#10 tension (pick-everything vs. pass-when-no-edge)

- **SplashSports contest (goal #3):** must pick every game → use the prediction

  on every game (required).

- **Bankroll / edge measurement (goal #10):** use the threshold → bet only real

  edge.

- One prediction engine, two consumers. Not a compromise — both done for their

  correct purpose.

### REQUIRED: mark each pick's role in the `picks` table  ⚠️

Add a field distinguishing **recommended-bet** vs. **contest-only / no-bet**

(same discipline as the `pick_type` live/backfilled/synthetic field).

- Edge/ROI/CLV performance is measured ONLY on games that would actually have

  been bet — otherwise forced low-edge contest picks drown out the real signal

  and understate true betting ROI.

- Prediction calibration is measured on ALL predictions.

- Never let the two categories silently blend — every honest metric downstream

  depends on separating them.

### Early-season SP+ handling (from §1 wrinkle) — still OPEN

Decide: use preseason prior for weeks 1–3, skip those weeks, or flag them as

lower-confidence by rule. Defer to the baseline sitting.

---

## 5b. DATA-HONESTY BOUNDARY on "deep factors" (goal #6)  ⚠️ CRITICAL

The owner wants deep inputs: scheme fit, trench mismatches, QB quality, coaching

tendencies, travel/rest, weather, motivation, market behavior, historical ATS.

**Not all of these are derivable from current data, and the model must NOT

fabricate the ones that aren't.**

- **Available now in `cfb.db`:** SP+, EPA, success rate, havoc, lines, weather.

  SP+/EPA already capture much of what trench mismatches and QB quality *produce*

  on the field, indirectly. The v1 quantitative model runs on THESE.

- **NOT in the data:** OL-vs-DL grades, QB-quality ratings, scheme

  classifications. CFBD does not expose these in usable form. The model must NOT

  invent scheme-fit or trench numbers it cannot derive (this is the

  "havoc-rate-for-offense" fabrication trap — a made-up feature the model then

  weights).

- **Resolution:** v1 predicts from the metrics that exist. Richer qualitative

  factors enter as **tracked manual overrides** during the daily-refresh step

  (goal #4), clearly logged as manual adjustments, OR as future

  data-collection projects — never as features the v1 model silently computes.

  Any manual override must be recorded so its effect on results can be audited

  separately from the model's own output.

---

## 6. OPEN — baseline definition + "no edge" reference  🔲

- Simplest defensible baseline given the data (~~SP+ only?~~ SP+ is not

  available historically per §3 — options are now EPA only, or

  EPA/success rate/havoc combined; SP+ vs. spread?).

- What score = "no real edge" so we know what beating the market actually

  requires. (Break-even ATS at standard -110 juice ≈ 52.4% — flagged here as

  the honest bar; to be expanded in this sitting.)

---

## 7. OPERATIONAL WORKFLOW (from owner goals #3, #4, #5, #8, #9, #10)

### Locked contest lines (goal #3)

- When SplashSports contest spreads are uploaded (~Tuesday), those become the

  **permanent reference lines** for the week. Store them explicitly flagged as

  contest-locked, distinct from market opening/closing lines.

- Subsequent market movement is tracked ONLY to measure CLV against the locked

  line — it does NOT change the official picks' reference number.

### Daily refresh with fixed lines (goal #4)

- Reassess daily using late-breaking info: injuries, weather, coaching changes,

  motivation, travel, roster availability.

- Picks may change, but ALWAYS measured against the original locked contest

  number, never a re-pegged line.

- Late-breaking qualitative factors enter as logged manual overrides per §5b —

  recorded so their effect is auditable separately from the model.

### Weekly betting card output (goal #5)

- Full slate: every lined FBS game with side, confidence (1–5), rationale.

- Ranked **Top 5** highest-edge plays.

- Clear separation of recommended-bets vs. contest-only/no-bet (per §5 field).

### Post-game audit (goal #8) — a first-class output, not an afterthought

After each week, produce:

- win / loss / push, final scores;

- CLV per pick (locked line vs. close);

- hook analysis (games decided by the half-point / key numbers);

- backdoor-cover analysis;

- identification of logic failures — WHY each pick succeeded or failed;

- scored across ALL predictions (calibration) AND the bet subset (ROI/edge)

  separately, per §5.

### Continuous improvement + versioning (goals #9, #10)

- Do NOT change rules from one surprising result. Require: weekly diagnostics →

  identify *systematic* weaknesses → quantify the adjustment → only then version

  (v2.7 → v2.8) after a COMPLETE audit.

- Model version stamped on every pick/prediction so performance is always

  attributable to a specific model version. (Enables "did v2.8 actually beat

  v2.7?" to be answered honestly.)

### Tone of the system (goal #7)

Be critical, not agreeable. If the data doesn't support a favored play, the

system says so. Confidence must fall out of edge math, never be inflated to

please. (This mirrors the whole build discipline: surface uncomfortable truth

rather than produce agreeable-looking output.)

---

## 8. GUIDING PRINCIPLE (owner's own framing)

Not maximizing number of picks — a disciplined process that tracks performance

objectively, minimizes emotional decisions, measures edge against the market,

and gets more accurate over successive seasons. The contest requires a pick on

every game; the *betting* process bets only real edge. The system serves both

without letting either corrupt the other's metrics.

---

## 8b. UNIT SIZING / STAKING  ✅ LOCKED (v1 approach + graduation path)

Bet size scales with edge — but FAR more conservatively than the math tempts.

This is where a decent model quietly becomes an account-blowing one.

### Why NOT full Kelly (the seductive-but-dangerous option)

The Kelly Criterion gives the mathematically optimal bankroll fraction — BUT

only if the edge estimate is correct. It is brutally unforgiving of

*overestimated* edge: think you have 5% but really have 1% → full Kelly drives

massive drawdowns / functional ruin even while "right on average." A new,

unproven model ALWAYS overestimates its own edge (that's what overfitting does).

Full Kelly on an unproven CFB model ≈ a fast way to lose money even if the model

is genuinely good.

### v1: flat confidence-tier sizing (do NOT use Kelly yet)

There is no evidence yet that the model's edge estimates are calibrated, so v1

must NOT compound an unproven probability into bet size. Tie units directly to

confidence tiers, e.g.:

- confidence 5 → 3 units

- confidence 4 → 2 units

- confidence 3 → 1 unit

- below threshold / no-bet → 0 units

Crude, transparent, and it does not trust the model's edge estimate with money.

### Graduation path: fractional Kelly, only after calibration is PROVEN

Over a season, measure whether 5-confidence plays actually won more than 3s

(calibration — part of the §7 audit). ONLY once confidence tiers are shown to

track real results do you graduate to **fractional Kelly** — and even then use

quarter-Kelly (0.25x) or less, never full. Fractional Kelly keeps most of the

growth benefit with far less risk and is forgiving of the estimate error that

will always exist.

Sequencing principle (same as the whole project): a confidence rating is a

*claim*; Kelly sizing *trusts* that claim with money; verify the claim against

real results before trusting it. Flat tiers now, Kelly once the audit earns it.

### HARD GUARDRAIL (all methods, non-negotiable): max bet cap

No single game exceeds a fixed ceiling of bankroll (≈2–3% for a new system),

regardless of what the model or Kelly says. Circuit breaker against a single

confident-and-wrong pick, a data bug feeding a garbage edge, or the model going

haywire. (This session showed how a silent bug produces confident-looking

garbage — the cap stops that garbage from costing the account.)

---

## Later features (parked — do NOT build now, keep in view)

Added one at a time, each measured against the baseline:

- Coach ATS situational splits: as underdog, off a bye, first year at a new

  program. (Raw career coach ATS% is mostly noise / small-sample — use

  *situational* splits with a plausible mechanism, not overall records.)

- Rest / schedule spots (bye weeks, short weeks, 3rd straight road game).

- Rivalry / letdown / look-ahead motivational spots.

- Success rate + havoc (once weekly-backfilled per §3).

- Book-name normalization (DraftKings/Draft Kings, 3× Caesars labels) — needed

  before any "track one book's line over time" analysis.
