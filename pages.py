"""HTML rendering. Just string building - no io, no db, no network."""

from __future__ import annotations

import datetime
import html as _html
import urllib.parse


def esc(value) -> str:
    """Escape anything going into markup, attributes included."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


SETUP_BANNER = "No SEC user agent is set, so checks are disabled."

DEADLINE_WINDOW = 30  # days the horizon rail and the deadlines stat look ahead


def _iso_date(value) -> datetime.date | None:
    """Strict yyyy-mm-dd, None for anything else. Bad data is shown, not fatal."""
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _day(d: datetime.date) -> str:
    """10 Sep 2026"""
    return f"{d.day} {d.strftime('%b')} {d.year}"


def layout(title: str, body: str, banner: str = "", *, section: str = "funds",
           pending: int | None = None,
           today: datetime.date | None = None) -> str:
    """The shell: sidebar nav, topbar with breadcrumb, banner, then the body.

    section picks the active nav link. pending is the queue count for the
    badge, only the fund list knows it so the others leave it off.
    """
    today = today or datetime.date.today()

    # only the setup banner is fixed in settings, the csv one isn't
    link = (' <a href="/settings">Open settings</a>'
            if banner.startswith(SETUP_BANNER) else "")
    banner_html = f'<div class="banner">{esc(banner)}{link}</div>' if banner else ""

    def nav(href, label, key, extra=""):
        active = ' class="active"' if key == section else ""
        return f'<a href="{href}"{active}>{label}{extra}</a>'

    badge = f'<span class="nav-badge">{esc(pending)}</span>' if pending else ""
    section_name = {"funds": "Funds", "research": "Research",
                    "settings": "Settings"}.get(section, "")
    if title and title != section_name:
        crumb = f'{esc(section_name)} <span>/</span> <strong>{esc(title)}</strong>'
    else:
        crumb = f'<strong>{esc(section_name)}</strong>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta name="theme-color" content="#0f1115"/>
<title>{esc(title)} — Interval Funds</title>
<link rel="stylesheet" href="/static/app.css"/>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<aside class="sidebar">
  <a class="brand" href="/"><span class="brand-mark" aria-hidden="true">◒</span>Interval Funds</a>
  <nav aria-label="Main">
    {nav("/", "Funds", "funds", badge)}
    {nav("/research", "Research", "research")}
    {nav("/settings", "Settings", "settings")}
  </nav>
  <p class="sidebar-note">Runs on this machine. The only outside calls go to sec.gov.</p>
</aside>
<div class="shell">
<header class="topbar">
  <div class="breadcrumb">{crumb}</div>
  <span class="top-date">{esc(_day(today))}</span>
</header>
{banner_html}
<main id="main" tabindex="-1">
{body}
</main>
</div>
<script src="/static/app.js"></script>
</body>
</html>"""


def settings_page(user_agent: str, port: str, test_message: str = "",
                  test_ok: bool | None = None, banner: str = "") -> str:
    result = ""
    if test_message:
        state = "ok" if test_ok else "bad"
        result = f'<p class="result {state}">{esc(test_message)}</p>'

    body = f"""
<div class="page-heading"><h1>Settings</h1></div>
<div class="tile settings">
  <header><span>This machine</span></header>
  <form method="post" action="/settings" class="body">
    <div class="settings-row">
      <div>
        <label for="sec_user_agent">SEC user agent</label>
        <p class="hint">SEC's fair-access policy requires a contact string on every
           request. Checks are refused until this is set.</p>
      </div>
      <input type="text" id="sec_user_agent" name="sec_user_agent"
             value="{esc(user_agent)}" placeholder="Your Name you@example.com" size="45"/>
    </div>
    <div class="settings-row">
      <div>
        <label for="port">Port</label>
        <p class="hint">Takes effect the next time you start the app.</p>
      </div>
      <input type="text" id="port" name="port" value="{esc(port)}" size="6"/>
    </div>
    <div class="settings-actions"><button type="submit" class="primary">Save</button></div>
  </form>
  <form method="post" action="/settings/test" class="body">
    <div class="settings-row">
      <div>
        <strong>Connection</strong>
        <p class="hint">Reaches data.sec.gov and www.sec.gov with the user agent above,
           and says which proxy or certificate setting is missing if it can't.</p>
      </div>
      <button type="submit">Test connection</button>
    </div>
    {result}
  </form>
</div>
"""
    return layout("Settings", body, banner=banner, section="settings")


