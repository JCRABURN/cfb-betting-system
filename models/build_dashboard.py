"""
build_dashboard.py
Renders docs/index.html (published via GitHub Pages) from the current DB
state: Market Movement and Pool Drift as the headline sections, Model
Picks (EPA-only) as a clearly-labeled secondary section with its
no-demonstrated-edge disclaimer, and a Season Ledger that only shows real
numbers once real picks have been graded -- no backtest numbers stand in
for it (ARCHITECTURE.md holds those; a page that gets shared does not).

Freshness, not just content: each of the four cadence checkpoints (Tuesday
card, Thursday/Saturday drift views, Monday audit) is checked against
ingestion_runs before rendering. A checkpoint whose latest run errored, or
hasn't run recently enough for its own cadence, is rendered with an
explicit stale banner INSIDE the section it feeds -- never silently
replaced by older data presented as current. Building a section's data is
wrapped so one section's unexpected failure can't prevent the others (or
the page itself) from being generated and published; genuine hard
failures (can't determine the current week at all, can't open the DB)
are NOT caught here and are expected to crash this script, per the same
fail-loudly discipline as fetch_stats.get_current_week().

Reuses card_generator.build_card / gambling_view.build_gambling_view /
pool_view.build_pool_view / pool_view.load_pool_entries directly (not
their CLI wrappers, and not their JSON output files) -- always computed
fresh from the current DB state at render time.
"""

import json
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "data"))
import db
import fetch_stats
import card_generator
import gambling_view
import pool_view

# How stale a checkpoint's last SUCCESSFUL run can be before it's flagged,
# even without an explicit error -- catches "this simply never ran again"
# (a broken cron, a renamed secret) that a pure status check would miss.
# Generous on purpose: these are heuristics for "does this look abandoned,"
# not precise cadence enforcement.
FRESHNESS_WINDOW_DAYS = {
    "cfbd_stats": 8, "odds_api": 4, "card_generator": 8,
    "gambling_view": 4, "pool_view": 4, "post_game_audit": 8,
}


def get_source_health(conn, source, now=None):
    """Latest ingestion_runs row for `source`, classified as one of:
    'pending' (never run), 'stale' (last run errored, or last SUCCESS is
    older than FRESHNESS_WINDOW_DAYS), or 'ok'."""
    now = now or datetime.utcnow()
    row = conn.execute(
        "SELECT started_at, finished_at, status, error FROM ingestion_runs "
        "WHERE source = ? ORDER BY id DESC LIMIT 1", (source,),
    ).fetchone()
    if row is None:
        return {"state": "pending", "finished_at": None, "error": None}

    started_at, finished_at, status, error = row
    if status == "error":
        return {"state": "stale", "finished_at": finished_at, "error": error}

    window = FRESHNESS_WINDOW_DAYS.get(source, 8)
    age_ok = True
    if finished_at:
        try:
            age_ok = (now - datetime.fromisoformat(finished_at)) <= timedelta(days=window)
        except ValueError:
            age_ok = True  # unparseable timestamp -- don't manufacture a false stale flag
    if not age_ok:
        return {"state": "stale", "finished_at": finished_at, "error": "no successful run recently enough"}

    return {"state": "ok", "finished_at": finished_at, "error": None}


def build_season_ledger(conn, season):
    """Record/ROI/CLV/hook count from every SETTLED live pick this season.
    Returns None if nothing has been graded yet -- the caller renders the
    honest empty state for that, never a placeholder number."""
    rows = conn.execute(
        "SELECT result, unit_pl, clv, key_factors FROM picks "
        "WHERE year = ? AND pick_type = 'live' AND status = 'settled'",
        (season,),
    ).fetchall()
    if not rows:
        return None

    wins = sum(1 for r in rows if r[0] == "win")
    losses = sum(1 for r in rows if r[0] == "loss")
    pushes = sum(1 for r in rows if r[0] == "push")
    decided = wins + losses
    ats_pct = wins / decided if decided else None
    total_pl = sum(r[1] for r in rows if r[1] is not None)
    roi = total_pl / len(rows) if rows else None
    clv_values = [r[2] for r in rows if r[2] is not None]
    avg_clv = sum(clv_values) / len(clv_values) if clv_values else None
    hooks = sum(1 for r in rows if r[3] and "hook" in json.loads(r[3]))

    return {
        "n": len(rows), "wins": wins, "losses": losses, "pushes": pushes,
        "ats_pct": ats_pct, "roi": roi, "avg_clv": avg_clv, "hooks": hooks,
    }


