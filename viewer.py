"""Filing rendering: sanitizing, date highlighting, disk cache.

Filings are someone else's html rendered inside my app, so anything that can
execute gets stripped here before it reaches a page.
"""

from __future__ import annotations

import bisect
import html as _html
import os
import re
import threading
import urllib.parse
from html.parser import HTMLParser
from pathlib import Path

import sec

# dropped along with everything inside them
DROP_ELEMENTS = frozenset({
    "script", "iframe", "object", "embed", "applet", "form",
    "input", "button", "select", "textarea", "link", "base",
})

# emitted verbatim. escaping css turns `td > p` into `td &gt; p` and breaks
# the selector, and the date tables depend on these stylesheets. HTMLParser
# ends the element at </style> so raw content can't break out, and the filing
# CSP (default-src 'none', style-src 'unsafe-inline') means no @import and
# images only from www.sec.gov.
RAW_TEXT_ELEMENTS = frozenset({"style"})

# never have a closing tag
VOID_ELEMENTS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

_JS_URL_RE = re.compile(r"^\s*javascript:", re.IGNORECASE)

# a click inside the sandboxed frame navigates the frame itself, so an
# off-site link would replace the filing pane with whatever. sec.gov links
# are kept and forced into a new tab, every other href is dropped and just
# the text stays.
LINK_ELEMENTS = frozenset({"a", "area"})


def is_sec_url(url: str, base_url: str) -> bool:
    """True if `url` resolved against `base_url` points at sec.gov."""
    resolved = urllib.parse.urljoin(base_url, url.strip())
    parts = urllib.parse.urlsplit(resolved)
    host = (parts.hostname or "").lower()
    return (parts.scheme in ("http", "https")
            and (host == "sec.gov" or host.endswith(".sec.gov")))


class Sanitizer(HTMLParser):
    """Rewrites filing html, dropping anything that can execute.

    A real parser, not regex, so a malformed document can't sneak a tag or
    attribute past it.
    """

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=False)
        self.base_url = base_url
        self.out: list[str] = []
        self._suppress_depth = 0
        self._suppressing: str | None = None
        self._in_raw_text = False

    def handle_starttag(self, tag, attrs):
        if self._suppressing:
            if tag == self._suppressing:
                self._suppress_depth += 1
            return
        if tag in DROP_ELEMENTS:
            if tag not in VOID_ELEMENTS:
                self._suppressing = tag
                self._suppress_depth = 1
            return
        if tag == "meta" and self._is_refresh(attrs):
            return
        if tag in RAW_TEXT_ELEMENTS:
            self._in_raw_text = True
        self.out.append(self._render_tag(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        if self._suppressing or tag in DROP_ELEMENTS:
            return
        if tag == "meta" and self._is_refresh(attrs):
            return
        if tag in RAW_TEXT_ELEMENTS:
            # there's no self-closing <style> in html, a browser would treat
            # the rest of the document as css. EDGAR's xhtml-ish filings do
            # write <style/> though, so emit an empty pair instead.
            self.out.append(self._render_tag(tag, attrs))
            self.out.append(f"</{tag}>")
            return
        self.out.append(self._render_tag(tag, attrs, force_void=True))

    def handle_endtag(self, tag):
        if self._suppressing:
            if tag == self._suppressing:
                self._suppress_depth -= 1
                if self._suppress_depth <= 0:
                    self._suppressing = None
            return
        if tag in DROP_ELEMENTS or tag in VOID_ELEMENTS:
            return
        if tag in RAW_TEXT_ELEMENTS:
            self._in_raw_text = False
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._suppressing:
            return
        # css is cdata, escaping it breaks selectors
        self.out.append(data if self._in_raw_text
                        else _html.escape(data, quote=False))

    def handle_entityref(self, name):
        if not self._suppressing:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self._suppressing:
            self.out.append(f"&#{name};")

    @staticmethod
    def _is_refresh(attrs) -> bool:
        return any(k.lower() == "http-equiv" and (v or "").strip().lower() == "refresh"
                   for k, v in attrs)

    def _render_tag(self, tag, attrs, force_void: bool = False) -> str:
        kept: list[tuple[str, str | None]] = []
        for key, value in attrs:
            key_lower = key.lower()
            if key_lower.startswith("on"):
                continue
            # namespaced attrs: take the name after the last colon
            attr_name = key_lower.rpartition(":")[-1]
            if attr_name in ("href", "src", "action") and value:
                # strip tab/cr/lf before checking for javascript: urls
                cleaned_value = re.sub(r"[\t\r\n]", "", value)
                if _JS_URL_RE.match(cleaned_value):
                    continue
            kept.append((key_lower, value))
        if tag in LINK_ELEMENTS:
            kept = self._link_attrs(kept)
        parts = [tag]
        for key, value in kept:
            if value is None:
                parts.append(key)
            else:
                parts.append(f'{key}="{_html.escape(value, quote=True)}"')
        body = " ".join(parts)
        if force_void or tag in VOID_ELEMENTS:
            return f"<{body}/>"
        return f"<{body}>"


    def _link_attrs(self, kept):
        """The LINK_ELEMENTS rule, applied to an <a> or <area>."""
        href = next((v for k, v in kept if k == "href"), None)
        if href is None:
            return kept
        if href.strip().startswith("#"):
            return kept  # in-page anchor, stays in the frame
        # the filing doesn't get to pick where its links open
        kept = [(k, v) for k, v in kept if k not in ("target", "rel")]
        if not is_sec_url(href, self.base_url):
            return [(k, v) for k, v in kept if k != "href"]
        return kept + [("target", "_blank"), ("rel", "noopener")]


def sanitize(raw_html: str, base_url: str) -> str:
    """Cleaned filing markup with one <base> tag so relative paths resolve."""
    parser = Sanitizer(base_url)
    parser.feed(raw_html or "")
    parser.close()
    base = f'<base href="{_html.escape(base_url, quote=True)}"/>'
    return base + "".join(parser.out)


MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_MONTH_ALT = "|".join(MONTHS)

DATE_RE = re.compile(
    r"(?:"
    rf"(?:{_MONTH_ALT})[a-z]*\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
    r"|"
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_ALT})[a-z]*\.?,?\s+\d{{4}}"
    r"|"
    r"\d{1,2}/\d{1,2}/\d{2,4}"
    r"|"
    r"\d{4}-\d{2}-\d{2}"
    r")",
    re.IGNORECASE,
)