STATUS_LABELS = {
    "unchecked": "not checked yet",
    "error": "check failed",
    "no_filings": "no repurchase filings",
    "needs_review": "NEEDS REVIEW",
    "up_to_date": "up to date",
}

REVIEW_LABELS = {
    "reviewed": "date recorded",
    "no_date": "no date in filing",
    "not_applicable": "not applicable",
}


_STATUS_TAG = {
    "needs_review": "pending",
    "up_to_date": "done",
    "error": "err",
}


def _initials(fund: dict) -> str:
    words = [w for w in (fund["fund_name"] or "").split() if w[0].isalnum()]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    if words:
        return words[0][:2].upper()
    return str(fund["cik"])[:2]


def _has_issue(fund: dict) -> bool:
    return fund["status"] == "error" or bool(fund["check_error"])


def _fund_row(fund: dict) -> str:
    status = fund["status"]
    cells = []

    name = esc(fund["fund_name"] or fund["cik"])
    name = f'<a href="/fund/{esc(fund["cik"])}">{name}</a>'
    if not fund["active"]:
        name += ' <span class="tag">inactive</span>'
    cells.append(
        f'<td><div class="fund-name"><span class="fund-avatar">{esc(_initials(fund))}</span>'
        f'<div><span class="fund-title">{name}</span>'
        f'<span class="fund-cik mono">CIK {esc(fund["cik"])}</span></div></div></td>'
    )
    cells.append(f'<td class="ticker-cell"><div class="ticker-content"><span class="mono" data-ticker-value>{esc(fund["ticker"] or "—")}</span> '
                 f'<a class="ticker-toggle" data-ticker-open href="/fund/{esc(fund["cik"])}/tickers" '
                 f'aria-label="Edit tickers for {esc(fund["fund_name"] or fund["cik"])}" aria-haspopup="dialog">›</a></div></td>')

    detail = ""
    if status == "error":
        detail = esc(fund["check_error"])
    elif status == "needs_review":
        detail = esc(f'{fund["latest_form"] or ""} filed '
                     f'{fund["latest_filing_date"] or ""}').strip()
    elif status == "up_to_date" and fund["review_status"]:
        detail = esc(REVIEW_LABELS.get(fund["review_status"], ""))
    short_labels = {
        "unchecked": "Unchecked", "error": "Failed", "no_filings": "No filings",
        "needs_review": "Review", "up_to_date": "Reviewed",
    }
    tag_class = esc(f"tag {_STATUS_TAG.get(status, '')}".strip())
    description = esc(STATUS_LABELS.get(status, status))
    if detail:
        description += f": {detail}"
    badge = (f'<span class="{tag_class}" title="{description}" aria-label="{description}">'
             f'{esc(short_labels.get(status, status))}</span>')
    if status == "needs_review" and fund["check_error"]:
        badge += (f'<span class="tag err" title="{esc(fund["check_error"])}" '
                  f'aria-label="check failed: {esc(fund["check_error"])}">check failed</span>')
    status_cell = f'<td class="status-cell">{badge}</td>'

    date_cell = esc(fund["next_redemption_date"] or "—")
    if fund["date_passed"]:
        date_cell += ' <span class="tag passed">passed</span>'
    cells.append(f'<td class="mono date-cell">{date_cell}</td>')
    cells.append(status_cell)

    cik = esc(fund["cik"])
    toggle = "Disable" if fund["active"] else "Enable"
    label = esc(fund["fund_name"] or fund["cik"])
    cells.append(f"""<td class="actions"><div class="row-actions">
      <a class="row-open" href="/fund/{cik}" aria-label="Open {label}">↗</a>
      <form method="post" action="/check/{cik}"><button type="submit">Check</button></form>
      <form method="post" action="/funds/{cik}/toggle">
        <button type="submit">{toggle}</button></form>
      <form method="post" action="/funds/{cik}/delete"
            onsubmit="return confirm('Remove this fund?')">
        <button type="submit">Remove</button></form>
    </div></td>""")

    # the filter tabs and the search box work off these, in the browser
    states = []
    if status == "needs_review":
        states.append("pending")
    if _has_issue(fund):
        states.append("error")
    search = esc(" ".join(str(v) for v in (fund["fund_name"], fund["cik"],
                                           fund["ticker"]) if v).lower())
    row_class = ' class="pending"' if status == "needs_review" else ""
    return (f'<tr{row_class} data-search-base="{esc((str(fund["fund_name"] or "") + " " + str(fund["cik"])).lower())}" data-state="{" ".join(states)}" data-search="{search}">'
            + "".join(cells) + "</tr>")