def _try_build(label, fn, *args):
    """Runs one section's build call, catching any exception so a single
    section's failure can't take down the whole page. Returns (data, error)."""
    try:
        return fn(*args), None
    except Exception as e:
        print(f"WARNING: {label} failed to build: {e}")
        return None, str(e)


# ---------------------------------------------------------------------------
# Rendering -- plain string templates, no external templating dependency
# (matches this project's "check stdlib/existing deps first" rule; the page
# is simple enough not to need one).
# ---------------------------------------------------------------------------

CSS = """
:root {
  --ink: #12181a; --ink-soft: #3a4245; --chalk: #e7e9e2; --panel: #ffffff;
  --line: rgba(18, 24, 26, 0.14); --accent: #b9812a; --accent-strong: #96691f;
  --good: #3f7a52; --good-bg: rgba(63, 122, 82, 0.12);
  --bad: #a84a32; --bad-bg: rgba(168, 74, 50, 0.12);
  --neutral-bg: rgba(18, 24, 26, 0.06);
  --text: var(--ink); --text-soft: var(--ink-soft); --bg: var(--chalk); --surface: var(--panel);
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --text: #e9ebe4; --text-soft: #aab0a8; --bg: #12181a; --surface: #182022;
    --line: rgba(233, 235, 228, 0.14); --accent: #d6a34a; --accent-strong: #eab766;
    --good: #6cbb85; --good-bg: rgba(108, 187, 133, 0.14);
    --bad: #e08064; --bad-bg: rgba(224, 128, 100, 0.14);
    --neutral-bg: rgba(233, 235, 228, 0.07); color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --text: #e9ebe4; --text-soft: #aab0a8; --bg: #12181a; --surface: #182022;
  --line: rgba(233, 235, 228, 0.14); --accent: #d6a34a; --accent-strong: #eab766;
  --good: #6cbb85; --good-bg: rgba(108, 187, 133, 0.14);
  --bad: #e08064; --bad-bg: rgba(224, 128, 100, 0.14);
  --neutral-bg: rgba(233, 235, 228, 0.07); color-scheme: dark;
}
:root[data-theme="light"] {
  --text: var(--ink); --text-soft: var(--ink-soft); --bg: var(--chalk); --surface: var(--panel);
  --line: rgba(18, 24, 26, 0.14); --accent: #b9812a; --accent-strong: #96691f;
  --good: #3f7a52; --good-bg: rgba(63, 122, 82, 0.12);
  --bad: #a84a32; --bad-bg: rgba(168, 74, 50, 0.12);
  --neutral-bg: rgba(18, 24, 26, 0.06); color-scheme: light;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text); font-family: Georgia, "Iowan Old Style", serif; font-size: 16px; line-height: 1.5; }
.page { max-width: 920px; margin: 0 auto; padding: 28px 20px 80px; }
.display { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-weight: 800; letter-spacing: -0.01em; text-wrap: balance; }
.eyebrow { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-weight: 700; font-size: 11.5px; letter-spacing: 0.11em; text-transform: uppercase; color: var(--text-soft); }
.mono { font-family: ui-monospace, "SF Mono", "Cascadia Mono", Consolas, "Roboto Mono", monospace; font-variant-numeric: tabular-nums; }
.masthead { display: flex; justify-content: space-between; align-items: flex-end; gap: 20px; flex-wrap: wrap; padding-bottom: 20px; border-bottom: 2px solid var(--ink); }
.masthead h1 { margin: 0 0 6px; font-size: 30px; line-height: 1.05; }
.masthead .sub { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 13.5px; color: var(--text-soft); max-width: 46ch; }
.masthead-meta { text-align: right; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }
.masthead-meta .week { font-size: 13px; font-weight: 700; color: var(--accent-strong); letter-spacing: 0.04em; text-transform: uppercase; }
.masthead-meta .synced { font-size: 12px; color: var(--text-soft); margin-top: 3px; }
.health { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 22px 0 8px; }
.health-chip { border: 1px solid var(--line); border-radius: 3px; padding: 10px 12px; background: var(--surface); }
.health-chip .day { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-soft); display: block; margin-bottom: 5px; }
.health-chip .state { display: flex; align-items: center; gap: 6px; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 13px; font-weight: 700; }
.health-chip .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.health-chip.ok .dot { background: var(--good); } .health-chip.ok .state { color: var(--good); }
.health-chip.stale .dot { background: var(--bad); } .health-chip.stale .state { color: var(--bad); }
.health-chip.pending .dot { background: var(--text-soft); } .health-chip.pending .state { color: var(--text-soft); }
.health-chip .ts { margin-top: 4px; font-size: 11.5px; color: var(--text-soft); }
.stale-banner { display: flex; align-items: flex-start; gap: 10px; background: var(--bad-bg); border: 1px solid var(--bad); border-radius: 3px; padding: 12px 14px; margin: 4px 0 18px; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 13px; color: var(--text); }
.stale-banner strong { color: var(--bad); }
.panel { padding: 34px 0 8px; border-top: 1px solid var(--line); margin-top: 34px; }
.panel:first-of-type { margin-top: 30px; }
.panel-head { display: flex; justify-content: space-between; align-items: baseline; gap: 14px; flex-wrap: wrap; margin-bottom: 6px; }
.panel-head .eyebrow.headline { color: var(--accent-strong); }
.panel h2 { margin: 2px 0 4px; font-size: 22px; }
.panel .dek { margin: 0 0 18px; font-size: 14.5px; color: var(--text-soft); max-width: 62ch; }
.panel .asof { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 11.5px; color: var(--text-soft); white-space: nowrap; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
thead th { text-align: left; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 11px; font-weight: 700; letter-spacing: 0.07em; text-transform: uppercase; color: var(--text-soft); padding: 0 10px 8px 0; border-bottom: 1px solid var(--line); }
tbody td { padding: 9px 10px 9px 0; border-bottom: 1px solid var(--line); vertical-align: middle; }
tbody tr:last-child td { border-bottom: none; }
td.num, th.num { text-align: right; }
.matchup { white-space: nowrap; } .matchup .away { color: var(--text-soft); } .matchup .at { color: var(--text-soft); padding: 0 4px; }
.pill { display: inline-flex; align-items: center; gap: 5px; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 11.5px; font-weight: 700; padding: 3px 9px; border-radius: 999px; white-space: nowrap; }
.pill.toward { background: var(--good-bg); color: var(--good); }
.pill.away { background: var(--bad-bg); color: var(--bad); }
.pill.flat { background: var(--neutral-bg); color: var(--text-soft); }
.pill.flip { background: transparent; border: 1px solid var(--bad); color: var(--bad); }
.pill.low-conf { background: transparent; border: 1px solid var(--text-soft); color: var(--text-soft); }
.pill.standard { background: var(--neutral-bg); color: var(--text-soft); }
.panel.secondary { background: var(--neutral-bg); margin-left: -20px; margin-right: -20px; padding-left: 20px; padding-right: 20px; }
.panel.secondary .eyebrow { color: var(--text-soft); }
.disclaimer { display: flex; gap: 12px; border: 1px solid var(--bad); background: var(--bad-bg); border-radius: 3px; padding: 14px 16px; margin: 4px 0 22px; }
.disclaimer .mark { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-weight: 800; color: var(--bad); font-size: 13px; flex: none; padding-top: 1px; }
.disclaimer p { margin: 0; font-size: 13.5px; color: var(--text); max-width: 64ch; }
.disclaimer p + p { margin-top: 6px; }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1px; background: var(--line); border: 1px solid var(--line); margin-bottom: 24px; }
.stat-cell { background: var(--surface); padding: 16px 16px 14px; }
.stat-cell .label { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; color: var(--text-soft); display: block; margin-bottom: 6px; }
.stat-cell .value { font-size: 26px; font-weight: 800; font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; }
.stat-cell .value.good { color: var(--good); } .stat-cell .value.bad { color: var(--bad); }
.stat-cell .note { font-size: 11.5px; color: var(--text-soft); margin-top: 4px; }
.empty-state { border: 1px dashed var(--line); border-radius: 3px; padding: 22px; text-align: center; color: var(--text-soft); font-size: 13.5px; }
.ref-note { font-size: 12px; color: var(--text-soft); margin-top: 10px; }
footer { margin-top: 50px; padding-top: 20px; border-top: 2px solid var(--ink); font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif; font-size: 12px; color: var(--text-soft); display: flex; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
footer .theme-toggle { display: inline-flex; border: 1px solid var(--line); border-radius: 999px; overflow: hidden; }
footer .theme-toggle button { appearance: none; border: none; background: transparent; color: var(--text-soft); padding: 5px 12px; font: inherit; font-size: 12px; cursor: pointer; }
footer .theme-toggle button.active { background: var(--ink); color: var(--bg); }
@media (max-width: 620px) { .health { grid-template-columns: repeat(2, 1fr); } .stat-row { grid-template-columns: repeat(2, 1fr); } .masthead { align-items: flex-start; } .masthead-meta { text-align: left; } }
"""

