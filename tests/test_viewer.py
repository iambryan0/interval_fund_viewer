import re
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path

import viewer

BASE = "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000123/"


class SanitizeTest(unittest.TestCase):
    def clean(self, html):
        return viewer.sanitize(html, BASE)

    def test_script_element_and_its_content_are_removed(self):
        out = self.clean("<p>before</p><script>alert('x')</script><p>after</p>")
        self.assertNotIn("script", out)
        self.assertNotIn("alert", out)
        self.assertIn("before", out)
        self.assertIn("after", out)

    def test_iframe_object_embed_applet_form_are_removed(self):
        out = self.clean(
            "<iframe src='x'></iframe><object></object><embed>"
            "<applet></applet><form><input></form><p>kept</p>"
        )
        for tag in ["iframe", "object", "embed", "applet", "form", "input"]:
            self.assertNotIn(f"<{tag}", out)
        self.assertIn("kept", out)

    def test_event_handler_attributes_are_stripped(self):
        out = self.clean("<p onclick=\"steal()\" onmouseover='x'>text</p>")
        self.assertNotIn("onclick", out)
        self.assertNotIn("onmouseover", out)
        self.assertIn("text", out)

    def test_javascript_urls_are_stripped(self):
        out = self.clean("<a href=\"javascript:alert(1)\">link</a>")
        self.assertNotIn("javascript:", out)
        self.assertIn("link", out)

    def test_javascript_url_detection_ignores_whitespace_and_case(self):
        out = self.clean("<a href=\"  JaVaScRiPt:alert(1)\">link</a>")
        self.assertNotIn("avascript", out)

    def test_javascript_url_with_embedded_tab_is_filtered(self):
        out = self.clean("<a href=\"java\tscript:alert(1)\">link</a>")
        self.assertNotIn("javascript", out)
        self.assertNotIn("alert", out)
        self.assertIn("link", out)

    def test_javascript_url_with_embedded_newline_is_filtered(self):
        out = self.clean("<a href=\"java\nscript:alert(1)\">link</a>")
        self.assertNotIn("javascript", out)
        self.assertNotIn("alert", out)
        self.assertIn("link", out)

    def test_meta_refresh_is_removed_but_other_meta_survives(self):
        out = self.clean(
            "<meta http-equiv='refresh' content='0;url=http://evil'>"
            "<meta name='author' content='acme'>"
        )
        self.assertNotIn("refresh", out)
        self.assertIn("author", out)

    def test_meta_refresh_with_whitespace_around_value_is_removed(self):
        out = self.clean(
            "<meta http-equiv=' refresh ' content='0;url=http://evil'>"
            "<meta name='year' content='2026'>"
        )
        self.assertNotIn("0;url=http://evil", out)
        self.assertIn("year", out)

    def test_namespaced_url_attributes_are_filtered(self):
        out = self.clean("<svg><a xlink:href=\"javascript:alert(1)\">click</a></svg>")
        self.assertNotIn("javascript", out)
        self.assertNotIn("alert", out)
        self.assertIn("click", out)

    def test_tables_and_inline_styles_survive(self):
        html = (
            "<table><tr><td style='font-weight:bold'>Deadline</td>"
            "<td>September 12, 2026</td></tr></table>"
        )
        out = self.clean(html)
        self.assertIn("<table>", out)
        self.assertIn("<td style=\"font-weight:bold\">", out)
        self.assertIn("September 12, 2026", out)

    def test_style_elements_survive(self):
        out = self.clean("<style>.deadline { color: red }</style><p>x</p>")
        self.assertIn("<style>", out)
        self.assertIn("</style>", out)
        self.assertIn(".deadline { color: red }", out)

    def test_css_is_emitted_raw_so_selectors_keep_working(self):
        out = self.clean("<style>td > p { color: red } a[href~='x'] & b {}</style>")
        self.assertIn("td > p { color: red }", out)
        self.assertIn("& b {}", out)
        self.assertNotIn("&gt;", out)
        self.assertNotIn("&amp;", out)

    def test_script_is_still_dropped_when_style_is_kept(self):
        out = self.clean("<style>p{color:red}</style><script>alert('x')</script>")
        self.assertIn("p{color:red}", out)
        self.assertNotIn("alert", out)
        self.assertNotIn("<script", out)

    def test_a_self_closing_style_is_emitted_in_paired_form(self):
        """There's no self-closing <style> in html, a browser reading <style/>
        would treat the rest of the document as css."""
        out = self.clean("<style />x<p>deadline</p>")
        self.assertNotIn("<style/>", out)
        self.assertIn("<style></style>", out)
        self.assertIn("<p>deadline</p>", out)

    def test_a_style_inside_a_dropped_element_stays_dropped(self):
        out = self.clean("<form><style>p{color:red}</style></form><p>kept</p>")
        self.assertNotIn("color:red", out)
        self.assertIn("kept", out)

    def test_base_tag_is_injected_once(self):
        out = self.clean("<p>hi</p>")
        self.assertEqual(out.count("<base"), 1)
        self.assertIn(BASE, out)

    def test_existing_base_tag_is_not_duplicated(self):
        out = self.clean("<base href='http://elsewhere/'><p>hi</p>")
        self.assertEqual(out.count("<base"), 1)
        self.assertNotIn("elsewhere", out)

    def test_void_elements_are_self_closed(self):
        out = self.clean("<p>a<br>b<img src='logo.png'>c</p>")
        self.assertIn("<br/>", out)
        self.assertIn("<img src=\"logo.png\"/>", out)

    def test_entities_are_preserved(self):
        out = self.clean("<p>A &amp; B &nbsp; C</p>")
        self.assertIn("&amp;", out)
        self.assertIn("&nbsp;", out)

    def test_text_is_escaped_so_it_cannot_introduce_markup(self):
        out = self.clean("<p>1 &lt; 2</p>")
        self.assertIn("&lt;", out)
        self.assertNotIn("<p>1 < 2", out)

    def test_malformed_html_does_not_raise(self):
        self.clean("<p>unclosed <b>bold <table><tr><td>x")


