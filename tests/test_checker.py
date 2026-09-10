import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import checker
import sec
import store

FILING = {
    "form_type": "N-23C3A",
    "filing_date": "2026-08-14",
    "accession_number": "0001234567-26-000123",
    "primary_document_url": "https://www.sec.gov/Archives/edgar/data/1/x/notice.htm",
}


class CheckerTest(unittest.TestCase):
    def setUp(self):
        checker.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "funds.db"
        conn = self.conn_factory()
        store.init_db(conn)
        for cik, name in [("1", "Acme Fund"), ("2", "Beta Fund"), ("3", "Ceti Fund")]:
            store.add_fund(conn, cik, "", name)
        conn.close()

    def tearDown(self):
        checker.join(timeout=5)
        checker.reset()
        self.tmp.cleanup()

    def conn_factory(self):
        return store.connect(self.db)

    def run_check(self, ciks, submissions_fn, detect_fn=lambda subs: FILING):
        started = checker.start_check(ciks, "UA", self.conn_factory,
                                      submissions_fn=submissions_fn,
                                      detect_fn=detect_fn)
        checker.join(timeout=5)
        return started

    def test_records_a_filing_for_every_fund(self):
        self.assertTrue(self.run_check(["1", "2", "3"], lambda cik, ua: {"cik": cik}))
        conn = self.conn_factory()
        for cik in ["1", "2", "3"]:
            self.assertEqual(store.get_fund(conn, cik)["status"], "needs_review")
        conn.close()

    def test_progress_counters_finish_complete(self):
        self.run_check(["1", "2", "3"], lambda cik, ua: {"cik": cik})
        state = checker.progress()
        self.assertFalse(state["running"])
        self.assertEqual(state["total"], 3)
        self.assertEqual(state["done"], 3)
        self.assertTrue(state["finished_at"])
        self.assertEqual(state["errors"], [])

    def test_database_open_failure_finishes_and_allows_retry(self):
        broken = Mock(side_effect=OSError("Cannot open database"))
        with patch("threading.excepthook"):
            self.assertTrue(checker.start_check(["1"], "UA", broken))
            checker.join()
        state = checker.progress()
        self.assertFalse(state["running"])
        self.assertEqual(state["done"], 0)
        self.assertTrue(state["finished_at"])
        self.assertIn("Cannot open database", state["job_error"])
        self.assertTrue(self.run_check(["1"], lambda c, u: {}))
        self.assertEqual(checker.progress()["job_error"], "")
        self.assertEqual(checker.progress()["done"], 1)

    def test_thread_start_failure_is_reported_and_allows_retry(self):
        with patch("threading.Thread.start", side_effect=RuntimeError("No threads available")):
            checker.start_check(["1"], "UA", self.conn_factory)
        checker.join()  # An unstarted thread must not remain joinable.
        self.assertFalse(checker.is_running())
        self.assertIn("No threads available", checker.progress()["job_error"])
        self.assertTrue(self.run_check(["1"], lambda c, u: {}))

    def test_database_write_failure_is_reported_as_a_job_failure(self):
        with patch.object(store, "record_check", side_effect=OSError("Disk full")), \
                patch("threading.excepthook"):
            self.run_check(["1", "2"], lambda c, u: {})
        self.assertFalse(checker.is_running())
        self.assertIn("Disk full", checker.progress()["job_error"])
        self.assertLess(checker.progress()["done"], checker.progress()["total"])

    def test_close_failure_cannot_leave_job_running(self):
        def factory():
            conn = self.conn_factory()
            wrapped = Mock(wraps=conn)

            def close():
                conn.close()
                raise OSError("Close failed")

            wrapped.close.side_effect = close
            return wrapped

        with patch("threading.excepthook"):
            checker.start_check(["1"], "UA", factory,
                                submissions_fn=lambda c, u: {}, detect_fn=lambda s: FILING)
            checker.join()
        self.assertFalse(checker.is_running())
        self.assertTrue(checker.progress()["finished_at"])
        self.assertIn("Close failed", checker.progress()["job_error"])

    def test_a_failing_fund_does_not_stop_the_run(self):
        def flaky(cik, user_agent):
            if cik == "2":
                raise sec.SECError("HTTP 403 from data.sec.gov")
            return {"cik": cik}

        self.run_check(["1", "2", "3"], flaky)
        conn = self.conn_factory()
        self.assertEqual(store.get_fund(conn, "1")["status"], "needs_review")
        self.assertEqual(store.get_fund(conn, "2")["status"], "error")
        self.assertEqual(store.get_fund(conn, "3")["status"], "needs_review")
        conn.close()

    def test_the_error_is_recorded_in_progress_and_on_the_row(self):
        def flaky(cik, user_agent):
            if cik == "2":
                raise sec.SECError("HTTP 403 from data.sec.gov")
            return {"cik": cik}

        self.run_check(["1", "2", "3"], flaky)
        errors = checker.progress()["errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["cik"], "2")
        self.assertIn("403", errors[0]["message"])
        conn = self.conn_factory()
        self.assertIn("403", store.get_fund(conn, "2")["check_error"])
        conn.close()

    def test_an_unexpected_exception_is_caught_like_a_sec_error(self):
        def broken(cik, user_agent):
            raise ValueError("something odd")

        self.run_check(["1"], broken)
        conn = self.conn_factory()
        self.assertEqual(store.get_fund(conn, "1")["status"], "error")
        conn.close()

    def test_a_fund_with_no_target_filing_records_no_filings(self):
        self.run_check(["1"], lambda cik, ua: {"cik": cik}, detect_fn=lambda subs: None)
        conn = self.conn_factory()
        self.assertEqual(store.get_fund(conn, "1")["status"], "no_filings")
        conn.close()

    def test_a_second_start_is_refused_while_one_is_running(self):
        import threading

        release = threading.Event()

        def slow(cik, user_agent):
            release.wait(timeout=5)
            return {"cik": cik}

        first = checker.start_check(["1", "2"], "UA", self.conn_factory,
                                    submissions_fn=slow, detect_fn=lambda s: FILING)
        self.assertTrue(first)
        self.assertTrue(checker.is_running())
        second = checker.start_check(["3"], "UA", self.conn_factory,
                                     submissions_fn=slow, detect_fn=lambda s: FILING)
        self.assertFalse(second)
        release.set()
        checker.join(timeout=5)
        self.assertFalse(checker.is_running())

    def test_progress_before_any_job_is_a_safe_empty_state(self):
        state = checker.progress()
        self.assertFalse(state["running"])
        self.assertEqual(state["total"], 0)
        self.assertEqual(state["done"], 0)
        self.assertEqual(state["errors"], [])

    def test_starting_with_no_ciks_is_refused(self):
        self.assertFalse(
            checker.start_check([], "UA", self.conn_factory,
                                submissions_fn=lambda c, u: {}, detect_fn=lambda s: None)
        )


if __name__ == "__main__":
    unittest.main()