JS = """
function setTheme(mode, btn) {
  if (mode) { document.documentElement.setAttribute('data-theme', mode); }
  else { document.documentElement.removeAttribute('data-theme'); }
  btn.parentElement.querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
  btn.classList.add('active');
}
"""


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _fmt_ts(iso_ts):
    """Portable format (no platform-specific strftime flags like %-d, which
    Windows' C runtime rejects -- this must render identically whether it's
    tested locally or run on GitHub Actions' ubuntu-latest)."""
    if not iso_ts:
        return "never"
    try:
        return datetime.fromisoformat(iso_ts).strftime("%b %d, %H:%M UTC")
    except ValueError:
        return iso_ts


def _health_chip_html(day_label, health):
    state = health["state"]
    label = {"ok": "Updated", "stale": "Stale", "pending": "Pending"}[state]
    ts = _fmt_ts(health["finished_at"]) if health["finished_at"] else "Not yet run"
    return (
        f'<div class="health-chip {state}"><span class="day">{_esc(day_label)}</span>'
        f'<span class="state"><span class="dot"></span>{label}</span>'
        f'<div class="ts mono">{_esc(ts)}</div></div>'
    )


def _stale_banner_html(section_label, health):
    if health["state"] != "stale":
        return ""
    reason = health["error"] or "no successful run recently enough"
    return (
        f'<div class="stale-banner"><span class="mark">&#9888;</span>'
        f'<span><strong>{_esc(section_label)} may be out of date</strong> '
        f'(last successful update: {_esc(_fmt_ts(health["finished_at"]))} &mdash; {_esc(reason)}). '
        f'Showing the most recent data available, held over rather than hidden.</span></div>'
    )


