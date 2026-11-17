"""Slate-only parser for a BLANK Splash pick-page print (no picks marked), e.g. Week 12 2026.

That print has different geometry and splits text into fragments (`4`,`-`,`6 ·`,`+`,`14.5`), so
parse_splash.py finds 0 games in it. This joins fragments per line and reads header/team/line/total rows.
It reads no picks. usage: python parse_blank_slate.py <extract.js json> <out.csv>
"""
import csv, json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
HDR = re.compile(r"^([A-Z0-9&'\.\-]+) @ ([A-Z0-9&'\.\-]+)$")
ROW = re.compile(r"^(.+?) (\d+-\d+) · ([+-]?\d+(?:\.\d+)?|PK) (.+?) (\d+-\d+) · ([+-]?\d+(?:\.\d+)?|PK)$")
OU = re.compile(r"^Over (\d+(?:\.\d+)?) Under (\d+(?:\.\d+)?)$")
games, cur = [], None
for pg in d["pages"]:
    lines = {}
    for it in pg["items"]:
        if it["x"] > 400:   # kickoff / pts column
            continue
        lines.setdefault(round(it["y"]), []).append(it)
    for y in sorted(lines):
        its = sorted(lines[y], key=lambda i: i["x"])
        s = ""
        prev_end = None
        for it in its:
            if prev_end is not None and it["x"] - prev_end > 1.5 and not s.endswith(" "):
                s += " "
            s += it["s"]
            prev_end = it["x"] + it.get("w", len(it["s"]) * 4.5)
        s = re.sub(r"\s+", " ", s).strip()
        s = re.sub(r"(\d) ?- ?(\d) ?·", r"\1-\2 ·", s)
        s = re.sub(r"· ?([+-]) ?(\d)", r"· \1\2", s)
        m = HDR.match(s)
        if m:
            cur = {"away_code": m[1], "home_code": m[2]}; games.append(cur); continue
        m = ROW.match(s)
        if m and cur is not None:
            # row text also contains the team code at the start of each pill; strip it
            a, h = m[1], m[4]
            if a.startswith(cur["away_code"] + " "): a = a[len(cur["away_code"]) + 1:]
            if h.startswith(cur["home_code"] + " "): h = h[len(cur["home_code"]) + 1:]
            cur.update(away=a, away_line=m[3], home=h, home_line=m[6]); continue
        m = OU.match(s)
        if m and cur is not None:
            cur.update(over=m[1], under=m[2])
probs = []
for g in games:
    if not all(k in g for k in ("away", "home", "away_line", "home_line", "over", "under")):
        probs.append(("incomplete", g)); continue
    if float(g["away_line"]) != -float(g["home_line"]): probs.append(("sign", g))
    if g["over"] != g["under"]: probs.append(("ou", g))
print(len(games), "games;", len(probs), "problems"); [print(p) for p in probs]
w = csv.writer(open(sys.argv[2], "w", newline=""))
w.writerow(["away_team", "home_team", "away_spread", "home_spread", "total", "source"])
for g in games:
    w.writerow([g.get("away"), g.get("home"), g.get("away_line"), g.get("home_line"), g.get("over"),
                "SplashSports pick-page PDF (blank print), tools/splash_pdf/parse_blank_slate.py"])