class NormalizeDateTest(unittest.TestCase):
    def test_month_name_first(self):
        self.assertEqual(viewer.normalize_date("September 12, 2026"), "2026-09-12")

    def test_month_name_without_comma(self):
        self.assertEqual(viewer.normalize_date("September 12 2026"), "2026-09-12")

    def test_abbreviated_month_with_period(self):
        self.assertEqual(viewer.normalize_date("Sept. 12, 2026"), "2026-09-12")

    def test_day_first(self):
        self.assertEqual(viewer.normalize_date("12 September 2026"), "2026-09-12")

    def test_slashes(self):
        self.assertEqual(viewer.normalize_date("9/12/2026"), "2026-09-12")

    def test_two_digit_year_assumes_2000s(self):
        self.assertEqual(viewer.normalize_date("9/12/26"), "2026-09-12")

    def test_already_iso(self):
        self.assertEqual(viewer.normalize_date("2026-09-12"), "2026-09-12")

    def test_non_breaking_spaces_are_tolerated(self):
        self.assertEqual(viewer.normalize_date("September\xa012,\xa02026"), "2026-09-12")

    def test_impossible_date_returns_empty(self):
        self.assertEqual(viewer.normalize_date("February 30, 2026"), "")

    def test_garbage_returns_empty(self):
        self.assertEqual(viewer.normalize_date("sometime soon"), "")


class ClassifyDatesTest(unittest.TestCase):
    def test_date_near_a_phrase_is_green(self):
        hits = viewer.classify_dates("Repurchase Request Deadline September 12, 2026")
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0][3])

    def test_date_far_from_any_phrase_is_not_green(self):
        text = "Repurchase Request Deadline " + ("filler " * 80) + "September 12, 2026"
        hits = viewer.classify_dates(text)
        self.assertFalse(hits[0][3])

    def test_window_boundary(self):
        # "must be received" anchors at word 0, so N filler words put the
        # date at word N+3, distance N+3 from the anchor
        for filler, expected in ((26, True), (27, True), (28, False)):
            text = "must be received " + ("x " * filler) + "September 12, 2026"
            hits = viewer.classify_dates(text)
            self.assertEqual(hits[0][3], expected,
                             f"filler={filler} puts the date {filler + 3} words out")

    def test_returns_document_order_with_offsets_and_iso(self):
        text = "filed 2026-07-28 and the request deadline is September 12, 2026"
        hits = viewer.classify_dates(text)
        self.assertEqual([h[2] for h in hits], ["2026-07-28", "2026-09-12"])
        self.assertEqual(text[hits[0][0]:hits[0][1]], "2026-07-28")

    def test_unparseable_date_gets_an_empty_iso(self):
        hits = viewer.classify_dates("deadline 13/45/2026")
        self.assertEqual(hits[0][2], "")

    def test_no_phrases_means_nothing_green(self):
        hits = viewer.classify_dates("organized on March 3, 1998")
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0][3])

    def test_empty_text(self):
        self.assertEqual(viewer.classify_dates(""), [])