def _upcoming(funds: list[dict], today: datetime.date) -> list[tuple]:
    """(date, days away, fund) for recorded dates inside the window, soonest first."""
    out = []
    for fund in funds:
        d = _iso_date(fund["next_redemption_date"])
        if d is None or fund["date_passed"]:
            continue
        days = (d - today).days
        if 0 <= days <= DEADLINE_WINDOW:
            out.append((d, days, fund))
    out.sort(key=lambda t: (t[0], (t[2]["fund_name"] or "").lower()))
    return out


def _days_label(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def _hero(pending: list[dict], upcoming: list[tuple]) -> str:
    n = len(pending)
    if n:
        headline = f"{n} filing{'s' if n != 1 else ''} {'are' if n != 1 else 'is'} waiting."
        lead = "Read the source filing, record the date, move on to the next one."
        action = (f'<a class="text-action" href="/fund/{esc(pending[0]["cik"])}">'
                  'Start reviewing <span aria-hidden="true">↗</span></a>')
    else:
        headline = "Nothing is waiting."
        lead = "Every filing has been looked at. Run a check to look for new ones."
        action = ""

    if upcoming:
        d = upcoming[0][0]
        callout = (f'<div class="orbit-callout"><span class="dot"></span>Next deadline '
                   f'<b>{esc(d.day)} {esc(d.strftime("%b"))}</b></div>')
    else:
        callout = ('<div class="orbit-callout"><span class="dot"></span>'
                   f'No deadlines in the next {DEADLINE_WINDOW} days</div>')

    return f"""
<section class="tile hero">
  <div class="hero-copy">
    <p class="signal"><span class="dot"></span>Review queue</p>
    <h2>{headline}</h2>
    <p>{lead}</p>
    {action}
  </div>
  <div class="orbital" aria-hidden="true">
    <svg viewBox="0 0 420 260" preserveAspectRatio="xMidYMid slice">
      <defs>
        <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">
          <path d="M28 0H0V28" fill="none" stroke="currentColor" stroke-opacity=".09"/>
        </pattern>
        <radialGradient id="fade">
          <stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#000"/>
        </radialGradient>
        <mask id="mask"><rect width="420" height="260" fill="url(#fade)"/></mask>
      </defs>
      <rect width="420" height="260" fill="url(#grid)" mask="url(#mask)"/>
      <g transform="rotate(-25 210 130)" fill="none" stroke="currentColor">
        <ellipse cx="210" cy="130" rx="92" ry="92" stroke-opacity=".55"/>
        <ellipse cx="210" cy="130" rx="150" ry="116" stroke-opacity=".3" stroke-dasharray="4 7"/>
        <ellipse cx="210" cy="130" rx="205" ry="146" stroke-opacity=".18"/>
      </g>
      <circle cx="258" cy="42" r="14" fill="currentColor" fill-opacity=".14"/>
      <circle cx="258" cy="42" r="4" fill="currentColor"/>
      <circle cx="108" cy="206" r="2.5" fill="currentColor" fill-opacity=".6"/>
    </svg>
    <div class="orbit-center"><strong id="orbit-count">{n}</strong><span>to review</span></div>
    {callout}
  </div>
</section>"""


def _stats(funds: list[dict], pending: int, deadlines: int) -> str:
    total = len(funds)
    issues = sum(1 for f in funds if _has_issue(f))

    def tile(key, label, value, meta, mod=""):
        share = round(100 * value / total) if total else 0
        cls = f"tile stat {mod}".strip()
        return (f'<div class="{cls}"><span class="stat-label">{label}</span>'
                f'<strong data-stat="{key}">{esc(value)}</strong>'
                f'<span class="stat-meta">{meta}</span>'
                f'<div class="stat-rule"><i style="width:{share}%"></i></div></div>')

    return '<div class="stats">' + "".join([
        tile("tracked", "Funds tracked", total, "on the roster"),
        tile("pending", "Needs review", pending, "filings waiting", "stat-accent"),
        tile("deadlines", f"Deadlines in {DEADLINE_WINDOW} days", deadlines,
             "recorded dates"),
        tile("issues", "Check issues", issues, "latest check failed", "stat-warn"),
    ]) + "</div>"


def _horizon(upcoming: list[tuple]) -> str:
    if upcoming:
        items = "".join(
            f'<div class="deadline">'
            f'<div class="calendar-day"><small>{esc(d.strftime("%b"))}</small>'
            f'<strong>{esc(d.day)}</strong></div>'
            f'<div><span class="deadline-name">{esc(f["fund_name"] or f["cik"])}</span>'
            f'<small class="hint">{esc(_days_label(days))}</small></div></div>'
            for d, days, f in upcoming[:8])
    else:
        items = (f'<p class="rail-note">No recorded dates fall in the next '
                 f'{DEADLINE_WINDOW} days.</p>')
    return f"""
<section class="tile" id="horizon">
  <header><span>On the horizon</span><span class="mono">next {DEADLINE_WINDOW} days</span></header>
  <div class="deadlines">{items}</div>
  <p class="rail-note">Dates you recorded. The filing is always the source.</p>
</section>"""


def fund_list_page(funds: list[dict], job: dict, message: str = "",
                   error: str = "", banner: str = "",
                   today: datetime.date | None = None, add_draft=None) -> str:
    add_draft = add_draft or {}
    today = today or datetime.date.today()
    pending = [f for f in funds if f["status"] == "needs_review"]
    upcoming = _upcoming(funds, today)
    issues = sum(1 for f in funds if _has_issue(f))

    notes = ""
    if message:
        notes += f'<p class="result ok">{esc(message)}</p>'
    if error:
        notes += f'<p class="result bad">{esc(error)}</p>'

    if job.get("job_error"):
        notes += (f'<p class="result bad">Check failed: {esc(job["job_error"])}. '
                  'You can run another check.</p>')

    if funds:
        rows = "".join(_fund_row(f) for f in funds)
        table = f"""<div class="table-scroll"><table>
<thead><tr><th>Fund</th><th>Tickers</th><th>Next redemption</th>
<th>Status</th><th><span class="sr-only">Actions</span></th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="table-footer">
  <span id="row-count">{esc(len(funds))} fund{'s' if len(funds) != 1 else ''}</span>
  <span>Needs review first, newest filing first</span>
</div>"""
    else:
        table = ('<div class="body">'
                 '<p class="empty">No funds yet. Add a fund to get started.</p>'
                 '</div>')

    running = job.get("running")
    progress_html = f"""
<div id="progress" class="progress" data-running="{'1' if running else '0'}"
     {'' if running else 'hidden'}>
  <span id="progress-text">Checking {esc(job.get('current') or '')} —
    {esc(job.get('done', 0))} of {esc(job.get('total', 0))}</span>
</div>"""

    n = len(pending)
    body = f"""
<div class="page-heading">
  <h1>Funds <span class="count">{n} needing review</span></h1>
  <form method="post" action="/check"><button type="submit" class="primary">Run full check</button></form>
</div>
{notes}
{progress_html}
{_hero(pending, upcoming)}
{_stats(funds, n, len(upcoming))}
<div class="content-grid">
<div class="tile" id="roster">
  <header><span>Funds</span><span class="mono">{esc(len(funds))} total</span></header>
  <div class="toolbar-row">
    <div class="filter-tabs" role="group" aria-label="Filter funds">
      <button type="button" data-filter="all" aria-pressed="true">All</button>
      <button type="button" data-filter="pending" aria-pressed="false">Needs review <span>{n}</span></button>
      <button type="button" data-filter="error" aria-pressed="false">Check issues <span>{issues}</span></button>
    </div>
    <label class="search"><span aria-hidden="true">⌕</span>
      <input id="search" type="search" placeholder="Find a fund" aria-label="Find a fund by name, ticker, or CIK"/>
      <kbd>/</kbd></label>
  </div>
  {table}
  <form method="post" action="/funds" class="addfund">
    <label>Add a fund by CIK
      <input type="text" name="query" value="{esc(add_draft.get('query', ''))}" placeholder="e.g. 1735964" size="20"/>
    </label>
    <button type="submit">Add</button>
    <span class="hint">A ticker works too, but interval funds rarely appear in
    EDGAR's ticker file — CIK is the reliable input.</span>
  </form>
</div>
<aside class="right-rail">{_horizon(upcoming)}</aside>
</div>
"""
    return layout("Funds", body, banner=banner, pending=n, today=today)


def _amendment_warning(fund: dict) -> str:
    """Warning for when the latest filing is an amendment.

    Amendments often don't restate the deadline, it stays in the original,
    so I might be looking at a document that can't answer the question. The
    link filters by base form so it works for N-23C3A/A and the SC TO family.
    """
    form = fund.get("latest_form") or ""
    if not form.endswith("/A"):
        return ""
    base = form[:-2]
    query = urllib.parse.urlencode({
        "action": "getcompany", "CIK": fund["cik"], "type": base,
        "dateb": "", "owner": "include", "count": "40",
    })
    url = f"https://www.sec.gov/cgi-bin/browse-edgar?{query}"
    return (
        f'<p class="warn">This is an amendment ({esc(form)}). The deadline may '
        f'only appear in the original filing. '
        f'<a href="{esc(url)}" target="_blank" rel="noopener">'
        f'See this fund\'s {esc(base)} filings on sec.gov</a></p>'
    )


def _review_entry(review: dict) -> str:
    label = REVIEW_LABELS.get(review["outcome"], review["outcome"])
    recorded = f' · {esc(review["recorded_date"])}' if review["recorded_date"] else ""
    filing = (f'{esc(review["form"] or "Filing")} '
              f'{esc(review["accession"] or "source not available")}')
    if review["url"]:
        filing = (f'<a href="{esc(review["url"])}" target="_blank" '
                  f'rel="noopener">{filing}</a>')
    legacy = " · recovered from previous app data" if review.get("legacy") else ""
    note = f'<p class="review-note">{esc(review["note"])}</p>' if review["note"] else ""
    return (f'<p>{esc(label)}{recorded}<br>{filing}<br>'
            f'<span class="hint">Filed {esc(review["filing_date"] or "unknown")} · '
            f'Reviewed {esc(review["reviewed_at"])}{legacy}</span></p>{note}')


def review_page(fund: dict, message: str = "", error: str = "",
                banner: str = "", reviews: list[dict] | None = None,
                draft: dict[str, str] | None = None) -> str:
    cik = esc(fund["cik"])

    notes = ""
    if message:
        notes += f'<p class="result ok">{esc(message)}</p>'
    if error:
        notes += f'<p class="result bad">{esc(error)}</p>'

    filed = esc(f'{fund["latest_form"] or ""} filed '
                f'{fund["latest_filing_date"] or ""}').strip()

    link = ""
    if fund["latest_url"]:
        link = (f' <a href="{esc(fund["latest_url"])}" target="_blank" '
                f'rel="noopener">View on sec.gov</a>')

    # Only a rejected submission may repopulate the form, never stored history.
    # Compare with the filing being rendered now; it may have changed even
    # after validation failed. An old draft date must not transfer to it.
    draft_date = ""
    draft_note = ""
    draft_hint = ""
    if draft is not None:
        draft_note = draft.get("note", "")
        if (fund["latest_accession"]
                and draft.get("accession") == fund["latest_accession"]):
            draft_date = draft.get("next_redemption_date", "")
            if draft_date:
                # Browsers blank invalid values in date inputs. Keep the raw
                # text visible too, so it can still be recovered and corrected.
                draft_hint = f'<p class="hint">Submitted date: {esc(draft_date)}</p>'
        else:
            notes += ('<p class="warn">Your note has been kept, but the submitted '
                      'date cannot be used for this filing. Read the current '
                      'filing and select its date again.</p>')

    # Previous saved state is shown as history, never as a suggested answer.
    prior = ""
    reviews = reviews or []
    if fund["next_redemption_date"]:
        prior = (f'<p class="hint">Previously recorded: '
                 f'<span class="mono">{esc(fund["next_redemption_date"])}</span>'
                 f'</p>')
        source = next((r for r in reviews if r["outcome"] == "reviewed"
                       and r["recorded_date"] == fund["next_redemption_date"]), None)
        if source:
            prior += '<details class="review-history"><summary>Recorded-date source</summary>'
            prior += _review_entry(source) + '</details>'
        else:
            prior += '<p class="hint">Source history is unavailable for this earlier date.</p>'

    history = ""
    if reviews:
        history = ('<details class="review-history"><summary>Review history '
                   f'({len(reviews)})</summary><ol>'
                   + ''.join(f'<li>{_review_entry(r)}</li>' for r in reviews)
                   + '</ol></details>')

    name = esc(fund["fund_name"] or fund["cik"])
    body = f"""
<div class="review">
  <div class="tile pane" id="form-tile">
    <header><span>Your review</span></header>
    <div class="body">
    <h2 class="fund-heading">{name}</h2>
    {prior}
    {notes}
    {_amendment_warning(fund)}
    <form method="post" action="/fund/{cik}/review" class="stack">
      <input type="hidden" name="accession" value="{esc(fund["latest_accession"])}">
      <label class="field"><span>Next redemption date</span>
        <input type="date" id="date-input" name="next_redemption_date"
               class="mono" value="{esc(draft_date)}"/>
      </label>
      {draft_hint}
      <label class="field"><span>Note</span>
        <textarea name="note" rows="3" cols="32">{esc(draft_note)}</textarea>
      </label>
      <div class="outcomes">
        <button type="submit" name="outcome" value="reviewed" class="primary">Save date</button>
        <button type="submit" name="outcome_next" value="reviewed">Save &amp; next</button>
      </div>
      <div class="outcomes secondary">
        <button type="submit" name="outcome" value="no_date">No date in this filing</button>
        <button type="submit" name="outcome" value="not_applicable">Not applicable</button>
      </div>
    </form>
    {history}
    <p><a href="/fund/{cik}/tickers" data-ticker-open aria-haspopup="dialog">Edit tickers ›</a></p>
    <p><a href="/">← Back to funds</a></p>
    </div>
  </div>
  <div class="tile" id="filing-tile">
    <header class="filing-toolbar">
      <span><span class="dot"></span>Source filing</span>
      <span class="mono">{filed}{link} <a href="/research/{cik}">Filings ›</a></span>
    </header>
    <section class="frame">
      <iframe id="filing" src="/filing/{cik}"
              sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
              title="Filing"></iframe>
    </section>
    <footer class="legend">
      <span><i class="swatch green"></i>near deadline language</span>
      <span><i class="swatch yellow"></i>any other date</span>
      <span class="nav">
        <button type="button" id="match-prev">‹ prev</button>
        <span id="match-count" class="count">no dates</span>
        <button type="button" id="match-next">next ›</button>
      </span>
    </footer>
  </div>
</div>
"""
    return layout(fund["fund_name"] or fund["cik"], body, banner=banner)


def tickers_page(fund, *, ticker="", message="", error="", banner="", shared=False):
    cik = esc(fund["cik"])
    revision = fund["ticker_revision"]
    hidden = f'<input type="hidden" name="revision" value="{revision}">'
    action = f'/fund/{cik}/tickers'
    items = []
    for row in fund["tickers"]:
        if row["on_roster"] and row["ticker"]:
            items.append(f'''<li><span class="mono">{esc(row['ticker'])}</span>
                <form method="post" action="{action}">{hidden}
                  <input type="hidden" name="id" value="{row['id']}">
                  <button name="intent" value="remove" aria-label="Remove {esc(row['ticker'])}">Remove</button>
                </form></li>''')
    notice = f'<p class="result bad" role="alert">{esc(error)}</p>' if error else ''
    if message:
        notice += f'<p class="hint" role="status">{esc(message)}</p>'
    if banner:
        notice += f'<p class="banner">{esc(banner)}</p>'
    confirmation = ('<label><input type="checkbox" name="allow_shared"> '
                    'Use this ticker on both funds</label>') if shared else ''
    body = f'''<section class="ticker-panel" data-ticker-panel data-tickers="{esc(fund['ticker'])}"
                 aria-labelledby="ticker-title">
      <header class="ticker-panel-heading"><h1 id="ticker-title">Tickers</h1>
        <a href="/" data-ticker-close aria-label="Close ticker drawer">×</a></header>
      <p class="hint">{esc(fund['fund_name'] or fund['cik'])}</p>
      {notice}
      <ul class="ticker-list">{''.join(items) if items else '<li class="hint">No tickers yet.</li>'}</ul>
      <form method="post" action="{action}" class="ticker-add">
        {hidden}<label for="new-ticker">New ticker</label>
        <div><input id="new-ticker" name="ticker" value="{esc(ticker)}" maxlength="20"
          placeholder="e.g. EXAIX" required autocomplete="off" spellcheck="false">
        <button type="submit" name="intent" value="add" class="primary">Add</button></div>
        {confirmation}
      </form>
    </section>'''
    return layout("Tickers", body)


# --- research ------------------------------------------------------------------

TREE_SHOW = 10      # filings shown per group before the "… N more" row
TREE_OPEN_MAX = 3   # groups this small start open


def research_doc_route(cik, accession, name) -> str:
    return f"/research/{esc(cik)}/{esc(accession)}/{esc(name)}"


def _archive_dir(cik, accession) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{esc(str(int(cik)))}/"
            f"{esc(accession.replace('-', ''))}/")


