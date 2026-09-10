import json
import os
import tempfile
import unittest
from pathlib import Path

import research
import sec
import viewer

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FilingRowsTest(unittest.TestCase):
    def test_one_dict_per_filing_with_plain_keys(self):
        rows = sec.filing_rows(load("submissions_grouped.json")["filings"]["recent"])
        self.assertEqual(len(rows), 14)
        self.assertEqual(rows[0], {
            "form": "N-23C3A", "filing_date": "2026-08-04",
            "accession": "0001783964-26-000031",
            "primary_document": "notice_q3.htm", "description": "NOTICE",
        })

    def test_missing_description_array_gives_empty_strings(self):
        block = {"form": ["N-CSR"], "accessionNumber": ["0000000001-26-000001"],
                 "filingDate": ["2026-01-01"], "primaryDocument": ["a.htm"]}
        self.assertEqual(sec.filing_rows(block)[0]["description"], "")

    def test_ragged_arrays_do_not_raise(self):
        block = {"form": ["N-CSR", "497"], "accessionNumber": ["0000000001-26-000001"],
                 "filingDate": ["2026-01-01"], "primaryDocument": []}
        rows = sec.filing_rows(block)
        self.assertEqual(rows[1]["accession"], "")
        self.assertEqual(rows[1]["primary_document"], "")


class GroupFilingsTest(unittest.TestCase):
    def setUp(self):
        self.groups = sec.group_filings(
            sec.filing_rows(load("submissions_grouped.json")["filings"]["recent"]))

    def keys(self):
        return [g["key"] for g in self.groups]

    def forms(self, key):
        group = next(g for g in self.groups if g["key"] == key)
        return [(f["form"], f["filing_date"]) for f in group["filings"]]

    def test_groups_come_in_fixed_order_with_empties_hidden(self):
        self.assertEqual(self.keys(),
                         ["prospectus", "reports", "repurchase", "holdings",
                          "governance", "other"])

    def test_labels(self):
        self.assertEqual([g["label"] for g in self.groups],
                         ["prospectus", "reports", "repurchase notices",
                          "holdings", "governance", "other"])

    def test_only_repurchase_is_tracked(self):
        self.assertEqual([g["key"] for g in self.groups if g["tracked"]],
                         ["repurchase"])

    def test_newest_first_within_a_group(self):
        self.assertEqual(self.forms("repurchase"), [
            ("N-23C3A", "2026-08-04"), ("N-23C3A", "2026-05-05"),
            ("N-23C3A", "2026-02-03"), ("N-23C3A", "2025-11-04")])

    def test_same_day_tie_breaks_on_accession_descending(self):
        self.assertEqual(self.forms("prospectus"),
                         [("497", "2026-05-15"), ("N-2/A", "2026-05-15")])

    def test_unknown_forms_land_in_other(self):
        self.assertEqual(self.forms("other"), [("8-K", "2025-08-01")])

    def test_empty_group_is_dropped(self):
        rows = [{"form": "N-CSR", "filing_date": "2026-01-01",
                 "accession": "0000000001-26-000001",
                 "primary_document": "a.htm", "description": ""}]
        self.assertEqual([g["key"] for g in sec.group_filings(rows)], ["reports"])

    def test_empty_input_gives_no_groups(self):
        self.assertEqual(sec.group_filings([]), [])


