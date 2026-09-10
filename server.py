"""HTTP layer: routing, request/response helpers, static files.

Binds 127.0.0.1 only. Each request thread opens its own sqlite connection
and closes it before responding.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import checker
import pages
import research
import sec
import store
import viewer

STATIC_DIR = Path(__file__).with_name("static")

CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}

SETUP_BANNER = pages.SETUP_BANNER

# what /api/health answers with, so a second launch can tell this app apart
# from whatever else might be sitting on the port
APP_ID = "interval-fund-viewer"

# form bodies are read into memory in one go. the biggest real one is a
# review note, a megabyte is way past that and way short of anything harmful.
MAX_FORM_BYTES = 1_000_000


class RequestRefused(Exception):
    """Raised by a helper to answer the request with an error status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


FILING_CSP = (
    "default-src 'none'; img-src https://www.sec.gov data:; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'"
)


def _filing_error_page(message: str) -> str:
    """Something readable for the frame when the filing can't be fetched."""
    return (
        "<!doctype html><html><body style=\"font-family:sans-serif;padding:2rem\">"
        f"<h2>Could not load the filing</h2><p>{pages.esc(message)}</p>"
        "<p>Open it on sec.gov using the link beside this frame, or run the "
        "check again.</p></body></html>"
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "IntervalsLite"
    db_path = store.DB_PATH

    # why the last redemptions.csv write failed, "" if it didn't. shows as a
    # banner until a write succeeds so I know the csv is behind the db.
    csv_error = ""

    # --- routing ---------------------------------------------------------

    routes: list[tuple[str, re.Pattern, str]] = [
        ("GET", re.compile(r"^/$"), "page_index"),
        ("GET", re.compile(r"^/settings$"), "page_settings"),
        ("POST", re.compile(r"^/settings$"), "action_save_settings"),
        ("POST", re.compile(r"^/settings/test$"), "action_test_connection"),
        ("POST", re.compile(r"^/funds$"), "action_add_fund"),
        ("POST", re.compile(r"^/funds/(?P<cik>\d+)/delete$"), "action_delete_fund"),
        ("POST", re.compile(r"^/funds/(?P<cik>\d+)/toggle$"), "action_toggle_fund"),
        ("POST", re.compile(r"^/check$"), "action_check_all"),
        ("POST", re.compile(r"^/check/(?P<cik>\d+)$"), "action_check_one"),
        ("GET", re.compile(r"^/api/progress$"), "api_progress"),
        ("GET", re.compile(r"^/api/health$"), "api_health"),
        ("GET", re.compile(r"^/static/(?P<name>[A-Za-z0-9._-]+)$"), "serve_static"),
        ("GET", re.compile(r"^/fund/(?P<cik>\d+)$"), "page_review"),
        ("GET", re.compile(r"^/fund/(?P<cik>\d+)/tickers$"), "page_tickers"),
        ("POST", re.compile(r"^/fund/(?P<cik>\d+)/tickers$"), "action_save_tickers"),
        ("GET", re.compile(r"^/filing/(?P<cik>\d+)$"), "serve_filing"),
        ("GET", re.compile(r"^/research$"), "page_research"),
        ("GET", re.compile(r"^/research/(?P<cik>\d+)$"), "page_research_fund"),
        ("GET", re.compile(r"^/research/(?P<cik>\d+)/(?P<accession>\d{10}-\d{2}-\d{6})$"),
         "page_research_filing"),
        ("GET", re.compile(r"^/research/(?P<cik>\d+)/(?P<accession>\d{10}-\d{2}-\d{6})/"
                           r"(?P<document>[A-Za-z0-9._-]+)$"),
         "serve_research_document"),
        ("POST", re.compile(r"^/fund/(?P<cik>\d+)/review$"), "action_save_review"),
    ]

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        if not self._same_origin(method):
            return
        path = urllib.parse.urlsplit(self.path).path
        for route_method, pattern, name in self.routes:
            if route_method != method:
                continue
            match = pattern.match(path)
            if match:
                params = match.groupdict()
                if "cik" in params:
                    # stored stripped, but EDGAR shows them padded to ten
                    # digits and that's how they get pasted
                    params["cik"] = sec.strip_cik(params["cik"])
                try:
                    getattr(self, name)(**params)
                except RequestRefused as exc:
                    self.send_error(exc.status, exc.message)
                return
        self.send_error(404, "Not found")

    def log_message(self, fmt, *args):
        pass  # the browser is the ui, keep the console quiet

    # --- origin checks ----------------------------------------------------

    def _own_hosts(self) -> set[str]:
        """The host:port spellings that mean this server."""
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            hosts |= {"127.0.0.1", "localhost"}
        return hosts

    def _same_origin(self, method: str) -> bool:
        """Refuse requests aimed at this server from somewhere else.

        There's no auth on purpose, so without this any page I have open in
        the same browser could auto-submit a form to a destructive route or
        fire EDGAR requests under my User-Agent. Rejecting a foreign Host
        also closes dns rebinding, which would let a remote page read the
        fund list.
        """
        hosts = self._own_hosts()
        if (self.headers.get("Host") or "").strip().lower() not in hosts:
            self.send_error(400, "Bad host header")
            return False
        if method == "POST":
            origin = (self.headers.get("Origin") or "").strip()
            # a form post with no Origin is same-origin from an older
            # browser, refusing it would break the no-js forms
            if origin and origin.lower() not in {f"http://{h}" for h in hosts}:
                self.send_error(400, "Cross-origin request refused")
                return False
        return True

    # --- helpers ---------------------------------------------------------

    def _conn(self):
        return store.connect(self.db_path)

    def _form(self) -> dict[str, str]:
        header = (self.headers.get("Content-Length") or "0").strip()
        if not header.isdigit():
            raise RequestRefused(400, "Bad Content-Length")
        length = int(header)
        if length > MAX_FORM_BYTES:
            raise RequestRefused(413, "Form body too large")
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _query(self) -> dict[str, str]:
        parsed = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,
                                       keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _send(self, body: bytes, content_type: str, status: int = 200,
              extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, markup: str, status: int = 200, extra_headers: dict | None = None):
        self._send(markup.encode("utf-8"), "text/html; charset=utf-8", status,
                   extra_headers)

    def _json(self, payload: dict, status: int = 200):
        self._send(json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _banner(self, conn) -> str:
        messages = []
        if not store.get_setting(conn, "sec_user_agent"):
            messages.append(SETUP_BANNER)
        if Handler.csv_error:
            messages.append(
                f"{store.CSV_PATH.name} is out of date: {Handler.csv_error} "
                "It is rewritten on the next change, or reload the fund list.")
        return " ".join(messages)

    def _sync_csv(self, conn) -> str:
        """Rewrite redemptions.csv from the db. Returns "" or why it failed.

        Call after anything the csv carries: add, remove, saved date. Never
        raises - the db is already committed and a stale csv is fixable, a
        lost review isn't. Startup rewrites it too so a restart heals it.
        """
        try:
            store.write_csv(conn, store.CSV_PATH)
        except OSError as exc:
            Handler.csv_error = f"{exc.strerror or exc}."
            return Handler.csv_error
        Handler.csv_error = ""
        return ""

    # --- static ----------------------------------------------------------

    def serve_static(self, name: str):
        path = (STATIC_DIR / name).resolve()
        if path.parent != STATIC_DIR.resolve() or not path.is_file():
            self.send_error(404, "Not found")
            return
        content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        self._send(path.read_bytes(), content_type)

    # --- pages -----------------------------------------------------------

    def _redirect_home(self, message: str = "", error: str = "", draft=None):
        params = {}
        if draft:
            params.update({k: draft.get(k, "") for k in ("query", "tickers")})
        if message:
            params["msg"] = message
        if error:
            params["err"] = error
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        self._redirect("/" + query)

    def page_index(self):
        query = self._query()
        conn = self._conn()
        try:
            if Handler.csv_error:
                self._sync_csv(conn)
            funds = store.list_funds(conn, order="queue")
            banner = self._banner(conn)
        finally:
            conn.close()
        markup = pages.fund_list_page(funds, checker.progress(),
                                      query.get("msg", ""), query.get("err", ""),
                                      banner=banner, add_draft=query)
        self._html(markup)

    def action_add_fund(self):
        form = self._form()
        query = (form.get("query") or "").strip()
        conn = self._conn()
        try:
            try:
                entered = store.parse_ticker_list(form.get("tickers", ""))
            except store.StoreError as exc:
                self._redirect_home(error=str(exc), draft=form)
                return
            matches = ([sec.strip_cik(query)] if query.isdigit()
                       else store.local_ticker_ciks(conn, query))
            if len(matches) > 1:
                self._redirect_home(error="That ticker matches several tracked funds. Use a CIK.", draft=form)
                return
            if matches and store.get_fund(conn, matches[0]):
                self._redirect(f"/?open_tickers={matches[0]}&" + urllib.parse.urlencode(
                    {"err": "This fund is already tracked. Use the drawer to add or remove tickers."}))
                return
            user_agent = store.get_setting(conn, "sec_user_agent")
            if not user_agent:
                self._redirect_home(error="Set a SEC user agent before adding funds.", draft=form)
                return
            try:
                cik = sec.resolve_query(query, user_agent)
                if store.get_fund(conn, cik):
                    self._redirect(f"/?open_tickers={cik}&" + urllib.parse.urlencode(
                        {"err": "This fund is already tracked. Use the drawer to add or remove tickers."}))
                    return
                submissions = sec.fetch_submissions(cik, user_agent)
                name, _ = sec.fund_identity(submissions)
                # The user's resolved query is an explicitly supplied ticker.
                # Other SEC symbols are not silently adopted.
                if not entered and not query.isdigit():
                    entered = store.parse_ticker_list(query)
                shared = [symbol for symbol in entered if store.local_ticker_ciks(conn, symbol)]
                if shared:
                    raise store.StoreError(
                        "Already used on another tracked fund: " + ", ".join(shared)
                        + ". Add this fund by CIK with the ticker field empty, then use its ticker drawer to confirm shared symbols.")
                store.add_fund(conn, cik, "; ".join(entered), name)
            except (sec.SECError, store.StoreError) as exc:
                self._redirect_home(error=str(exc), draft=form)
                return
            csv_error = self._sync_csv(conn)
        finally:
            conn.close()
        if not entered:
            self._redirect(f"/?open_tickers={cik}&" + urllib.parse.urlencode(
                {"msg": f"Added {name or cik}. Add a ticker in the drawer.", "err": csv_error}))
        else:
            self._redirect_home(message=f"Added {name or cik}.", error=csv_error)

    def page_tickers(self, cik, ticker="", error="", shared=False, status_code=200):
        conn = self._conn()
        try:
            fund = store.get_fund(conn, cik)
            banner = self._banner(conn)
        finally:
            conn.close()
        if fund is None:
            self.send_error(404, "Fund not found")
            return
        query = self._query()
        self._html(pages.tickers_page(fund, ticker=ticker,
            message=query.get("msg", ""), error=error or query.get("err", ""),
            banner=banner, shared=shared), status=status_code)

    def action_save_tickers(self, cik):
        form = self._form()
        revision = form.get("revision", "")
        intent = form.get("intent", "")
        if not revision.isascii() or not revision.isdigit() or len(revision) > 18:
            raise RequestRefused(400, "Invalid ticker revision")
        if intent not in ("add", "remove"):
            raise RequestRefused(400, "Invalid ticker action")
        ticker = form.get("ticker", "")
        conn = self._conn()
        error, shared, status = "", False, 400
        try:
            fund = store.get_fund(conn, cik)
            if fund is None:
                self.send_error(404, "Fund not found")
                return
            try:
                store.change_ticker(conn, cik, int(revision), intent,
                    ticker=ticker, ticker_id=form.get("id"), allow_shared="allow_shared" in form)
            except store.StoreError as exc:
                error = str(exc)
                shared = isinstance(exc, store.SharedTickerError)
                if isinstance(exc, store.TickerConflict):
                    status = 409
                    error = "Tickers changed in another tab. The list below is current; check it and try again."
            except sqlite3.Error:
                error = "Could not save the change. Your ticker is kept below; try again."
            else:
                self._sync_csv(conn)
        finally:
            conn.close()
        if error:
            self.page_tickers(cik, ticker=ticker, error=error, shared=shared, status_code=status)
        else:
            self._redirect(f"/fund/{cik}/tickers?msg=Ticker+" + ("added." if intent == "add" else "removed."))

    def action_delete_fund(self, cik: str):
        conn = self._conn()
        try:
            store.delete_fund(conn, cik)
            csv_error = self._sync_csv(conn)
        finally:
            conn.close()
        self._redirect_home(message="Fund removed.", error=csv_error)

    def action_toggle_fund(self, cik: str):
        conn = self._conn()
        try:
            fund = store.get_fund(conn, cik)
            if fund:
                store.set_active(conn, cik, not fund["active"])
        finally:
            conn.close()
        self._redirect_home()

    def _start_check(self, ciks: list[str]):
        conn = self._conn()
        try:
            user_agent = store.get_setting(conn, "sec_user_agent")
        finally:
            conn.close()
        if not user_agent:
            self._redirect_home(error="Set a SEC user agent before running a check.")
            return
        if not ciks:
            self._redirect_home(error="No active funds to check.")
            return
        db_path = self.db_path
        started = checker.start_check(ciks, user_agent,
                                      lambda: store.connect(db_path))
        if not started:
            self._redirect_home(error="A check is already running.")
            return
        self._redirect_home()

    def action_check_all(self):
        conn = self._conn()
        try:
            ciks = [f["cik"] for f in store.list_funds(conn, active_only=True)]
        finally:
            conn.close()
        self._start_check(ciks)

    def action_check_one(self, cik: str):
        conn = self._conn()
        try:
            fund = store.get_fund(conn, cik)
        finally:
            conn.close()
        if fund is None:
            # never hit EDGAR for a cik nobody added
            self.send_error(404, "Not found")
            return
        self._start_check([cik])

    def api_progress(self):
        self._json(checker.progress())

    def api_health(self):
        """Who I am and which database I serve. app.find_running reads this
        on a second launch to reuse this instance instead of starting another
        one beside it."""
        self._json({"app": APP_ID, "db": str(Path(self.db_path).resolve())})

    def page_settings(self, message: str = "", ok=None):
        conn = self._conn()
        try:
            markup = pages.settings_page(
                store.get_setting(conn, "sec_user_agent"),
                store.get_setting(conn, "port", "8765"),
                message, ok,
                banner=self._banner(conn),
            )
        finally:
            conn.close()
        self._html(markup)

    def action_save_settings(self):
        form = self._form()
        conn = self._conn()
        try:
            store.set_setting(conn, "sec_user_agent",
                              (form.get("sec_user_agent") or "").strip())
            port = (form.get("port") or "").strip()
            if port.isdigit() and 1 <= int(port) <= 65535:
                store.set_setting(conn, "port", port)
        finally:
            conn.close()
        self._redirect("/settings")

    def action_test_connection(self):
        conn = self._conn()
        try:
            user_agent = store.get_setting(conn, "sec_user_agent")
        finally:
            conn.close()
        ok, message = sec.check_connection(user_agent)
        self.page_settings(message=message, ok=ok)

    def page_review(self, cik: str, message: str = "", error: str = "",
                    status_code: int = 200, draft: dict[str, str] | None = None):
        conn = self._conn()
        try:
            fund = store.get_fund(conn, cik)
            reviews = store.list_reviews(conn, cik)
            banner = self._banner(conn)
        finally:
            conn.close()

        if fund is None:
            self.send_error(404, "Not found")
            return

        markup = pages.review_page(fund, message, error, banner=banner,
                                   reviews=reviews, draft=draft)
        self._html(markup, status=status_code)

    def serve_filing(self, cik: str):
        conn = self._conn()
        try:
            fund = store.get_fund(conn, cik)
            user_agent = store.get_setting(conn, "sec_user_agent")
        finally:
            conn.close()

        if fund is None or not fund["latest_url"]:
            self.send_error(404, "No filing for this fund")
            return

        try:
            raw = viewer.fetch_filing(fund["latest_url"], fund["latest_accession"],
                                      user_agent)
            document = viewer.build_filing_page(
                raw, viewer.base_url_for(fund["latest_url"]))
        except Exception as exc:  # noqa: BLE001 - show me what happened
            document = _filing_error_page(str(exc) or type(exc).__name__)

        self._html(document, extra_headers={
            "Content-Security-Policy": FILING_CSP,
            "X-Content-Type-Options": "nosniff",
        })

    # --- research --------------------------------------------------------

    def _research_context(self, cik: str):
        """(fund, user_agent, active funds). fund is None when off the roster."""
        conn = self._conn()
        try:
            fund = store.get_fund(conn, cik) if cik else None
            user_agent = store.get_setting(conn, "sec_user_agent")
            funds = store.list_funds(conn, active_only=True)
            banner = self._banner(conn)
        finally:
            conn.close()
        return fund, user_agent, funds, banner

    def _fund_groups(self, fund, user_agent):
        """(groups, filings by accession, note, error) for one fund's feed."""
        try:
            feed, age, note = research.load_submissions(fund["cik"], user_agent)
        except Exception as exc:  # noqa: BLE001 - show me what happened
            return [], {}, "", str(exc)
        filings = sec.filing_rows((feed.get("filings") or {}).get("recent") or {})
        if age >= research.SUBMISSIONS_TTL:
            days = int(age // 86400)
            note = (f"EDGAR could not be reached; showing a filing list from "
                    f"{days} day{'s' if days != 1 else ''} ago. {note}").strip()
        return sec.group_filings(filings), {f["accession"]: f for f in filings}, note, ""

    def page_research(self):
        _fund, _ua, funds, banner = self._research_context("")
        self._html(pages.research_page(funds, banner=banner))

    def page_research_fund(self, cik: str):
        fund, user_agent, funds, banner = self._research_context(cik)
        if fund is None:
            self.send_error(404, "Not found")
            return
        groups, _by_acc, note, error = self._fund_groups(fund, user_agent)
        subtree = pages.research_subtree(fund, groups, note=note, error=error)
        if self._query().get("fragment"):
            self._html(subtree)
            return
        self._html(pages.research_page(funds, expanded=fund, subtree=subtree, banner=banner))

    def page_research_filing(self, cik: str, accession: str):
        fund, user_agent, funds, banner = self._research_context(cik)
        if fund is None:
            self.send_error(404, "Not found")
            return
        groups, by_acc, note, error = self._fund_groups(fund, user_agent)
        filing = by_acc.get(accession)
        if filing is None:
            self.send_error(404, "Not found")
            return
        documents = research.filing_documents(
            cik, accession, user_agent,
            primary=filing["primary_document"], description=filing["description"])
        docs_html = pages.research_documents(fund, filing, documents)
        if self._query().get("fragment"):
            self._html(docs_html)
            return
        subtree = pages.research_subtree(fund, groups, note=note, error=error,
                                         open_accession=accession, documents_html=docs_html)
        self._html(pages.research_page(funds, expanded=fund, subtree=subtree, banner=banner))

    def serve_research_document(self, cik: str, accession: str, document: str):
        fund, user_agent, _funds, _banner = self._research_context(cik)
        if fund is None:
            self.send_error(404, "Not found")
            return
        _groups, by_acc, _note, _error = self._fund_groups(fund, user_agent)
        filing = by_acc.get(accession)
        if filing is None:
            self.send_error(404, "Not found")
            return
        documents = research.filing_documents(
            cik, accession, user_agent, primary=filing["primary_document"])
        match = next((d for d in documents if d["name"] == document and d["is_html"]), None)
        if match is None:
            self.send_error(404, "Not found")
            return
        try:
            raw = research.fetch_document(match["url"], accession, document, user_agent)
            page = viewer.build_document_page(raw, research.archive_dir_url(cik, accession))
        except Exception as exc:  # noqa: BLE001 - show me what happened
            page = _filing_error_page(str(exc) or type(exc).__name__)
        self._html(page, extra_headers={
            "Content-Security-Policy": FILING_CSP,
            "X-Content-Type-Options": "nosniff",
        })

    def action_save_review(self, cik: str):
        form = self._form()
        go_next = "outcome_next" in form
        outcome = form.get("outcome") or form.get("outcome_next") or ""

        error = None
        following = None
        csv_error = ""
        conn = self._conn()
        try:
            try:
                # missing accession counts as a mismatch, the form always
                # carries the filing I was shown
                store.record_review(conn, cik, outcome,
                                    form.get("next_redemption_date", ""),
                                    form.get("note", ""),
                                    form.get("accession", ""))
            except store.StoreError as exc:
                error = str(exc)
            else:
                csv_error = self._sync_csv(conn)
                if go_next:
                    following = store.next_needing_review(conn, after_cik=cik)
        finally:
            conn.close()

        if error is not None:
            self.page_review(cik, error=error, draft=form)
            return
        if go_next and following:
            # csv failure isn't lost here, the banner shows it
            self._redirect(f"/fund/{following}")
            return
        self._redirect_home(message="Review saved.", error=csv_error)


class LoopbackServer(ThreadingHTTPServer):
    """Binds without SO_REUSEADDR, same as app.pick_port's probe.

    HTTPServer turns address reuse on by default. On Windows that lets a
    second instance bind a port that already has a listener and the two
    split the connections between them.
    """

    allow_reuse_address = False


def make_server(port: int, db_path) -> ThreadingHTTPServer:
    """A server on loopback with handlers pointed at `db_path`."""
    bound = type("BoundHandler", (Handler,), {"db_path": Path(db_path)})
    return LoopbackServer(("127.0.0.1", port), bound)


def run(port: int, db_path) -> None:
    httpd = make_server(port, db_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