def _tree_line(prefix: str, inner: str, *, last: bool) -> str:
    branch = "└─" if last else "├─"
    return (f'<pre class="tree-line">{esc(prefix)}<span class="tree-c">{branch}</span> '
            f'{inner}</pre>')


def _fund_root(fund: dict, *, expanded: bool, subtree: str) -> str:
    cik = esc(fund["cik"])
    name = esc(fund["fund_name"] or fund["cik"])
    search = esc(" ".join(str(v) for v in (fund["fund_name"], fund["cik"],
                                           fund["ticker"]) if v).lower())
    arrow = "▾" if expanded else "▸"
    state = "true" if expanded else "false"
    # I mark this data-loaded so the script won't refetch what came with the page
    children = (f'<div class="tree-children" data-tree-children data-loaded="1">{subtree}</div>'
                if expanded else '<div class="tree-children" data-tree-children hidden></div>')
    return (f'<div class="tree-root" data-tree-root data-cik="{cik}" data-search="{search}">'
            f'<pre class="tree-line"><a class="tree-fold" href="/research/{cik}" '
            f'data-tree-expand="/research/{cik}?fragment=1" aria-expanded="{state}">'
            f'<span class="tree-c">{arrow}</span> {name}</a></pre>{children}</div>')


def research_page(funds: list[dict], *, expanded: dict | None = None,
                  subtree: str = "", banner: str = "") -> str:
    roots = "".join(
        _fund_root(f, expanded=bool(expanded and f["cik"] == expanded["cik"]),
                   subtree=subtree if expanded and f["cik"] == expanded["cik"] else "")
        for f in funds)
    if not roots:
        roots = '<p class="hint">No funds on the roster yet. Add one on the Funds page.</p>'
    body = f"""
<div class="research">
  <div class="tile" id="tree-tile">
    <header><span>Filings</span><span class="mono">{esc(len(funds))} funds</span></header>
    <div class="body">
      <div class="search">
        <input id="research-search" type="search" placeholder="Find a fund"
               aria-label="Find a fund by name, ticker, or CIK"/><kbd>/</kbd>
      </div>
      <div class="tree" id="tree" role="tree">{roots}</div>
    </div>
  </div>
  <div class="tile" id="viewer-tile">
    <header class="filing-toolbar">
      <span><span class="dot"></span><span id="research-title">Pick a filing</span></span>
      <a id="research-external" href="#" target="_blank" rel="noopener" hidden>open on sec.gov ↗</a>
    </header>
    <section class="frame">
      <iframe id="research-frame" src="about:blank"
              sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
              title="Filing document"></iframe>
    </section>
  </div>
</div>
"""
    return layout("Research", body, banner=banner, section="research")


