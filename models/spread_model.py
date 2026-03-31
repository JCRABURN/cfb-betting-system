"""
spread_model.py
Core projection engine. Combines SP+, EPA, situational factors,
and line movement to generate projected spreads and edge calculations.
Loads weights from weights.json and updates them weekly after results.
"""

import json
import os
import glob
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class GameAnalysis:
    game_id: str
    week: int
    year: int
    home_team: str
    away_team: str
    consensus_spread: Optional[float]  # positive = home favored
    projected_spread: Optional[float]
    edge: Optional[float]             # projected - consensus (positive = lean home)
    recommended_side: Optional[str]
    units: int
    confidence_signals: list
    key_factors: list
    line_movement: Optional[float]
    weather: dict
    risk_flags: list
    qualifies: bool


def load_weights():
    path = "models/weights.json"
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    # Default weights — tuned over time via self-learning loop
    return {
        "sp_rating_diff": 0.85,
        "offense_epa_diff": 6.5,
        "defense_epa_diff": 5.0,
        "home_field_advantage": 2.5,
        "momentum_factor": 1.2,
        "rest_advantage": 1.5,
        "line_movement_weight": 0.6,
        "min_edge_threshold": 3.0,
        "version": "1.0"
    }


def load_latest_stats():
    files = sorted(glob.glob("data/stats/week_*.json"))
    if not files:
        return None
    with open(files[-1]) as f:
        return json.load(f)


def load_latest_odds():
    files = sorted(glob.glob("data/spreads/current_week_*.json"))
    if not files:
        return {}
    with open(files[-1]) as f:
        return {g.get("home_team", "") + g.get("away_team", ""): g for g in json.load(f)}


def calculate_projected_spread(game, odds_map, weights):
    """
    Project the true spread for a game using weighted model inputs.
    Returns projected spread from home team perspective (negative = home favored).
    """
    home = game.get("home_team", "")
    away = game.get("away_team", "")

    home_sp = game.get("home_sp") or 0.0
    away_sp = game.get("away_sp") or 0.0
    sp_diff = (home_sp - away_sp) * weights["sp_rating_diff"]

    home_off_epa = game.get("home_offense_epa") or 0.0
    away_off_epa = game.get("away_offense_epa") or 0.0
    home_def_epa = game.get("home_defense_epa") or 0.0
    away_def_epa = game.get("away_defense_epa") or 0.0

    # EPA advantage: home offense vs away defense, away offense vs home defense
    epa_advantage = (
        (home_off_epa - away_def_epa) * weights["offense_epa_diff"]
        - (away_off_epa - home_def_epa) * weights["defense_epa_diff"]
    )

    # Home field adjustment (neutral site = 0)
    home_field = 0.0 if game.get("neutral_site") else weights["home_field_advantage"]

    projected = -(sp_diff + epa_advantage + home_field)  # negative = home favored
    return round(projected, 1)