class AnnotateTest(unittest.TestCase):
    def test_wraps_a_date_with_index_and_iso(self):
        html, total, green = viewer.annotate("<p>due September 12, 2026 ok</p>")
        self.assertIn('data-i="0"', html)
        self.assertIn('data-iso="2026-09-12"', html)
        self.assertEqual((total, green), (1, 0))

    def test_keywords_are_no_longer_marked(self):
        html, total, green = viewer.annotate("<p>the repurchase request deadline applies</p>")
        self.assertNotIn("<mark", html)
        self.assertEqual((total, green), (0, 0))

    def test_proximity_works_across_table_cells(self):
        # the case that forces two passes: label and date in sibling <td>s
        html, total, green = viewer.annotate(
            "<table><tr><td>Repurchase Request Deadline</td>"
            "<td>September 12, 2026</td></tr></table>")
        self.assertEqual((total, green), (1, 1))
        self.assertIn('class="hit green"', html)

    def test_a_distant_date_in_another_cell_is_not_green(self):
        filler = "".join(f"<td>{'x ' * 20}</td>" for _ in range(3))
        html, total, green = viewer.annotate(
            f"<table><tr><td>Repurchase Request Deadline</td>{filler}"
            f"<td>September 12, 2026</td></tr></table>")
        self.assertEqual((total, green), (1, 0))

    def test_a_date_split_across_an_inline_element_is_marked_not_dropped(self):
        # real filings split dates across <font> tags all the time, a date
        # crossing a text node boundary has to be wrapped not dropped
        html, total, green = viewer.annotate("<p>on <b>August</b> 31, 2026</p>")
        self.assertEqual(total, 1)
        self.assertEqual(html.count("<mark"), 2, "one fragment per node the date overlaps")
        self.assertEqual(html.count('data-i="0"'), 2)
        self.assertEqual(html.count('data-iso="2026-08-31"'), 2)

    def test_markup_around_a_split_date_is_not_corrupted(self):
        html, _, _ = viewer.annotate("<p>on <b>August</b> 31, 2026</p>")
        # the <b> survives and the fragment inside it is fully nested, no
        # <mark> crosses the </b>
        self.assertIn("<b><mark", html)
        self.assertIn("</mark></b>", html)

    def test_counts_are_per_date_not_per_fragment(self):
        html, total, _ = viewer.annotate(
            "<p>on <b>August</b> 31, 2026 and also September 12, 2026</p>")
        self.assertEqual(total, 2)
        self.assertEqual(html.count('data-i="1"'), 1)

    def test_a_split_date_near_deadline_language_is_green_on_every_fragment(self):
        html, total, green = viewer.annotate(
            "<p>Repurchase Request Deadline: <b>August</b> 31, 2026</p>")
        self.assertEqual((total, green), (1, 1))
        self.assertEqual(html.count('class="hit green"'), 2)

    def test_marks_are_numbered_in_document_order(self):
        html, total, _ = viewer.annotate("<p>2026-07-28</p><p>2026-09-12</p>")
        self.assertEqual(total, 2)
        self.assertLess(html.index('data-i="0"'), html.index('data-i="1"'))

    def test_unparseable_date_is_marked_without_an_iso_attribute(self):
        html, total, _ = viewer.annotate("<p>13/45/2026</p>")
        self.assertEqual(total, 1)
        self.assertNotIn("data-iso", html)

    def test_nbsp_split_date_still_matches(self):
        html, total, _ = viewer.annotate("<p>September&nbsp;12,&nbsp;2026</p>")
        self.assertEqual(total, 1)

    def test_tag_names_and_attributes_are_never_matched(self):
        html, total, _ = viewer.annotate('<p class="deadline" title="2026-09-12">x</p>')
        self.assertEqual(total, 0)
        self.assertIn('title="2026-09-12"', html)

    def test_style_content_is_left_verbatim_and_unmarked(self):
        html, total, _ = viewer.annotate("<style>td > b { color: red }</style><p>x</p>")
        self.assertIn("td > b", html)
        self.assertEqual(total, 0)

    def test_text_outside_matches_is_escaped(self):
        html, _, _ = viewer.annotate("<p>a &lt; b, 2026-09-12</p>")
        self.assertIn("&lt;", html)

    def test_empty_input(self):
        self.assertEqual(viewer.annotate(""), ("", 0, 0))


