"""
update_results.py
Runs every Monday after games complete.
- Updates pick results (W/L/Push) in all_picks.json
- Calculates CLV (closing line value)
- Retrains model weights based on recent performance
- Flags if a full recalibration is needed (3+ consecutive losing weeks)
"""

import json
import os
import glob
import requests
from datetime import datetime

CFBD_API_KEY = os.environ.get("CFBD_API_KEY", "")
CFBD_BASE = "https://api.collegefootballdata.com"
HEADERS = {"Authorization": f"Bearer {CFBD_API_KEY}"}
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_BASE = "https://api.the-odds-api.com/v4"


def fetch_game_results(year, week):
    """Get final scores from CFBD."""
    url = f"{CFBD_BASE}/games"
    resp = requests.get(url, headers=HEADERS, params={
        "year": year, "week": week, "division": "fbs"
    })
    resp.raise_for_status()
    return {g["id"]: g for g in resp.json()}


def fetch_closing_lines(year, week):
    """
    In a real system you'd pull historical closing lines from a paid source.
    This uses the last saved 'current' odds file as a proxy.
    """
    path = f"data/spreads/current_week_{week}_{year}.json"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {g["home_team"] + g["away_team"]: g for g in json.load(f)}


def determine_ats_result(pick, actual_home_score, actual_away_score):
    """
    Returns 'win', 'loss', or 'push'.
    pick: dict with recommended_side, home_team, consensus_spread
    """
    if actual_home_score is None or actual_away_score is None:
        return "pending"

    spread = pick.get("consensus_spread") or 0
    home = pick["home_team"]
    away = pick["away_team"]
    side = pick.get("recommended_side")

    # Actual margin: positive = home won by X
    actual_margin = actual_home_score - actual_away_score

    if side == home:
        # Bet home team to cover (need to win by more than spread magnitude)
        covered_margin = actual_margin + spread  # spread is negative if home favored
        if covered_margin > 0:
            return "win"
        elif covered_margin < 0:
            return "loss"
        else:
            return "push"
    elif side == away:
        covered_margin = actual_margin + spread
        if covered_margin < 0:
            return "win"
        elif covered_margin > 0:
            return "loss"
        else:
            return "push"
    return "pending"


def calculate_clv(pick, closing_lines):
    """
    Closing Line Value: did we beat the closing spread?
    Positive CLV = we got a better number than the market settled at.
    """
    key = pick["home_team"] + pick["away_team"]
    close = closing_lines.get(key, {})
    closing_spread = close.get("consensus_home_spread")
    open_spread = pick.get("consensus_spread")

    if closing_spread is None or open_spread is None:
        return None

    side = pick.get("recommended_side")
    home = pick["home_team"]

    if side == home:
        # We want the spread to go higher (home more favored = we got value)
        clv = open_spread - closing_spread
    else:
        # We want the spread to go lower
        clv = closing_spread - open_spread

    return round(clv, 1)


def calculate_unit_pl(result, units):
    """Standard -110 juice: win 0.909 units, lose 1.0 unit."""
    if result == "win":
        return round(units * 0.909, 3)
    elif result == "loss":
        return -units
    elif result == "push":
        return 0
    return 0


def update_weights(all_picks, current_weights):
    """
    Simple adaptive weight update:
    - Look at last 8 weeks of results
    - If CLV is consistently positive, increase signal weights
    - If unit P&L is negative for 3+ weeks, flag for recalibration
    """
    recent_picks = [
        p for p in all_picks.get("picks", [])
        if p.get("status") == "settled" and p.get("result") != "pending"
    ]

    if len(recent_picks) < 10:
        return current_weights, False  # not enough data yet

    # CLV analysis
    clv_values = [p["clv"] for p in recent_picks if p.get("clv") is not None]
    avg_clv = sum(clv_values) / len(clv_values) if clv_values else 0

    # Unit P&L by week
    summaries = all_picks.get("weekly_summaries", [])
    settled = [s for s in summaries if s.get("status") == "settled"]
    last_3 = sorted(settled, key=lambda x: (x["year"], x["week"]))[-3:]
    losing_streak = all(s.get("unit_pl", 0) < 0 for s in last_3)

    new_weights = current_weights.copy()

    # Boost weights if CLV is strong (beating the close consistently)
    if avg_clv > 0.5:
        new_weights["sp_rating_diff"] = round(
            min(current_weights["sp_rating_diff"] * 1.05, 1.2), 3
        )
        new_weights["offense_epa_diff"] = round(
            min(current_weights["offense_epa_diff"] * 1.03, 9.0), 3
        )
    elif avg_clv < -0.5:
        new_weights["sp_rating_diff"] = round(
            max(current_weights["sp_rating_diff"] * 0.97, 0.5), 3
        )

    # Recalibration flag
    needs_recalibration = losing_streak and len(last_3) == 3

    new_weights["version"] = str(
        round(float(current_weights.get("version", "1.0")) + 0.1, 1)
    )
    new_weights["last_updated"] = datetime.utcnow().isoformat()
    new_weights["avg_clv_trailing"] = round(avg_clv, 3)

    return new_weights, needs_recalibration


