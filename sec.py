"""EDGAR client: rate-limited fetching, cik helpers, finding the latest filing.

Knows nothing about the db or the web server. Every call takes user_agent
explicitly because SEC requires one and I keep it in settings, not the env.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from typing import Optional

SEC_RATE_SECONDS = 0.5  # SEC wants >=500ms between requests

N23C_FORMS = {"N-23C3A", "N-23C3B", "N-23C3C", "N-23C3A/A", "N-23C3B/A", "N-23C3C/A"}
# issuer tender offers only. SC TO-C is just a communication and SC TO-T is
# a third party offer, neither has a repurchase deadline, so picking one up
# as "latest filing" would just force a pointless no-date review.
TO_FORMS = {"SC TO", "SC TO-I", "SC TO/A", "SC TO-I/A"}
ALL_TARGET_FORMS = N23C_FORMS | TO_FORMS

# the research tree sorts a fund's filings into these, in this order. one
# place to add a form. anything unmatched falls into "other".
FILING_GROUPS: list[tuple[str, str, frozenset[str]]] = [
    ("prospectus", "prospectus", frozenset({"N-2", "N-2/A", "497", "497K", "424B3"})),
    ("reports", "reports", frozenset({"N-CSR", "N-CSRS"})),
    ("repurchase", "repurchase notices", frozenset(ALL_TARGET_FORMS)),
    ("holdings", "holdings", frozenset({"N-PORT", "NPORT-P", "NPORT-EX", "N-Q"})),
    ("governance", "governance", frozenset({"DEF 14A", "N-CEN", "N-PX", "40-17G"})),
]
OTHER_GROUP = ("other", "other")


def filing_rows(block: dict) -> list[dict]:
    """One dict per filing from EDGAR's parallel arrays.

    The `recent` block and the older paged files share this shape. Arrays
    can be ragged in odd feeds, so every column is read defensively.
    """
    forms = block.get("form") or []
    accessions = block.get("accessionNumber") or []
    dates = block.get("filingDate") or []
    docs = block.get("primaryDocument") or []
    descriptions = block.get("primaryDocDescription") or []

    def at(column, i):
        return column[i] if i < len(column) and column[i] is not None else ""

    return [{
        "form": at(forms, i),
        "filing_date": at(dates, i),
        "accession": at(accessions, i),
        "primary_document": at(docs, i),
        "description": at(descriptions, i),
    } for i in range(len(forms))]


def filing_group(form: str) -> str:
    for key, _label, forms in FILING_GROUPS:
        if form in forms:
            return key
    return OTHER_GROUP[0]


def group_filings(filings: list[dict]) -> list[dict]:
    """Filings sorted into FILING_GROUPS order, newest first inside each.

    Empty groups are left out. The repurchase group is flagged `tracked`
    because those are the forms the checker watches.
    """
    buckets: dict[str, list[dict]] = {key: [] for key, _, _ in FILING_GROUPS}
    buckets[OTHER_GROUP[0]] = []
    for filing in filings:
        buckets[filing_group(filing["form"])].append(filing)

    labels = {key: label for key, label, _ in FILING_GROUPS}
    labels[OTHER_GROUP[0]] = OTHER_GROUP[1]
    out = []
    for key in list(buckets):
        rows = buckets[key]
        if not rows:
            continue
        rows.sort(key=lambda f: (f["filing_date"], f["accession"]), reverse=True)
        out.append({"key": key, "label": labels[key],
                    "tracked": key == "repurchase", "filings": rows})
    return out

_last_request = 0.0
# sec_get runs on the check thread and request threads at the same time.
# the rate limit is per process so the wait+stamp has to be serialised.
_rate_lock = threading.Lock()


class SECError(Exception):
    """Network or protocol failure talking to SEC. Message is meant to be
    shown on the page, not a traceback."""


def pad_cik(cik: str | int) -> str:
    return str(int(str(cik).strip())).zfill(10)


def strip_cik(cik: str | int) -> str:
    return str(int(str(cik).strip()))


def sec_get(url: str, user_agent: str) -> bytes:
    """GET a sec.gov url with the User-Agent and rate limit SEC wants.

    Raises SECError with a readable message on any failure.
    """
    global _last_request
    if not user_agent:
        raise SECError("No SEC user agent configured. Set one on the Settings page.")

    # stamp before the request not after, so the lock only covers the wait
    # and one slow response doesn't stall everyone else
    with _rate_lock:
        wait = SEC_RATE_SECONDS - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()

    req = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            encoding = resp.headers.get("Content-Encoding", "")
    except urllib.error.HTTPError as exc:
        raise SECError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise SECError(f"Could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SECError(f"Timed out fetching {url}") from exc

    if encoding == "gzip":
        import gzip
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        import zlib
        raw = zlib.decompress(raw)
    return raw


def fetch_submissions(cik: str, user_agent: str) -> dict:
    url = f"https://data.sec.gov/submissions/CIK{pad_cik(cik)}.json"
    return json.loads(sec_get(url, user_agent))


def find_latest_filing(submissions: dict, target_forms: set[str]) -> Optional[dict]:
    """Most recent filing whose form is in target_forms, by filingDate."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    filing_dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []

    best_idx, best_date = -1, None
    for i, form in enumerate(forms):
        if form not in target_forms:
            continue
        d_str = filing_dates[i] if i < len(filing_dates) else None
        if not d_str:
            continue
        try:
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        if best_date is None or d > best_date:
            best_date, best_idx = d, i

    if best_idx == -1:
        return None
    return {
        "form_type": forms[best_idx],
        "filing_date": filing_dates[best_idx],
        "accession_number": accessions[best_idx] if best_idx < len(accessions) else "",
        "primary_document": primary_docs[best_idx] if best_idx < len(primary_docs) else "",
    }


