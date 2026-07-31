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

**The fix (data collection, not redesign):** CFBD serves *historical
point-in-time* ratings — "what was each team's SP+ as of week N of season Y."
Backfill weekly snapshots so a team goes from one row/season to one
row/team/week, each stamped with what was known at that point.
`team_game_stats` already has nullable `week`, so it supports this directly.
Same idempotent-backfill pattern already built twice this session, aimed at a
different endpoint.

**Known wrinkle (design decision, not a bug):** SP+ does not publish meaningful
weekly ratings for the first few weeks of a season — early on it leans on
preseason priors (returning production, recruiting) until real results
accumulate. How the model handles early weeks (use the prior? skip weeks 1–3?
treat separately?) is an OPEN decision — see §5.

---

## 2. BUILD ORDER (revised after the §1 finding)

The point-in-time backfill moves ahead of the harness — the harness has nothing
honest to measure until the underlying data is point-in-time.

1. **Point-in-time weekly stats backfill** ← real first build step
2. **Honest backtest harness** (lookahead-safe; CLV as first-class metric)
3. **Dead-simple baseline** (SP+/EPA only) — the reference every feature must beat
4. **Features, one at a time**, each measured against the baseline

Rationale (consistent all session): get the foundation truly right before
building on top, because a wrong foundation silently poisons everything
downstream.

---

## 3. DECISION — point-in-time backfill scope  ✅ LOCKED

**Pull SP+ and EPA weekly, all 7 seasons, point-in-time. Defer success rate
and havoc.**

- **SP+** — non-negotiable. Best single predictor, backbone of the baseline,
  and the specific stat whose season-final version poisons the backtest.
- **EPA** — comes with SP+. Same lookahead problem, and it returns from largely
  the same CFBD calls, so grabbing it in the same pass is nearly free. Leaving
  it season-final while SP+ is weekly would create a lopsided honest/dishonest
  dataset.
- **Success rate + havoc — DEFERRED.** Secondary, step-three features. Not
  needed until we're past the baseline and adding features one at a time.
  Backfilling their weekly history now = collecting/verifying data we won't
  touch for weeks, possibly for features that don't survive the baseline test.
  The backfill script will be reusable + idempotent, so adding them later is
  trivial (point the same script at those fields, re-run, idempotency fetches
  only what's missing).
- **All 7 seasons**, not a partial-year sample — a backtest needs full history
  to be meaningful. Rate limits never triggered on the last full backfill, so
  cost is low.

**Verification discipline (carry forward):** before trusting the backfill,
confirm the point-in-time CFBD field names/shape against ONE real response
(one season-week), exactly as done for every other data path this session.
Every checked path so far has had a hidden bug — assume this one does too until
a live call proves otherwise.

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

- Simplest defensible baseline given the data (SP+ only? SP+ vs. spread?).
- What score = "no real edge" so we know what beating the market actually
  requires. (Break-even ATS at standard -110 juice ≈ 52.4% — flagged here as
  the honest bar; to be expanded in this sitting.)

### ADDENDUM (2026-07-30) — baseline run, and facts learned, not yet folded into the section above

Ran the EPA-differential baseline (offense_epa_play - defense_epa_play, home
minus away, one slope+intercept fit per season on strictly-prior seasons)
through the §4 walk-forward harness. Result: 2021-2025 combined, 51.1% ATS /
-2.5% ROI on all predictions, 51.3% ATS / -2.0% ROI on the edge≥3 bet-subset.
This is the correct, expected outcome for a dumb single-feature model against
an efficient market, not a failure -- checked explicitly for a lookahead leak
before reporting it as fine (a profitable dumb baseline would have been the
suspicious result). Full per-season table in ARCHITECTURE.md §14.

Three facts learned while running it, recorded here as known limitations, not to be "fixed" as bugs:

- **Usable backtest history is 2021-2025 (five seasons), NOT 2020-2025.** 2020
  has zero gradeable games -- 415 of 489 have no opening line from any book at
  all, confirmed via direct query. This is a flat data-coverage hole, distinct
  from (though partly compounded by) 2020's COVID-shortened, late-starting
  schedule. Any future reference to "the backtest window" should say
  2021-2025.
- **Opening-line coverage is thin and single-book-patched.** CFBD's historical
  archive has no true consensus opener at all (0 rows, any season) and
  consensus closers collapse after 2022 (29 rows in 2023, 0 in 2024/2025).
  The harness falls back to a single book (flagged, never silently relabeled
  as consensus -- see ARCHITECTURE.md §14), but this means **CLV numbers rest
  on thinner data than ATS%/ROI** and should be read as directional, not as
  tight as the win-rate columns next to them.
- **The edge≥3 threshold is not demonstrably adding signal yet.** The
  bet-subset beats the all-predictions ATS% in only 1 of 5 seasons (2024,
  55.2% vs. its own 52.1%); 2021/2023/2025 run at or below their
  all-predictions number, 2022 is roughly flat. One hot season out of five is
  not validation of the threshold -- `EDGE_THRESHOLD = 3.0` remains an
  unvalidated placeholder (consistent with §8b's caution against trusting an
  unproven edge estimate with money), not a tuned value.

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
- ~~Rest / schedule spots (bye weeks, short weeks, 3rd straight road game).~~
  **Tested 2026-07-31, REJECTED** — see ARCHITECTURE.md §17. Days-of-rest
  differential + bye-week-flag differential, computed point-in-time from
  `games.start_date` (dates confirmed 100% reliable, 0 NULLs, before
  building). Failed all three criteria — the first feature to fail
  coefficient-sign stability too, not just McNemar/per-season: the
  rest/bye coefficient signs flipped across seasons
  (`{(-1,1), (1,-1), (-1,-1)}` observed), unlike EPA's and even the
  two rejected performance stats' consistently-signed coefficients.
  McNemar was the closest of the three features tested so far (p=0.20
  on 87 disagreement games, vs. 0.82 and 1.00 for success rate/havoc)
  and improved in 3/5 seasons (also the best showing yet) — still a
  clear miss against the pre-registered bar, not treated as a near-pass.
  Building this also surfaced and fixed a real, structural data gap:
  `games` is intentionally FBS-only, so 204 (team, season, week) cases
  had no computable rest because the team's actual prior game was an
  FBS-vs-FCS buy game not in the archive. Recovered via a small,
  bounded, time-boxed CFBD query (dates only, not full game rows) —
  see ARCHITECTURE.md §17 for the full investigation.
- Rivalry / letdown / look-ahead motivational spots.
- ~~Success rate + havoc (once weekly-backfilled per §3).~~ Both **tested**
  **2026-07-30, both REJECTED** — see the feature-test log in
  ARCHITECTURE.md §15 (success rate) and §16 (havoc). Success rate:
  failed McNemar (p=0.82 on 301 disagreement games) and the per-season
  criterion (improved in only 2/5 seasons); only coefficient-sign
  stability passed. Havoc: same outcome, even more decisively on
  McNemar (60 vs. 59 disagreement wins, p=1.00 — indistinguishable
  from a coin flip); improved in only 2/5 seasons; only coefficient
  sign passed. The owner's a-priori case for havoc (a more
  disruption-specific mechanism than success rate) did not pan out —
  the data decided against it, which the question itself anticipated
  as a real possibility. Two independent EPA-derived-stat candidates
  now rejected by the same bar.
- Book-name normalization (DraftKings/Draft Kings, 3× Caesars labels) — needed
  before any "track one book's line over time" analysis.