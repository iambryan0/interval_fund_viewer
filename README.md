# intervals-lite

A local tool for tracking interval-fund redemption deadlines. It watches SEC
EDGAR for each fund's latest N-23C3 or SC TO filing and, when a new one turns
up, shows it to you in a browser so you can read the redemption date yourself
and record it.

No account, no API key, no installation — Python's standard library and a
browser are the whole dependency list.

## Running it

Python 3.10 or newer is required; `python --version` will tell you what is
installed.

    python app.py

The app opens at <http://127.0.0.1:8765/>. On Windows you can double-click
`run.bat` instead.

On first run, open **Settings** and enter a contact string as the SEC user
agent — something like `Your Name you@example.com`. SEC's fair-access policy
requires one on every request, and checks stay disabled until it is set. If
this machine sits behind a proxy, set `HTTPS_PROXY` before starting; if the
proxy intercepts TLS, point `SSL_CERT_FILE` at the proxy's root
certificate. The **Test connection** button on the Settings page tells you
which of those you need.

## Using it

1. **Add funds** by CIK. A ticker works too, but interval funds rarely appear
   in EDGAR's ticker file, so CIK is the reliable input. The app confirms each
   one against EDGAR.
2. **Run a full check.** Each fund is polled for its latest N-23C3 / SC TO
   filing. Anything with a filing you have not reviewed is flagged
   **NEEDS REVIEW**, and the list is ordered as a work queue: funds needing
   review first, newest filing first, everything else alphabetically after
   them. Repurchase windows close, so the most recent filing is the most
   urgent.

   The top of the page sums the queue up: how many filings are waiting, with
   a link to the first one, and counts of funds tracked, deadlines within 30
   days and failed checks. **On the horizon** lists the recorded dates that
   fall in the next 30 days. The filter tabs narrow the table to funds
   needing review or funds whose check failed, and the search box matches
   name, ticker or CIK; `/` jumps to it. Both work on the rows already on the
   page, so nothing is hidden from the list itself.

   If a later check fails, a known unreviewed filing stays in the queue. Its
   row shows both **NEEDS REVIEW** and **check failed**, with the error below
   the filing details. Reviewing that filing clears the review flag; the
   check error stays visible until a check succeeds.
   If the background worker cannot start or encounters a database failure,
   the page reports the error and allows another check. A failed job does
   not leave the app stuck showing a check in progress.
3. **Open a flagged fund.** The filing renders beside the form, with every
   date in it highlighted:

   - **yellow** — a date, somewhere in the document.
   - **green** — a date sitting within 30 words of deadline language
     ("repurchase request deadline", "must be received by", "expiration
     date" and the like).

   Green means *worth reading first*, never *this is the answer*. The page
   opens scrolled to the first green date rather than the top of a long
   document; with nothing green it stays put, because scrolling somewhere
   would imply a confidence the tool does not have. *prev* / *next* step
   through the green dates, or through all of them when there are none.

   If the latest filing is an **amendment**, a warning says so and links to
   the fund's original filings on sec.gov — amendments frequently do not
   restate the deadline.

4. **Read the date yourself, then record it.** The date field always starts
   empty; nothing is ever pre-filled or proposed. Clicking a highlight in the
   filing fills the field with that date — you have necessarily just read it
   in context — or type it in. Any date previously recorded for the fund is
   shown as plain text above the form, as history, so you can see what
   changed without it being one click from submission.

   Save the date, or mark the filing as having no date, or as not applicable.
   All three mean "I have looked at this filing", so it stops being flagged.
   **Save & next** goes to whatever is now at the top of the queue.

   **Recorded-date source** shows the filing and note that supplied the date.
   **Review history** keeps every saved outcome and correction, newest first.
   Marking a later filing as having no date or not applicable retains the
   earlier date and its source. Both sections expand on the review page;
   the date and note inputs still start empty on a fresh page load. If a save
   fails validation, your typed note and date stay in the form; an invalid
   date is also shown as text so you can correct it. If the filing changed,
   your note stays but the date field is cleared for fresh confirmation.

   On upgrade, the last review still present in the old database is recovered
   once. Earlier overwritten reviews cannot be recovered. Dates without a
   known source are labeled accordingly. Removing a fund also removes its
   review history.
5. **Use `redemptions.csv`** whenever you need the data outside the app. The app
   keeps it current: every saved date, added fund or removed fund is written
   to it immediately, and it is rewritten at startup. Three columns: fund
   name, ticker, next redemption date. If the file cannot be written —
   usually because it is open in Excel — a banner says so, the review is
   still saved, and the file catches up on the next change or reload.
   Export failures during startup show the same banner, even before SEC
   setup is complete. Close the file in Excel and reload the fund list to
   retry; the export warning clears once the write succeeds.

A redemption date that has already passed is flagged in the list, so a stale
entry is visible at a glance.

## Maintaining tickers

Click the small **›** arrow beside a fund's tickers to open its drawer.
Enter a symbol and click **Add**, or click **Remove** beside an existing
symbol. Each change saves immediately and updates the table and CSV. Close
with **×**, Escape, or a click outside the drawer.

A new fund added by CIK without tickers opens the same drawer. Ticker edits
work offline for existing funds, with no new dependencies. Without JavaScript,
the arrow opens a simple page with the same Add and Remove controls.

Symbols are normalized to uppercase. Duplicates within a fund are rejected;
a symbol already used by another fund requires confirmation. If another tab
changes the tickers, the current list is shown so you can retry your action.
Removed records and change history remain in the database; adding a removed
symbol restores it. Filing reviews and redemption dates are unaffected.

CSV keeps one row per fund with active tickers joined by `; `. Export failures
use the existing warning and retry behavior. There is no automatic ticker
lookup or removal.

Tickers live in `fund_tickers`; additions and removals are recorded in
`ticker_changes`. Each fund has a `ticker_revision` counter so two tabs
editing the same fund can't overwrite each other.

## Research

The Research page lists every fund on the roster as a tree. Click a fund to
see its filings from EDGAR, grouped by what they are for: prospectus,
reports, repurchase notices, holdings, governance, other. Click a filing to
open it in the viewer on the right; filings with several documents list them
first. Holdings reports and PDFs open on sec.gov instead. The filing list is
fetched the first time you expand a fund and kept for a day. Nothing you do
here is saved to the database. The review page's **Filings ›** link opens
Research with that fund expanded.

## Files it writes

Everything lives beside the source:

- `funds.db` — the fund list, checks, reviews, tickers and ticker history
- `cache/` — downloaded filings, plus `cache/submissions/` (filing lists, a day old at most) and `cache/research/` (documents opened from Research); nothing in here is ever pruned, it only shrinks if you delete the folder
- `redemptions.csv` — the roster, kept in step with the database
- `redemptions-YYYY-MM-DD.csv` — a copy taken each day the roster changed

Disaster recovery: if `funds.db` is missing or has no funds, startup imports
`redemptions-seed.csv` from beside the app when it exists. The seed needs a
`cik` column; the three-column `redemptions.csv` export is not a seed file.
Once the database has funds the seed is ignored, so it can stay in place.

## Tests

    python -m unittest discover -s tests -v

No test touches the network.