class FakeFetch:
    """Serves fixture bytes by url and remembers what was asked for."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, url, user_agent):
        self.calls.append(url)
        if url not in self.mapping:
            raise sec.SECError(f"HTTP 404 from {url}")
        body = self.mapping[url]
        if isinstance(body, Exception):
            raise body
        return body


FEED_URL = "https://data.sec.gov/submissions/CIK0001234567.json"
PAGE_URL = "https://data.sec.gov/submissions/CIK0001234567-submissions-001.json"


class LoadSubmissionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = viewer.CACHE_DIR
        viewer.CACHE_DIR = Path(self.tmp.name)
        self.fetch = FakeFetch({
            FEED_URL: (FIXTURES / "submissions_paged.json").read_bytes(),
            PAGE_URL: (FIXTURES / "submissions_page_001.json").read_bytes(),
        })

    def tearDown(self):
        viewer.CACHE_DIR = self._cache
        self.tmp.cleanup()

    def test_first_load_fetches_and_folds_older_pages_into_recent(self):
        feed, age, error = research.load_submissions("1234567", "ua", fetch=self.fetch)
        self.assertEqual(self.fetch.calls, [FEED_URL, PAGE_URL])
        self.assertEqual(feed["filings"]["recent"]["form"], ["N-CSR", "N-2", "N-23C3A"])
        self.assertEqual(feed["filings"]["recent"]["primaryDocDescription"],
                         ["ANNUAL REPORT", "PROSPECTUS", "NOTICE"])
        self.assertEqual(age, 0.0)
        self.assertEqual(error, "")

    def test_first_load_writes_the_folded_feed_to_the_cache(self):
        research.load_submissions("1234567", "ua", fetch=self.fetch)
        path = research.submissions_cache_path("1234567")
        self.assertEqual(path, Path(self.tmp.name) / "submissions" / "1234567.json")
        cached = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(cached["filings"]["recent"]["form"]), 3)
        self.assertEqual(list(Path(self.tmp.name, "submissions").glob("*.tmp")), [])

    def test_a_fresh_cache_is_not_refetched(self):
        research.load_submissions("1234567", "ua", fetch=self.fetch)
        self.fetch.calls.clear()
        feed, age, error = research.load_submissions("1234567", "ua", fetch=self.fetch)
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(len(feed["filings"]["recent"]["form"]), 3)
        self.assertGreaterEqual(age, 0.0)
        self.assertLess(age, research.SUBMISSIONS_TTL)

    def test_an_old_cache_is_refetched(self):
        research.load_submissions("1234567", "ua", fetch=self.fetch)
        path = research.submissions_cache_path("1234567")
        old = os.path.getmtime(path) - research.SUBMISSIONS_TTL - 60
        os.utime(path, (old, old))
        self.fetch.calls.clear()
        _, age, error = research.load_submissions("1234567", "ua", fetch=self.fetch)
        self.assertEqual(self.fetch.calls, [FEED_URL, PAGE_URL])
        self.assertEqual(age, 0.0)

    def test_a_failed_refetch_serves_the_stale_copy_and_says_so(self):
        research.load_submissions("1234567", "ua", fetch=self.fetch)
        path = research.submissions_cache_path("1234567")
        old = os.path.getmtime(path) - research.SUBMISSIONS_TTL - 60
        os.utime(path, (old, old))
        broken = FakeFetch({FEED_URL: sec.SECError("Could not reach data.sec.gov")})
        feed, age, error = research.load_submissions("1234567", "ua", fetch=broken)
        self.assertEqual(len(feed["filings"]["recent"]["form"]), 3)
        self.assertGreater(age, research.SUBMISSIONS_TTL)
        self.assertIn("Could not reach", error)

    def test_no_cache_and_a_failed_fetch_raises(self):
        broken = FakeFetch({})
        with self.assertRaises(sec.SECError):
            research.load_submissions("1234567", "ua", fetch=broken)

    def test_a_page_name_that_is_not_an_edgar_page_is_skipped(self):
        feed = json.loads((FIXTURES / "submissions_paged.json").read_text())
        feed["filings"]["files"][0]["name"] = "../../etc/passwd"
        fetch = FakeFetch({FEED_URL: json.dumps(feed).encode()})
        loaded, _, _ = research.load_submissions("1234567", "ua", fetch=fetch)
        self.assertEqual(fetch.calls, [FEED_URL])
        self.assertEqual(loaded["filings"]["recent"]["form"], ["N-CSR"])

    def test_a_failed_page_fetch_keeps_what_was_loaded(self):
        fetch = FakeFetch({FEED_URL: (FIXTURES / "submissions_paged.json").read_bytes()})
        loaded, _, error = research.load_submissions("1234567", "ua", fetch=fetch)
        self.assertEqual(loaded["filings"]["recent"]["form"], ["N-CSR"])
        self.assertIn("older filings", error)

    def test_a_partial_list_stays_marked_partial_on_the_next_load(self):
        fetch = FakeFetch({FEED_URL: (FIXTURES / "submissions_paged.json").read_bytes()})
        research.load_submissions("1234567", "ua", fetch=fetch)
        _, _, note = research.load_submissions("1234567", "ua", fetch=fetch)
        self.assertIn("older filings", note)
        self.assertEqual(fetch.calls.count(PAGE_URL), 2)  # retried, still failing

    def test_a_missing_page_is_folded_in_once_it_becomes_available(self):
        fetch = FakeFetch({FEED_URL: (FIXTURES / "submissions_paged.json").read_bytes()})
        research.load_submissions("1234567", "ua", fetch=fetch)
        fetch.mapping[PAGE_URL] = (FIXTURES / "submissions_page_001.json").read_bytes()
        feed, _, note = research.load_submissions("1234567", "ua", fetch=fetch)
        self.assertEqual(note, "")
        self.assertEqual(feed["filings"]["recent"]["form"], ["N-CSR", "N-2", "N-23C3A"])
        cached = json.loads(research.submissions_cache_path("1234567").read_text())
        self.assertEqual(len(cached["filings"]["recent"]["form"]), 3)
        self.assertEqual(cached["filings"]["files"], [])


INDEX_URL = "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000017/index.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000017/n2a.htm"


class FilingDocumentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = viewer.CACHE_DIR
        viewer.CACHE_DIR = Path(self.tmp.name)
        self.fetch = FakeFetch({INDEX_URL: (FIXTURES / "filing_index_multi.json").read_bytes()})

    def tearDown(self):
        viewer.CACHE_DIR = self._cache
        self.tmp.cleanup()

    def docs(self, **kw):
        return research.filing_documents("1783964", "0001783964-26-000017", "ua",
                                         fetch=self.fetch, **kw)

    def test_archive_dir_url_strips_the_cik_and_the_dashes(self):
        self.assertEqual(research.archive_dir_url("0001783964", "0001783964-26-000017"),
                         "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000017/")

    def test_index_files_the_txt_bundle_and_images_are_left_out(self):
        names = [d["name"] for d in self.docs(primary="n2a.htm")]
        self.assertEqual(names, ["n2a.htm", "ex99a.htm", "feeschedule.pdf"])

    def test_primary_comes_first_and_carries_the_feed_description(self):
        first = self.docs(primary="n2a.htm", description="PROSPECTUS")[0]
        self.assertEqual(first["name"], "n2a.htm")
        self.assertTrue(first["primary"])
        self.assertEqual(first["description"], "PROSPECTUS")
        self.assertEqual(first["url"], DOC_URL)

    def test_html_is_decided_by_extension(self):
        by_name = {d["name"]: d for d in self.docs(primary="n2a.htm")}
        self.assertTrue(by_name["n2a.htm"]["is_html"])
        self.assertTrue(by_name["ex99a.htm"]["is_html"])
        self.assertFalse(by_name["feeschedule.pdf"]["is_html"])
        self.assertEqual(by_name["feeschedule.pdf"]["kind"], "PDF")
        self.assertEqual(by_name["ex99a.htm"]["kind"], "HTML")

    def test_the_index_is_fetched_once_and_cached_forever(self):
        self.docs(primary="n2a.htm")
        self.docs(primary="n2a.htm")
        self.assertEqual(self.fetch.calls, [INDEX_URL])
        cached = Path(self.tmp.name, "research", "0001783964-26-000017", "index.json")
        self.assertTrue(cached.exists())

    def test_a_primary_missing_from_the_index_is_still_listed_first(self):
        # the index can lag the feed by a few minutes after filing
        names = [d["name"] for d in self.docs(primary="late.htm")]
        self.assertEqual(names[0], "late.htm")

    def test_a_broken_index_falls_back_to_the_primary_alone(self):
        fetch = FakeFetch({INDEX_URL: sec.SECError("HTTP 503 from www.sec.gov")})
        docs = research.filing_documents("1783964", "0001783964-26-000017", "ua",
                                         primary="n2a.htm", fetch=fetch)
        self.assertEqual([d["name"] for d in docs], ["n2a.htm"])


class FetchDocumentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._cache = viewer.CACHE_DIR
        viewer.CACHE_DIR = Path(self.tmp.name)

    def tearDown(self):
        viewer.CACHE_DIR = self._cache
        self.tmp.cleanup()

    def test_cache_path_is_under_research_by_accession_and_name(self):
        path = research.document_cache_path("0001783964-26-000017", "n2a.htm")
        self.assertEqual(path, Path(self.tmp.name, "research", "0001783964-26-000017", "n2a.htm"))

    def test_cache_path_neutralises_traversal(self):
        path = research.document_cache_path("../x", "../../etc/passwd")
        self.assertEqual(path.parent.parent, Path(self.tmp.name, "research"))
        self.assertNotIn("..", str(path.relative_to(Path(self.tmp.name, "research"))))
        self.assertEqual(path.parent.name, "_x")

    def test_fetches_once_then_serves_from_disk(self):
        fetch = FakeFetch({DOC_URL: b"<html><body>Prospectus</body></html>"})
        first = research.fetch_document(DOC_URL, "0001783964-26-000017", "n2a.htm", "ua", fetch=fetch)
        second = research.fetch_document(DOC_URL, "0001783964-26-000017", "n2a.htm", "ua", fetch=fetch)
        self.assertEqual(first, second)
        self.assertEqual(fetch.calls, [DOC_URL])

    def test_is_html_name(self):
        self.assertTrue(research.is_html_name("a.htm"))
        self.assertTrue(research.is_html_name("A.HTML"))
        self.assertFalse(research.is_html_name("a.pdf"))
        self.assertFalse(research.is_html_name("nport.xml"))


class BuildDocumentPageTest(unittest.TestCase):
    def test_sanitizes_without_marking_dates(self):
        raw = "<html><body><script>var x=1</script><p>Deadline: September 12, 2026</p></body></html>"
        page = viewer.build_document_page(raw, "https://www.sec.gov/Archives/edgar/data/1/2/")
        self.assertNotIn("var x=1", page)
        self.assertIn("September 12, 2026", page)
        self.assertNotIn("<mark", page)
        self.assertNotIn("postMessage", page)
        self.assertIn('<base href="https://www.sec.gov/Archives/edgar/data/1/2/"/>', page)


if __name__ == "__main__":
    unittest.main()
