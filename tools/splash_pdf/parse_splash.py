"""Parse extract.js output (text positions + button-outline darkness) of Splash pick-page PDFs into
slates + the user's picks. See README.md in this folder for the full procedure and validation.

usage: SPLASH_JSON_DIR=<dir holding w{N}.json from extract.js> python parse_splash.py <out.json> [week ...]
"""
import json
import os
import re
import sys

JSON_DIR = os.environ.get("SPLASH_JSON_DIR", "json")
AWAY_MAX, HOME_MIN, HOME_MAX = 181, 182, 328
REC = re.compile(r"^\d+-\d+(?:-\d+)?\s*[·•\.]\s*(PK|EVEN|[+-]?\d+(?:\.\d+)?)\s*$")
HDR = re.compile(r"^[A-Z0-9&'\.\-]+\s@\s[A-Z0-9&'\.\-]+$")
OU = re.compile(r"^(Over|Under)\s+(\d+(?:\.\d+)?)$")


def to_num(s):
    return 0.0 if s in ("PK", "EVEN") else float(s)


def parse_week(week):
    d = json.load(open(os.path.join(JSON_DIR, f"w{week}.json"), encoding="utf-8"))
    games, problems = [], []
    allpicks = []
    for pg in d["pages"]:
        items = sorted(pg["items"], key=lambda i: (i["y"], i["x"]))
        headers = [i for i in items if HDR.match(i["s"]) and i["x"] < 100]
        headers.sort(key=lambda i: i["y"])
        for hi, h in enumerate(headers):
            y0 = h["y"]
            y1 = headers[hi + 1]["y"] if hi + 1 < len(headers) else 10 ** 6
            block = [i for i in items if y0 < i["y"] < y1 and i["x"] < 330]
            rec_items = [i for i in block if REC.match(i["s"])]
            ou_items = [i for i in block if OU.match(i["s"])]
            away_r = [i for i in rec_items if i["x"] <= AWAY_MAX]
            home_r = [i for i in rec_items if HOME_MIN <= i["x"] <= HOME_MAX]
            if len(away_r) != 1 or len(home_r) != 1:
                problems.append((week, pg["page"], h["s"], f"rec items away={len(away_r)} home={len(home_r)}"))
                continue
            row_y = away_r[0]["y"]
            row = [i for i in block if abs(i["y"] - row_y) < 4]
            away = sorted([i for i in row if i["x"] <= AWAY_MAX], key=lambda i: i["x"])
            home = sorted([i for i in row if HOME_MIN <= i["x"] <= HOME_MAX], key=lambda i: i["x"])
            if len(away) < 3 or len(home) < 3:
                problems.append((week, pg["page"], h["s"], f"row split away={len(away)} home={len(home)}"))
                continue
            al = to_num(REC.match(away_r[0]["s"]).group(1))
            hl = to_num(REC.match(home_r[0]["s"]).group(1))
            over = [OU.match(i["s"]) for i in ou_items if i["s"].startswith("Over")]
            under = [OU.match(i["s"]) for i in ou_items if i["s"].startswith("Under")]
            total = float(over[0].group(2)) if over else None
            if over and under and over[0].group(2) != under[0].group(2):
                problems.append((week, pg["page"], h["s"], "over/under numbers differ"))
            if abs(al + hl) > 1e-9:
                problems.append((week, pg["page"], h["s"], f"spreads not opposite {al} {hl}"))
            g = {"week": week, "page": pg["page"], "header": h["s"], "y": h["y"],
                 "away_code": away[0]["s"], "away": away[1]["s"], "home_code": home[0]["s"], "home": home[1]["s"],
                 "away_line": al, "home_line": hl, "total": total, "row_y": row_y,
                 "ou_y": ou_items[0]["y"] if ou_items else None}
            g["_block"] = (pg["page"], y0, y1)
            games.append(g)
        for p in pg["picks"]:
            allpicks.append({**p, "page": pg["page"]})
    picks = []
    for p in allpicks:
        cands = [g for g in games if g["page"] == p["page"] and g["_block"][1] < p["y"] < g["_block"][2]]
        if len(cands) != 1:
            problems.append((week, p["page"], "pick", f"digit {p['digit']} matched {len(cands)} games"))
            continue
        g = cands[0]
        is_total = g["ou_y"] is not None and abs(p["y"] - g["ou_y"]) < 4 and abs(p["y"] - g["row_y"]) > 8
        contrast = abs(p["leftDark"] - p["rightDark"])
        if contrast < 60:
            problems.append((week, p["page"], g["header"], f"weak outline contrast {contrast}"))
        left = p["leftDark"] < p["rightDark"]
        if is_total:
            side, num, market, team = ("over" if left else "under"), g["total"], "total", None
        else:
            side = "away" if left else "home"
            market = "spread"
            num = g["away_line"] if left else g["home_line"]
            team = g["away"] if left else g["home"]
        picks.append({"week": week, "points": p["digit"], "game": g["header"], "away": g["away"], "home": g["home"],
                      "market": market, "side": side, "team": team, "number": num, "contrast": contrast})
    for g in games:
        g.pop("_block", None)
    return games, picks, problems


if __name__ == "__main__":
    out_path = sys.argv[1]
    weeks = [int(a) for a in sys.argv[2:]] or range(1, 12)
    out = {}
    for w in weeks:
        games, picks, problems = parse_week(w)
        out[w] = {"games": games, "picks": picks, "problems": problems}
        print(f"week {w:2d}: {len(games):2d} games parsed, {len(picks)} picks, {len(problems)} problems")
        for pr in problems[:6]:
            print("     PROBLEM:", pr)
    json.dump(out, open(out_path, "w"), indent=1)
