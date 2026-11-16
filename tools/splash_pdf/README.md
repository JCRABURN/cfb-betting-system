# Splash pick-page PDF -> slate + picks

SplashSports "print" PDFs of the pick page (one per week) carry BOTH the pool's full slate of frozen
spreads/totals AND the user's picks: the chosen option has a dark outline and the confidence points sit
in the Pts box on that row. The PDFs have a text layer for games/lines/points but the *selection* is
purely visual, so the pipeline reads text positions and measures button-outline darkness.

    # one-time, in this folder (NOT part of requirements.txt; Node-only, used ad hoc):
    npm install pdfjs-dist@3.11.174 @napi-rs/canvas

    node extract.js "Week 7.pdf" json/w7.json        # per PDF -> text items + outline darkness per points digit
    SPLASH_JSON_DIR=json python parse_splash.py parsed.json 7

`render.js "<pdf>" <outPrefix> [scale]` writes PNGs, for eyeballing a page.

## How it was validated (2026-11-16, Weeks 1-11)

* Every week yields exactly 5 points digits; outline contrast is 17 vs ~209 (no ambiguous selections).
* Parsed slates equal the independently-loaded slates for Weeks 7-11: 300 of 300 games, 0 disagreements.
* The 20 picks for Weeks 8-11 equal the picks the user reported in chat: 20 of 20.
* Spot-checked against the rendered page images (e.g. Week 3 Miami -20.5; Week 6 Vanderbilt +10.5, SDSU/ORST Under 55.5).

The hard-coded geometry (AWAY/HOME/PTS column x-ranges, in PDF points on a Letter page) matches
Splash's current layout; if the pool UI changes, re-measure from `render.js` output before trusting a run.