class CrossCellFabricationTest(unittest.TestCase):
    r"""A match can only span a few text nodes.

    _NODE_SEPARATOR is a newline and DATE_RE's \s+ matches it, so with no cap
    a match runs across any number of blank nodes. The real case
    (0000930413-26-001814.html): a lone '6' in one cell and 'MAY 2026' in
    one twelve nodes away made up a clickable 2026-05-06 that's nowhere in
    the filing, and painted ten blobs across the empty cells between.
    """

    def test_a_digit_and_a_month_year_in_distant_cells_make_no_mark(self):
        empties = "".join("<td>  </td>" for _ in range(6))
        html, total, green = viewer.annotate(
            f"<table><tr><td>6</td>{empties}<td>MAY 2026</td></tr></table>")
        self.assertEqual((total, green), (0, 0))
        self.assertNotIn("<mark", html)
        self.assertNotIn("2026-05-06", html)

    def test_the_cells_themselves_are_left_intact(self):
        empties = "".join("<td>  </td>" for _ in range(6))
        html, _, _ = viewer.annotate(
            f"<table><tr><td>6</td>{empties}<td>MAY 2026</td></tr></table>")
        self.assertIn("<td>6</td>", html)
        self.assertIn("<td>MAY 2026</td>", html)

    def test_a_three_node_split_is_still_marked(self):
        # 89 real splits in the corpus, none wider than three nodes
        html, total, _ = viewer.annotate("<p>August <b>31,</b> 2026</p>")
        self.assertEqual(total, 1)
        self.assertIn('data-iso="2026-08-31"', html)

    def test_a_four_node_split_is_rejected(self):
        html, total, _ = viewer.annotate(
            "<p><b>August</b> <b>31,</b> <b>2026</b></p>")
        self.assertEqual(total, 0)
        self.assertNotIn("<mark", html)

    def test_whitespace_only_fragments_are_not_painted(self):
        # the gap between day and year is its own text node here. none of
        # the date is in it, marking it just paints a blob.
        html, total, _ = viewer.annotate("<p>August<b> </b>31, 2026</p>")
        self.assertEqual(total, 1)
        self.assertEqual(html.count("<mark"), 2)
        self.assertIn("<b> </b>", html)


FIXTURES = Path(__file__).parent / "fixtures"


def filing_html():
    return (FIXTURES / "filing_notice.html").read_text(encoding="utf-8")


def scto_html():
    return (FIXTURES / "filing_scto.html").read_text(encoding="utf-8", errors="replace")


def scto_amendment_html():
    return (FIXTURES / "filing_scto_amendment.html").read_text(
        encoding="utf-8", errors="replace")


def n23c3a_real_html():
    return (FIXTURES / "filing_n23c3a_real.html").read_text(
        encoding="utf-8", errors="replace")


def cross_cell_html():
    return (FIXTURES / "filing_cross_cell.html").read_text(
        encoding="utf-8", errors="replace")