_MONTH_FIRST_RE = re.compile(
    rf"^({_MONTH_ALT})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})$",
    re.IGNORECASE,
)
_DAY_FIRST_RE = re.compile(
    rf"^(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})[a-z]*\.?,?\s+(\d{{4}})$",
    re.IGNORECASE,
)
_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def normalize_date(text: str) -> str:
    """Any of the four formats I recognise -> yyyy-mm-dd, or '' if it doesn't parse."""
    from datetime import date as _date

    cleaned = re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()

    year = month = day = None
    if m := _MONTH_FIRST_RE.match(cleaned):
        month, day, year = MONTHS[m[1][:3].lower()], int(m[2]), int(m[3])
    elif m := _DAY_FIRST_RE.match(cleaned):
        day, month, year = int(m[1]), MONTHS[m[2][:3].lower()], int(m[3])
    elif m := _SLASH_RE.match(cleaned):
        month, day, year = int(m[1]), int(m[2]), int(m[3])
        if year < 100:
            year += 2000
    elif m := _ISO_DATE_RE.match(cleaned):
        year, month, day = int(m[1]), int(m[2]), int(m[3])

    if year is None:
        return ""
    try:
        return _date(year, month, day).isoformat()
    except ValueError:
        return ""


# a date near one of these is worth looking at. first line is how N-23C3s
# say it (94% of the funds), second line is the SC TO wording.
DEADLINE_PHRASES = (
    "repurchase request deadline", "request deadline",
    "must be received", "must be received by", "repurchase request must",
    "commencement date", "notice date", "expiration date", "offer expires",
)

# how many words from a phrase a date can be and still go green.
#
# I checked this through sanitize() + annotate() on the 47 cached real
# filings (not strip_html on raw markup, since the annotator output is what
# I actually see):
#
#   marks per filing:  median 12, max 69
#   green per filing:  median 3,  max 16
#   the recorded deadline is green in 43 of 47
#
# of the 4 misses, 3 are SC TO-I/A amendments that don't contain the deadline
# at all (it's in the original, that's what the amendment warning is for) and
# 1 was stale db data. so 43/44 where the date is actually in the document.
GREEN_WINDOW_WORDS = 30