def assess_confidence_signals(game, odds_entry, projected_spread, weights):
    """
    Check the 4 key confidence signals.
    Returns (list of signals present, list of risk flags, list of key factors).
    """
    signals = []
    risk_flags = []
    key_factors = []

    consensus = odds_entry.get("consensus_home_spread") if odds_entry else None
    line_movement = odds_entry.get("line_movement") if odds_entry else None

    # Signal 1: Line movement in our favor
    if line_movement is not None and consensus is not None:
        edge = projected_spread - consensus
        if (edge < 0 and line_movement < -0.5) or (edge > 0 and line_movement > 0.5):
            signals.append("line_movement_agreement")
            key_factors.append(
                f"Line moved {abs(line_movement)} pts in our direction (smart money agreement)"
            )
        elif abs(line_movement) > 1.5 and (
            (edge < 0 and line_movement > 1.0) or (edge > 0 and line_movement < -1.0)
        ):
            risk_flags.append("Line movement AGAINST our position — possible sharp fade")

    # Signal 2: SP+ and EPA both agree
    home_sp = game.get("home_sp") or 0
    away_sp = game.get("away_sp") or 0
    home_off_epa = game.get("home_offense_epa") or 0
    away_off_epa = game.get("away_offense_epa") or 0
    sp_favors_home = home_sp > away_sp
    epa_favors_home = home_off_epa > away_off_epa

    if sp_favors_home == epa_favors_home:
        signals.append("statistical_alignment")
        sp_gap = abs(home_sp - away_sp)
        key_factors.append(
            f"SP+ and EPA both align: SP+ gap {sp_gap:.1f}, "
            f"EPA edge {abs(home_off_epa - away_off_epa):.3f}"
        )

    # Signal 3: Weather concern flag
    weather = game.get("weather", {})
    wind = weather.get("wind_mph", 0)
    precip = weather.get("precip_pct", 0)
    if wind > 20:
        risk_flags.append(f"High wind advisory: {wind} mph — impacts passing games")
    if precip > 60:
        risk_flags.append(f"High precipitation probability: {precip}% — may compress scoring")
    if wind < 12 and precip < 30:
        signals.append("favorable_conditions")
        key_factors.append(f"Clean weather: {weather.get('temp_f', 'N/A')}°F, wind {wind} mph")

    # Signal 4: Home record / game context
    home_record = game.get("home_record", {})
    away_record = game.get("away_record", {})
    home_wins = home_record.get("wins", 0)
    home_losses = home_record.get("losses", 1)
    away_wins = away_record.get("wins", 0)
    away_losses = away_record.get("losses", 1)

    home_pct = home_wins / max(home_wins + home_losses, 1)
    away_pct = away_wins / max(away_wins + away_losses, 1)

    if abs(home_pct - away_pct) > 0.25:
        signals.append("record_differential")
        better_team = game["home_team"] if home_pct > away_pct else game["away_team"]
        key_factors.append(
            f"Meaningful record gap: {game['home_team']} {home_wins}-{home_losses} "
            f"vs {game['away_team']} {away_wins}-{away_losses}"
        )

    return signals, risk_flags, key_factors


def assign_units(edge_magnitude, signal_count):
    """Tiered unit sizing based on edge and signal confidence."""
    if edge_magnitude >= 7.0 and signal_count >= 4:
        return 3
    elif edge_magnitude >= 5.0 and signal_count >= 3:
        return 2
    elif edge_magnitude >= 3.0 and signal_count >= 2:
        return 1
    return 0


def analyze_games():
    weights = load_weights()
    stats_data = load_latest_stats()
    if not stats_data:
        print("No stats data found.")
        return []

    week = stats_data["week"]
    year = stats_data["year"]
    odds_map = load_latest_odds()

    results = []
    total_units = 0

    for game in stats_data.get("games", []):
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        odds_key = home + away
        odds_entry = odds_map.get(odds_key)

        consensus_spread = odds_entry.get("consensus_home_spread") if odds_entry else None
        projected_spread = calculate_projected_spread(game, odds_entry, weights)

        edge = None
        recommended_side = None
        qualifies = False
        units = 0

        if consensus_spread is not None:
            raw_edge = projected_spread - consensus_spread
            edge = round(abs(raw_edge), 1)
            recommended_side = away if raw_edge > 0 else home

        signals, risk_flags, key_factors = assess_confidence_signals(
            game, odds_entry, projected_spread, weights
        )

        # Qualification check
        if (
            edge is not None
            and edge >= weights["min_edge_threshold"]
            and not any("AGAINST" in f for f in risk_flags)
            and not any("unresolved" in f.lower() for f in risk_flags)
        ):
            units = assign_units(edge, len(signals))
            if units > 0 and total_units + units <= 15:
                qualifies = True
                total_units += units

        analysis = GameAnalysis(
            game_id=str(game.get("game_id", "")),
            week=week,
            year=year,
            home_team=home,
            away_team=away,
            consensus_spread=consensus_spread,
            projected_spread=projected_spread,
            edge=edge,
            recommended_side=recommended_side,
            units=units,
            confidence_signals=signals,
            key_factors=key_factors,
            line_movement=odds_entry.get("line_movement") if odds_entry else None,
            weather=game.get("weather", {}),
            risk_flags=risk_flags,
            qualifies=qualifies
        )
        results.append(asdict(analysis))

    # Sort qualified bets by units (desc) then edge (desc)
    results.sort(key=lambda x: (-(x["units"] or 0), -(x["edge"] or 0)))

    os.makedirs("models", exist_ok=True)
    out_path = f"data/analysis/week_{week}_{year}.json"
    os.makedirs("data/analysis", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"week": week, "year": year, "total_units": total_units, "games": results}, f, indent=2)

    qualified = [g for g in results if g["qualifies"]]
    print(f"Week {week}: {len(qualified)} qualified bets, {total_units} total units")
    return results


if __name__ == "__main__":
    analyze_games()
