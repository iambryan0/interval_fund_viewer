import json
import socket
import contextlib
import io
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import checker
import app
import research
import sec
import server
import store
import viewer


class ServerTestCase(unittest.TestCase):
    """Starts a real server on a random port against a temp db."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "funds.db"
        conn = store.connect(self.db)
        store.init_db(conn)
        conn.close()
        # store.CSV_PATH is the real file beside the source, point it at tmp
        # so tests don't write there
        self._csv = store.CSV_PATH
        store.CSV_PATH = Path(self.tmp.name) / "redemptions.csv"
        server.Handler.csv_error = ""

        self.httpd = server.make_server(0, self.db)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        store.CSV_PATH = self._csv
        server.Handler.csv_error = ""
        self.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, headers=None):
        req = urllib.request.Request(self.url(path), headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8"), resp
        except urllib.error.HTTPError as exc:
            exc.read()
            exc.close()
            raise

    def post(self, path, fields, follow=False, headers=None):
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(self.url(path), data=data, method="POST",
                                     headers=headers or {})
        opener = urllib.request.build_opener()
        if not follow:
            opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8"), resp
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            exc.close()
            return exc.code, body, exc

    def conn(self):
        return store.connect(self.db)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RoutingTest(ServerTestCase):
    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_static_css_is_served_with_the_right_type(self):
        status, body, resp = self.get("/static/app.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", resp.headers["Content-Type"])
        self.assertIn(".topbar", body)

    def test_static_traversal_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/..%2Fserver.py")
        self.assertIn(ctx.exception.code, (400, 404))

    def test_unknown_static_file_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/static/nothing.css")
        self.assertEqual(ctx.exception.code, 404)


class ServerBindTest(unittest.TestCase):
    """The real bind has to follow the same rule as app.pick_port's probe: on
    Windows SO_REUSEADDR would let a second instance bind a port that already
    has a listener."""

    def test_the_server_does_not_allow_address_reuse(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            httpd = server.make_server(0, Path(tmp.name) / "funds.db")
            try:
                self.assertFalse(httpd.allow_reuse_address)
            finally:
                httpd.server_close()
        finally:
            tmp.cleanup()


class OriginAndHostTest(ServerTestCase):
    """No auth on purpose, so a page I happen to have open must not be able
    to drive this server."""

    def setUp(self):
        super().setUp()
        conn = self.conn()
        store.add_fund(conn, "1234567", "ACME", "Acme Interval Fund")
        conn.close()

    def test_a_post_from_a_foreign_origin_is_refused_and_changes_nothing(self):
        status, _, _ = self.post("/funds/1234567/delete", {},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 400)
        conn = self.conn()
        self.assertIsNotNone(store.get_fund(conn, "1234567"))
        conn.close()

    def test_a_post_from_the_servers_own_origin_is_allowed(self):
        status, _, _ = self.post(
            "/funds/1234567/delete", {},
            headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 303)
        conn = self.conn()
        self.assertIsNone(store.get_fund(conn, "1234567"))
        conn.close()

    def test_a_form_post_with_no_origin_header_still_works(self):
        status, _, _ = self.post("/funds/1234567/delete", {})
        self.assertEqual(status, 303)
        conn = self.conn()
        self.assertIsNone(store.get_fund(conn, "1234567"))
        conn.close()

    def test_a_get_with_a_foreign_host_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/", headers={"Host": "fund-tracker.evil.example"})
        self.assertEqual(ctx.exception.code, 400)

    def test_localhost_is_accepted_as_a_host(self):
        status, _, _ = self.get("/", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)


class SettingsRouteTest(ServerTestCase):
    def test_settings_page_renders(self):
        status, body, _ = self.get("/settings")
        self.assertEqual(status, 200)
        self.assertIn("SEC user agent", body)

    def test_saving_settings_persists_and_redirects(self):
        status, _, resp = self.post("/settings",
                                    {"sec_user_agent": "Me me@example.com",
                                     "port": "9000"})
        self.assertEqual(status, 303)
        self.assertEqual(resp.headers["Location"], "/settings")
        conn = self.conn()
        self.assertEqual(store.get_setting(conn, "sec_user_agent"), "Me me@example.com")
        self.assertEqual(store.get_setting(conn, "port"), "9000")
        conn.close()

    def test_setup_banner_shows_until_a_user_agent_is_set(self):
        _, body, _ = self.get("/settings")
        self.assertIn("banner", body)
        self.post("/settings", {"sec_user_agent": "Me me@example.com", "port": "8765"})
        _, body, _ = self.get("/settings")
        self.assertNotIn('class="banner"', body)

    def test_a_bad_port_is_rejected_without_saving(self):
        self.post("/settings", {"sec_user_agent": "Me me@example.com", "port": "abc"})
        conn = self.conn()
        self.assertEqual(store.get_setting(conn, "port", "8765"), "8765")
        conn.close()


class FundRouteTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        checker.reset()
        conn = self.conn()
        store.set_setting(conn, "sec_user_agent", "Me me@example.com")
        conn.close()
        self._real_resolve = sec.resolve_query
        self._real_submissions = sec.fetch_submissions
        sec.resolve_query = lambda q, ua, fetch=None: "1234567"
        sec.fetch_submissions = lambda cik, ua: {
            "cik": cik, "name": "Acme Interval Fund", "tickers": ["ACME"]}

    def tearDown(self):
        sec.resolve_query = self._real_resolve
        sec.fetch_submissions = self._real_submissions
        checker.join(timeout=5)
        checker.reset()
        super().tearDown()

    def test_index_renders_the_list(self):
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("No funds yet", body)

    def test_adding_a_fund_persists_it_and_redirects(self):
        status, _, resp = self.post("/funds", {"query": "ACME"})
        self.assertEqual(status, 303)
        self.assertTrue(resp.headers["Location"].startswith("/"))
        conn = self.conn()
        fund = store.get_fund(conn, "1234567")
        conn.close()
        self.assertEqual(fund["fund_name"], "Acme Interval Fund")
        self.assertEqual(fund["ticker"], "ACME")

    def test_adding_the_same_fund_twice_reports_an_error(self):
        self.post("/funds", {"query": "ACME"})
        _, _, resp = self.post("/funds", {"query": "ACME"})
        self.assertIn("err=", resp.headers["Location"])

    def test_an_unresolvable_query_reports_an_error(self):
        def boom(q, ua, fetch=None):
            raise sec.SECError("No fund found for ticker 'NOPE'.")

        sec.resolve_query = boom
        _, _, resp = self.post("/funds", {"query": "NOPE"})
        self.assertIn("err=", resp.headers["Location"])
        conn = self.conn()
        self.assertEqual(store.list_funds(conn), [])
        conn.close()

    def test_toggle_disables_and_re_enables(self):
        self.post("/funds", {"query": "ACME"})
        self.post("/funds/1234567/toggle", {})
        conn = self.conn()
        self.assertEqual(store.get_fund(conn, "1234567")["active"], 0)
        conn.close()
        self.post("/funds/1234567/toggle", {})
        conn = self.conn()
        self.assertEqual(store.get_fund(conn, "1234567")["active"], 1)
        conn.close()

    def test_delete_removes_the_fund(self):
        self.post("/funds", {"query": "ACME"})
        self.post("/funds/1234567/delete", {})
        conn = self.conn()
        self.assertIsNone(store.get_fund(conn, "1234567"))
        conn.close()

    def test_deleting_an_unknown_fund_is_not_an_error(self):
        status, _, _ = self.post("/funds/9999999/delete", {})
        self.assertEqual(status, 303)

    def test_index_lists_pending_funds_before_reviewed_ones(self):
        # Acme is pending (checked, never reviewed). Aaa sorts first
        # alphabetically but is reviewed, so queue order has to flip them.
        conn = self.conn()
        store.add_fund(conn, "1234567", "ACME", "Acme Interval Fund")
        store.record_check(conn, "1234567", FILING_ROW)
        store.add_fund(conn, "1111111", "", "Aaa Reviewed Fund")
        store.record_check(conn, "1111111", FILING_ROW)
        store.record_review(conn, "1111111", "reviewed", "2026-12-01", "",
                            FILING_ROW["accession_number"])
        conn.close()
        _, body, _ = self.get("/")
        self.assertLess(body.index("Acme Interval Fund"),
                        body.index("Aaa Reviewed Fund"))


class CheckRouteTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        checker.reset()
        conn = self.conn()
        store.set_setting(conn, "sec_user_agent", "Me me@example.com")
        store.add_fund(conn, "1234567", "ACME", "Acme Interval Fund")
        conn.close()
        self._real_submissions = sec.fetch_submissions
        self._real_detect = sec.detect_latest
        sec.fetch_submissions = lambda cik, ua: {"cik": cik}
        sec.detect_latest = lambda subs: {
            "form_type": "N-23C3A", "filing_date": "2026-08-14",
            "accession_number": "0001234567-26-000123",
            "primary_document_url": "https://www.sec.gov/Archives/edgar/data/1/x/n.htm",
        }

    def tearDown(self):
        checker.join(timeout=5)
        checker.reset()
        sec.fetch_submissions = self._real_submissions
        sec.detect_latest = self._real_detect
        super().tearDown()

    def test_full_check_runs_and_records(self):
        status, _, _ = self.post("/check", {})
        self.assertEqual(status, 303)
        checker.join(timeout=5)
        conn = self.conn()
        self.assertEqual(store.get_fund(conn, "1234567")["status"], "needs_review")
        conn.close()

    def test_single_fund_check_runs(self):
        self.post("/check/1234567", {})
        checker.join(timeout=5)
        conn = self.conn()
        self.assertEqual(store.get_fund(conn, "1234567")["status"], "needs_review")
        conn.close()

    def test_progress_endpoint_returns_json(self):
        import json
        status, body, resp = self.get("/api/progress")
        self.assertEqual(status, 200)
        self.assertIn("application/json", resp.headers["Content-Type"])
        payload = json.loads(body)
        self.assertIn("running", payload)
        self.assertIn("errors", payload)

    def test_checking_an_unknown_fund_is_404_and_starts_no_job(self):
        status, _, _ = self.post("/check/9999999", {})
        self.assertEqual(status, 404)
        checker.join(timeout=5)
        self.assertFalse(checker.progress()["running"])
        self.assertEqual(checker.progress()["total"], 0)

    def test_check_without_a_user_agent_is_refused(self):
        conn = self.conn()
        store.set_setting(conn, "sec_user_agent", "")
        conn.close()
        _, _, resp = self.post("/check", {})
        self.assertIn("err=", resp.headers["Location"])
        checker.join(timeout=5)
        conn = self.conn()
        self.assertEqual(store.get_fund(conn, "1234567")["status"], "unchecked")
        conn.close()

    def test_check_with_no_active_funds_is_refused(self):
        conn = self.conn()
        store.set_active(conn, "1234567", False)
        conn.close()
        _, _, resp = self.post("/check", {})
        self.assertIn("err=", resp.headers["Location"])


FILING_ROW = {
    "form_type": "N-23C3A", "filing_date": "2026-08-14",
    "accession_number": "0001234567-26-000123",
    "primary_document_url": "https://www.sec.gov/Archives/edgar/data/1/x/notice.htm",
}

FILING_HTML = (
    "<html><body><p>All repurchase requests must be received by "
    "September 12, 2026.</p><script>var x=1;</script></body></html>"
)


class ReviewRouteTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        conn = self.conn()
        store.set_setting(conn, "sec_user_agent", "Me me@example.com")
        store.add_fund(conn, "1234567", "ACME", "Acme Interval Fund")
        store.record_check(conn, "1234567", FILING_ROW)
        conn.close()
        self._cache = viewer.CACHE_DIR
        viewer.CACHE_DIR = Path(self.tmp.name) / "cache"
        self._real_fetch = viewer.fetch_filing
        viewer.fetch_filing = lambda url, acc, ua, fetch=None: FILING_HTML

    def tearDown(self):
        viewer.fetch_filing = self._real_fetch
        viewer.CACHE_DIR = self._cache
        super().tearDown()

    def test_review_page_renders(self):
        status, body, _ = self.get("/fund/1234567")
        self.assertEqual(status, 200)
        self.assertIn("Acme Interval Fund", body)
        self.assertIn('src="/filing/1234567"', body)

    def test_filing_route_marks_the_deadline_green(self):
        _, body, _ = self.get("/filing/1234567")
        self.assertIn('class="hit green"', body)
        self.assertIn('data-iso="2026-09-12"', body)

    def test_review_page_no_longer_fetches_the_filing(self):
        calls = []
        real = viewer.fetch_filing
        viewer.fetch_filing = lambda *a, **k: (calls.append(1), FILING_HTML)[1]
        try:
            status, _, _ = self.get("/fund/1234567")
        finally:
            viewer.fetch_filing = real
        self.assertEqual(status, 200)
        self.assertEqual(calls, [])

    def test_unknown_fund_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/fund/9999999")
        self.assertEqual(ctx.exception.code, 404)

    def test_filing_route_serves_sanitized_html_with_csp(self):
        status, body, resp = self.get("/filing/1234567")
        self.assertEqual(status, 200)
        self.assertNotIn("var x=1", body)
        self.assertIn('class="hit green"', body)
        self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])

    def test_filing_route_for_a_fund_with_no_filing_is_404(self):
        conn = self.conn()
        store.add_fund(conn, "555", "", "Unchecked Fund")
        conn.close()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/filing/555")
        self.assertEqual(ctx.exception.code, 404)

    def test_a_fetch_failure_renders_a_readable_frame(self):
        def boom(url, acc, ua, fetch=None):
            raise sec.SECError("HTTP 403 from www.sec.gov")

        viewer.fetch_filing = boom
        status, body, _ = self.get("/filing/1234567")
        self.assertEqual(status, 200)
        self.assertIn("403", body)

    def test_saving_a_date_records_the_review_and_redirects_home(self):
        status, _, resp = self.post("/fund/1234567/review",
                                    {"outcome": "reviewed",
                                     "next_redemption_date": "2026-09-12",
                                     "note": "section 3",
                                     "accession": FILING_ROW["accession_number"]})
        self.assertEqual(status, 303)
        self.assertEqual(resp.headers["Location"].split("?")[0], "/")
        conn = self.conn()
        fund = store.get_fund(conn, "1234567")
        conn.close()
        self.assertEqual(fund["next_redemption_date"], "2026-09-12")
        self.assertEqual(fund["status"], "up_to_date")
        self.assertEqual(fund["note"], "section 3")

    def test_no_date_outcome_advances_the_watermark(self):
        self.post("/fund/1234567/review",
                  {"outcome": "no_date", "next_redemption_date": "", "note": "",
                   "accession": FILING_ROW["accession_number"]})
        conn = self.conn()
        self.assertEqual(store.get_fund(conn, "1234567")["status"], "up_to_date")
        conn.close()

    def test_an_invalid_date_re_renders_with_an_error_and_saves_nothing(self):
        status, body, _ = self.post("/fund/1234567/review",
                                    {"outcome": "reviewed",
                                     "next_redemption_date": "nonsense",
                                     "note": "Read section 3\nKeep this draft <note>",
                                     "accession": FILING_ROW["accession_number"]},
                                    follow=True)
        self.assertEqual(status, 200)
        self.assertIn("yyyy-mm-dd", body)
        self.assertIn("Read section 3\nKeep this draft &lt;note&gt;</textarea>", body)
        self.assertIn("Submitted date: nonsense", body)
        conn = self.conn()
        self.assertEqual(store.get_fund(conn, "1234567")["status"], "needs_review")
        conn.close()

    def test_a_stale_accession_is_refused_and_saves_nothing(self):
        """A background check landing between page load and submit must not
        stamp my review onto a filing I never saw."""
        conn = self.conn()
        store.record_check(conn, "1234567",
                           dict(FILING_ROW, accession_number="0001234567-26-000999"))
        conn.close()
        status, body, _ = self.post("/fund/1234567/review",
                                    {"outcome": "reviewed",
                                     "next_redemption_date": "2026-09-12",
                                     "note": "Notes from the original filing",
                                     "accession": FILING_ROW["accession_number"]},
                                    follow=True)
        self.assertEqual(status, 200)
        self.assertIn("re-checked", body)
        self.assertIn("Notes from the original filing</textarea>", body)
        self.assertNotIn('value="2026-09-12"', body)
        self.assertIn("Your note has been kept", body)
        self.assertIn('name="accession" value="0001234567-26-000999"', body)
        conn = self.conn()
        fund = store.get_fund(conn, "1234567")
        conn.close()
        self.assertEqual(fund["status"], "needs_review")
        self.assertIsNone(fund["next_redemption_date"])

    def test_validation_failure_retains_valid_date_and_note_for_the_same_filing(self):
        status, body, _ = self.post(
            "/fund/1234567/review",
            {"outcome": "invalid", "next_redemption_date": "2026-09-12",
             "note": "My draft", "accession": FILING_ROW["accession_number"]})
        self.assertEqual(status, 200)
        self.assertIn('value="2026-09-12"', body)
        self.assertIn("My draft</textarea>", body)
        conn = self.conn()
        try:
            self.assertEqual(store.list_reviews(conn, "1234567"), [])
        finally:
            conn.close()
        # A normal GET must still start blank, even after this failed POST.
        _, body, _ = self.get("/fund/1234567")
        self.assertNotIn('value="2026-09-12"', body)
        self.assertNotIn("My draft</textarea>", body)

    def test_a_submit_with_no_accession_field_is_refused(self):
        status, body, _ = self.post("/fund/1234567/review",
                                    {"outcome": "reviewed",
                                     "next_redemption_date": "2026-09-12",
                                     "note": ""},
                                    follow=True)
        self.assertEqual(status, 200)
        self.assertIn("re-checked", body)
        conn = self.conn()
        self.assertEqual(store.get_fund(conn, "1234567")["status"], "needs_review")
        conn.close()

    def test_the_review_form_carries_the_rendered_accession(self):
        _, body, _ = self.get("/fund/1234567")
        self.assertIn(
            f'<input type="hidden" name="accession" '
            f'value="{FILING_ROW["accession_number"]}">', body)

    def test_review_page_loads_history_from_the_database(self):
        self.post("/fund/1234567/review",
                  {"outcome": "reviewed", "next_redemption_date": "2026-09-12",
                   "note": "Original source note",
                   "accession": FILING_ROW["accession_number"]})
        self.post("/fund/1234567/review",
                  {"outcome": "no_date", "note": "Follow-up note",
                   "accession": FILING_ROW["accession_number"]})
        _, body, _ = self.get("/fund/1234567")
        self.assertIn("Recorded-date source", body)
        self.assertIn("Review history (2)", body)
        self.assertIn("Original source note", body)
        self.assertIn("Follow-up note", body)

    def test_save_and_next_goes_to_the_next_pending_fund(self):
        conn = self.conn()
        store.add_fund(conn, "7654321", "BETA", "Beta Credit Fund")
        store.record_check(conn, "7654321", FILING_ROW)
        conn.close()
        _, _, resp = self.post("/fund/1234567/review",
                               {"outcome_next": "reviewed",
                                "next_redemption_date": "2026-09-12", "note": "",
                                "accession": FILING_ROW["accession_number"]})
        self.assertEqual(resp.headers["Location"], "/fund/7654321")

    def test_save_and_next_returns_home_when_nothing_is_left(self):
        _, _, resp = self.post("/fund/1234567/review",
                               {"outcome_next": "reviewed",
                                "next_redemption_date": "2026-09-12", "note": "",
                                "accession": FILING_ROW["accession_number"]})
        self.assertEqual(resp.headers["Location"].split("?")[0], "/")

    def test_save_and_next_includes_pending_filing_with_a_failed_check(self):
        conn = self.conn()
        try:
            store.add_fund(conn, "7654321", "BETA", "Beta Credit Fund")
            store.record_check(conn, "7654321", FILING_ROW)
            store.record_check(conn, "7654321", None, error="HTTP 403")
        finally:
            conn.close()
        _, body, _ = self.get("/")
        self.assertIn("2 needing review", body)
        self.assertIn("check failed", body)
        self.assertIn("HTTP 403", body)
        status, _, resp = self.post(
            "/fund/1234567/review",
            {"outcome_next": "reviewed", "next_redemption_date": "2026-09-12",
             "note": "", "accession": FILING_ROW["accession_number"]})
        self.assertEqual(status, 303)
        self.assertEqual(resp.headers["Location"], "/fund/7654321")


class CsvMirrorTest(ReviewRouteTest):
    """db is the source of truth, redemptions.csv mirrors it. every change
    is on disk before the response comes back, no export step."""

    def csv_text(self):
        return store.CSV_PATH.read_text(encoding="utf-8")

    def test_the_export_route_is_gone(self):
        status, _, _ = self.post("/export", {})
        self.assertEqual(status, 404)

    def test_the_fund_list_page_offers_no_export(self):
        _, body, _ = self.get("/")
        self.assertNotIn("/export", body)
        self.assertNotIn("Export", body)

    def test_saving_a_date_writes_it_to_the_csv_at_once(self):
        self.assertFalse(store.CSV_PATH.exists())
        status, _, resp = self.post("/fund/1234567/review",
                                    {"outcome": "reviewed",
                                     "next_redemption_date": "2026-09-12",
                                     "note": "", "accession": FILING_ROW["accession_number"]})
        self.assertEqual(status, 303)
        self.assertNotIn("err=", resp.headers["Location"])
        self.assertIn("Acme Interval Fund,ACME,2026-09-12", self.csv_text())

    def test_save_and_next_writes_the_csv_too(self):
        self.post("/fund/1234567/review",
                  {"outcome_next": "reviewed", "next_redemption_date": "2026-10-01",
                   "note": "", "accession": FILING_ROW["accession_number"]})
        self.assertIn("2026-10-01", self.csv_text())

    def test_removing_a_fund_drops_it_from_the_csv(self):
        conn = self.conn()
        store.add_fund(conn, "999", "", "Doomed Fund")
        store.write_csv(conn, store.CSV_PATH)
        conn.close()
        self.assertIn("Doomed Fund", self.csv_text())
        self.post("/funds/999/delete", {})
        self.assertNotIn("Doomed Fund", self.csv_text())
        self.assertIn("Acme Interval Fund", self.csv_text())

    def test_a_failed_write_still_saves_the_review_and_says_so(self):
        store.CSV_PATH.mkdir()  # directory in the way = can't write
        status, _, resp = self.post("/fund/1234567/review",
                                    {"outcome": "reviewed",
                                     "next_redemption_date": "2026-09-12",
                                     "note": "", "accession": FILING_ROW["accession_number"]})
        self.assertEqual(status, 303)
        self.assertIn("err=", resp.headers["Location"])

        conn = self.conn()
        try:
            self.assertEqual(store.get_fund(conn, "1234567")["next_redemption_date"],
                             "2026-09-12")
        finally:
            conn.close()

        _, body, _ = self.get("/settings")
        self.assertIn("redemptions.csv is out of date", body)
        self.assertNotIn("Open settings", body)

    def test_the_banner_clears_once_a_write_succeeds(self):
        store.CSV_PATH.mkdir()
        self.post("/fund/1234567/review",
                  {"outcome": "reviewed", "next_redemption_date": "2026-09-12",
                   "note": "", "accession": FILING_ROW["accession_number"]})
        _, body, _ = self.get("/")
        self.assertIn("out of date", body)

        store.CSV_PATH.rmdir()  # closed excel
        _, body, _ = self.get("/")
        self.assertNotIn("out of date", body)
        self.assertIn("2026-09-12", self.csv_text())


class StartupCsvBannerTest(ServerTestCase):
    def test_startup_failure_is_visible_and_reload_recovers_with_or_without_setup(self):
        for user_agent in ("Tester test@example.com", ""):
            with self.subTest(user_agent=user_agent):
                conn = self.conn()
                try:
                    store.set_setting(conn, "sec_user_agent", user_agent)
                    if store.get_fund(conn, "1") is None:
                        store.add_fund(conn, "1", "", "Example Fund")
                finally:
                    conn.close()
                # A directory at the export path fails reliably on every OS.
                if store.CSV_PATH.exists():
                    store.CSV_PATH.unlink()
                store.CSV_PATH.mkdir()
                from unittest.mock import patch
                with contextlib.redirect_stderr(io.StringIO()), patch.dict(
                        "os.environ", {"SEC_USER_AGENT": ""}):
                    app.bootstrap(self.db, legacy_csv=Path(self.tmp.name) / "no-seed.csv")
                for path in ("/settings", "/"):
                    _, body, _ = self.get(path)
                    self.assertIn("redemptions.csv is out of date", body)
                    self.assertIn("reload the fund list", body)
                    if not user_agent:
                        self.assertIn(server.SETUP_BANNER, body)
                        self.assertIn("Open settings", body)
                store.CSV_PATH.rmdir()
                _, body, _ = self.get("/")
                self.assertNotIn("redemptions.csv is out of date", body)
                self.assertEqual(server.Handler.csv_error, "")
                self.assertIn("Example Fund", store.CSV_PATH.read_text())


class RequestBodyTest(ServerTestCase):
    """Loopback only, but a form body still gets read into memory in one go."""

    def raw(self, request: bytes) -> str:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(request)
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def test_an_oversized_form_body_is_refused_with_413(self):
        length = server.MAX_FORM_BYTES + 1
        head = (f"POST /settings HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Content-Type: application/x-www-form-urlencoded\r\n"
                f"Content-Length: {length}\r\n\r\n").encode()
        response = self.raw(head + b"sec_user_agent=x")
        self.assertTrue(response.startswith("HTTP/1.0 413") or
                        response.startswith("HTTP/1.1 413"), response[:80])

    def test_a_non_numeric_content_length_is_a_bad_request(self):
        head = (f"POST /settings HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
                f"Content-Length: lots\r\n\r\n").encode()
        response = self.raw(head)
        self.assertIn(" 400 ", response.splitlines()[0])

    def test_a_body_at_the_limit_is_accepted(self):
        value = "x" * (server.MAX_FORM_BYTES - len("sec_user_agent="))
        status, _, _ = self.post("/settings", {"sec_user_agent": value})
        self.assertEqual(status, 303)


class CikRouteTest(ServerTestCase):
    """Ciks are stored without leading zeros, but EDGAR shows them padded to
    ten digits and that's how they get pasted."""

    def setUp(self):
        super().setUp()
        conn = self.conn()
        store.add_fund(conn, "1234567", "ACME", "Acme Interval Fund")
        conn.close()

    def test_a_zero_padded_cik_reaches_the_fund_page(self):
        status, body, _ = self.get("/fund/0001234567")
        self.assertEqual(status, 200)
        self.assertIn("Acme Interval Fund", body)

    def test_a_zero_padded_cik_acts_on_the_stored_fund(self):
        self.post("/funds/0001234567/toggle", {})
        conn = self.conn()
        try:
            self.assertEqual(store.get_fund(conn, "1234567")["active"], 0)
        finally:
            conn.close()

    def test_a_zero_padded_unknown_cik_is_still_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/fund/0009999999")
        self.assertEqual(ctx.exception.code, 404)