_WORD_RE = re.compile(r"\S+")


def _word_starts(text: str) -> list[int]:
    return [m.start() for m in _WORD_RE.finditer(text)]


def _word_index(starts: list[int], pos: int) -> int:
    """Index of the word containing `pos`, or the one just before it."""
    return max(0, bisect.bisect_right(starts, pos) - 1)


def classify_dates(text: str) -> list[tuple[int, int, str, bool]]:
    """Every date in `text` as (start, end, iso, is_green), in document order.

    Green = within GREEN_WINDOW_WORDS words of a DEADLINE_PHRASES phrase.
    Distance is counted over the whole buffer, which is why the annotator
    works on the joined document text and not one text node at a time.
    """
    if not text:
        return []

    starts = _word_starts(text)
    lowered = text.lower()

    anchors: list[int] = []
    for phrase in DEADLINE_PHRASES:
        i = lowered.find(phrase)
        while i != -1:
            anchors.append(_word_index(starts, i))
            i = lowered.find(phrase, i + 1)
    anchors.sort()

    out: list[tuple[int, int, str, bool]] = []
    for match in DATE_RE.finditer(text):
        word = _word_index(starts, match.start())
        green = False
        if anchors:
            j = bisect.bisect_left(anchors, word)
            for k in (j - 1, j):
                if 0 <= k < len(anchors) and abs(anchors[k] - word) <= GREEN_WINDOW_WORDS:
                    green = True
                    break
        out.append((match.start(), match.end(),
                    normalize_date(match.group(0)), green))
    return out


# text in these never gets marked - not rendered, or not prose
_SKIP_ELEMENTS = frozenset({"script", "style", "head", "title", "textarea"})

# put between text nodes so a phrase ending one node and a date starting the
# next don't get counted as one word
_NODE_SEPARATOR = "\n"

# most text nodes a date is allowed to span, counting whitespace-only ones.
#
# _NODE_SEPARATOR is whitespace and DATE_RE's \s+ matches it, so with no cap
# a match can run across any number of blank nodes. one real filing
# (0000930413-26-001814.html) had a lone "6" in one cell and "MAY 2026" in
# another twelve nodes over, which made up a clickable 2026-05-06 that isn't
# in the document and painted ten blobs across the empty cells between. a
# made-up clickable date is the one thing this tool must never produce.
#
# 3 is measured not guessed. node-span histogram over the 47 cached filings:
# 668 one-node dates, 13 two-node, 57 three-node, and that one twelve-node
# outlier. 3 keeps all 70 real splits and rejects exactly the made-up one.
MAX_NODE_SPAN = 3


