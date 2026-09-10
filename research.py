"""Research: a fund's whole filing list and any of its documents.

Reads EDGAR through sec.py, caches under viewer.CACHE_DIR, writes nothing
to the db. Knows nothing about http.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import sec
import viewer

SUBMISSIONS_TTL = 24 * 60 * 60  # a day. filings land daily, no need to be quicker

SUBMISSIONS_HOST = "https://data.sec.gov/submissions/"
# older pages are named by EDGAR, never by me. anything else is refused so a
# doctored feed can't make me fetch an arbitrary url under this host.
_PAGE_NAME_RE = re.compile(r"^CIK\d{10}-submissions-\d{3}\.json$")

_RECENT_COLUMNS = ("form", "accessionNumber", "filingDate", "primaryDocument",
                   "primaryDocDescription")


def submissions_cache_path(cik: str) -> Path:
    return viewer.CACHE_DIR / "submissions" / f"{sec.strip_cik(cik)}.json"


def _fold_pages(feed: dict, user_agent: str, fetch) -> str:
    """Append every older page's filings onto filings.recent.

    A page that fails to fetch stays in filings.files so the next load
    retries it instead of the list being cached as complete. A name that
    can never be a real page is dropped instead, it would just fail again.

    Returns "" or a note about pages that could not be fetched.
    """
    filings = feed.setdefault("filings", {})
    recent = filings.setdefault("recent", {})
    for column in _RECENT_COLUMNS:
        recent.setdefault(column, [])
    remaining = []
    failed = 0
    for entry in list(filings.get("files") or []):
        name = str(entry.get("name") or "")
        if not _PAGE_NAME_RE.match(name):
            continue
        try:
            page = json.loads(fetch(SUBMISSIONS_HOST + name, user_agent))
        except (sec.SECError, ValueError):
            failed += 1
            remaining.append(entry)
            continue
        count = len(page.get("form") or [])
        for column in _RECENT_COLUMNS:
            values = list(page.get(column) or [])
            values += [""] * (count - len(values))
            recent[column].extend(values[:count])
    filings["files"] = remaining
    if failed:
        return f"{failed} page(s) of older filings could not be fetched; the list is partial."
    return ""


def load_submissions(cik: str, user_agent: str, *, fetch=None,
                     now: Optional[float] = None) -> tuple[dict, float, str]:
    """(feed, age in seconds, error note).

    Fresh cache: served as is. Old cache: refetched, and if that fails the
    old copy is served with the error in the note. No cache and no network
    raises SECError.
    """
    fetch = fetch or sec.sec_get
    now = time.time() if now is None else now
    path = submissions_cache_path(cik)

    cached: Optional[dict] = None
    age = 0.0
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            age = max(0.0, now - os.path.getmtime(path))
        except (OSError, ValueError):
            cached = None
        if cached is not None and age < SUBMISSIONS_TTL:
            files = (cached.get("filings") or {}).get("files")
            if files:
                # some older pages are still missing, retry them on this load too
                before = len((cached["filings"].get("recent") or {}).get("form") or [])
                note = _fold_pages(cached, user_agent, fetch)
                after = len(cached["filings"]["recent"].get("form") or [])
                if after > before:
                    viewer.cache_write(path, json.dumps(cached))
                return cached, age, note
            return cached, age, ""

    try:
        url = f"{SUBMISSIONS_HOST}CIK{sec.pad_cik(cik)}.json"
        feed = json.loads(fetch(url, user_agent))
    except (sec.SECError, ValueError) as exc:
        if cached is not None:
            return cached, age, str(exc)
        if isinstance(exc, ValueError):
            raise sec.SECError("EDGAR returned something that is not a submissions feed.") from exc
        raise

    note = _fold_pages(feed, user_agent, fetch)
    viewer.cache_write(path, json.dumps(feed))
    return feed, 0.0, note


# --- one filing's documents --------------------------------------------------

ARCHIVES_HOST = "https://www.sec.gov/Archives/edgar/data/"

_HTML_SUFFIXES = (".htm", ".html")
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".gif", ".png")
_KINDS = {".pdf": "PDF", ".xml": "XML", ".txt": "text", ".xsd": "XML",
          ".json": "JSON", ".xlsx": "spreadsheet", ".zip": "archive"}


def archive_dir_url(cik: str, accession: str) -> str:
    return (f"{ARCHIVES_HOST}{sec.strip_cik(cik)}/"
            f"{accession.replace('-', '')}/")


def is_html_name(name: str) -> bool:
    return name.lower().endswith(_HTML_SUFFIXES)


def _kind(name: str) -> str:
    lowered = name.lower()
    if is_html_name(lowered):
        return "HTML"
    for suffix, kind in _KINDS.items():
        if lowered.endswith(suffix):
            return kind
    return "file"


def _safe(part: str) -> str:
    """One path segment, traversal-proof. Real EDGAR names come through unchanged."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", part or "unknown")
    cleaned = re.sub(r"\.{2,}", ".", cleaned)  # no ".." left anywhere
    return cleaned.strip(".") or "unknown"


def document_cache_path(accession: str, name: str) -> Path:
    return viewer.CACHE_DIR / "research" / _safe(accession) / _safe(name)


def _skip(name: str, accession: str) -> bool:
    """EDGAR's own index pages, the full .txt bundle, images."""
    lowered = name.lower()
    if lowered == "index.json" or lowered.endswith("-index.html") or lowered.endswith("-index-headers.html"):
        return True
    if lowered == f"{accession}.txt".lower():
        return True
    return lowered.endswith(_IMAGE_SUFFIXES)


def filing_documents(cik: str, accession: str, user_agent: str, *, primary: str = "",
                     description: str = "", fetch=None) -> list[dict]:
    """The documents in one filing, primary first, the rest by name.

    The index is cached for good, filings do not change. If the index can't
    be read the primary document alone is returned so something still opens.
    """
    fetch = fetch or sec.sec_get
    base = archive_dir_url(cik, accession)
    index_path = document_cache_path(accession, "index.json")

    names: list[str] = []
    raw = None
    if index_path.exists():
        raw = index_path.read_text(encoding="utf-8", errors="replace")
    else:
        try:
            raw = fetch(base + "index.json", user_agent).decode("utf-8", errors="replace")
            viewer.cache_write(index_path, raw)
        except sec.SECError:
            raw = None
    if raw is not None:
        try:
            items = (json.loads(raw).get("directory") or {}).get("item") or []
        except (ValueError, AttributeError):
            items = []
        names = [str(item.get("name") or "") for item in items
                 if item.get("name") and not _skip(str(item["name"]), accession)]

    if primary and primary not in names:
        names.insert(0, primary)
    names.sort(key=lambda n: (n != primary, n.lower()))

    return [{
        "name": name,
        "url": base + name,
        "is_html": is_html_name(name),
        "kind": _kind(name),
        "description": description if name == primary else "",
        "primary": name == primary,
    } for name in names]


def fetch_document(url: str, accession: str, name: str, user_agent: str, *,
                   fetch=None) -> str:
    """One research document, from disk if I already have it."""
    path = document_cache_path(accession, name)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    raw = (fetch or sec.sec_get)(url, user_agent)
    text = raw.decode("utf-8", errors="replace")
    viewer.cache_write(path, text)
    return text