def _market_movement_rows(gambling):
    if not gambling or not gambling["games"]:
        return '<tr><td colspan="5"><div class="empty-state">No lines yet this week.</div></td></tr>'
    rows = []
    for g in gambling["games"]:
        flag = ' <span class="pill flip" title="Latest line came from a different book than the opener">book mismatch</span>' if not g["same_book_match"] else ""
        rows.append(
            f'<tr><td class="matchup"><span class="away">{_esc(g["away_team"])}</span>'
            f'<span class="at">@</span>{_esc(g["home_team"])}</td>'
            f'<td class="num mono">{g["opening_home_spread"]:+.1f}</td>'
            f'<td class="num mono">{g["latest_home_spread"]:+.1f}</td>'
            f'<td class="mono">{g["magnitude"]:.1f} pts &rarr; {g["direction"].replace("toward_", "")}</td>'
            f'<td class="num mono">{_esc(g["latest_book"])}{flag}</td></tr>'
        )
    return "\n".join(rows)


def _pool_drift_rows(pool):
    if not pool or not pool["games"]:
        return None  # caller renders a dedicated empty state with setup instructions
    rows = []
    for g in pool["games"]:
        pill_class = {"toward_pick": "toward", "away_from_pick": "away", "flat": "flat"}[g["movement_vs_pick"]]
        pill_label = {"toward_pick": "toward pick", "away_from_pick": "away from pick", "flat": "flat"}[g["movement_vs_pick"]]
        flip = ' <span class="pill flip">flipped</span>' if g["favorite_flipped"] else ""
        rows.append(
            f'<tr><td class="matchup"><span class="away">{_esc(g["away_team"])}</span>'
            f'<span class="at">@</span>{_esc(g["home_team"])}</td>'
            f'<td>{_esc(g["picked_side"])}</td>'
            f'<td class="num mono">{g["pool_home_spread"]:+.1f}</td>'
            f'<td class="num mono">{g["live_home_spread"]:+.1f}</td>'
            f'<td><span class="pill {pill_class}">{pill_label}, {abs(g["signed_drift_vs_pick"]):.1f} pts</span>{flip}</td></tr>'
        )
    return "\n".join(rows)