class _TextCollector(HTMLParser):
    """Pass 1: all the markable text joined up, with per-node offsets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._len = 0
        self.spans: list[tuple[int, int]] = []
        self._in_skip = 0
        self._in_raw_text = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_ELEMENTS:
            self._in_skip += 1
        if tag in RAW_TEXT_ELEMENTS:
            self._in_raw_text = True

    def handle_endtag(self, tag):
        if tag in _SKIP_ELEMENTS:
            self._in_skip = max(0, self._in_skip - 1)
        if tag in RAW_TEXT_ELEMENTS:
            self._in_raw_text = False

    def handle_data(self, data):
        if self._in_skip or self._in_raw_text:
            return
        start = self._len
        self._parts.append(data)
        self._len += len(data)
        self.spans.append((start, self._len))
        self._parts.append(_NODE_SEPARATOR)
        self._len += len(_NODE_SEPARATOR)

    @property
    def buffer(self) -> str:
        return "".join(self._parts)


class Annotator(HTMLParser):
    """Pass 2: re-emit the markup with the dates from pass 1 wrapped in <mark>.

    Same skip and raw-text rules as _TextCollector, so the Nth text node here
    is the Nth node the collector saw.

    `per_node` maps node index -> fragments of dates inside that node:
    (start, end, date_i, iso, green), start/end relative to the node's own
    text. A date spanning several nodes shows up once per node with the same
    date_i and iso. Counting dates rather than fragments is annotate()'s job.
    """

    def __init__(self, per_node: dict[int, list[tuple[int, int, int, str, bool]]]):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._per_node = per_node
        self._node = -1
        self._in_skip = 0
        self._in_raw_text = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_ELEMENTS:
            self._in_skip += 1
        if tag in RAW_TEXT_ELEMENTS:
            self._in_raw_text = True
        self.out.append(self._render(tag, attrs, tag in VOID_ELEMENTS))

    def handle_startendtag(self, tag, attrs):
        self.out.append(self._render(tag, attrs, True))

    def handle_endtag(self, tag):
        if tag in _SKIP_ELEMENTS:
            self._in_skip = max(0, self._in_skip - 1)
        if tag in RAW_TEXT_ELEMENTS:
            self._in_raw_text = False
        if tag not in VOID_ELEMENTS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self._in_raw_text:
            self.out.append(data)  # css, verbatim, see RAW_TEXT_ELEMENTS
            return
        if self._in_skip:
            self.out.append(_html.escape(data, quote=False))
            return

        self._node += 1
        cursor = 0
        for start, end, date_i, iso, green in self._per_node.get(self._node, ()):
            self.out.append(_html.escape(data[cursor:start], quote=False))
            cls = "hit green" if green else "hit"
            iso_attr = f' data-iso="{_html.escape(iso, quote=True)}"' if iso else ""
            self.out.append(
                f'<mark class="{cls}" data-i="{date_i}"{iso_attr}>'
                f'{_html.escape(data[start:end], quote=False)}</mark>')
            cursor = end
        self.out.append(_html.escape(data[cursor:], quote=False))

    @staticmethod
    def _render(tag, attrs, void: bool) -> str:
        parts = [tag]
        for key, value in attrs:
            parts.append(key if value is None
                         else f'{key}="{_html.escape(value, quote=True)}"')
        body = " ".join(parts)
        return f"<{body}/>" if void else f"<{body}>"


def annotate(html: str) -> tuple[str, int, int]:
    """Sanitized markup in, markup with dates wrapped in <mark> out.

    Returns (markup, total marks, green marks). Counts are per date not per
    fragment. Real filings split dates across text nodes all the time (a
    <font> around one word is common), so a date gets wrapped once per node
    it touches and the fragments share a date index, iso and green flag. That
    way no <mark> ever crosses a tag but it still reads as one highlight.
    """
    if not html:
        return "", 0, 0

    collector = _TextCollector()
    collector.feed(html)
    collector.close()

    node_starts = [start for start, _ in collector.spans]
    n_nodes = len(collector.spans)
    per_node: dict[int, list[tuple[int, int, int, str, bool]]] = {}
    total = 0
    green_count = 0

    for start, end, iso, green in classify_dates(collector.buffer):
        idx = max(bisect.bisect_right(node_starts, start) - 1, 0)
        fragments = []  # (node_idx, frag_start_rel, frag_end_rel)
        for j in range(idx, n_nodes):
            nstart, nend = collector.spans[j]
            if nstart >= end:
                break
            frag_start, frag_end = max(start, nstart), min(end, nend)
            if frag_start < frag_end:
                fragments.append((j, frag_start - nstart, frag_end - nstart))
        if not fragments:
            continue  # only matched separator text between nodes, nothing to mark
        if len(fragments) > MAX_NODE_SPAN:
            continue  # spans too many nodes to be a real date
        # a fragment that's only whitespace has no part of the date in it,
        # marking it just paints a blob on blank space
        painted = [f for f in fragments
                   if collector.buffer[collector.spans[f[0]][0] + f[1]:
                                       collector.spans[f[0]][0] + f[2]].strip()]
        if not painted:
            continue
        date_i = total
        total += 1
        if green:
            green_count += 1
        for node_idx, frag_start, frag_end in painted:
            per_node.setdefault(node_idx, []).append(
                (frag_start, frag_end, date_i, iso, green))

    emitter = Annotator(per_node)
    emitter.feed(html)
    emitter.close()
    return "".join(emitter.out), total, green_count


CACHE_DIR = Path(__file__).with_name("cache")

_NAV_SCRIPT = """
(function () {
  // a date can be several <mark> fragments (one per text node it crosses,
  // e.g. a <font> around the month) sharing a data-i. group by data-i so
  // counting and prev/next work on dates, not fragments.
  var marks = document.querySelectorAll('mark.hit');
  var order = [];      // unique data-i values, in document order
  var groups = {};     // data-i -> its fragment elements
  var isGreen = {};    // data-i -> true if any fragment is green

  for (var i = 0; i < marks.length; i++) {
    var mark = marks[i];
    var key = mark.dataset.i;
    if (!groups[key]) {
      groups[key] = [];
      order.push(key);
    }
    groups[key].push(mark);
    if (mark.classList.contains('green')) isGreen[key] = true;
  }

  var green = [];
  for (var g = 0; g < order.length; g++) {
    if (isGreen[order[g]]) green.push(g);
  }

  function post(msg) {
    if (window.parent !== window) window.parent.postMessage(msg, '*');
  }

  function show(i) {
    for (var j = 0; j < order.length; j++) {
      var frags = groups[order[j]];
      for (var f = 0; f < frags.length; f++) frags[f].classList.remove('current');
    }
    var target = groups[order[i]];
    for (var f2 = 0; f2 < target.length; f2++) target[f2].classList.add('current');
    target[0].scrollIntoView({block: 'center'});
    post({type: 'at', index: i});
  }

  post({type: 'count', total: order.length, green: green});

  window.addEventListener('message', function (event) {
    if (event.source !== window.parent) return;
    var data = event.data || {};
    if (data.type !== 'goto') return;
    if (data.index < 0 || data.index >= order.length) return;
    show(data.index);
  });

  for (var k = 0; k < marks.length; k++) {
    (function (mark) {
      mark.addEventListener('click', function () {
        if (mark.dataset.iso) post({type: 'pick', iso: mark.dataset.iso});
      });
    })(marks[k]);
  }

  // land on the first likely date instead of the top of a long document.
  // nothing likely = stay put, scrolling somewhere would imply I know
  // something I don't.
  if (green.length) show(green[0]);
})();
"""

_FILING_CSS = """
body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.5;
       margin: 0; padding: 1.25rem; color: #111; background: #fff; }