def _is_html_name(name: str) -> bool:
    return name.lower().endswith((".htm", ".html"))


def _filing_row(fund: dict, filing: dict, *, last: bool, prefix: str,
                open_accession, documents_html: str) -> str:
    cik = esc(fund["cik"])
    acc = esc(filing["accession"])
    description = (f' <span class="tree-desc">{esc(filing["description"].lower())}</span>'
                   if filing["description"] else "")
    if not _is_html_name(filing["primary_document"]):
        # nothing to fold open, the viewer can't render this, so send it to sec.gov directly
        href = _archive_dir(fund["cik"], filing["accession"]) + esc(filing["primary_document"])
        inner = (f'<a class="tree-doc" data-external href="{href}" target="_blank" rel="noopener">'
                 f'<span class="tree-form">{esc(filing["form"])}</span> '
                 f'<span class="tree-date">{esc(filing["filing_date"])}</span>{description} '
                 f'<span class="tree-ext">sec.gov ↗</span></a>')
        return _tree_line(prefix, inner, last=last)
    is_open = bool(open_accession and filing["accession"] == open_accession)
    state = "true" if is_open else "false"
    inner = (f'<a class="tree-fold" href="/research/{cik}/{acc}" '
             f'data-tree-expand="/research/{cik}/{acc}?fragment=1" aria-expanded="{state}">'
             f'<span class="tree-form">{esc(filing["form"])}</span> '
             f'<span class="tree-date">{esc(filing["filing_date"])}</span>{description}</a>')
    children = (f'<div class="tree-children" data-tree-children data-loaded="1">{documents_html}</div>'
                if is_open else '<div class="tree-children" data-tree-children hidden></div>')
    return _tree_line(prefix, inner, last=last) + children


