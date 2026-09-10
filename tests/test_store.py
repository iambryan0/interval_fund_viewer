import io
import contextlib
import sqlite3
import threading
import unittest
from unittest.mock import patch

import store


class StoreTestCase(unittest.TestCase):
    """Every test gets its own in-memory db."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.init_db(self.conn)

    def tearDown(self):
        self.conn.close()


class SettingsTest(StoreTestCase):
    def test_missing_setting_returns_the_default(self):
        self.assertEqual(store.get_setting(self.conn, "sec_user_agent"), "")
        self.assertEqual(store.get_setting(self.conn, "port", "8765"), "8765")

    def test_set_then_get(self):
        store.set_setting(self.conn, "sec_user_agent", "Me me@example.com")
        self.assertEqual(
            store.get_setting(self.conn, "sec_user_agent"), "Me me@example.com"
        )

    def test_set_overwrites(self):
        store.set_setting(self.conn, "port", "8765")
        store.set_setting(self.conn, "port", "9000")
        self.assertEqual(store.get_setting(self.conn, "port"), "9000")


class FundCrudTest(StoreTestCase):
    def test_add_then_get(self):
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["fund_name"], "Acme Interval Fund")
        self.assertEqual(fund["ticker"], "ACME")
        self.assertEqual(fund["active"], 1)
        self.assertTrue(fund["added_at"])

    def test_get_unknown_fund_returns_none(self):
        self.assertIsNone(store.get_fund(self.conn, "9999999"))

    def test_adding_the_same_cik_twice_raises(self):
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")
        with self.assertRaises(store.StoreError):
            store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")

    def test_list_is_ordered_by_fund_name(self):
        store.add_fund(self.conn, "3", "ZED", "Zed Fund")
        store.add_fund(self.conn, "1", "ACME", "Acme Fund")
        store.add_fund(self.conn, "2", "MID", "Mid Fund")
        names = [f["fund_name"] for f in store.list_funds(self.conn)]
        self.assertEqual(names, ["Acme Fund", "Mid Fund", "Zed Fund"])

    def test_active_only_filters_inactive(self):
        store.add_fund(self.conn, "1", "ACME", "Acme Fund")
        store.add_fund(self.conn, "2", "BETA", "Beta Fund")
        store.set_active(self.conn, "2", False)
        self.assertEqual(len(store.list_funds(self.conn)), 2)
        self.assertEqual(len(store.list_funds(self.conn, active_only=True)), 1)

    def test_set_active_toggles_back_on(self):
        store.add_fund(self.conn, "1", "ACME", "Acme Fund")
        store.set_active(self.conn, "1", False)
        store.set_active(self.conn, "1", True)
        self.assertEqual(store.get_fund(self.conn, "1")["active"], 1)

    def test_delete_removes_the_fund(self):
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")
        store.delete_fund(self.conn, "1234567")
        self.assertIsNone(store.get_fund(self.conn, "1234567"))


class SchemaTest(StoreTestCase):
    def test_init_db_is_idempotent(self):
        store.init_db(self.conn)
        store.init_db(self.conn)
        self.assertEqual(store.list_funds(self.conn), [])

    def test_funds_table_has_the_expected_columns(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(funds)")}
        expected = {
            "cik", "ticker_revision", "fund_name", "active", "added_at",
            "latest_accession", "latest_filing_date", "latest_form",
            "latest_url", "checked_at", "check_error",
            "next_redemption_date", "last_reviewed_accession",
            "last_reviewed_filing_date", "last_reviewed_form",
            "last_reviewed_url", "review_status", "note", "reviewed_at",
        }
        self.assertEqual(cols, expected)


from datetime import date

FILING = {
    "form_type": "N-23C3A",
    "filing_date": "2026-08-14",
    "accession_number": "0001234567-26-000123",
    "primary_document_url": "https://www.sec.gov/Archives/edgar/data/1234567/x/notice.htm",
}


class ParseIsoTest(unittest.TestCase):
    def test_parses_a_valid_date(self):
        self.assertEqual(store.parse_iso("2026-09-12"), date(2026, 9, 12))

    def test_rejects_garbage(self):
        for bad in ["", None, "not a date", "2026-13-01", "09/12/2026", "2026-9-12"]:
            self.assertIsNone(store.parse_iso(bad), bad)


class DeriveStatusTest(unittest.TestCase):
    def test_unchecked_when_never_checked(self):
        self.assertEqual(store.derive_status({"checked_at": None}), "unchecked")

    def test_error_is_shown_when_the_known_filing_is_already_reviewed(self):
        row = {"checked_at": "t", "check_error": "HTTP 403", "latest_accession": "a",
               "last_reviewed_accession": "a"}
        self.assertEqual(store.derive_status(row), "error")

    def test_pending_review_is_preserved_after_a_failed_check(self):
        row = {"checked_at": "t", "check_error": "HTTP 403",
               "latest_accession": "new", "last_reviewed_accession": "old"}
        self.assertEqual(store.derive_status(row), "needs_review")

    def test_no_filings_when_nothing_matched(self):
        row = {"checked_at": "t", "check_error": "", "latest_accession": None,
               "last_reviewed_accession": None}
        self.assertEqual(store.derive_status(row), "no_filings")

    def test_needs_review_when_watermark_is_behind(self):
        row = {"checked_at": "t", "check_error": "", "latest_accession": "new",
               "last_reviewed_accession": "old"}
        self.assertEqual(store.derive_status(row), "needs_review")

    def test_needs_review_when_never_reviewed(self):
        row = {"checked_at": "t", "check_error": "", "latest_accession": "new",
               "last_reviewed_accession": None}
        self.assertEqual(store.derive_status(row), "needs_review")

    def test_up_to_date_when_watermark_matches(self):
        row = {"checked_at": "t", "check_error": "", "latest_accession": "same",
               "last_reviewed_accession": "same"}
        self.assertEqual(store.derive_status(row), "up_to_date")


class DatePassedTest(unittest.TestCase):
    def test_false_when_no_date(self):
        self.assertFalse(store.date_passed({"next_redemption_date": None}))

    def test_true_when_in_the_past(self):
        row = {"next_redemption_date": "2026-09-07"}
        self.assertTrue(store.date_passed(row, today=date(2026, 9, 8)))

    def test_false_on_the_day_itself(self):
        row = {"next_redemption_date": "2026-09-08"}
        self.assertFalse(store.date_passed(row, today=date(2026, 9, 8)))

    def test_false_when_in_the_future(self):
        row = {"next_redemption_date": "2026-09-09"}
        self.assertFalse(store.date_passed(row, today=date(2026, 9, 8)))


class RecordCheckTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")

    def test_records_a_found_filing(self):
        store.record_check(self.conn, "1234567", FILING)
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["latest_accession"], "0001234567-26-000123")
        self.assertEqual(fund["latest_filing_date"], "2026-08-14")
        self.assertEqual(fund["latest_form"], "N-23C3A")
        self.assertEqual(fund["latest_url"], FILING["primary_document_url"])
        self.assertEqual(fund["check_error"], "")
        self.assertTrue(fund["checked_at"])
        self.assertEqual(fund["status"], "needs_review")

    def test_records_absence_of_a_filing(self):
        store.record_check(self.conn, "1234567", None)
        fund = store.get_fund(self.conn, "1234567")
        self.assertIsNone(fund["latest_accession"])
        self.assertEqual(fund["status"], "no_filings")

    def test_records_an_error_and_clears_it_on_the_next_success(self):
        store.record_check(self.conn, "1234567", None, error="HTTP 403")
        self.assertEqual(store.get_fund(self.conn, "1234567")["status"], "error")
        store.record_check(self.conn, "1234567", FILING)
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["check_error"], "")
        self.assertEqual(fund["status"], "needs_review")

    def test_a_failed_check_keeps_the_last_known_filing(self):
        store.record_check(self.conn, "1234567", FILING)
        before = store.get_fund(self.conn, "1234567")["checked_at"]
        store.record_check(self.conn, "1234567", None, error="HTTP 403")
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["latest_accession"], FILING["accession_number"])
        self.assertEqual(fund["latest_filing_date"], FILING["filing_date"])
        self.assertEqual(fund["latest_form"], FILING["form_type"])
        self.assertEqual(fund["latest_url"], FILING["primary_document_url"])
        self.assertEqual(fund["check_error"], "HTTP 403")
        self.assertEqual(fund["status"], "needs_review")
        self.assertTrue(fund["checked_at"] >= before)

    def test_a_check_that_finds_no_filing_clears_the_last_known_filing(self):
        store.record_check(self.conn, "1234567", FILING)
        store.record_check(self.conn, "1234567", None)
        fund = store.get_fund(self.conn, "1234567")
        self.assertIsNone(fund["latest_accession"])
        self.assertIsNone(fund["latest_filing_date"])
        self.assertIsNone(fund["latest_form"])
        self.assertIsNone(fund["latest_url"])
        self.assertEqual(fund["status"], "no_filings")


class RecordReviewTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")
        store.record_check(self.conn, "1234567", FILING)

    def test_reviewed_saves_the_date_and_advances_the_watermark(self):
        store.record_review(self.conn, "1234567", "reviewed", "2026-09-12",
                            "section 3", FILING["accession_number"])
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["next_redemption_date"], "2026-09-12")
        self.assertEqual(fund["last_reviewed_accession"], FILING["accession_number"])
        self.assertEqual(fund["last_reviewed_filing_date"], "2026-08-14")
        self.assertEqual(fund["last_reviewed_form"], "N-23C3A")
        self.assertEqual(fund["last_reviewed_url"], FILING["primary_document_url"])
        self.assertEqual(fund["review_status"], "reviewed")
        self.assertEqual(fund["note"], "section 3")
        self.assertTrue(fund["reviewed_at"])
        self.assertEqual(fund["status"], "up_to_date")

    def test_no_date_advances_the_watermark_without_a_date(self):
        store.record_review(self.conn, "1234567", "no_date", "", "nothing in here",
                            FILING["accession_number"])
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["status"], "up_to_date")
        self.assertEqual(fund["review_status"], "no_date")
        self.assertIsNone(fund["next_redemption_date"])

    def test_not_applicable_advances_the_watermark(self):
        store.record_review(self.conn, "1234567", "not_applicable", "", "",
                            FILING["accession_number"])
        self.assertEqual(store.get_fund(self.conn, "1234567")["status"], "up_to_date")

    def test_no_date_preserves_a_previously_entered_date(self):
        store.record_review(self.conn, "1234567", "reviewed", "2026-09-12", "",
                            FILING["accession_number"])
        newer = dict(FILING, accession_number="0001234567-26-000200",
                     filing_date="2026-09-01")
        store.record_check(self.conn, "1234567", newer)
        store.record_review(self.conn, "1234567", "no_date", "", "",
                            newer["accession_number"])
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["next_redemption_date"], "2026-09-12")
        self.assertEqual(fund["last_reviewed_accession"], "0001234567-26-000200")

    def test_reviewed_without_a_valid_date_raises_and_writes_nothing(self):
        with self.assertRaises(store.StoreError):
            store.record_review(self.conn, "1234567", "reviewed", "not a date", "",
                                FILING["accession_number"])
        fund = store.get_fund(self.conn, "1234567")
        self.assertIsNone(fund["review_status"])
        self.assertEqual(fund["status"], "needs_review")

    def test_unknown_outcome_raises(self):
        with self.assertRaises(store.StoreError):
            store.record_review(self.conn, "1234567", "whatever", "2026-09-12", "",
                                FILING["accession_number"])

    def test_a_stale_accession_is_refused_and_changes_nothing(self):
        """I reviewed the filing the page showed. If a background check
        replaced it in the meantime, my review must not land on a filing I
        never opened."""
        store.record_review(self.conn, "1234567", "reviewed", "2026-09-12", "",
                            FILING["accession_number"])
        newer = dict(FILING, accession_number="0001234567-26-000200",
                     filing_date="2026-09-01")
        store.record_check(self.conn, "1234567", newer)
        with self.assertRaises(store.StoreError) as ctx:
            store.record_review(self.conn, "1234567", "reviewed", "2026-10-01",
                                "read the old one", FILING["accession_number"])
        self.assertIn("re-checked", str(ctx.exception))
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["status"], "needs_review")
        self.assertEqual(fund["next_redemption_date"], "2026-09-12")
        self.assertEqual(fund["last_reviewed_accession"],
                         FILING["accession_number"])
        self.assertEqual(fund["note"], "")

    def test_an_empty_accession_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.record_review(self.conn, "1234567", "reviewed", "2026-09-12",
                                "", "")
        self.assertEqual(store.get_fund(self.conn, "1234567")["status"],
                         "needs_review")

    def test_reviewing_an_unchecked_fund_raises(self):
        store.add_fund(self.conn, "999", "NEW", "New Fund")
        with self.assertRaises(store.StoreError):
            store.record_review(self.conn, "999", "reviewed", "2026-09-12", "",
                                FILING["accession_number"])


class ReviewHistoryTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        store.add_fund(self.conn, "1", "", "History Fund")
        store.record_check(self.conn, "1", FILING)

    def save(self, outcome="reviewed", date_text="2026-09-12", note="original note"):
        store.record_review(self.conn, "1", outcome, date_text, note,
                            FILING["accession_number"])

    def test_corrections_and_no_date_outcomes_preserve_source_and_notes(self):
        self.save()
        self.save(date_text="2026-09-15", note="corrected date")
        newer = dict(FILING, accession_number="new", filing_date="2026-09-01")
        store.record_check(self.conn, "1", newer)
        for outcome in ("no_date", "not_applicable"):
            store.record_review(self.conn, "1", outcome, "2026-12-01", outcome, "new")
        history = store.list_reviews(self.conn, "1")
        self.assertEqual([r["recorded_date"] for r in history],
                         [None, None, "2026-09-15", "2026-09-12"])
        self.assertEqual(history[2]["accession"], FILING["accession_number"])
        self.assertEqual(history[2]["url"], FILING["primary_document_url"])
        self.assertEqual(history[2]["note"], "corrected date")
        self.assertEqual(history[3]["note"], "original note")
        self.assertEqual(store.get_fund(self.conn, "1")["next_redemption_date"],
                         "2026-09-15")

    def test_invalid_and_stale_reviews_never_enter_history(self):
        self.save()
        before = store.list_reviews(self.conn, "1")
        with self.assertRaises(store.StoreError):
            self.save(date_text="invalid")
        store.record_check(self.conn, "1", dict(FILING, accession_number="new"))
        with self.assertRaises(store.StoreError):
            self.save(date_text="2026-10-01")
        self.assertEqual(store.list_reviews(self.conn, "1"), before)

    def test_history_failure_rolls_back_the_date_and_watermark(self):
        self.conn.execute("""CREATE TRIGGER fail_history BEFORE INSERT ON review_history
                          BEGIN SELECT RAISE(ABORT, 'history failed'); END""")
        before = store.get_fund(self.conn, "1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.save()
        self.assertEqual(store.get_fund(self.conn, "1"), before)
        self.assertEqual(store.list_reviews(self.conn, "1"), [])

    def test_upgrade_preserves_the_last_known_review_once(self):
        self.save()
        # Simulate a database written by the old app: fund state, no history table.
        self.conn.execute("DROP TABLE review_history")
        self.conn.commit()
        store.init_db(self.conn)
        store.init_db(self.conn)
        history = store.list_reviews(self.conn, "1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["recorded_date"], "2026-09-12")
        self.assertEqual(history[0]["note"], "original note")
        self.assertEqual(history[0]["legacy"], 1)

    def test_upgrade_does_not_attribute_a_retained_date_to_a_no_date_review(self):
        self.save()
        self.save(outcome="no_date", note="later review")
        self.conn.execute("DROP TABLE review_history")
        self.conn.commit()
        store.init_db(self.conn)
        history = store.list_reviews(self.conn, "1")
        self.assertEqual(history[0]["outcome"], "no_date")
        self.assertIsNone(history[0]["recorded_date"])
        self.assertEqual(store.get_fund(self.conn, "1")["next_redemption_date"],
                         "2026-09-12")

    def test_removing_and_readding_fund_does_not_inherit_old_history(self):
        self.save()
        store.delete_fund(self.conn, "1")
        store.add_fund(self.conn, "1", "", "Readded Fund")
        self.assertEqual(store.list_reviews(self.conn, "1"), [])


class NextNeedingReviewTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        for cik, name in [("1", "Acme Fund"), ("2", "Beta Fund"), ("3", "Ceti Fund")]:
            store.add_fund(self.conn, cik, "", name)
            store.record_check(self.conn, cik, FILING)

    def test_returns_the_first_when_no_cursor(self):
        self.assertEqual(store.next_needing_review(self.conn), "1")

    def test_returns_the_next_one_after_the_cursor(self):
        self.assertEqual(store.next_needing_review(self.conn, after_cik="1"), "2")

    def test_returns_none_when_nothing_else_is_pending(self):
        for cik in ("1", "2"):
            store.record_review(self.conn, cik, "no_date", "", "",
                                FILING["accession_number"])
        self.assertIsNone(store.next_needing_review(self.conn, after_cik="3"))

    def test_skips_funds_already_reviewed(self):
        store.record_review(self.conn, "2", "no_date", "", "",
                            FILING["accession_number"])
        self.assertEqual(store.next_needing_review(self.conn, after_cik="1"), "3")

    def test_skips_inactive_funds(self):
        store.set_active(self.conn, "2", False)
        self.assertEqual(store.next_needing_review(self.conn, after_cik="1"), "3")

    def test_save_and_next_walks_the_queue_not_the_alphabet(self):
        """The list is queue ordered, newest filing first, so Save & next
        from the top has to land on the second-newest pending fund, not
        whatever is next alphabetically."""
        store.add_fund(self.conn, "10", "", "Aardvark Fund")
        store.record_check(self.conn, "10", dict(FILING, filing_date="2026-01-01"))
        store.add_fund(self.conn, "20", "", "Zulu Fund")
        store.record_check(self.conn, "20", dict(FILING, filing_date="2026-09-08"))
        store.add_fund(self.conn, "30", "", "Zebra Fund")
        store.record_check(self.conn, "30", dict(FILING, filing_date="2026-09-07"))

        queue = store.list_funds(self.conn, active_only=True, order="queue")
        top = queue[0]["cik"]
        self.assertEqual(top, "20", "the newest filing heads the queue")
        self.assertEqual(store.next_needing_review(self.conn, after_cik=top), "30")

    def test_a_cursor_late_in_the_queue_still_reaches_earlier_pending_funds(self):
        """The old positional cursor made every fund sorting before the start
        unreachable - 53 of 155 on the live db."""
        store.add_fund(self.conn, "9", "", "Zulu Fund")
        store.record_check(self.conn, "9", dict(FILING, filing_date="2020-01-01"))
        following = store.next_needing_review(self.conn, after_cik="9")
        self.assertIsNotNone(following)
        self.assertNotEqual(following, "9")


import csv
import tempfile
from pathlib import Path


class WriteCsvTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "redemptions.csv"

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def read_rows(self):
        with self.path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_delayed_export_cannot_overwrite_a_newer_committed_review(self):
        # Pause the first export just before it acquires the write lock.
        # A second connection commits and exports a newer date meanwhile.
        db = Path(self.tmp.name) / "funds.db"
        with contextlib.closing(store.connect(db)) as conn:
            store.init_db(conn)
            store.add_fund(conn, "1234567", "ACME", "Acme Fund")
            store.record_check(conn, "1234567", FILING)
            waiting = threading.Event()
            resume = threading.Event()
            lock = threading.Lock()
            errors = []

            class DelayedLock:
                def __enter__(self):
                    if threading.current_thread() is worker:
                        waiting.set()
                        if not resume.wait(5):
                            raise TimeoutError("Second export did not finish")
                    lock.acquire()

                def __exit__(self, *args):
                    lock.release()

            def export():
                other = store.connect(db)
                try:
                    store.write_csv(other, self.path)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    other.close()

            worker = threading.Thread(target=export)
            with patch.object(store, "_CSV_LOCK", DelayedLock()):
                worker.start()
                try:
                    self.assertTrue(waiting.wait(5), "First export never reached the lock")
                    store.record_review(conn, "1234567", "reviewed", "2026-12-01", "",
                                        FILING["accession_number"])
                    store.write_csv(conn, self.path)
                finally:
                    resume.set()
                    worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(self.read_rows()[0]["next_redemption_date"], "2026-12-01")
            self.assertEqual(store.dated_csv_path(self.path).read_bytes(),
                             store.render_csv(conn))


    def test_header_is_exactly_three_columns(self):
        store.write_csv(self.conn, self.path)
        with self.path.open(newline="", encoding="utf-8") as f:
            self.assertEqual(next(csv.reader(f)),
                             ["fund_name", "ticker", "next_redemption_date"])

    def test_internal_columns_are_gone(self):
        for gone in ("cik", "filing_date", "filing_url", "review_status", "note",
                     "reviewed_at", "latest_filing_date", "needs_review",
                     "confidence"):
            self.assertNotIn(gone, store.CSV_FIELDS)

    def test_row_carries_the_name_ticker_and_date(self):
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")
        store.record_check(self.conn, "1234567", FILING)
        store.record_review(self.conn, "1234567", "reviewed", "2026-09-12", "n",
                            FILING["accession_number"])
        store.write_csv(self.conn, self.path)
        row = self.read_rows()[0]
        self.assertEqual(row, {"fund_name": "Acme Interval Fund", "ticker": "ACME",
                              "next_redemption_date": "2026-09-12"})

    def test_a_fund_with_no_date_writes_an_empty_cell(self):
        store.add_fund(self.conn, "999", "", "Unreviewed Fund")
        store.write_csv(self.conn, self.path)
        self.assertEqual(self.read_rows()[0]["next_redemption_date"], "")

    def test_rows_are_ordered_like_the_fund_list(self):
        store.add_fund(self.conn, "3", "", "Zed Fund")
        store.add_fund(self.conn, "1", "", "Acme Fund")
        store.write_csv(self.conn, self.path)
        self.assertEqual([r["fund_name"] for r in self.read_rows()],
                         ["Acme Fund", "Zed Fund"])

    def test_reports_whether_the_file_changed(self):
        self.assertTrue(store.write_csv(self.conn, self.path))
        self.assertTrue(self.path.exists())
        self.assertFalse(store.write_csv(self.conn, self.path),
                         "identical content must not be rewritten")
        store.add_fund(self.conn, "999", "", "New Fund")
        self.assertTrue(store.write_csv(self.conn, self.path))

    def test_mirrors_the_database_after_every_kind_of_change(self):
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")
        store.add_fund(self.conn, "999", "", "Doomed Fund")
        store.write_csv(self.conn, self.path)
        self.assertEqual([r["fund_name"] for r in self.read_rows()],
                         ["Acme Interval Fund", "Doomed Fund"])

        store.delete_fund(self.conn, "999")
        store.write_csv(self.conn, self.path)
        self.assertEqual([r["fund_name"] for r in self.read_rows()],
                         ["Acme Interval Fund"])

        store.record_check(self.conn, "1234567", FILING)
        store.record_review(self.conn, "1234567", "reviewed", "2026-09-12", "",
                            FILING["accession_number"])
        store.write_csv(self.conn, self.path)
        self.assertEqual(self.read_rows()[0]["next_redemption_date"], "2026-09-12")

    def test_a_failed_write_leaves_no_temp_file_and_raises(self):
        blocked = Path(self.tmp.name) / "as-a-directory.csv"
        blocked.mkdir()
        with self.assertRaises(OSError):
            store.write_csv(self.conn, blocked)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()],
                         ["as-a-directory.csv"])

    def test_a_write_replaces_the_file_in_one_step(self):
        # temp file + rename, so the old file stays readable until the new
        # one is complete
        store.write_csv(self.conn, self.path)
        store.add_fund(self.conn, "1", "", "Another Fund")
        store.write_csv(self.conn, self.path)
        self.assertFalse(self.path.with_name("redemptions.csv.tmp").exists())
        self.assertIn("Another Fund", self.path.read_text(encoding="utf-8"))


class ImportLegacyCsvTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "redemptions.csv"
        self.path.write_text(
            "cik,ticker,fund_name,next_redemption_date,filing_date,filing_url,"
            "confidence,updated_at\n"
            "0001234567,ACME,Acme Interval Fund,2026-09-12,2026-08-14,https://x,0.9,t\n"
            "7654321,BETA,Beta Credit Fund,,2026-07-01,https://y,0.0,t\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_imports_both_rows(self):
        self.assertEqual(store.import_legacy_csv(self.conn, self.path), 2)
        self.assertEqual(len(store.list_funds(self.conn)), 2)

    def test_strips_leading_zeros_from_the_cik(self):
        store.import_legacy_csv(self.conn, self.path)
        self.assertIsNotNone(store.get_fund(self.conn, "1234567"))

    def test_a_row_with_a_date_imports_as_reviewed(self):
        store.import_legacy_csv(self.conn, self.path)
        fund = store.get_fund(self.conn, "1234567")
        self.assertEqual(fund["review_status"], "reviewed")
        self.assertEqual(fund["next_redemption_date"], "2026-09-12")

    def test_a_row_without_a_date_imports_as_no_date(self):
        store.import_legacy_csv(self.conn, self.path)
        self.assertEqual(store.get_fund(self.conn, "7654321")["review_status"], "no_date")

    def test_imported_funds_have_no_watermark_so_they_need_review(self):
        store.import_legacy_csv(self.conn, self.path)
        fund = store.get_fund(self.conn, "1234567")
        self.assertIsNone(fund["last_reviewed_accession"])
        self.assertEqual(fund["status"], "unchecked")
        store.record_check(self.conn, "1234567", FILING)
        self.assertEqual(store.get_fund(self.conn, "1234567")["status"], "needs_review")

    def test_redemptions_csv_seeds_nothing_because_it_has_no_cik(self):
        # no cik column means every row gets skipped - that's why the seed
        # is a separate file
        export = Path(self.tmp.name) / "export.csv"
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")
        store.write_csv(self.conn, export)
        store.delete_fund(self.conn, "1234567")
        self.assertEqual(store.import_legacy_csv(self.conn, export), 0)
        self.assertEqual(store.list_funds(self.conn), [])

    def test_missing_file_is_a_no_op(self):
        self.assertEqual(
            store.import_legacy_csv(self.conn, self.path.with_name("nope.csv")), 0
        )

    def test_does_nothing_when_funds_already_exist(self):
        store.add_fund(self.conn, "999", "", "Existing Fund")
        self.assertEqual(store.import_legacy_csv(self.conn, self.path), 0)
        self.assertEqual(len(store.list_funds(self.conn)), 1)

    def test_malformed_csv_leaves_funds_empty_and_retryable(self):
        # csv with invalid utf-8 partway through so decoding fails mid-file
        bad_path = Path(self.tmp.name) / "bad.csv"
        # valid header and first row, then bad utf-8 in the second
        bad_path.write_bytes(
            b"cik,ticker,fund_name,next_redemption_date,filing_date,filing_url,"
            b"confidence,updated_at\n"
            b"0001234567,ACME,Acme Interval Fund,2026-09-12,2026-08-14,https://x,0.9,t\n"
            b"7654321,BETA,Fund\xff\xfe,2026-07-01,https://y,0.0,t\n",
        )
        # import should fail cleanly and leave the funds table empty
        result = store.import_legacy_csv(self.conn, bad_path)
        # returns 0 and leaves no funds
        self.assertEqual(result, 0)
        self.assertEqual(len(store.list_funds(self.conn)), 0)

    def test_unparseable_date_does_not_get_stored(self):
        # csv with a date that doesn't parse
        bad_date_path = Path(self.tmp.name) / "bad_date.csv"
        bad_date_path.write_text(
            "cik,ticker,fund_name,next_redemption_date,filing_date,filing_url,"
            "confidence,updated_at\n"
            "0001234567,ACME,Acme Fund,not-a-date,2026-08-14,https://x,0.9,t\n",
            encoding="utf-8",
        )
        store.import_legacy_csv(self.conn, bad_date_path)
        fund = store.get_fund(self.conn, "1234567")
        # should be no_date with no date value
        self.assertEqual(fund["review_status"], "no_date")
        self.assertIsNone(fund["next_redemption_date"])


class ListFundsOrderTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        rows = [
            ("1", "Alpha Fund", "2026-07-01", True),
            ("2", "Beta Fund", "2026-09-08", True),
            ("3", "Ceti Fund", "2026-08-15", True),
            ("4", "Delta Fund", "2026-09-01", False),
        ]
        for cik, name, filed, pending in rows:
            store.add_fund(self.conn, cik, "", name)
            store.record_check(self.conn, cik, dict(FILING, filing_date=filed,
                                                    accession_number=f"acc-{cik}"))
            if not pending:
                store.record_review(self.conn, cik, "reviewed", "2026-12-01", "",
                                    f"acc-{cik}")

    def test_default_order_is_by_name(self):
        names = [f["fund_name"] for f in store.list_funds(self.conn)]
        self.assertEqual(names, ["Alpha Fund", "Beta Fund", "Ceti Fund", "Delta Fund"])

    def test_failed_check_keeps_pending_fund_in_queue_until_reviewed(self):
        store.record_check(self.conn, "2", None, error="HTTP 403")
        self.assertEqual(store.list_funds(self.conn, order="queue")[0]["cik"], "2")
        self.assertEqual(store.next_needing_review(self.conn, after_cik="1"), "2")
        store.record_review(self.conn, "2", "no_date", "", "", "acc-2")
        self.assertEqual(store.next_needing_review(self.conn, after_cik="1"), "3")
        # Reviewing the cached filing must not claim the failed check succeeded.
        self.assertEqual(store.get_fund(self.conn, "2")["check_error"], "HTTP 403")
        self.assertEqual(store.get_fund(self.conn, "2")["status"], "error")

    def test_queue_order_puts_pending_funds_first_newest_filing_first(self):
        names = [f["fund_name"] for f in store.list_funds(self.conn, order="queue")]
        self.assertEqual(names, ["Beta Fund", "Ceti Fund", "Alpha Fund", "Delta Fund"])

    def test_reviewed_funds_sort_below_pending_ones_regardless_of_name(self):
        # Aardvark sorts first alphabetically but is reviewed, so queue order
        # has to put it last. if order= did nothing it'd come first.
        store.add_fund(self.conn, "5", "", "Aardvark Fund")
        store.record_check(self.conn, "5", dict(FILING, filing_date="2026-09-09",
                                                accession_number="acc-5"))
        store.record_review(self.conn, "5", "reviewed", "2026-12-01", "", "acc-5")
        names = [f["fund_name"] for f in store.list_funds(self.conn, order="queue")]
        self.assertEqual(names[-2:], ["Aardvark Fund", "Delta Fund"])
        self.assertEqual(names[0], "Beta Fund")

    def test_pending_funds_filed_the_same_day_stay_in_name_order(self):
        # this is why it's two stable sorts: a single sort(key=date,
        # reverse=True) would flip the name tiebreak too
        for cik, name in (("6", "Yankee Fund"), ("7", "Xray Fund")):
            store.add_fund(self.conn, cik, "", name)
            store.record_check(self.conn, cik, dict(FILING, filing_date="2026-09-08",
                                                    accession_number=f"acc-{cik}"))
        names = [f["fund_name"] for f in store.list_funds(self.conn, order="queue")]
        same_day = [n for n in names if n in ("Xray Fund", "Yankee Fund")]
        self.assertEqual(same_day, ["Xray Fund", "Yankee Fund"])

    def test_the_csv_is_unaffected_by_the_queue_order(self):
        import tempfile, csv as _csv
        from pathlib import Path as _P
        with tempfile.TemporaryDirectory() as tmp:
            path = _P(tmp) / "out.csv"
            store.write_csv(self.conn, path)
            with path.open(newline="", encoding="utf-8") as f:
                names = [r["fund_name"] for r in _csv.DictReader(f)]
        self.assertEqual(names, sorted(names, key=str.lower))



class ImportLegacyCsvReportingTest(StoreTestCase):
    """A seed that imports nothing has to say so somewhere I can see it. Here
    that's the console window the app is started from."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_a_failed_import_says_which_file_and_why_on_stderr(self):
        bad_path = Path(self.tmp.name) / "bad.csv"
        bad_path.write_bytes(
            b"cik,ticker,fund_name,next_redemption_date\n"
            b"1234567,ACME,Acme Interval Fund,2026-09-12\n"
            b"7654321,BETA,Fund\xff\xfe,2026-07-01\n",
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(store.import_legacy_csv(self.conn, bad_path), 0)
        self.assertIn("bad.csv", err.getvalue())
        self.assertIn("decode", err.getvalue().lower())

    def test_a_seed_with_no_importable_rows_warns_about_the_cik_column(self):
        no_cik = Path(self.tmp.name) / "export.csv"
        no_cik.write_text("fund_name,ticker,next_redemption_date\n"
                          "Acme Interval Fund,ACME,2026-09-12\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(store.import_legacy_csv(self.conn, no_cik), 0)
        self.assertIn("export.csv", err.getvalue())
        self.assertIn("cik", err.getvalue().lower())

    def test_a_successful_import_is_quiet(self):
        good = Path(self.tmp.name) / "seed.csv"
        good.write_text("cik,ticker,fund_name,next_redemption_date\n"
                        "1234567,ACME,Acme Interval Fund,2026-09-12\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(store.import_legacy_csv(self.conn, good), 1)
        self.assertEqual(err.getvalue(), "")


class DatedCopyTest(StoreTestCase):
    """redemptions.csv gets overwritten on every change. the dated copy next
    to it is the history, one per day something changed."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "redemptions.csv"
        store.add_fund(self.conn, "1234567", "ACME", "Acme Interval Fund")

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_dated_csv_path_puts_the_date_before_the_suffix(self):
        self.assertEqual(
            store.dated_csv_path(Path("x") / "redemptions.csv", date(2026, 9, 8)),
            Path("x") / "redemptions-2026-09-08.csv",
        )

    def test_a_write_takes_a_dated_copy_identical_to_the_main_file(self):
        store.write_csv(self.conn, self.path, today=date(2026, 9, 8))
        dated = self.path.with_name("redemptions-2026-09-08.csv")
        self.assertTrue(dated.exists())
        self.assertEqual(dated.read_bytes(), self.path.read_bytes())

    def test_a_later_write_leaves_the_earlier_dated_copy_alone(self):
        store.write_csv(self.conn, self.path, today=date(2026, 9, 8))
        first = self.path.with_name("redemptions-2026-09-08.csv").read_bytes()
        store.add_fund(self.conn, "7654321", "BETA", "Beta Credit Fund")
        store.write_csv(self.conn, self.path, today=date(2026, 9, 15))
        self.assertEqual(
            self.path.with_name("redemptions-2026-09-08.csv").read_bytes(), first)
        self.assertIn(b"Beta Credit Fund", self.path.read_bytes())
        self.assertIn(b"Beta Credit Fund",
                      self.path.with_name("redemptions-2026-09-15.csv").read_bytes())

    def test_an_unchanged_csv_takes_no_snapshot(self):
        # startup rewrites the csv, opening the app on a quiet day shouldn't
        # add a snapshot
        store.write_csv(self.conn, self.path, today=date(2026, 9, 8))
        store.write_csv(self.conn, self.path, today=date(2026, 9, 9))
        self.assertFalse(self.path.with_name("redemptions-2026-09-09.csv").exists())


if __name__ == "__main__":
    unittest.main()
