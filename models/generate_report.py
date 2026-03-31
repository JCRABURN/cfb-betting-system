"""
generate_report.py
Reads analysis output and generates:
  1. A markdown report (outputs/week_XX_report.md)
  2. A JSON file for the web dashboard (docs/data/week_XX.json)
  3. Updates the master picks log (docs/data/all_picks.json)
  4. Updates the performance tracker (tracker/performance_log.csv)
"""

import json
import os
import glob
import csv
from datetime import datetime


def load_latest_analysis():
    files = sorted(glob.glob("data/analysis/week_*.json"))
    if not files:
        return None
    with open(files[-1]) as f:
        return json.load(f)


def load_performance_log():
    path = "tracker/performance_log.csv"
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def load_all_picks():
    path = "docs/data/all_picks.json"
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"picks": [], "weekly_summaries": []}


def format_spread(team, spread):
    if spread is None:
        return f"{team} (line N/A)"
    sign = "+" if spread > 0 else ""
    return f"{team} {sign}{spread}"


def generate_markdown_report(data):
    week = data["week"]
    year = data["year"]
    games = data["games"]
    total_units = data.get("total_units", 0)

    qualified = [g for g in games if g.get("qualifies")]
    top5 = qualified[:5]
    all_bets = qualified
    notable_passes = [
        g for g in games
        if not g.get("qualifies") and g.get("edge") and g["edge"] >= 2.0
    ][:8]

    lines = [
        f"# 🏈 CFB Spread Betting Report — Week {week}, {year}",
        f"*Generated: {datetime.utcnow().strftime('%A, %B %d %Y at %I:%M %p UTC')}*",
        f"*Total Units Deployed: {total_units}/15*\n",
        "---\n",
        "## 🔥 Top 5 Games of the Week",
        "*Ranked by edge strength + confidence composite*\n",
    ]

    for i, game in enumerate(top5, 1):
        side = game.get("recommended_side", "N/A")
        consensus = game.get("consensus_spread")
        projected = game.get("projected_spread")
        edge = game.get("edge")
        units = game.get("units", 0)
        movement = game.get("line_movement")
        signals = game.get("confidence_signals", [])
        factors = game.get("key_factors", [])
        risk_flags = game.get("risk_flags", [])

        movement_str = (
            f"Moved {movement:+.1f} pts" if movement is not None else "Opening line"
        )
        signal_names = {
            "line_movement_agreement": "Sharp money aligned",
            "statistical_alignment": "SP+ & EPA agree",
            "favorable_conditions": "Good weather",
            "record_differential": "Meaningful record gap"
        }
        signal_display = " | ".join(
            signal_names.get(s, s) for s in signals
        )

        lines += [
            f"### {i}. {game['away_team']} @ {game['home_team']}",
            f"**Our Side:** {side}  ",
            f"**Spread:** {format_spread(game['home_team'], consensus)}  ",
            f"**Projected:** {format_spread(game['home_team'], projected)}  ",
            f"**Edge:** {edge:.1f} pts  ",
            f"**Units:** {'⭐' * units} ({units})  ",
            f"**Line Movement:** {movement_str}  ",
            f"**Signals:** {signal_display if signal_display else 'None'}",
            "",
        ]

        for factor in factors[:4]:
            lines.append(f"- {factor}")

        if risk_flags:
            for flag in risk_flags:
                lines.append(f"- ⚠️ {flag}")

        lines.append("")

    lines += [
        "---\n",
        "## 📋 Full Card — All Qualified Bets\n",
    ]

    for game in all_bets:
        side = game.get("recommended_side", "N/A")
        consensus = game.get("consensus_spread")
        edge = game.get("edge")
        units = game.get("units", 0)
        lines.append(
            f"- **{game['away_team']} @ {game['home_team']}** | "
            f"Take: {side} | Spread: {format_spread(game['home_team'], consensus)} | "
            f"Edge: {edge:.1f} | Units: {units}"
        )

    lines += [
        "\n---\n",
        "## ❌ Notable Passes\n",
    ]

    for game in notable_passes:
        edge = game.get("edge", 0)
        risk = game.get("risk_flags", ["Below edge threshold"])
        lines.append(
            f"- **{game['away_team']} @ {game['home_team']}**: "
            f"Edge {edge:.1f} — {risk[0] if risk else 'Threshold not met'}"
        )

    return "\n".join(lines)


def build_dashboard_json(data):
    """Build the JSON payload for the React dashboard."""
    week = data["week"]
    year = data["year"]
    games = data["games"]
    qualified = [g for g in games if g.get("qualifies")]

    return {
        "week": week,
        "year": year,
        "generated_at": datetime.utcnow().isoformat(),
        "total_units": data.get("total_units", 0),
        "top5": qualified[:5],
        "all_picks": qualified,
        "all_games_analyzed": len(games),
        "passes": [
            g for g in games
            if not g.get("qualifies") and g.get("edge") and g["edge"] >= 2.0
        ][:8]
    }


def main():
    data = load_latest_analysis()
    if not data:
        print("No analysis data found.")
        return

    week = data["week"]
    year = data["year"]

    # 1. Markdown report
    os.makedirs("outputs", exist_ok=True)
    md = generate_markdown_report(data)
    md_path = f"outputs/week_{week}_{year}_report.md"
    with open(md_path, "w") as f:
        f.write(md)
    print(f"Markdown report: {md_path}")

    # 2. Dashboard JSON for this week
    os.makedirs("docs/data", exist_ok=True)
    dashboard_payload = build_dashboard_json(data)
    week_json_path = f"docs/data/week_{week}_{year}.json"
    with open(week_json_path, "w") as f:
        json.dump(dashboard_payload, f, indent=2)
    print(f"Dashboard JSON: {week_json_path}")

    # 3. Update all_picks master log
    all_picks = load_all_picks()
    existing_weeks = {s["week"] for s in all_picks.get("weekly_summaries", [])}

    if week not in existing_weeks:
        # Add picks
        for pick in dashboard_payload["all_picks"]:
            pick["status"] = "pending"
            pick["result"] = None
            pick["clv"] = None
            all_picks["picks"].append(pick)

        # Add weekly summary stub
        all_picks["weekly_summaries"].append({
            "week": week,
            "year": year,
            "total_picks": len(dashboard_payload["all_picks"]),
            "total_units": dashboard_payload["total_units"],
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "unit_pl": 0.0,
            "status": "pending"
        })

    with open("docs/data/all_picks.json", "w") as f:
        json.dump(all_picks, f, indent=2)
    print("Updated docs/data/all_picks.json")

    # 4. Update weeks index (for dashboard navigation)
    weeks_index_path = "docs/data/weeks_index.json"
    existing_index = []
    if os.path.exists(weeks_index_path):
        with open(weeks_index_path) as f:
            existing_index = json.load(f)

    week_entry = {
        "week": week,
        "year": year,
        "file": f"week_{week}_{year}.json",
        "picks_count": len(dashboard_payload["all_picks"]),
        "units": dashboard_payload["total_units"]
    }
    existing_index = [e for e in existing_index if e["week"] != week]
    existing_index.append(week_entry)
    existing_index.sort(key=lambda x: (x["year"], x["week"]), reverse=True)

    with open(weeks_index_path, "w") as f:
        json.dump(existing_index, f, indent=2)
    print("Updated weeks_index.json")


if __name__ == "__main__":
    main()