class RealFilingTest(unittest.TestCase):
    """Measured against real filings. The numbers below came from the actual
    documents and are regression guards - if a change moves them it should
    be a change I meant to make."""

    def marks(self, raw):
        html, total, green = viewer.annotate(viewer.sanitize(raw, "https://example/"))
        isos = sorted({m for m in re.findall(r'class="hit green" data-i="\d+" '
                                             r'data-iso="([^"]+)"', html)})
        return total, green, isos

    def test_n23c3a_fixture_marks_the_deadline_green(self):
        total, green, isos = self.marks(filing_html())
        self.assertIn("2026-09-12", isos)

    def test_scto_finds_the_deadline_and_stays_selective(self):
        total, green, isos = self.marks(scto_html())
        self.assertIn("2026-08-17", isos, "the real deadline must be green")
        self.assertLessEqual(green, 4, "green must stay selective on a real filing")
        self.assertGreater(total, green, "not every date should be green")

    def test_amendment_has_no_green_because_the_date_is_not_in_it(self):
        # this filing really doesn't contain its deadline, it's in the
        # original SC TO-I. green correctly finds nothing, the amendment
        # warning is what tells me where to look.
        total, green, isos = self.marks(scto_amendment_html())
        self.assertEqual(green, 0)
        self.assertGreater(total, 0, "dates are still marked yellow")

    def test_mark_volume_is_far_below_v1(self):
        # v1 highlighted keywords as well as dates and hit a median of 81
        # marks per filing. only dates now.
        for raw in (scto_html(), scto_amendment_html()):
            total, _, _ = self.marks(raw)
            self.assertLess(total, 40)

    def test_cross_cell_fixture_does_not_fabricate_a_date(self):
        # the page footer digit and the next page's month-year heading are
        # twelve text nodes apart in this real excerpt
        total, green, isos = self.marks(cross_cell_html())
        html, _, _ = viewer.annotate(viewer.sanitize(cross_cell_html(),
                                                     "https://example/"))
        self.assertNotIn("2026-05-06", html,
                         "a date assembled from a page number and a heading")
        self.assertIn("2026-07-07", isos, "the real deadline is still green")
        self.assertEqual(total, 2)

    def test_n23c3a_real_filing_survives_node_splitting(self):
        # the real Cliffwater N-23C3A: <font> tags split most dates across
        # text nodes. before the fix only 2 of 21 dates (both non-green)
        # survived the node boundary rule. these thresholds come from that
        # measurement with some slack, don't raise them to make it pass.
        total, green, isos = self.marks(n23c3a_real_html())
        self.assertGreaterEqual(total, 15)
        self.assertGreaterEqual(green, 2)
        self.assertIn("2026-08-31", isos)


