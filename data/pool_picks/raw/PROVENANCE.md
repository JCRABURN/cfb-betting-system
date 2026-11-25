# Provenance: Week 8-10 pool slates

`week_8_2026_splashsports_slate.csv`, `week_9_...`, `week_10_...` were NOT supplied
directly. They were read (read-only; nothing in that repo was modified) from a
separate repository, `JCRABURN/cfb-betting-system-v3`, commit
`d3ba84db74b67a6e4dd8d61e49f28c0759d3a34e` (2026-11-10), files
`data/ingest/2026-week{8,9,10}-splashsports-screen-capture.csv`, byte-for-byte.

What they are: transcriptions of SplashSports screenshots taken *after* the
Tuesday-noon freeze (Oct 21, Oct 27, Nov 4). The pool displays its frozen
numbers, so they should equal the locked lines, but that repo's own README is
explicit that they are "not independently timestamp-qualified Tuesday-market
locks", the screenshot time zone is unverified, and the original image files
are not archived there.

Validation performed before loading (2026-11-16):
- Compared against that repo's independent screenshot-review copies: **0**
  numeric disagreements across all matched games in all three weeks (only team
  name spellings differ).
- Spreads are exact opposites and every game has a total.
- **15 of 15** pick numbers the user reported for these weeks (their submitted
  spreads/totals) are reproduced exactly by these slates.
- Team names resolved to CFBD games with two added aliases (Connecticut -> UConn,
  Miami OH -> Miami (OH)); 174 games, 0 unmatched, 0 conflicts.

Extra columns in these files (status, final_away, final_home, source) are ignored
by the ingester; final scores come from CFBD, never from these files.

# Provenance: Weeks 1-6 slate files derived from Splash PDFs (added 2026-11-16)

`week_{3,4,5,6}_2026_splashsports_slate.csv` and `week_{1,2}_2026_splashsports_slate_FROM_PDF.csv` were
parsed from the user's Splash pick-page PDFs (`Splash.zip`, printed 2026-11-16, one per week) by
`tools/splash_pdf/`. The PDFs are Splash's own display of the frozen lines. Validated against the
independently loaded slates for Weeks 7-11 (300 of 300 games identical) before use.

The `_FROM_PDF` files for Weeks 1-2 are CORRECTIONS to the earlier hand-transcribed slates (Week 1: 22 games
with a wrong spread and/or total; Week 2: one flipped spread sign). They are named so the ingester does
not auto-discover them as new slates; they were applied through the audited
`ingest_pool_slate.py --correct --reason` path, which archives every original in `pool_lines_corrections`.

# Provenance: Week 12 slate (added 2026-11-17)

`week_12_2026_splashsports_slate.csv` was parsed from the user's Splash pick-page PDF printed
2026-11-17 14:22 CST ("Week 12 · Entry 1", blank: no picks marked). That print splits text into
fragments and uses different page geometry, so the validated `parse_splash.py` found 0 games; it was read
with `tools/splash_pdf/parse_blank_slate.py` instead. Checks: 66 games, 0 parse problems, every away spread
is the exact opposite of its home spread, Over and Under numbers agree on every game, and all 66 rows were
compared by eye with the PDF's text. All 132 team names resolve to CFBD school names (5 via existing aliases:
UMass, Louisiana-Monroe, Appalachian State, Miami (FL), San Jose State). Week 12 games were not yet in the DB
when this was written, so game matching happens when the midweek workflow ingests the file.

# Provenance: Week 13 slate (added 2026-11-25)

`week_13_2026_splashsports_slate.csv` was parsed from the user's Splash pick-page PDF printed
2026-11-25 08:39 ("Week 13 · Entry 1", blank: no picks marked) with `tools/splash_pdf/parse_blank_slate.py`.
Text was extracted with pdfjs 5.6 (text only), not `extract.js`, whose pdfjs 3 packages were not installed.
Checks: 69 games (matching the page-by-page count), 0 parse problems, rows reviewed against the PDF's text.
On a scratch copy all 69 games matched CFBD games; against a 2026-11-25 market pull, no spread or total on the
games with a market line differed by more than 3.0 points.
