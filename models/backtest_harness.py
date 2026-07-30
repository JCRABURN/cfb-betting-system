"""
backtest_harness.py
Walk-forward backtest engine (MODEL_DESIGN.md §4). This is the measuring
instrument -- if it leaks the future into the past, every number produced by
anything built on top of it is fake. Three structural guarantees, each
enforced by code shape, not by convention or comments:

1. STATS ARE POINT-IN-TIME. `get_team_stats_as_of()` is the ONLY function
   permitted to read team_game_stats for prediction purposes. It returns the
   most recent `cfbd_point_in_time` snapshot strictly BEFORE the target week
   (`week < target_week`), or None. No other code path may query that table
   for features -- a badly-written prediction function structurally cannot
   see week N's own data, because it never gets a database handle at all
   (see #3).

2. LINE TIMING IS FIXED. `get_opening_line()` only ever returns
   `line_type='opening'` rows. If no genuine opener exists, the caller must
   skip the game -- there is no fallback path that silently substitutes a
   later line.

3. PREDICTION CODE NEVER TOUCHES THE DATABASE. The harness assembles a sealed
   feature package (team stats for both sides + the opening line) BEFORE
   calling the prediction function, and hands over ONLY that package -- never
   a connection, never the season/week it's predicting. Whatever the package
   doesn't contain, the model cannot use. This is what makes "season-wide
   aggregate" leaks (MODEL_DESIGN.md §4, leak #3) structurally impossible
   rather than merely discouraged: any aggregate a model computes can only be
   built from data the package already bounded.

Walk-forward structure (§4's "retrain once per season" v1 simplification):
for each season Y being predicted, the FIT step (whatever coefficients a
model needs) uses only games from seasons < Y -- and each of THOSE training
games' own features are pulled via the same get_team_stats_as_of(), as of
THAT game's own week, never that game's or any later season's full-season
numbers. One accessor function serves both training-set construction and
live week-by-week prediction, so the two can't drift apart into different
(and differently-leaky) code paths.
"""

import sys
import os
import math
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db


# ---------------------------------------------------------------------------
# Guarantee #1: point-in-time stats access. This is the ONLY sanctioned read
# path into team_game_stats for feature construction.
# ---------------------------------------------------------------------------

def get_team_stats_as_of(conn, team, season, week):
    """Most recent point-in-time snapshot strictly before `week` in `season`.
    Returns None if none exists (e.g. week 1, nothing played yet this season)."""
    row = conn.execute(
        """
        SELECT offense_epa_play, defense_epa_play, offense_success_rate,
               defense_success_rate, havoc_rate, week
        FROM team_game_stats
        WHERE source = 'cfbd_point_in_time' AND team = ? AND season = ? AND week < ?
        ORDER BY week DESC
        LIMIT 1
        """,
        (team, season, week),
    ).fetchone()
    if row is None:
        return None
    return {
        "offense_epa_play": row[0],
        "defense_epa_play": row[1],
        "offense_success_rate": row[2],
        "defense_success_rate": row[3],
        "havoc_rate": row[4],
        "as_of_week": row[5],
    }


# ---------------------------------------------------------------------------
# Guarantee #2: line timing. Opening for prediction input, closing for CLV
# only -- two separate functions so a caller can never accidentally use the
# wrong one where a spread is expected.
# ---------------------------------------------------------------------------

def get_opening_line(conn, game_id):
    """Prefer a genuine consensus opener. CFBD's historical /lines archive
    never populates one (confirmed: 0 rows anywhere, any season, for
    line_type='opening' AND book='consensus' -- CFBD only ever computes
    consensus for the CLOSING number, not the open), so this falls back to
    whichever single book has an opener for this game, flagged via `book` in
    the returned dict -- per MODEL_DESIGN.md §4's explicit instruction to
    fall back and FLAG it, never silently substitute a later line and call it
    the opener. If no book has one either (true for every 2019/2020 game --
    also confirmed live), returns None and the caller must skip the game."""
    row = conn.execute(
        "SELECT home_spread, total FROM betting_lines "
        "WHERE game_id = ? AND line_type = 'opening' AND book = 'consensus'",
        (game_id,),
    ).fetchone()
    if row is not None and row[0] is not None:
        return {"home_spread": row[0], "total": row[1], "book": "consensus"}

    row = conn.execute(
        "SELECT home_spread, total, book FROM betting_lines "
        "WHERE game_id = ? AND line_type = 'opening' AND home_spread IS NOT NULL "
        "ORDER BY book LIMIT 1",
        (game_id,),
    ).fetchone()
    if row is None:
        return None
    return {"home_spread": row[0], "total": row[1], "book": row[2]}