class _MarkScanner(HTMLParser):
    """Reads annotated markup back: every <mark>, its attributes, and where
    its text sits in the rendered text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._text: list[str] = []
        self._pos = 0
        self._open: list[list] = []
        self.marks: list[tuple] = []   # (data_i, iso, green, start, end)
        self.tags_inside_marks: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self._text)

    def handle_starttag(self, tag, attrs):
        if tag == "mark":
            a = dict(attrs)
            self._open.append([a.get("data-i"), a.get("data-iso", ""),
                               "green" in (a.get("class") or "").split(),
                               self._pos, None])
        elif self._open:
            self.tags_inside_marks.append(tag)

    def handle_startendtag(self, tag, attrs):
        if self._open:
            self.tags_inside_marks.append(tag)

    def handle_endtag(self, tag):
        if tag == "mark" and self._open:
            record = self._open.pop()
            record[4] = self._pos
            self.marks.append(tuple(record))

    def handle_data(self, data):
        self._text.append(data)
        self._pos += len(data)


class StructuralInvariantTest(unittest.TestCase):
    """Things every annotated filing has to satisfy whatever its markup.

    RealFilingTest checks counts and one iso per fixture, these are the
    structural guarantees under that. They're what catches a node-spanning
    bug - the original defect (dates dropped at node boundaries) and the
    cross-cell made-up date both break one of these three.
    """

    fixtures = ("filing_notice.html", "filing_scto.html",
                "filing_scto_amendment.html", "filing_n23c3a_real.html",
                "filing_cross_cell.html")

    def annotated(self, name):
        raw = (FIXTURES / name).read_text(encoding="utf-8", errors="replace")
        html, total, green = viewer.annotate(viewer.sanitize(raw, "https://example/"))
        scanner = _MarkScanner()
        scanner.feed(html)
        scanner.close()
        return scanner, total, green

    def grouped(self, scanner):
        groups = {}
        for data_i, iso, green, start, end in scanner.marks:
            groups.setdefault(data_i, []).append((iso, green, start, end))
        return groups

    def test_no_mark_ever_contains_a_tag(self):
        for name in self.fixtures:
            with self.subTest(name):
                scanner, _, _ = self.annotated(name)
                self.assertEqual(scanner.tags_inside_marks, [],
                                 "a <mark> that wraps a tag is broken markup - "
                                 "a date crossing a node boundary must be "
                                 "wrapped once per node, not once overall")

    def test_fragments_of_one_date_reassemble_to_a_date(self):
        for name in self.fixtures:
            with self.subTest(name):
                scanner, total, _ = self.annotated(name)
                groups = self.grouped(scanner)
                self.assertGreater(len(groups), 0, "the fixture must mark dates")
                for data_i, frags in groups.items():
                    span = scanner.text[min(f[2] for f in frags):
                                        max(f[3] for f in frags)]
                    self.assertRegex(
                        span, viewer.DATE_RE,
                        f"{name} data-i={data_i}: the marked span is not a date")
                    self.assertIsNotNone(
                        viewer.DATE_RE.fullmatch(span),
                        f"{name} data-i={data_i}: {span!r} spans more than the "
                        f"date - marks bracket text that is not part of it")

    def test_no_mark_is_entirely_whitespace(self):
        # a mark with no visible character paints a blob on blank space, and
        # a run of them is the signature of a match stitched across
        # unrelated cells
        for name in self.fixtures:
            with self.subTest(name):
                scanner, _, _ = self.annotated(name)
                for data_i, iso, green, start, end in scanner.marks:
                    self.assertTrue(
                        scanner.text[start:end].strip(),
                        f"{name} data-i={data_i}: a whitespace-only mark")

    def test_iso_and_green_are_identical_across_every_fragment(self):
        for name in self.fixtures:
            with self.subTest(name):
                scanner, _, _ = self.annotated(name)
                for data_i, frags in self.grouped(scanner).items():
                    self.assertEqual(len({f[0] for f in frags}), 1,
                                     f"{name} data-i={data_i}: fragments disagree "
                                     f"on data-iso")
                    self.assertEqual(len({f[1] for f in frags}), 1,
                                     f"{name} data-i={data_i}: fragments disagree "
                                     f"on the green class")

    def test_an_iso_that_is_present_is_the_marked_text_normalized(self):
        # a clickable value has to be a date I can actually see, never one
        # stitched together from text that isn't there
        for name in self.fixtures:
            with self.subTest(name):
                scanner, _, _ = self.annotated(name)
                for data_i, frags in self.grouped(scanner).items():
                    iso = frags[0][0]
                    if not iso:
                        continue
                    span = scanner.text[min(f[2] for f in frags):
                                        max(f[3] for f in frags)]
                    self.assertEqual(viewer.normalize_date(span), iso,
                                     f"{name} data-i={data_i}: data-iso={iso} is "
                                     f"not what {span!r} says")

    def test_counts_match_the_number_of_distinct_marked_dates(self):
        for name in self.fixtures:
            with self.subTest(name):
                scanner, total, green = self.annotated(name)
                groups = self.grouped(scanner)
                self.assertEqual(total, len(groups))
                self.assertEqual(
                    green, sum(1 for frags in groups.values() if frags[0][1]))


class BaseUrlForTest(unittest.TestCase):
    def test_strips_the_document_filename(self):
        self.assertEqual(
            viewer.base_url_for(
                "https://www.sec.gov/Archives/edgar/data/1234567/00012/notice.htm"
            ),
            "https://www.sec.gov/Archives/edgar/data/1234567/00012/",
        )

    def test_a_url_already_ending_in_a_slash_is_unchanged(self):
        url = "https://www.sec.gov/Archives/edgar/data/1234567/00012/"
        self.assertEqual(viewer.base_url_for(url), url)


class FetchFilingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_cache = viewer.CACHE_DIR
        viewer.CACHE_DIR = Path(self.tmp.name)

    def tearDown(self):
        viewer.CACHE_DIR = self._old_cache
        self.tmp.cleanup()

    def test_fetches_and_writes_the_cache(self):
        calls = []

        def fake(url, user_agent):
            calls.append(url)
            return b"<p>hello</p>"

        html = viewer.fetch_filing("https://x/notice.htm", "0001-26-1", "UA", fetch=fake)
        self.assertEqual(html, "<p>hello</p>")
        self.assertEqual(len(calls), 1)
        self.assertTrue(viewer.filing_cache_path("0001-26-1").exists())

    def test_second_call_reads_the_cache_without_fetching(self):
        def fake(url, user_agent):
            return b"<p>hello</p>"

        viewer.fetch_filing("https://x/notice.htm", "0001-26-1", "UA", fetch=fake)

        def explode(url, user_agent):
            raise AssertionError("should not refetch")

        html = viewer.fetch_filing("https://x/notice.htm", "0001-26-1", "UA",
                                   fetch=explode)
        self.assertEqual(html, "<p>hello</p>")

    def test_concurrent_fetches_of_one_filing_do_not_share_a_temp_file(self):
        """Two tabs on the same fund fetch it at once. Each writer needs its
        own temp file or one replaces a file the other is still writing."""
        barrier = threading.Barrier(2, timeout=5)
        sources = []
        real_replace = viewer.os.replace

        def recording_replace(src, dst):
            sources.append(str(src))
            return real_replace(src, dst)

        def fetch_together(url, user_agent):
            barrier.wait()
            return b"<p>hello</p>"

        failures = []

        def run():
            try:
                viewer.fetch_filing("https://x/notice.htm", "0001-26-1", "UA",
                                    fetch=fetch_together)
            except BaseException as exc:  # noqa: BLE001 - reported below
                failures.append(exc)

        viewer.os.replace = recording_replace
        try:
            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        finally:
            viewer.os.replace = real_replace

        self.assertEqual(failures, [])
        self.assertEqual(len(set(sources)), 2)
        self.assertEqual(list(viewer.CACHE_DIR.glob("*.tmp")), [])
        self.assertEqual(
            viewer.filing_cache_path("0001-26-1").read_text(encoding="utf-8"),
            "<p>hello</p>")

    def test_a_failed_write_leaves_no_cache_and_no_temp_file(self):
        def fake(url, user_agent):
            return b"<p>hello</p>"

        real_replace = viewer.os.replace

        def boom(src, dst):
            raise OSError("disk full")

        viewer.os.replace = boom
        try:
            with self.assertRaises(OSError):
                viewer.fetch_filing("https://x/notice.htm", "0001-26-1", "UA",
                                    fetch=fake)
        finally:
            viewer.os.replace = real_replace
        self.assertFalse(viewer.filing_cache_path("0001-26-1").exists())
        self.assertEqual(list(viewer.CACHE_DIR.glob("*.tmp")), [])

    def test_cache_filename_is_safe_for_odd_accessions(self):
        path = viewer.filing_cache_path("../../etc/passwd")
        self.assertEqual(path.parent, viewer.CACHE_DIR)
        self.assertNotIn("/", path.name)


class BuildFilingPageTest(unittest.TestCase):
    def setUp(self):
        self.page = viewer.build_filing_page(filing_html(), "https://example/base/")

    def test_is_a_complete_document(self):
        self.assertTrue(self.page.lstrip().lower().startswith("<!doctype html>"))
        self.assertIn("</html>", self.page)

    def test_filing_scripts_are_gone(self):
        self.assertNotIn("var x=1", self.page)

    def test_our_navigation_script_is_present(self):
        self.assertIn("mark.hit", self.page)
        self.assertIn("postMessage", self.page)

    def test_matches_are_marked(self):
        self.assertIn('<mark class="hit"', self.page)

    def test_base_tag_points_at_the_supplied_base(self):
        self.assertIn('<base href="https://example/base/"', self.page)

    def test_the_filing_body_and_our_script_are_inside_body(self):
        head_end = self.page.index("</head>")
        self.assertIn("</head><body>", self.page)
        self.assertLess(head_end, self.page.index("ACME INTERVAL FUND"))
        self.assertLess(head_end, self.page.index("<script>"))
        self.assertTrue(self.page.rstrip().endswith("</script></body></html>"))

    def test_table_content_survives(self):
        self.assertIn("Repurchase Pricing Date", self.page)

    def test_a_self_closing_style_leaves_the_filing_body_outside_it(self):
        """<style/> isn't self-closing in html. Emitted as-is, a browser would
        read the rest of the filing (and my nav script) as css."""
        page = viewer.build_filing_page(
            "<html><head><style /></head><body><p>BODYTEXT 2026-09-12"
            "</p></body></html>", "https://example/base/")
        self.assertNotIn("<style/>", page)
        body_at = page.index("BODYTEXT")
        # every style opened before the body text gets closed again
        self.assertNotIn("<style", page[page.rindex("</style>", 0, body_at):body_at])
        self.assertIn('<mark class="hit"', page)
        self.assertLess(body_at, page.index("<script>"))

    def test_the_filings_own_stylesheet_survives_intact(self):
        page = viewer.build_filing_page(
            "<html><head><style>table td > b { font-weight: 700 }</style></head>"
            "<body><p>deadline</p></body></html>",
            "https://example/base/")
        self.assertIn("table td > b { font-weight: 700 }", page)


class FrameScriptTest(unittest.TestCase):
    def page(self):
        return viewer.build_filing_page(filing_html(), "https://example/base/")

    def test_reports_which_marks_are_green(self):
        self.assertIn("type: 'count'", self.page())
        self.assertIn("green: green", self.page())

    def test_lands_on_the_first_green_mark(self):
        self.assertIn("if (green.length) show(green[0]);", self.page())

    def test_does_not_scroll_when_nothing_is_green(self):
        # the guard above is the whole mechanism: no green, no show() call
        page = self.page()
        self.assertNotIn("show(0);", page)

    def test_a_click_posts_the_normalized_date(self):
        self.assertIn("type: 'pick'", self.page())

    def test_a_mark_without_an_iso_is_not_clickable(self):
        self.assertIn("if (mark.dataset.iso)", self.page())

    def test_the_frame_still_identifies_its_parent_by_source(self):
        self.assertIn("event.source !== window.parent", self.page())



class FilingLinkTest(unittest.TestCase):
    """A click inside the sandboxed frame navigates the frame itself, so an
    off-site link in a filing would replace the filing pane with whatever.
    Only SEC links survive and they open in a normal tab."""

    BASE = "https://www.sec.gov/Archives/edgar/data/1/2/"

    def clean(self, markup):
        return viewer.sanitize(markup, self.BASE)

    def test_an_external_link_keeps_its_text_but_loses_its_href(self):
        out = self.clean('<a href="https://evil.example/x">Exhibit 1</a>')
        self.assertNotIn("evil.example", out)
        self.assertIn(">Exhibit 1</a>", out)

    def test_a_www_sec_gov_link_opens_in_a_new_tab(self):
        out = self.clean('<a href="https://www.sec.gov/Archives/edgar/data/1/2/ex1.htm">'
                         "Exhibit</a>")
        self.assertIn('href="https://www.sec.gov/Archives/edgar/data/1/2/ex1.htm"', out)
        self.assertIn('target="_blank"', out)
        self.assertIn('rel="noopener"', out)

    def test_a_relative_link_resolves_to_the_archive_and_opens_in_a_new_tab(self):
        out = self.clean('<a href="ex1.htm">Exhibit</a>')
        self.assertIn('href="ex1.htm"', out)
        self.assertIn('target="_blank"', out)

    def test_a_link_to_another_sec_host_is_kept(self):
        out = self.clean('<a href="https://data.sec.gov/x.json">Data</a>')
        self.assertIn('href="https://data.sec.gov/x.json"', out)

    def test_a_lookalike_host_is_not_trusted(self):
        out = self.clean('<a href="https://www.sec.gov.evil.example/x">Exhibit</a>'
                         '<a href="https://notsec.gov/x">Other</a>')
        self.assertIn("<a>Exhibit</a>", out)
        self.assertIn("<a>Other</a>", out)

    def test_a_filing_cannot_choose_its_own_target(self):
        out = self.clean('<a href="ex1.htm" target="_top" rel="opener">Exhibit</a>')
        self.assertNotIn("_top", out)
        self.assertNotIn('rel="opener"', out)
        self.assertIn('target="_blank"', out)

    def test_a_link_without_an_href_is_left_alone(self):
        out = self.clean('<a name="anchor">Here</a>')
        self.assertIn('<a name="anchor">Here</a>', out)
        self.assertNotIn("target=", out)

    def test_an_in_document_anchor_stays_in_the_frame(self):
        out = self.clean('<a href="#top">Back to top</a>')
        self.assertIn('href="#top"', out)
        self.assertNotIn("target=", out)

    def test_image_maps_follow_the_same_rule(self):
        out = self.clean('<map><area href="https://evil.example/x"/></map>')
        self.assertNotIn("evil.example", out)


if __name__ == "__main__":
    unittest.main()