table { border-collapse: collapse; }
td, th { padding: 0.2rem 0.5rem; vertical-align: top; }
mark.hit { background: #fdf2b8; padding: 0 1px; }
mark.hit.green { background: #b8ecc4; box-shadow: inset 0 -2px 0 #2f9e52; }
mark.hit.current { outline: 2px solid #2c5fd0; }
"""


def base_url_for(filing_url: str) -> str:
    """The Archives directory the filing lives in, for <base href>."""
    return filing_url if filing_url.endswith("/") else filing_url.rsplit("/", 1)[0] + "/"


def filing_cache_path(accession: str) -> Path:
    r"""Cache file for an accession number, name made filesystem-safe.

    This is about path traversal ('../../etc/passwd'), not collisions. Two
    accessions could in theory map to one name but real EDGAR ones
    (\d{10}-\d{2}-\d{6}) come through unchanged.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", accession or "unknown")
    return CACHE_DIR / f"{safe}.html"


def cache_write(path: Path, text: str) -> None:
    """Write text to path without ever leaving a half-written file.

    Temp file then replace. The temp name is per writer because two tabs
    can fetch the same thing at once, and with one shared name one tab
    would replace a file the other is still writing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(str(temp_path), str(path))
    except BaseException:
        temp_path.unlink(missing_ok=True)  # unique name, would linger otherwise
        raise


def fetch_filing(url: str, accession: str, user_agent: str, fetch=None) -> str:
    """The filing's primary document, from the disk cache if I already have it."""
    path = filing_cache_path(accession)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    raw = (fetch or sec.sec_get)(url, user_agent)
    text = raw.decode("utf-8", errors="replace")
    cache_write(path, text)
    return text


def build_filing_page(raw_html: str, base_url: str) -> str:
    """A complete self-contained document for the review iframe."""
    body, _total, _green = annotate(sanitize(raw_html, base_url))
    return (
        "<!doctype html>\n<html>"
        f"<head><style>{_FILING_CSS}</style></head>"
        f"<body>{body}"
        f"<script>{_NAV_SCRIPT}</script>"
        "</body></html>"
    )


def build_document_page(raw_html: str, base_url: str) -> str:
    """A research document for the frame: sanitized, no date marks, no nav.

    The annotator is tuned to repurchase notices and would litter a
    prospectus with false marks, so research skips it.
    """
    return (
        "<!doctype html>\n<html>"
        f"<head><style>{_FILING_CSS}</style></head>"
        f"<body>{sanitize(raw_html, base_url)}</body></html>"
    )