GROUPED = json.loads((Path(__file__).parent / "fixtures" / "submissions_grouped.json")
                     .read_text(encoding="utf-8"))
INDEX_MULTI = (Path(__file__).parent / "fixtures" / "filing_index_multi.json").read_bytes()
N2A_URL = "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000017/n2a.htm"
EX_URL = "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000017/ex99a.htm"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000017/index.json"
NOTICE_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000031/index.json"


class ResearchRoutesTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        conn = self.conn()
        store.set_setting(conn, "sec_user_agent", "Test test@example.com")
        store.add_fund(conn, "1783964", "ODCAX", "Accordant ODCE Index Fund")
        store.add_fund(conn, "1748680", "OWSCX", "1WS Credit Income Fund")
        conn.close()
        self._cache = viewer.CACHE_DIR
        viewer.CACHE_DIR = Path(self.tmp.name) / "cache"
        self.calls = []
        self._real_get = sec.sec_get
        sec.sec_get = self.fake_get
        self.responses = {
            "https://data.sec.gov/submissions/CIK0001783964.json": json.dumps(GROUPED).encode(),
            INDEX_URL: INDEX_MULTI,
            NOTICE_INDEX_URL: sec.SECError("HTTP 503 from www.sec.gov"),
            N2A_URL: b"<html><body><script>var x=1</script><p>Prospectus text, September 12, 2026</p></body></html>",
            EX_URL: b"<html><body>Exhibit</body></html>",
        }

    def tearDown(self):
        sec.sec_get = self._real_get
        viewer.CACHE_DIR = self._cache
        super().tearDown()

    def fake_get(self, url, user_agent):
        self.calls.append(url)
        body = self.responses.get(url)
        if body is None:
            raise sec.SECError(f"HTTP 404 from {url}")
        if isinstance(body, Exception):
            raise body
        return body

    def test_research_page_lists_funds_without_touching_edgar(self):
        status, body, _ = self.get("/research")
        self.assertEqual(status, 200)
        self.assertIn('data-cik="1783964"', body)
        self.assertIn('data-cik="1748680"', body)
        self.assertLess(body.index("1WS Credit"), body.index("Accordant"))
        self.assertEqual(self.calls, [])

    def test_expanding_a_fund_fetches_the_feed_and_renders_groups(self):
        status, body, _ = self.get("/research/1783964")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls, ["https://data.sec.gov/submissions/CIK0001783964.json"])
        self.assertIn('data-group="prospectus"', body)
        self.assertIn('data-group="repurchase"', body)
        self.assertIn('data-group="other"', body)
        self.assertIn('href="/research/1783964/0001783964-26-000017"', body)
        self.assertIn('<a href="/research" class="active">Research</a>', body)

    def test_fragment_returns_only_the_subtree(self):
        _, page, _ = self.get("/research/1783964")
        _, fragment, resp = self.get("/research/1783964?fragment=1")
        self.assertNotIn("<html", fragment)
        self.assertIn('data-group="prospectus"', fragment)
        self.assertIn(fragment.strip(), page)

    def test_second_expand_is_served_from_the_cache(self):
        self.get("/research/1783964")
        self.get("/research/1783964")
        self.assertEqual(len(self.calls), 1)

    def test_a_feed_failure_renders_inline_with_a_retry_link(self):
        self.responses["https://data.sec.gov/submissions/CIK0001783964.json"] = \
            sec.SECError("Could not reach data.sec.gov")
        status, body, _ = self.get("/research/1783964?fragment=1")
        self.assertEqual(status, 200)
        self.assertIn("Could not reach data.sec.gov", body)
        self.assertIn('href="/research/1783964">Retry</a>', body)

    def test_a_non_sec_failure_loading_the_feed_renders_inline(self):
        real = research.load_submissions
        research.load_submissions = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            status, body, _ = self.get("/research/1783964?fragment=1")
        finally:
            research.load_submissions = real
        self.assertEqual(status, 200)
        self.assertIn("disk full", body)

    def test_a_stale_feed_says_how_old_it_is(self):
        real = research.load_submissions
        research.load_submissions = lambda *a, **k: (GROUPED, 3 * 86400, "Could not reach data.sec.gov")
        try:
            _, body, _ = self.get("/research/1783964?fragment=1")
        finally:
            research.load_submissions = real
        self.assertIn("3 days ago", body)
        self.assertIn("Could not reach data.sec.gov", body)

    def test_off_roster_cik_is_404_with_no_fetch(self):
        for path in ("/research/9999999", "/research/9999999/0001783964-26-000017",
                     "/research/9999999/0001783964-26-000017/n2a.htm"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(self.calls, [])

    def test_malformed_accession_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/research/1783964/not-an-accession")
        self.assertEqual(ctx.exception.code, 404)
        self.assertEqual(self.calls, [])

    def test_accession_not_in_the_fund_feed_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/research/1783964/0009999999-26-000001")
        self.assertEqual(ctx.exception.code, 404)
        self.assertNotIn(
            "https://www.sec.gov/Archives/edgar/data/1783964/000999999926000001/index.json",
            self.calls)

    def test_filing_fragment_lists_documents_primary_first(self):
        status, body, _ = self.get("/research/1783964/0001783964-26-000017?fragment=1")
        self.assertEqual(status, 200)
        self.assertLess(body.index("n2a.htm"), body.index("ex99a.htm"))
        self.assertIn("feeschedule.pdf", body)
        self.assertNotIn("data-open", body)  # two html documents, nothing auto-opens

    def test_filing_page_renders_fund_open_and_filing_open(self):
        status, body, _ = self.get("/research/1783964/0001783964-26-000017")
        self.assertEqual(status, 200)
        self.assertIn("<html", body)
        self.assertIn("ex99a.htm", body)
        self.assertIn('data-group="prospectus"', body)

    def test_single_html_document_filing_carries_data_open(self):
        # the notice filing's index is unavailable, so the primary alone is listed
        status, body, _ = self.get("/research/1783964/0001783964-26-000031?fragment=1")
        self.assertEqual(status, 200)
        self.assertIn('data-open="/research/1783964/0001783964-26-000031/notice_q3.htm"', body)

    def test_document_route_renders_sanitized_html_with_csp_and_no_marks(self):
        status, body, resp = self.get("/research/1783964/0001783964-26-000017/n2a.htm")
        self.assertEqual(status, 200)
        self.assertNotIn("var x=1", body)
        self.assertIn("Prospectus text", body)
        self.assertNotIn("<mark", body)
        self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        cached = Path(self.tmp.name, "cache", "research", "0001783964-26-000017", "n2a.htm")
        self.assertTrue(cached.exists())

    def test_document_not_in_the_index_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/research/1783964/0001783964-26-000017/secret.htm")
        self.assertEqual(ctx.exception.code, 404)
        self.assertNotIn(
            "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000017/secret.htm",
            self.calls)

    def test_non_html_document_is_404_in_the_frame(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/research/1783964/0001783964-26-000017/feeschedule.pdf")
        self.assertEqual(ctx.exception.code, 404)

    def test_document_fetch_failure_renders_a_readable_frame(self):
        self.responses[EX_URL] = sec.SECError("HTTP 403 from www.sec.gov")
        status, body, _ = self.get("/research/1783964/0001783964-26-000017/ex99a.htm")
        self.assertEqual(status, 200)
        self.assertIn("403", body)

    def test_research_writes_nothing_to_the_db(self):
        before = Path(self.db).read_bytes()
        self.get("/research/1783964")
        self.get("/research/1783964/0001783964-26-000017")
        self.get("/research/1783964/0001783964-26-000017/n2a.htm")
        self.assertEqual(Path(self.db).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
