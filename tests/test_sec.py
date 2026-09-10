import time
import threading
import json
import unittest
from pathlib import Path

import sec

FIXTURES = Path(__file__).parent / "fixtures"


def load_submissions():
    return json.loads((FIXTURES / "submissions_acme.json").read_text(encoding="utf-8"))


class CikHelpersTest(unittest.TestCase):
    def test_pad_cik_pads_to_ten_digits(self):
        self.assertEqual(sec.pad_cik("1234567"), "0001234567")

    def test_pad_cik_accepts_already_padded(self):
        self.assertEqual(sec.pad_cik("0001234567"), "0001234567")

    def test_pad_cik_accepts_int(self):
        self.assertEqual(sec.pad_cik(1234567), "0001234567")

    def test_strip_cik_removes_leading_zeros(self):
        self.assertEqual(sec.strip_cik("0001234567"), "1234567")

    def test_round_trip(self):
        self.assertEqual(sec.strip_cik(sec.pad_cik("1234567")), "1234567")


class FindLatestFilingTest(unittest.TestCase):
    def test_picks_most_recent_target_form_ignoring_newer_non_target(self):
        result = sec.find_latest_filing(load_submissions(), sec.ALL_TARGET_FORMS)
        self.assertEqual(result["form_type"], "N-23C3A")
        self.assertEqual(result["filing_date"], "2026-08-14")
        self.assertEqual(result["accession_number"], "0001234567-26-000123")
        self.assertEqual(result["primary_document"], "notice.htm")

    def test_respects_a_narrower_form_set(self):
        result = sec.find_latest_filing(load_submissions(), sec.TO_FORMS)
        self.assertEqual(result["form_type"], "SC TO-I")
        self.assertEqual(result["filing_date"], "2025-11-03")

    def test_returns_none_when_no_target_form_present(self):
        subs = load_submissions()
        subs["filings"]["recent"]["form"] = ["N-CSR", "8-K", "N-CSR", "8-K", "8-K"]
        self.assertIsNone(sec.find_latest_filing(subs, sec.ALL_TARGET_FORMS))

    def test_returns_none_on_empty_feed(self):
        self.assertIsNone(sec.find_latest_filing({}, sec.ALL_TARGET_FORMS))

    def test_tolerates_short_primary_document_array(self):
        subs = load_submissions()
        subs["filings"]["recent"]["primaryDocument"] = ["ncsr.htm"]
        result = sec.find_latest_filing(subs, sec.ALL_TARGET_FORMS)
        self.assertEqual(result["primary_document"], "")

    def test_skips_entries_with_unparseable_dates(self):
        subs = load_submissions()
        subs["filings"]["recent"]["filingDate"][1] = "not-a-date"
        result = sec.find_latest_filing(subs, sec.ALL_TARGET_FORMS)
        self.assertEqual(result["filing_date"], "2026-05-12")


class DetectLatestTest(unittest.TestCase):
    def test_builds_the_archives_url_without_dashes_in_accession(self):
        result = sec.detect_latest(load_submissions())
        self.assertEqual(
            result["primary_document_url"],
            "https://www.sec.gov/Archives/edgar/data/1234567/"
            "000123456726000123/notice.htm",
        )

    def test_carries_form_and_accession_through(self):
        result = sec.detect_latest(load_submissions())
        self.assertEqual(result["form_type"], "N-23C3A")
        self.assertEqual(result["accession_number"], "0001234567-26-000123")

    def test_returns_none_when_nothing_matches(self):
        subs = load_submissions()
        subs["filings"]["recent"]["form"] = ["8-K"] * 5
        self.assertIsNone(sec.detect_latest(subs))


class FundIdentityTest(unittest.TestCase):
    def test_reads_name_and_first_ticker(self):
        name, ticker = sec.fund_identity(load_submissions())
        self.assertEqual(name, "Acme Interval Fund")
        self.assertEqual(ticker, "ACME")

    def test_missing_tickers_yields_empty_string(self):
        subs = load_submissions()
        subs["tickers"] = []
        name, ticker = sec.fund_identity(subs)
        self.assertEqual(name, "Acme Interval Fund")
        self.assertEqual(ticker, "")


