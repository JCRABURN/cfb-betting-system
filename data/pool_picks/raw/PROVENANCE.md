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