def research_subtree(fund: dict, groups: list[dict], *, note: str = "",
                     error: str = "", open_accession=None,
                     documents_html: str = "") -> str:
    cik = esc(fund["cik"])
    out = []
    if error:
        out.append(f'<p class="tree-note bad">{esc(error)} '
                   f'<a href="/research/{cik}">Retry</a></p>')
    if note:
        out.append(f'<p class="tree-note">{esc(note)}</p>')
    for gi, group in enumerate(groups):
        last_group = gi == len(groups) - 1
        filings = group["filings"]

        # the group that holds the filing I was asked for has to start open
        group_has_open = any(filing["accession"] == open_accession for filing in filings) if open_accession else False
        is_open = " open" if (len(filings) <= TREE_OPEN_MAX or group_has_open) else ""

        tracked = ' <span class="tree-tracked">● tracked</span>' if group["tracked"] else ""
        branch = "└─" if last_group else "├─"
        prefix = "   " if last_group else "│  "
        rows = []

        # I need where the open filing sits before I can decide whether it's hidden
        open_filing_index = None
        if open_accession:
            for fi, filing in enumerate(filings):
                if filing["accession"] == open_accession:
                    open_filing_index = fi
                    break

        # reveal every row so the filing I opened isn't stuck behind "more"
        show_all = open_filing_index is not None and open_filing_index >= TREE_SHOW

        for fi, filing in enumerate(filings):
            last = fi == len(filings) - 1
            hidden = fi >= TREE_SHOW and not show_all
            row = _filing_row(fund, filing, last=last, prefix=prefix,
                              open_accession=open_accession,
                              documents_html=documents_html)
            if hidden:
                row = f'<div data-tree-rest hidden>{row}</div>'
            rows.append(row)

        if len(filings) > TREE_SHOW and not show_all:
            rest = len(filings) - TREE_SHOW
            rows.insert(TREE_SHOW, _tree_line(
                prefix, f'<a class="tree-more" href="/research/{cik}" data-tree-more>'
                        f'… {rest} more</a>', last=False))

        out.append(
            f'<details class="tree-group" data-group="{esc(group["key"])}"{is_open}>'
            f'<summary><pre class="tree-line"><span class="tree-c">{branch}</span> '
            f'<span class="tree-group-label">{esc(group["label"])}</span> '
            f'<span class="tree-count">{len(filings)}</span>{tracked}</pre></summary>'
            + "".join(rows) + '</details>')
    if not groups and not error:
        out.append('<p class="tree-note">No filings on EDGAR for this fund.</p>')
    return "".join(out)