def get_closing_line(conn, game_id, book=None):
    """CLV reference only, never a model input. If `book` is given (the same
    book the opening line actually came from), prefers that book's closing
    line first, so CLV compares like-for-like instead of opening-from-one-
    book vs. closing-from-another. Confirmed live: consensus closing coverage
    collapses after 2022 (29 rows total in 2023, 0 in 2024/2025) while
    individual books (e.g. Bovada) stay fully covered -- same class of gap as
    get_opening_line, same fix: fall back and don't silently go without."""
    if book is not None:
        row = conn.execute(
            "SELECT home_spread, total FROM betting_lines "
            "WHERE game_id = ? AND line_type = 'closing' AND book = ?",
            (game_id, book),
        ).fetchone()
        if row is not None and row[0] is not None:
            return {"home_spread": row[0], "total": row[1], "book": book}

    row = conn.execute(
        "SELECT home_spread, total FROM betting_lines "
        "WHERE game_id = ? AND line_type = 'closing' AND book = 'consensus'",
        (game_id,),
    ).fetchone()
    if row is not None and row[0] is not None:
        return {"home_spread": row[0], "total": row[1], "book": "consensus"}

    row = conn.execute(
        "SELECT home_spread, total, book FROM betting_lines "
        "WHERE game_id = ? AND line_type = 'closing' AND home_spread IS NOT NULL "
        "ORDER BY book LIMIT 1",
        (game_id,),
    ).fetchone()
    if row is None:
        return None
    return {"home_spread": row[0], "total": row[1], "book": row[2]}


def get_final_score(conn, game_id):
    row = conn.execute(
        "SELECT home_points, away_points, completed FROM games WHERE game_id = ?",
        (game_id,),
    ).fetchone()
    if row is None or not row[2] or row[0] is None or row[1] is None:
        return None
    return {"home_points": row[0], "away_points": row[1]}


# ---------------------------------------------------------------------------
# Guarantee #3: the sealed package. Built here, handed to the prediction
# function as a plain dict -- no db connection ever passed through.
# ---------------------------------------------------------------------------

def get_pregame_stats(conn, home_team, away_team, season, week):
    """Just the stats half of the package -- used for TRAINING set
    construction, which needs (feature, actual_margin) pairs only, never a
    betting line. Keeping this separate from build_feature_package means
    2019/2020's total absence of opening lines (see get_opening_line) doesn't
    wrongly starve the training set of games it never actually needed a line
    for in the first place."""
    home_stats = get_team_stats_as_of(conn, home_team, season, week)
    away_stats = get_team_stats_as_of(conn, away_team, season, week)
    if home_stats is None or away_stats is None:
        return None
    return {"home_stats": home_stats, "away_stats": away_stats}


def build_feature_package(conn, game_id, season, week, home_team, away_team):
    """Returns the sealed package for one game, or None (with a reason) if
    anything required is missing -- caller must skip, not substitute. Used
    for the actual PREDICT step, which does need a line (to compute edge and
    to grade against)."""
    stats = get_pregame_stats(conn, home_team, away_team, season, week)
    if stats is None:
        return None, "missing_pregame_stats"

    opening = get_opening_line(conn, game_id)
    if opening is None:
        return None, "missing_opening_line"

    return {
        "home_stats": stats["home_stats"],
        "away_stats": stats["away_stats"],
        "opening_spread": opening["home_spread"],
        "opening_total": opening["total"],
        "opening_book": opening["book"],
    }, None


# ---------------------------------------------------------------------------
# List every lined FBS game for a given season/week, in the shape the
# walk-forward loop needs. Only games that are actually completed (so we can
# grade them) are included -- an in-progress/future week naturally yields
# nothing yet, which is exactly walk-forward correctness.
# ---------------------------------------------------------------------------

def list_games(conn, season, week):
    return conn.execute(
        """
        SELECT game_id, home_team, away_team, home_points, away_points
        FROM games
        WHERE season = ? AND week = ? AND completed = 1
        ORDER BY game_id
        """,
        (season, week),
    ).fetchall()


def list_weeks(conn, season):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT week FROM games WHERE season = ? ORDER BY week", (season,)
    ).fetchall()]


# ---------------------------------------------------------------------------
# Grading -- matches update_results.py's ATS/CLV/unit-P&L conventions exactly
# (verified against that file), reimplemented here rather than imported so the
# backtest harness has no coupling to the live-pipeline module.
# ---------------------------------------------------------------------------

def grade_ats(side, home_team, away_team, opening_spread, home_points, away_points):
    """Returns 'win' | 'loss' | 'push' for `side` against `opening_spread`
    (home_spread convention: negative = home favored)."""
    actual_margin = home_points - away_points
    if side == home_team:
        covered = actual_margin + opening_spread
    elif side == away_team:
        covered = -(actual_margin + opening_spread)
    else:
        raise ValueError(f"side {side!r} is neither {home_team!r} nor {away_team!r}")
    if covered > 0:
        return "win"
    if covered < 0:
        return "loss"
    return "push"