def main():
    # Load all picks
    picks_path = "docs/data/all_picks.json"
    if not os.path.exists(picks_path):
        print("No picks file found.")
        return

    with open(picks_path) as f:
        all_picks = json.load(f)

    # Find pending picks
    pending = [p for p in all_picks["picks"] if p.get("status") == "pending"]
    if not pending:
        print("No pending picks to update.")
        return

    # Group by week/year
    weeks = {}
    for pick in pending:
        key = (pick["week"], pick["year"])
        weeks.setdefault(key, []).append(pick)

    for (week, year), picks in weeks.items():
        print(f"Updating results for Week {week}, {year}...")
        results = fetch_game_results(year, week)
        closing_lines = fetch_closing_lines(year, week)

        week_wins = week_losses = week_pushes = 0
        week_unit_pl = 0.0

        for pick in picks:
            gid = int(pick.get("game_id", 0))
            game_result = results.get(gid, {})

            home_score = game_result.get("home_points")
            away_score = game_result.get("away_points")

            if home_score is None:
                # Game not yet final
                continue

            result = determine_ats_result(pick, home_score, away_score)
            clv = calculate_clv(pick, closing_lines)
            unit_pl = calculate_unit_pl(result, pick.get("units", 1))

            pick["result"] = result
            pick["clv"] = clv
            pick["unit_pl"] = unit_pl
            pick["status"] = "settled"
            pick["home_final"] = home_score
            pick["away_final"] = away_score

            if result == "win":
                week_wins += 1
            elif result == "loss":
                week_losses += 1
            elif result == "push":
                week_pushes += 1
            week_unit_pl += unit_pl

        # Update weekly summary
        for summary in all_picks["weekly_summaries"]:
            if summary["week"] == week and summary["year"] == year:
                summary["wins"] = week_wins
                summary["losses"] = week_losses
                summary["pushes"] = week_pushes
                summary["unit_pl"] = round(week_unit_pl, 3)
                summary["status"] = "settled"
                break

    # Self-learning: update weights
    weights_path = "models/weights.json"
    current_weights = {}
    if os.path.exists(weights_path):
        with open(weights_path) as f:
            current_weights = json.load(f)

    from spread_model import load_weights
    current_weights = current_weights or load_weights()
    new_weights, needs_recal = update_weights(all_picks, current_weights)

    os.makedirs("models", exist_ok=True)
    with open(weights_path, "w") as f:
        json.dump(new_weights, f, indent=2)
    print(f"Weights updated to version {new_weights['version']}")

    if needs_recal:
        print("⚠️  RECALIBRATION TRIGGERED: 3+ consecutive losing weeks detected.")
        print("Consider reviewing model inputs and adjusting edge threshold.")

    # Save updated picks
    with open(picks_path, "w") as f:
        json.dump(all_picks, f, indent=2)

    # Update aggregate performance stats for dashboard
    all_settled = [p for p in all_picks["picks"] if p.get("status") == "settled"]
    total_wins = sum(1 for p in all_settled if p["result"] == "win")
    total_losses = sum(1 for p in all_settled if p["result"] == "loss")
    total_pushes = sum(1 for p in all_settled if p["result"] == "push")
    total_pl = sum(p.get("unit_pl", 0) for p in all_settled)
    clv_positive = sum(
        1 for p in all_settled if p.get("clv") and p["clv"] > 0
    )
    clv_total = sum(1 for p in all_settled if p.get("clv") is not None)

    stats = {
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_pushes": total_pushes,
        "total_unit_pl": round(total_pl, 2),
        "clv_positive_rate": round(clv_positive / max(clv_total, 1), 3),
        "win_rate": round(total_wins / max(total_wins + total_losses, 1), 3),
        "by_tier": {
            "3_unit": _tier_stats(all_settled, 3),
            "2_unit": _tier_stats(all_settled, 2),
            "1_unit": _tier_stats(all_settled, 1),
        },
        "weekly_summaries": all_picks["weekly_summaries"],
        "last_updated": datetime.utcnow().isoformat()
    }

    with open("docs/data/performance_stats.json", "w") as f:
        json.dump(stats, f, indent=2)
    print("Performance stats saved to docs/data/performance_stats.json")


def _tier_stats(picks, units):
    tier = [p for p in picks if p.get("units") == units]
    wins = sum(1 for p in tier if p["result"] == "win")
    losses = sum(1 for p in tier if p["result"] == "loss")
    pl = sum(p.get("unit_pl", 0) for p in tier)
    return {
        "wins": wins, "losses": losses,
        "unit_pl": round(pl, 2),
        "win_rate": round(wins / max(wins + losses, 1), 3)
    }


if __name__ == "__main__":
    main()