_CONFIDENCE_DISPLAY = {
    "low_confidence_large_edge": ("low-conf", "low confidence"),
    "low_confidence_prior_season_data": ("low-conf", "prior-season data"),
    "standard": ("standard", "standard"),
}


def _model_picks_rows(card):
    if not card or not card["games"]:
        return '<tr><td colspan="4"><div class="empty-state">No card generated yet this week.</div></td></tr>'
    rows = []
    for g in card["games"]:
        conf_class, conf_label = _CONFIDENCE_DISPLAY[g["confidence"]]
        prior_season_note = (
            ' <span class="pill flip" title="Uses prior-season final EPA -- no in-season data exists yet (Week 1)">week 1</span>'
            if g.get("uses_prior_season_data") else ""
        )
        rows.append(
            f'<tr><td class="matchup"><span class="away">{_esc(g["away_team"])}</span>'
            f'<span class="at">@</span>{_esc(g["home_team"])}</td>'
            f'<td>{_esc(g["side"])}</td>'
            f'<td class="num mono">{g["edge"]:.1f}</td>'
            f'<td class="num"><span class="pill {conf_class}">{conf_label}</span>{prior_season_note}</td></tr>'
        )
    return "\n".join(rows)


def _prior_season_banner_html(card):
    """Explicit, visible callout -- not just the per-row pill -- per
    MODEL_DESIGN.md §6: week 1 games use prior-season-final EPA (roster
    turnover, transfer portal, a full offseason of change since the data
    was current) as a fallback input, and that must be obvious at a
    glance, not something a viewer has to notice row by row."""
    if not card or not card.get("flagged_prior_season_data"):
        return ""
    n = len(card["flagged_prior_season_data"])
    return (
        '<div class="stale-banner"><span class="mark">&#9888;</span>'
        f'<span><strong>{n} pick{"s" if n != 1 else ""} this week use{"s" if n == 1 else ""} prior-season EPA</strong> '
        "-- no in-season data can exist yet this early (Week 1), so these use last season's final numbers "
        "as a fallback input (MODEL_DESIGN.md &sect;6). Confidence is capped accordingly, not standard.</span></div>"
    )