class ResolveQueryTest(unittest.TestCase):
    def setUp(self):
        sec.reset_ticker_cache()
        self.tickers_json = (FIXTURES / "company_tickers.json").read_bytes()

    def fake_fetch(self, url, user_agent):
        if url.endswith("company_tickers.json"):
            return self.tickers_json
        raise AssertionError(f"unexpected fetch of {url}")

    def test_digits_are_treated_as_a_cik(self):
        cik = sec.resolve_query("0001234567", "UA", fetch=self.fake_fetch)
        self.assertEqual(cik, "1234567")

    def test_ticker_is_looked_up(self):
        cik = sec.resolve_query("acme", "UA", fetch=self.fake_fetch)
        self.assertEqual(cik, "1234567")

    def test_unknown_ticker_raises_with_a_readable_message(self):
        with self.assertRaises(sec.SECError) as ctx:
            sec.resolve_query("NOPE", "UA", fetch=self.fake_fetch)
        self.assertIn("NOPE", str(ctx.exception))

    def test_blank_query_raises(self):
        with self.assertRaises(sec.SECError):
            sec.resolve_query("   ", "UA", fetch=self.fake_fetch)

    def test_ticker_map_is_fetched_only_once(self):
        calls = []

        def counting_fetch(url, user_agent):
            calls.append(url)
            return self.tickers_json

        sec.resolve_query("ACME", "UA", fetch=counting_fetch)
        sec.resolve_query("BETA", "UA", fetch=counting_fetch)
        self.assertEqual(len(calls), 1)


class CheckConnectionTest(unittest.TestCase):
    def test_success(self):
        ok, message = sec.check_connection("UA", fetch=lambda u, a: b"{}")
        self.assertTrue(ok)
        self.assertIn("data.sec.gov", message)

    def test_404_counts_as_reachable(self):
        def fetch(url, ua):
            raise sec.SECError("HTTP 404 from https://data.sec.gov/x")

        ok, _ = sec.check_connection("UA", fetch=fetch)
        self.assertTrue(ok)

    def test_403_is_a_failure_and_reports_the_status(self):
        def fetch(url, ua):
            raise sec.SECError("HTTP 403 from https://data.sec.gov/x")

        ok, message = sec.check_connection("UA", fetch=fetch)
        self.assertFalse(ok)
        self.assertIn("403", message)

    def test_tls_failure_suggests_ssl_cert_file(self):
        def fetch(url, ua):
            raise sec.SECError("Could not reach x: [SSL: CERTIFICATE_VERIFY_FAILED] "
                               "certificate verify failed")

        ok, message = sec.check_connection("UA", fetch=fetch)
        self.assertFalse(ok)
        self.assertIn("SSL_CERT_FILE", message)

    def test_dns_failure_mentions_a_proxy(self):
        def fetch(url, ua):
            raise sec.SECError("Could not reach x: [Errno -2] Name or service not known")

        ok, message = sec.check_connection("UA", fetch=fetch)
        self.assertFalse(ok)
        self.assertIn("proxy", message.lower())

    def test_missing_user_agent_fails_without_fetching(self):
        def explode(url, ua):
            raise AssertionError("should not fetch")

        ok, message = sec.check_connection("", fetch=explode)
        self.assertFalse(ok)
        self.assertIn("user agent", message.lower())

    def test_probes_both_hosts(self):
        seen = []

        def fetch(url, ua):
            seen.append(url)
            return b"{}"

        ok, message = sec.check_connection("UA", fetch=fetch)
        self.assertTrue(ok)
        self.assertTrue(any("data.sec.gov" in u for u in seen))
        self.assertTrue(any("www.sec.gov" in u for u in seen))
        self.assertIn("data.sec.gov", message)
        self.assertIn("www.sec.gov", message)

    def test_a_www_failure_fails_overall_even_when_data_passes(self):
        def fetch(url, ua):
            if "www.sec.gov" in url:
                raise sec.SECError("HTTP 403 from https://www.sec.gov/robots.txt")
            return b"{}"

        ok, message = sec.check_connection("UA", fetch=fetch)
        self.assertFalse(ok, "a working API host must not mask a broken document host")
        self.assertIn("www.sec.gov", message)
        self.assertIn("403", message)

    def test_a_403_explains_the_user_agent_format(self):
        def fetch(url, ua):
            raise sec.SECError("HTTP 403 from https://www.sec.gov/robots.txt")

        ok, message = sec.check_connection("UA", fetch=fetch)
        self.assertFalse(ok)
        self.assertIn("you@example.com", message)



