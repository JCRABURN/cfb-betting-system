# CFB Spread Bet Recommendation System

## Project Goal
Build a college football against-the-spread (ATS) recommendation system.
End state: each week during the season, output ranked spread bets with
predicted edge vs. the market line, confidence level, and suggested unit size.

## Current Status
- Data pipeline exists and runs; no prediction model yet
- Currently expanding data ingestion before building the model

## Owner Context
- Owner is technical (finance/engineering background) but is not the one
  who wrote most of this code — always explain architectural decisions
  in plain terms and keep a running ARCHITECTURE.md up to date
- Budget for paid data: ~$10–30/month max. Prefer free sources
  (CollegeFootballData API is the backbone — it's free with a key)

## Data Sources (target state)
1. Betting lines / odds history — CFBD has historical lines free;
   The Odds API free tier (500 req/mo) for live lines
2. Advanced team metrics — SP+, EPA/play, success rate, havoc rate
   (all available via CFBD API)
3. Injuries / roster / transfer portal — no great free API; prefer
   scraping a reliable source with respectful rate limits, flag if
   a paid option is clearly better within budget
4. Weather — Open-Meteo (free, no key) using stadium lat/long

## Engineering Rules
- Python. Store data in SQLite (single file, easy to back up) unless
  there's a strong reason otherwise
- Every ingestion script must be idempotent (safe to re-run) and
  incremental (only fetch new data)
- API keys live in .env, never committed. Keep .env.example updated
- Write a smoke test for each data source that validates schema +
  row counts; runnable via `make test` or `pytest`
- Log every ingestion run (source, rows added, errors) to a runs table
- Before adding any new dependency, check if stdlib or an existing
  dep covers it

## Modeling Rules (for later phases)
- Backtest everything against closing lines; report ATS win % and
  ROI per bet flat-staked BEFORE any Kelly sizing
- Guard hard against lookahead bias — features must only use data
  available before kickoff
- This is a personal decision-support tool, not financial advice

## Workflow
- Work in phases. At the end of each phase, summarize what was built,
  what was verified, and what's next — then STOP and wait for approval