def research_documents(fund: dict, filing: dict, documents: list[dict], *,
                       error: str = "") -> str:
    cik = fund["cik"]
    acc = filing["accession"]
    title = (f'{fund["fund_name"] or fund["cik"]} · {filing["form"]} · '
             f'{filing["filing_date"]}')
    out = []
    if error:
        out.append(f'<p class="tree-note bad">{esc(error)} '
                   f'<a href="{_archive_dir(cik, acc)}" target="_blank" rel="noopener">'
                   f'Open the filing on sec.gov ↗</a></p>')
    html_docs = [d for d in documents if d["is_html"]]
    auto = (f' data-open="{research_doc_route(cik, acc, html_docs[0]["name"])}"'
            if len(html_docs) == 1 else "")
    rows = []
    for di, doc in enumerate(documents):
        last = di == len(documents) - 1
        label = esc(doc["description"].lower()) if doc["description"] else esc(doc["kind"])
        if doc["is_html"]:
            inner = (f'<a class="tree-doc" data-doc href="{research_doc_route(cik, acc, doc["name"])}" '
                     f'data-title="{esc(title)} · {esc(doc["name"])}">'
                     f'<span class="tree-file">{esc(doc["name"])}</span> '
                     f'<span class="tree-desc">{label}</span></a>')
        else:
            inner = (f'<a class="tree-doc" data-external href="{esc(doc["url"])}" '
                     f'target="_blank" rel="noopener">'
                     f'<span class="tree-file">{esc(doc["name"])}</span> '
                     f'<span class="tree-desc">{label}</span> '
                     f'<span class="tree-ext">sec.gov ↗</span></a>')
        # fragment can't know if the filing is the last row in its group, so plain indent
        rows.append(_tree_line("      ", inner, last=last))
    return f'<div class="tree-docs"{auto}>' + "".join(out) + "".join(rows) + "</div>"
