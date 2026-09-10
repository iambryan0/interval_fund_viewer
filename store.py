"""SQLite: schema, settings, funds, reviews, redemptions.csv.

One row per fund, with a separate history of saved reviews. Status is derived.
Knows nothing about the network or http.
"""

from __future__ import annotations

import csv as _csv
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).with_name("funds.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS funds (
  cik                       TEXT PRIMARY KEY,
  ticker_revision           INTEGER NOT NULL DEFAULT 0,
  fund_name                 TEXT NOT NULL DEFAULT '',
  active                    INTEGER NOT NULL DEFAULT 1,
  added_at                  TEXT NOT NULL,

  latest_accession          TEXT,
  latest_filing_date        TEXT,
  latest_form               TEXT,
  latest_url                TEXT,
  checked_at                TEXT,
  check_error               TEXT NOT NULL DEFAULT '',

  next_redemption_date      TEXT,
  last_reviewed_accession   TEXT,
  last_reviewed_filing_date TEXT,
  last_reviewed_form        TEXT,
  last_reviewed_url         TEXT,
  review_status             TEXT,
  note                      TEXT NOT NULL DEFAULT '',
  reviewed_at               TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_history (
  id             INTEGER PRIMARY KEY,
  cik            TEXT NOT NULL REFERENCES funds(cik) ON DELETE CASCADE,
  accession      TEXT,
  filing_date    TEXT,
  form           TEXT,
  url            TEXT,
  outcome        TEXT NOT NULL,
  recorded_date  TEXT,
  note           TEXT NOT NULL DEFAULT '',
  reviewed_at    TEXT NOT NULL,
  legacy         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS review_history_fund ON review_history(cik, id);

CREATE TABLE IF NOT EXISTS fund_tickers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cik TEXT NOT NULL REFERENCES funds(cik) ON DELETE CASCADE,
  ticker TEXT NOT NULL,
  on_roster INTEGER NOT NULL DEFAULT 1 CHECK (on_roster IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (cik, ticker)
);
CREATE INDEX IF NOT EXISTS tickers_symbol ON fund_tickers(ticker, on_roster);
CREATE TABLE IF NOT EXISTS ticker_changes (
  id INTEGER PRIMARY KEY,
  cik TEXT NOT NULL REFERENCES funds(cik) ON DELETE CASCADE,
  changed_at TEXT NOT NULL,
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ticker_changes_fund ON ticker_changes(cik, id);

"""


class StoreError(Exception):
    """A db failure with a message I can show on the page."""


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open a fresh connection. Don't share one across threads - each request
    is its own thread and so is the check job, so each opens its own."""
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Older databases kept only the most recent review. Preserve that known
    # state once, without assigning a retained date to a no-date filing.
    with conn:
        conn.execute("""
            INSERT INTO review_history
              (cik, accession, filing_date, form, url, outcome, recorded_date,
               note, reviewed_at, legacy)
            SELECT cik, last_reviewed_accession, last_reviewed_filing_date,
                   last_reviewed_form, last_reviewed_url, review_status,
                   CASE WHEN review_status = 'reviewed' THEN next_redemption_date END,
                   note, reviewed_at, 1
            FROM funds
            WHERE reviewed_at IS NOT NULL AND review_status IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM review_history WHERE cik = funds.cik)
        """)


def list_reviews(conn: sqlite3.Connection, cik: str) -> list[dict]:
    """Newest saved review first, including corrections to the same filing."""
    return [dict(row) for row in conn.execute(
        "SELECT * FROM review_history WHERE cik = ? ORDER BY id DESC", (cik,))]


# --- ticker maintenance ----------------------------------------------------

_FUND_SELECT = "SELECT funds.* FROM funds"
_TICKER_RE = re.compile(r"[A-Z0-9]+(?:[.-][A-Z0-9]+)*\Z", re.ASCII)


class TickerConflict(StoreError):
    """An editor was based on an older saved ticker roster."""


class SharedTickerError(StoreError):
    """A symbol is already in another fund's maintained roster."""


def normalize_ticker(value: str) -> str:
    symbol = value.strip().upper()
    if symbol and (len(symbol) > 20 or not _TICKER_RE.fullmatch(symbol)
                   or symbol in {"N/A", "NA", "NONE", "NULL"}):
        raise StoreError("Use a ticker of up to 20 letters/digits, with optional internal periods or hyphens. Leave unassigned tickers blank.")
    return symbol


def parse_ticker_list(value: str) -> list[str]:
    symbols = [normalize_ticker(s) for s in re.split(r"[,;]", value) if s.strip()]
    if len(symbols) != len(set(symbols)):
        raise StoreError("Each ticker can appear only once on a fund's roster.")
    if len(symbols) > 100:
        raise StoreError("Enter at most 100 tickers at a time.")
    return sorted(symbols)


def _insert_ticker(conn, cik, symbol):
    stamp = now_iso()
    conn.execute("""INSERT INTO fund_tickers (cik, ticker, created_at, updated_at)
                    VALUES (?, ?, ?, ?)""", (cik, symbol, stamp, stamp))


def list_tickers(conn, cik):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM fund_tickers WHERE cik = ? ORDER BY id", (cik,))]


def list_ticker_changes(conn, cik):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM ticker_changes WHERE cik = ? ORDER BY id DESC", (cik,))]


def local_ticker_ciks(conn, ticker):
    return [r[0] for r in conn.execute("""SELECT cik FROM fund_tickers
        WHERE ticker = ? AND on_roster = 1 ORDER BY cik""", (ticker.strip().upper(),))]


def _ticker_snapshot(conn, cik):
    return [r[0] for r in conn.execute("""SELECT ticker FROM fund_tickers
                WHERE cik = ? AND on_roster = 1 ORDER BY ticker""", (cik,))]


def _audit_tickers(conn, cik, before, after):
    conn.execute("""INSERT INTO ticker_changes
        (cik, changed_at, before_json, after_json) VALUES (?, ?, ?, ?)""",
        (cik, now_iso(), json.dumps(before), json.dumps(after)))


def change_ticker(conn, cik, revision, intent, *, ticker="", ticker_id=None, allow_shared=False):
    """Add/restore or remove one ticker, recording the change atomically."""
    if intent not in ("add", "remove"):
        raise StoreError("Invalid ticker action.")
    symbol = normalize_ticker(ticker) if intent == "add" else ""
    if intent == "add" and not symbol:
        raise StoreError("Enter a ticker to add.")
    with conn:
        changed = conn.execute("""UPDATE funds SET ticker_revision = ticker_revision + 1
                                  WHERE cik = ? AND ticker_revision = ?""", (cik, revision))
        if changed.rowcount != 1:
            raise TickerConflict("Tickers changed in another tab, or this fund was removed.")
        before = _ticker_snapshot(conn, cik)
        if intent == "add":
            if symbol in before:
                raise StoreError("That ticker is already on this fund.")
            if len(before) >= 100:
                raise StoreError("A fund can have at most 100 active tickers.")
            if not allow_shared and any(other != cik for other in local_ticker_ciks(conn, symbol)):
                raise SharedTickerError("This ticker is already used on another tracked fund. Confirm below if intentional.")
            restored = conn.execute("""UPDATE fund_tickers SET on_roster = 1, updated_at = ?
                                      WHERE cik = ? AND ticker = ?""", (now_iso(), cik, symbol))
            if not restored.rowcount:
                _insert_ticker(conn, cik, symbol)
        else:
            removed = conn.execute("""UPDATE fund_tickers SET on_roster = 0, updated_at = ?
                WHERE cik = ? AND id = ? AND on_roster = 1""", (now_iso(), cik, ticker_id))
            if removed.rowcount != 1:
                raise StoreError("That ticker is no longer on this fund. The list below is current.")
        _audit_tickers(conn, cik, before, _ticker_snapshot(conn, cik))


# --- settings ---------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# --- funds ------------------------------------------------------------------

def _decorate(row: sqlite3.Row, tickers=()) -> dict:
    """Row -> dict plus derived ticker display and review status."""
    data = dict(row)
    data["tickers"] = [dict(t) for t in tickers]
    data["ticker"] = "; ".join(sorted(t["ticker"] for t in tickers if t["on_roster"]))
    data["status"] = derive_status(data)
    data["date_passed"] = date_passed(data)
    return data


def add_fund(conn: sqlite3.Connection, cik: str, ticker: str, fund_name: str) -> None:
    symbols = parse_ticker_list(ticker)
    try:
        with conn:
            conn.execute("INSERT INTO funds (cik, fund_name, added_at) VALUES (?, ?, ?)",
                         (cik, fund_name, now_iso()))
            for symbol in symbols:
                _insert_ticker(conn, cik, symbol)
            if symbols:
                _audit_tickers(conn, cik, [], symbols)
    except sqlite3.IntegrityError as exc:
        if str(exc) == "UNIQUE constraint failed: funds.cik":
            raise StoreError(f"{fund_name or cik} is already in the list.") from None
        raise


def delete_fund(conn: sqlite3.Connection, cik: str) -> None:
    conn.execute("DELETE FROM funds WHERE cik = ?", (cik,))
    conn.commit()


def set_active(conn: sqlite3.Connection, cik: str, active: bool) -> None:
    conn.execute("UPDATE funds SET active = ? WHERE cik = ?", (1 if active else 0, cik))
    conn.commit()


def get_fund(conn: sqlite3.Connection, cik: str) -> Optional[dict]:
    row = conn.execute(_FUND_SELECT + " WHERE funds.cik = ?", (cik,)).fetchone()
    return _decorate(row, list_tickers(conn, cik)) if row else None


def list_funds(conn: sqlite3.Connection, active_only: bool = False,
               order: str = "name") -> list[dict]:
    """Every fund, with derived status.

    order="name"  - alphabetical. the default, and what the csv uses.
    order="queue" - needs review first, newest filing first, then the rest
                    alphabetically. the fund list is a work queue and
                    repurchase windows close.

    Sorted here not in sql because status is derived in _decorate, not stored.
    """
    sql = _FUND_SELECT
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY fund_name COLLATE NOCASE, funds.cik"
    tickers = {}
    for row in conn.execute("SELECT * FROM fund_tickers ORDER BY id"):
        tickers.setdefault(row["cik"], []).append(row)
    rows = [_decorate(r, tickers.get(r["cik"], ())) for r in conn.execute(sql)]

    if order == "queue":
        pending = [f for f in rows if f["status"] == "needs_review"]
        rest = [f for f in rows if f["status"] != "needs_review"]
        # two stable sorts: name asc then date desc, so names stay ascending
        # within the same date
        pending.sort(key=lambda f: (f["fund_name"] or "").lower())
        pending.sort(key=lambda f: f["latest_filing_date"] or "", reverse=True)
        return pending + rest
    return rows


# --- review outcomes and status derivation ------------------------------------

REVIEW_OUTCOMES = ("reviewed", "no_date", "not_applicable")

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_iso(s: str | None) -> Optional[date]:
    """Strict yyyy-mm-dd, None for anything else."""
    if not s or not isinstance(s, str):
        return None
    m = _ISO_RE.match(s.strip())
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def derive_status(row: dict) -> str:
    if not row.get("checked_at"):
        return "unchecked"
    # A failed refresh doesn't erase known review work. check_error stays
    # separate on the row so the UI can show both conditions.
    if (row.get("latest_accession")
            and row["latest_accession"] != row.get("last_reviewed_accession")):
        return "needs_review"
    if row.get("check_error"):
        return "error"
    if not row.get("latest_accession"):
        return "no_filings"
    return "up_to_date"


def date_passed(row: dict, today: Optional[date] = None) -> bool:
    d = parse_iso(row.get("next_redemption_date"))
    if d is None:
        return False
    return d < (today or date.today())


def record_check(conn: sqlite3.Connection, cik: str, filing: Optional[dict],
                 error: str = "") -> None:
    """Record one EDGAR check. `filing` is what detect_latest returned.

    A failed check only records the failure. The latest_* columns are the
    only copy of the current filing and there's no history to get them back
    from, so a passing 403 must not wipe them. A check that succeeded and
    found nothing does clear them - that means EDGAR really has nothing.
    """
    if error:
        conn.execute(
            "UPDATE funds SET checked_at = ?, check_error = ? WHERE cik = ?",
            (now_iso(), error, cik),
        )
        conn.commit()
        return

    conn.execute(
        """UPDATE funds SET
             latest_accession = ?, latest_filing_date = ?, latest_form = ?,
             latest_url = ?, checked_at = ?, check_error = ?
           WHERE cik = ?""",
        (
            filing["accession_number"] if filing else None,
            filing["filing_date"] if filing else None,
            filing["form_type"] if filing else None,
            filing["primary_document_url"] if filing else None,
            now_iso(),
            "",
            cik,
        ),
    )
    conn.commit()


STALE_REVIEW = ("This fund was re-checked while you were reading it. "
                "Reload the page and review the new filing.")


def record_review(conn: sqlite3.Connection, cik: str, outcome: str,
                  date_text: str, note: str, accession: str) -> None:
    """Record my review and move the reviewed-filing watermark forward.

    'reviewed' needs a date that parses. 'no_date' and 'not_applicable' leave
    any earlier date alone - a new filing with no date doesn't invalidate
    the one I read from the previous filing.

    `accession` is the filing I actually had open, carried by the form.
    Every write checks it, so a background check landing between page load
    and submit can't stamp my review onto a filing I never saw and quietly
    unflag it.
    """
    if outcome not in REVIEW_OUTCOMES:
        raise StoreError(f"Unknown review outcome {outcome!r}.")

    fund = get_fund(conn, cik)
    if fund is None:
        raise StoreError("That fund is not in the list.")
    if not fund.get("latest_accession"):
        raise StoreError("Check this fund for a filing before reviewing it.")

    if outcome == "reviewed" and parse_iso(date_text) is None:
        raise StoreError("Enter the redemption date as yyyy-mm-dd.")

    # The current state and its history must either both save or both roll
    # back. Copy the filing from the guarded update, not the earlier read.
    with conn:
        cursor = conn.execute(
            """UPDATE funds SET
             next_redemption_date = CASE WHEN ? = 'reviewed' THEN ?
                                        ELSE next_redemption_date END,
             last_reviewed_accession = latest_accession,
             last_reviewed_filing_date = latest_filing_date,
             last_reviewed_form = latest_form,
             last_reviewed_url = latest_url,
             review_status = ?, note = ?, reviewed_at = ?
           WHERE cik = ? AND latest_accession = ?""",
            (outcome, date_text.strip(), outcome, note or "", now_iso(), cik, accession),
        )
        if cursor.rowcount != 1:
            raise StoreError(STALE_REVIEW)
        conn.execute("""
            INSERT INTO review_history
              (cik, accession, filing_date, form, url, outcome, recorded_date,
               note, reviewed_at)
            SELECT cik, last_reviewed_accession, last_reviewed_filing_date,
                   last_reviewed_form, last_reviewed_url, review_status,
                   CASE WHEN review_status = 'reviewed' THEN next_redemption_date END,
                   note, reviewed_at
            FROM funds WHERE cik = ?
        """, (cik,))


def next_needing_review(conn: sqlite3.Connection,
                        after_cik: Optional[str] = None) -> Optional[str]:
    """Cik of the next active fund needing review, in queue order.

    Queue order not alphabetical - this is what Save & next walks and it has
    to match the fund list, otherwise starting at the top of the queue throws
    me somewhere random in the alphabet.

    `after_cik` is the fund I just reviewed. It's only excluded, never used as
    a place to resume from. Resuming by position made every pending fund
    sorting before the start unreachable, so I'd hit "nothing left" with most
    of the queue still pending. The fund just reviewed normally drops out of
    the queue by itself, but excluding it means a review that didn't clear
    the flag can't loop back onto the same fund.

    None when nothing else is pending.
    """
    for fund in list_funds(conn, active_only=True, order="queue"):
        if fund["cik"] == after_cik:
            continue
        if fund["status"] == "needs_review":
            return fund["cik"]
    return None


# --- redemptions.csv and the seed file ---

# the csv I pass around. the db is the source of truth and this
# file just mirrors it - I rewrite it on every add/remove/review and on startup.
CSV_PATH = Path(__file__).with_name("redemptions.csv")

# what an empty db gets seeded from on startup. kept separate from CSV_PATH:
# redemptions.csv has no cik column so seeding from it would import nothing.
SEED_CSV_PATH = Path(__file__).with_name("redemptions-seed.csv")

CSV_FIELDS = ["fund_name", "ticker", "next_redemption_date"]

# requests run on separate threads, don't let two writes interleave
_CSV_LOCK = threading.Lock()


def dated_csv_path(path: Path | str, today: Optional[date] = None) -> Path:
    """`redemptions.csv` -> `redemptions-2026-09-08.csv`, beside it."""
    path = Path(path)
    stamp = (today or date.today()).isoformat()
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def render_csv(conn: sqlite3.Connection) -> bytes:
    """The fund list as csv bytes, alphabetical. Just the three columns the
    team cares about, no review bookkeeping. CRLF line endings for Excel."""
    buf = io.StringIO(newline="")
    writer = _csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writeheader()
    for fund in list_funds(conn):
        writer.writerow({
            "fund_name": fund["fund_name"] or "",
            "ticker": fund["ticker"] or "",
            "next_redemption_date": fund["next_redemption_date"] or "",
        })
    return buf.getvalue().encode("utf-8")


def write_csv(conn: sqlite3.Connection, path: Path | str = CSV_PATH,
              today: Optional[date] = None) -> bool:
    """Rewrite redemptions.csv from the db.

    Returns True if the file changed, False if it already matched and I left
    it alone. Skipping matters because every real write also drops a dated
    snapshot next to it - I want those to be the days something changed, not
    every time I opened the app.

    Writes to a temp file then renames so nobody ever reads half a file.
    Raises OSError if it can't write (usually the file is open in Excel).
    The db is already committed by then so the caller just reports it and
    the next write catches up.
    """
    path = Path(path)
    with _CSV_LOCK:
        # Read under the same lock as the write: a delayed request must not
        # overwrite a newer export with a snapshot taken before waiting.
        content = render_csv(conn)
        try:
            if path.read_bytes() == content:
                return False
        except OSError:
            pass  # no file yet or can't read it, just write
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        shutil.copyfile(path, dated_csv_path(path, today))
    return True


def import_legacy_csv(conn: sqlite3.Connection, path: Path | str) -> int:
    """Seed the fund list from the old CLI tool's csv.

    Needs a cik column, rows without one are skipped since cik is the only
    thing EDGAR can be checked by. redemptions.csv has no cik column, which
    is why the seed is a separate file.

    Does nothing unless the file exists and the funds table is empty. The old
    csv has no accession number so no watermark gets set - every imported
    fund shows as needing review after its first check, which is right, the
    watermark is only worth trusting once I've set it myself.

    A seed that imports nothing says so on stderr (the console window run.bat
    leaves open), otherwise I'd get an empty fund list with no explanation.
    """
    path = Path(path)
    if not path.exists() or list_funds(conn):
        return 0

    imported = 0
    try:
        with path.open(newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                raw_cik = (row.get("cik") or "").strip()
                if not raw_cik:
                    continue
                try:
                    cik = str(int(raw_cik))
                except ValueError:
                    continue
                date_text = (row.get("next_redemption_date") or "").strip()
                try:
                    conn.execute(
                        "INSERT INTO funds (cik, fund_name, added_at) VALUES (?, ?, ?)",
                        (cik, (row.get("fund_name") or "").strip(), now_iso()),
                    )
                except sqlite3.IntegrityError:
                    continue
                conn.execute(
                    """UPDATE funds SET next_redemption_date = ?, review_status = ?,
                                        reviewed_at = ? WHERE cik = ?""",
                    (date_text if parse_iso(date_text) else None,
                     "reviewed" if parse_iso(date_text) else "no_date",
                    now_iso(), cik),
                )
                symbols = parse_ticker_list(row.get("ticker") or "")
                for symbol in symbols:
                    _insert_ticker(conn, cik, symbol)
                if symbols:
                    _audit_tickers(conn, cik, [], symbols)
                imported += 1
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"Seed import from {path} failed: {exc}", file=sys.stderr)
        return 0
    if imported == 0:
        print(f"Seed file {path} contained no importable rows: a cik column "
              f"is required.", file=sys.stderr)
    return imported