def unit_pl(result):
    """Standard -110 juice: win 0.909 units, lose 1.0 unit, push 0."""
    if result == "win":
        return round(0.909, 3)
    if result == "loss":
        return -1.0
    return 0.0


def calculate_clv(side, home_team, opening_spread, closing_spread):
    """Positive CLV = the number we bet was better than where the market closed."""
    if side == home_team:
        return round(opening_spread - closing_spread, 2)
    return round(closing_spread - opening_spread, 2)


# ---------------------------------------------------------------------------
# McNemar's test -- the instrument for "did adding this feature change picks
# in a way that mattered," used for every one-at-a-time feature test
# (MODEL_DESIGN.md "Later features" list), not just this one. On a game where
# a challenger model disagrees with the baseline (picks the opposite side of
# the SAME opening line), exactly one of them wins the bet, unless it's a
# push -- so among non-push disagreement games, this reduces to: did the
# challenger win more of the games it changed than it lost? That's a paired
# comparison, not an aggregate one, and far more powerful than comparing two
# overall win rates over thousands of games where most picks agree anyway.
# ---------------------------------------------------------------------------

def mcnemar_test(challenger_right, baseline_right):
    """challenger_right: count of disagreement games the challenger won (so
    the baseline lost that same game). baseline_right: count of disagreement
    games the baseline won (so the challenger lost). Concordant outcomes
    don't enter McNemar's test at all -- only where the two models disagree
    and therefore produced a different real-money outcome. Uses the standard
    continuity-corrected chi-square(1 df) statistic; the p-value comes from
    math.erf (stdlib) since chi-square(1) is the square of a standard normal
    -- no scipy/numpy needed for a single degree of freedom.
    Returns (chi2_statistic, p_value)."""
    b, c = challenger_right, baseline_right
    if b + c == 0:
        return 0.0, 1.0
    chi2 = (abs(b - c) - 1) ** 2 / (b + c)
    z = math.sqrt(chi2)
    p_value = 1 - math.erf(z / math.sqrt(2))
    return chi2, p_value


# ---------------------------------------------------------------------------
# Multivariate linear regression (closed form, normal equations) -- generalizes
# from the single-feature EPA baseline to N features (e.g. EPA + success rate)
# without adding a feature-count ceiling to the harness. No new dependency:
# pure Python Gaussian elimination is entirely adequate at the 2-4 feature
# scale this project uses; a real linear algebra library would only earn its
# place well beyond that (per CLAUDE.md's rule to check before adding one).
# ---------------------------------------------------------------------------

def _solve_linear_system(A, b):
    """Gaussian elimination with partial pivoting. A: square matrix (list of
    lists), b: vector, both length n. Returns the solution vector. Small
    matrices only (as many rows/cols as fitted features + 1) -- this is not
    meant to scale past a handful of features."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot_row][col]) < 1e-12:
            raise ValueError("singular matrix -- cannot solve (check for a constant "
                              "or perfectly collinear feature in training data)")
        M[col], M[pivot_row] = M[pivot_row], M[col]
        pivot = M[col][col]
        M[col] = [v / pivot for v in M[col]]
        for r in range(n):
            if r != col:
                factor = M[r][col]
                M[r] = [M[r][c] - factor * M[col][c] for c in range(n + 1)]
    return [M[i][n] for i in range(n)]


def fit_multilinear(feature_rows, ys):
    """OLS for y ~ b0 + b1*x1 + b2*x2 + ... via normal equations
    (beta = (X^T X)^-1 X^T y). feature_rows: list of tuples, each the same
    length k (no intercept column -- added automatically). Returns
    (intercept, (coef1, coef2, ...)). Raises if fewer than k+1 training
    points or the design matrix is singular (e.g. a constant feature)."""
    n = len(ys)
    k = len(feature_rows[0])
    if n < k + 1:
        raise ValueError(f"need at least {k + 1} training points for {k} feature(s), got {n}")
    X = [[1.0] + list(row) for row in feature_rows]
    p = k + 1
    XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(p)] for a in range(p)]
    Xty = [sum(X[i][a] * ys[i] for i in range(n)) for a in range(p)]
    beta = _solve_linear_system(XtX, Xty)
    return beta[0], tuple(beta[1:])


# ---------------------------------------------------------------------------
# The walk-forward loop itself.
# ---------------------------------------------------------------------------

@dataclass
class PredictionRecord:
    game_id: int
    season: int
    week: int
    home_team: str
    away_team: str
    side: str
    edge: float
    opening_spread: float
    opening_book: Optional[str] = None
    result: Optional[str] = None
    clv: Optional[float] = None
    unit_pl: Optional[float] = None
    skipped_reason: Optional[str] = None


def build_training_set(conn, feature_fn, seasons_before):
    """Every completed game from seasons strictly < the season being
    predicted, each with ITS OWN as-of-that-game's-week feature value via
    feature_fn (built from get_pregame_stats -- the same accessor the
    predict step uses, so training and prediction can't drift onto
    different, differently-leaky code paths). feature_fn(stats) must return
    a TUPLE of one or more values (a single-feature model returns a 1-tuple,
    e.g. `(epa_diff,)`) -- this is what lets fit_multilinear generalize from
    one feature to several without a second code path. Returns (rows, ys) for
    fit_multilinear. Deliberately does NOT require an opening line -- fitting
    only needs (features, actual_margin) pairs, no betting line at all, so a
    season with no opening-line coverage (2019/2020, see get_opening_line) is
    still valid, full training data.

    seasons_before is a list of season ints, all strictly less than the
    season under test -- constructing it that way is what keeps this
    function from ever seeing the season it's about to help predict.
    """
    rows, ys = [], []
    for season in seasons_before:
        for week in list_weeks(conn, season):
            for game_id, home_team, away_team, home_points, away_points in list_games(conn, season, week):
                stats = get_pregame_stats(conn, home_team, away_team, season, week)
                if stats is None:
                    continue
                rows.append(feature_fn(stats))
                ys.append(home_points - away_points)
    return rows, ys


def available_seasons_before(conn, season):
    """Every season in the archive strictly before `season` -- queried from
    the data itself rather than assumed, so training naturally picks up
    whatever history exists (e.g. 2019, which v1 never predicts against but
    IS valid training data for 2020) without a hardcoded floor year."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT season FROM games WHERE season < ? ORDER BY season", (season,)
    ).fetchall()]