def render_dashboard(season, week, card, gambling, pool, ledger, healths, generated_at):
    # Literal "·" here, not the &middot; entity -- _health_chip_html() runs
    # day_label through _esc(), which would escape the entity's own "&"
    # into "&amp;middot;" and render literally instead of as a dot (the bug
    # this fixed). A raw Unicode character passes through _esc() untouched
    # since it only escapes &, <, > -- and docs/index.html is UTF-8.
    tue = _health_chip_html("Tue · Card", healths["card_generator"])
    thu = _health_chip_html("Thu/Sat · Drift", healths["gambling_view"])
    sat = _health_chip_html("Sat · Pool", healths["pool_view"])
    mon = _health_chip_html("Mon · Audit", healths["post_game_audit"])

    market_banner = _stale_banner_html("Market Movement", healths["gambling_view"])
    pool_banner = _stale_banner_html("Pool Drift", healths["pool_view"])
    model_banner = _stale_banner_html("Model Picks", healths["card_generator"])
    prior_season_banner = _prior_season_banner_html(card)

    pool_rows = _pool_drift_rows(pool)
    if pool_rows is None:
        pool_body = (
            '<div class="empty-state">No pool picks entered for this week yet &mdash; '
            "run <code>models/pool_view.py</code> after committing "
            f"<code>data/pool_picks/week_{week}_{season}.csv</code>.</div>"
        )
    else:
        pool_body = (
            '<div class="table-wrap"><table><thead><tr><th>Game</th><th>Picked</th>'
            '<th>Pool line</th><th>Live line</th><th>Since you picked</th></tr></thead>'
            f"<tbody>{pool_rows}</tbody></table></div>"
        )

    if ledger is None:
        ledger_body = (
            '<div class="empty-state">No games graded yet this season &mdash; the first Monday audit '
            "runs after Week 1 and will populate Record (ATS), ROI, Avg CLV, and hook losses here, "
            "game by game.</div>"
        )
    else:
        ats_pct = f'{ledger["ats_pct"] * 100:.1f}%' if ledger["ats_pct"] is not None else "n/a"
        roi_pct = ledger["roi"] * 100 if ledger["roi"] is not None else None
        roi_class = "good" if (roi_pct or 0) >= 0 else "bad"
        roi_str = f"{roi_pct:+.1f}%" if roi_pct is not None else "n/a"
        clv_str = f'{ledger["avg_clv"]:+.2f}' if ledger["avg_clv"] is not None else "n/a"
        ledger_body = (
            '<div class="stat-row">'
            f'<div class="stat-cell"><span class="label">Record (ATS)</span>'
            f'<span class="value mono">{ledger["wins"]}&ndash;{ledger["losses"]}&ndash;{ledger["pushes"]}</span>'
            f'<div class="note">{ats_pct}</div></div>'
            f'<div class="stat-cell"><span class="label">ROI</span>'
            f'<span class="value mono {roi_class}">{roi_str}</span>'
            '<div class="note">flat-staked, &minus;110 juice</div></div>'
            f'<div class="stat-cell"><span class="label">Avg CLV</span>'
            f'<span class="value mono">{clv_str}</span>'
            '<div class="note">pts vs. closing line</div></div>'
            f'<div class="stat-cell"><span class="label">Hook losses</span>'
            f'<span class="value mono">{ledger["hooks"]}</span>'
            '<div class="note">decided by half a point</div></div>'
            "</div>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CFB Line &amp; Pool Monitor</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">

  <div class="masthead">
    <div>
      <h1 class="display">CFB Line &amp; Pool Monitor</h1>
      <p class="sub">Market movement and pool drift first &mdash; the model's picks are reference-only, never sharp. Personal decision support, not advice.</p>
    </div>
    <div class="masthead-meta">
      <div class="week">Week {week} &middot; {season}</div>
      <div class="synced">Generated {_esc(_fmt_ts(generated_at))}</div>
    </div>
  </div>

  <div class="health">{tue}{thu}{sat}{mon}</div>

  <section class="panel">
    <div class="panel-head">
      <div><div class="eyebrow headline">Headline &middot; Market Read</div><h2 class="display">Market Movement</h2></div>
      <div class="asof mono">opening &rarr; latest, same book</div>
    </div>
    <p class="dek">Every lined game, opening line vs. the latest number from the <em>same</em> book. No model involved: this is what the market itself has done.</p>
    {market_banner}
    <div class="table-wrap"><table><thead><tr><th>Game</th><th>Open</th><th>Latest</th><th>Movement</th><th class="num">Book</th></tr></thead>
    <tbody>{_market_movement_rows(gambling)}</tbody></table></div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <div><div class="eyebrow headline">Headline &middot; Your Pool</div><h2 class="display">Pool Drift</h2></div>
      <div class="asof mono">vs. entered pool line</div>
    </div>
    <p class="dek">Your locked picks against the pool's own number, compared to where the market sits now. Sorted worst-for-your-pick first.</p>
    {pool_banner}
    {pool_body}
  </section>

  <section class="panel secondary">
    <div class="panel-head">
      <div><div class="eyebrow">Reference Only &middot; Not a Recommendation</div><h2 class="display">Model Picks &mdash; EPA-Only Baseline</h2></div>
      <div class="asof mono">fit on prior seasons</div>
    </div>
    {model_banner}
    <div class="disclaimer"><span class="mark">&#9888;</span><div>
      <p><strong>This baseline has not demonstrated a betting edge over the market.</strong> Six independent tests &mdash; three candidate features, three structural fixes for its worst bucket &mdash; were all rejected against a pre-registered bar. Every edge bucket sits at or below the ~52.4% break-even line.</p>
      <p>The picks below are shown for reference, not as a recommendation. Large-edge picks are flagged low-confidence, not high &mdash; the model's biggest disagreements with the market are its <em>least</em> reliable, not its best.</p>
    </div></div>
    {prior_season_banner}
    <div class="table-wrap"><table><thead><tr><th>Game</th><th>Side</th><th class="num">Edge</th><th class="num">Confidence</th></tr></thead>
    <tbody>{_model_picks_rows(card)}</tbody></table></div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <div><div class="eyebrow">Season Performance</div><h2 class="display">The Ledger</h2></div>
      <div class="asof mono">updated Mondays after grading</div>
    </div>
    <p class="dek">Every graded pick counts here, wins and losses both &mdash; nothing excluded after the fact.</p>
    {ledger_body}
  </section>

  <footer>
    <span>CFB Line &amp; Pool Monitor &middot; personal decision support, not financial advice &middot; data: CFBD, The Odds API</span>
    <div class="theme-toggle" role="group" aria-label="Theme">
      <button type="button" onclick="setTheme('light', this)">Light</button>
      <button type="button" onclick="setTheme('dark', this)">Dark</button>
      <button type="button" class="active" onclick="setTheme(null, this)">Auto</button>
    </div>
  </footer>

</div>
<script>{JS}</script>
</body>
</html>
"""


def main():
    with db.log_run("dashboard_build") as run:
        week, season = fetch_stats.get_current_week()

        conn = db.get_connection()
        try:
            healths = {
                source: get_source_health(conn, source)
                for source in ("cfbd_stats", "odds_api", "card_generator", "gambling_view", "pool_view", "post_game_audit")
            }

            card, _ = _try_build("card", card_generator.build_card, conn, season, week)
            gambling, _ = _try_build("gambling_view", gambling_view.build_gambling_view, conn, season, week)

            csv_path = pool_view.default_csv_path(week, season)
            pool = None
            if os.path.exists(csv_path):
                entries = pool_view.load_pool_entries(csv_path)
                pool, _ = _try_build("pool_view", pool_view.build_pool_view, conn, entries)

            ledger = build_season_ledger(conn, season)
        finally:
            conn.close()

        html = render_dashboard(
            season, week, card, gambling, pool, ledger, healths,
            generated_at=datetime.utcnow().isoformat(),
        )

        os.makedirs("docs", exist_ok=True)
        with open("docs/index.html", "w", encoding="utf-8") as f:
            f.write(html)

        run["rows_added"] = 1
        print(f"Dashboard built for Week {week}, {season} -> docs/index.html")


if __name__ == "__main__":
    main()