class TargetFormsTest(unittest.TestCase):
    """SC TO-C is just a communication and SC TO-T a third party offer.
    Neither has a repurchase deadline, so treating one as the latest filing
    forces a pointless no-date review."""

    def test_tender_offer_communications_are_not_targets(self):
        self.assertNotIn("SC TO-C", sec.ALL_TARGET_FORMS)
        self.assertNotIn("SC TO-C/A", sec.ALL_TARGET_FORMS)

    def test_third_party_tender_offers_are_not_targets(self):
        self.assertNotIn("SC TO-T", sec.ALL_TARGET_FORMS)
        self.assertNotIn("SC TO-T/A", sec.ALL_TARGET_FORMS)

    def test_the_issuer_offer_and_its_amendment_remain_targets(self):
        self.assertIn("SC TO-I", sec.ALL_TARGET_FORMS)
        self.assertIn("SC TO-I/A", sec.ALL_TARGET_FORMS)

    def test_a_newer_communication_does_not_displace_the_offer(self):
        subs = load_submissions()
        recent = subs["filings"]["recent"]
        recent["form"][0] = "SC TO-C"
        recent["filingDate"][0] = "2026-08-20"
        result = sec.find_latest_filing(subs, sec.ALL_TARGET_FORMS)
        self.assertEqual(result["form_type"], "N-23C3A")


class _FakeResponse:
    headers: dict = {}

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ConcurrencyTest(unittest.TestCase):
    """sec_get gets called from the check thread and request threads at the
    same time. The 500ms spacing has to hold across all of them, not per
    thread."""

    def setUp(self):
        self._rate = sec.SEC_RATE_SECONDS
        self._last = sec._last_request
        self._urlopen = sec.urllib.request.urlopen
        sec.SEC_RATE_SECONDS = 0.1

    def tearDown(self):
        sec.SEC_RATE_SECONDS = self._rate
        sec._last_request = self._last
        sec.urllib.request.urlopen = self._urlopen
        sec.reset_ticker_cache()

    def test_concurrent_requests_are_still_spaced_by_the_rate(self):
        stamps = []
        guard = threading.Lock()

        def fake_urlopen(req, timeout=None):
            with guard:
                stamps.append(time.monotonic())
            return _FakeResponse()

        sec.urllib.request.urlopen = fake_urlopen
        # every thread sees the same fresh stamp, so without the lock they
        # all work out the same wait and fire together
        sec._last_request = time.time()
        threads = [threading.Thread(target=sec.sec_get, args=("https://x", "ua"))
                   for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        stamps.sort()
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertEqual(len(stamps), 3)
        self.assertTrue(all(g >= sec.SEC_RATE_SECONDS * 0.9 for g in gaps), gaps)

    def test_concurrent_ticker_loads_fetch_the_file_once(self):
        sec.reset_ticker_cache()
        calls = []

        def slow_fetch(url, user_agent):
            calls.append(url)
            time.sleep(0.05)
            return json.dumps({"0": {"ticker": "ACME", "cik_str": 1234567}}).encode()

        threads = [threading.Thread(target=sec.load_company_tickers,
                                    args=("ua", slow_fetch)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(calls), 1)
        self.assertEqual(sec.resolve_query("acme", "ua", fetch=slow_fetch), "1234567")


if __name__ == "__main__":
    unittest.main()