def run_walk_forward(conn, seasons, feature_fn, predict_fn):
    """seasons: ordered list of season ints to test (e.g. [2020..2025]).
    feature_fn(package) -> tuple of feature values (e.g. `(epa_diff,)` for a
    single feature, `(epa_diff, success_rate_diff)` for two).
    predict_fn(package, intercept, coefs) -> predicted margin (home - away),
    where coefs is a tuple the same length as feature_fn's output.

    For each season Y: fit intercept+coefs on seasons < Y only, then predict
    every lined FBS game week-by-week using week N-1 features. Returns
    (records, season_fits) -- a flat list of PredictionRecord (including
    skipped games, reason noted) and a {season: (intercept, coefs)} dict, so
    callers can check coefficient stability across seasons (a coefficient
    that flips sign season to season is fitting noise, not a relationship).
    Every record gets a side + edge regardless of size -- bet/no-bet
    thresholding (MODEL_DESIGN.md §5) is a reporting-time concern applied
    afterward, not something the harness itself decides.
    """
    records = []
    season_fits = {}
    for season in seasons:
        all_prior = available_seasons_before(conn, season)
        rows, ys = build_training_set(conn, feature_fn, all_prior)
        if len(rows) < 2:
            raise ValueError(f"not enough training data before season {season} "
                              f"({len(rows)} games) -- cannot fit")
        intercept, coefs = fit_multilinear(rows, ys)
        season_fits[season] = (intercept, coefs)

        for week in list_weeks(conn, season):
            for game_id, home_team, away_team, home_points, away_points in list_games(conn, season, week):
                package, reason = build_feature_package(conn, game_id, season, week, home_team, away_team)
                if package is None:
                    records.append(PredictionRecord(
                        game_id=game_id, season=season, week=week,
                        home_team=home_team, away_team=away_team,
                        side=None, edge=None, opening_spread=None,
                        skipped_reason=reason,
                    ))
                    continue

                predicted_margin = predict_fn(package, intercept, coefs)
                opening_spread = package["opening_spread"]
                # market-implied home margin = -opening_spread (home_spread
                # negative means home favored by that many points)
                edge_home = predicted_margin - (-opening_spread)
                side = home_team if edge_home > 0 else away_team
                edge = abs(edge_home)

                result = grade_ats(side, home_team, away_team, opening_spread, home_points, away_points)
                pl = unit_pl(result)

                closing = get_closing_line(conn, game_id, book=package["opening_book"])
                clv = (calculate_clv(side, home_team, opening_spread, closing["home_spread"])
                       if closing else None)

                records.append(PredictionRecord(
                    game_id=game_id, season=season, week=week,
                    home_team=home_team, away_team=away_team,
                    side=side, edge=round(edge, 2), opening_spread=opening_spread,
                    opening_book=package["opening_book"],
                    result=result, clv=clv, unit_pl=pl,
                ))
    return records, season_fits