def detect_latest(submissions: dict) -> Optional[dict]:
    """Latest N-23C3 / SC TO filing with the Archives document url worked out."""
    row = find_latest_filing(submissions, ALL_TARGET_FORMS)
    if not row:
        return None
    cik_no_zeros = strip_cik(submissions.get("cik", "0") or "0")
    acc_no_dashes = row["accession_number"].replace("-", "")
    return {
        "form_type": row["form_type"],
        "filing_date": row["filing_date"],
        "accession_number": row["accession_number"],
        "primary_document_url": (
            f"https://www.sec.gov/Archives/edgar/data/{cik_no_zeros}/"
            f"{acc_no_dashes}/{row['primary_document']}"
        ),
    }


COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_ticker_cache: dict[str, str] = {}
_ticker_lock = threading.Lock()


def reset_ticker_cache() -> None:
    """Clear the ticker map. For tests."""
    _ticker_cache.clear()


def fund_identity(submissions: dict) -> tuple[str, str]:
    """(fund_name, ticker) from a submissions feed. Ticker can be empty."""
    tickers = submissions.get("tickers") or []
    return submissions.get("name", "") or "", (tickers[0] if tickers else "")


def load_company_tickers(user_agent: str, fetch=sec_get) -> dict[str, str]:
    """Uppercase ticker -> stripped cik. Fetched once per process."""
    with _ticker_lock:
        if _ticker_cache:
            return _ticker_cache
        raw = json.loads(fetch(COMPANY_TICKERS_URL, user_agent))
        for entry in raw.values():
            ticker = str(entry.get("ticker", "")).upper()
            if ticker:
                _ticker_cache[ticker] = strip_cik(entry["cik_str"])
        return _ticker_cache


def resolve_query(query: str, user_agent: str, fetch=sec_get) -> str:
    """Typed cik or ticker -> stripped cik."""
    query = (query or "").strip()
    if not query:
        raise SECError("Enter a CIK or ticker.")
    if query.isdigit():
        return strip_cik(query)
    mapping = load_company_tickers(user_agent, fetch=fetch)
    cik = mapping.get(query.upper())
    if not cik:
        raise SECError(f"No fund found for ticker {query!r}. Try the CIK instead.")
    return cik


# need both hosts. data.sec.gov (submissions api) accepts any User-Agent,
# www.sec.gov (the filings) enforces the "Name email@domain" format. if I
# only probed the first one it'd say ok while every filing 403s.
CONNECTION_TESTS = (
    ("data.sec.gov", "https://data.sec.gov/submissions/CIK0000000000.json"),
    ("www.sec.gov", "https://www.sec.gov/robots.txt"),
)


def _probe(url: str, user_agent: str, fetch) -> tuple[bool, str]:
    """One host. Returns (reachable, short description of what happened).

    404 counts as reachable - dns, tls and http all worked.
    """
    try:
        fetch(url, user_agent)
        return True, "reachable"
    except SECError as exc:
        message = str(exc)
        lowered = message.lower()
        if "http 404" in lowered:
            return True, "reachable"
        if "http 403" in lowered:
            return False, ("refused (403) — SEC wants a contact string shaped "
                           "like 'Your Name you@example.com'")
        if "certificate verify failed" in lowered or "certificate_verify_failed" in lowered:
            return False, ("TLS verification failed — a proxy is probably "
                           "intercepting HTTPS. Point SSL_CERT_FILE at the "
                           "proxy's root certificate and restart")
        if ("name or service not known" in lowered or "getaddrinfo" in lowered
                or "nodename nor servname" in lowered):
            return False, ("DNS lookup failed — if this machine uses a proxy, "
                           "set HTTPS_PROXY before starting")
        if "timed out" in lowered:
            return False, "timed out — a firewall or proxy may be blocking it"
        return False, message


def check_connection(user_agent: str, fetch=sec_get) -> tuple[bool, str]:
    """Probe every SEC host I depend on and say what happened with each."""
    if not user_agent:
        return False, "Set a SEC user agent first — SEC requires one on every request."
    results = [(host, *_probe(url, user_agent, fetch)) for host, url in CONNECTION_TESTS]
    ok = all(reachable for _, reachable, _ in results)
    return ok, " | ".join(f"{host}: {note}" for host, _, note in results)
