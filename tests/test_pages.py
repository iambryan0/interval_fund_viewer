import datetime
import pathlib
import unittest

import pages


class EscTest(unittest.TestCase):
    def test_escapes_markup(self):
        self.assertEqual(pages.esc("<b>&"), "&lt;b&gt;&amp;")

    def test_escapes_quotes(self):
        self.assertIn("&quot;", pages.esc('say "hi"'))

    def test_none_becomes_empty(self):
        self.assertEqual(pages.esc(None), "")

    def test_numbers_render(self):
        self.assertEqual(pages.esc(3), "3")


class LayoutTest(unittest.TestCase):
    def test_includes_the_title_and_body(self):
        html = pages.layout("My Title", "<p>hello</p>")
        self.assertIn("<title>My Title", html)
        self.assertIn("<p>hello</p>", html)

    def test_links_the_stylesheet(self):
        self.assertIn("/static/app.css", pages.layout("t", ""))

    def test_banner_is_shown_when_given(self):
        html = pages.layout("t", "", banner="Set a user agent")
        self.assertIn("Set a user agent", html)

    def test_no_banner_markup_when_absent(self):
        self.assertNotIn("class=\"banner\"", pages.layout("t", ""))


class SettingsPageTest(unittest.TestCase):
    def test_prefills_the_current_values(self):
        html = pages.settings_page("Me me@example.com", "8765")
        self.assertIn('value="Me me@example.com"', html)
        self.assertIn('value="8765"', html)

    def test_escapes_the_user_agent(self):
        html = pages.settings_page('Me "x" <b>', "8765")
        self.assertNotIn("<b>", html)

    def test_shows_a_successful_test_result(self):
        html = pages.settings_page("UA", "8765", "Reached it.", test_ok=True)
        self.assertIn("Reached it.", html)
        self.assertIn("ok", html)

    def test_shows_a_failed_test_result(self):
        html = pages.settings_page("UA", "8765", "DNS failed.", test_ok=False)
        self.assertIn("DNS failed.", html)
        self.assertIn("bad", html)


IDLE_JOB = {"running": False, "total": 0, "done": 0, "current": "",
            "started_at": None, "finished_at": None, "errors": []}


def fund(**overrides):
    base = {
        "cik": "1234567", "ticker": "ACME", "fund_name": "Acme Interval Fund",
        "active": 1, "status": "needs_review", "date_passed": False,
        "next_redemption_date": None, "latest_filing_date": "2026-08-14",
        "latest_form": "N-23C3A", "check_error": "", "review_status": None,
        "note": "",
    }
    base.update(overrides)
    return base


class FundListPageTest(unittest.TestCase):
    def test_worker_failure_is_visible_after_polling_reloads_the_page(self):
        job = dict(IDLE_JOB, job_error="Cannot open <database>")
        html = pages.fund_list_page([], job)
        self.assertIn("Check failed: Cannot open &lt;database&gt;", html)
        self.assertIn("You can run another check", html)
        self.assertNotIn("<database>", html)

    def test_empty_list_explains_what_to_do(self):
        html = pages.fund_list_page([], IDLE_JOB)
        self.assertIn("Add a fund", html)

    def test_shows_the_fund_name_and_ticker(self):
        html = pages.fund_list_page([fund()], IDLE_JOB)
        self.assertIn("Acme Interval Fund", html)
        self.assertIn("ACME", html)

    def test_needs_review_links_to_the_review_page(self):
        html = pages.fund_list_page([fund()], IDLE_JOB)
        self.assertIn('href="/fund/1234567"', html)

    def test_status_label_is_human_readable(self):
        html = pages.fund_list_page([fund()], IDLE_JOB)
        self.assertIn(pages.STATUS_LABELS["needs_review"], html)

    def test_up_to_date_fund_shows_its_redemption_date(self):
        html = pages.fund_list_page(
            [fund(status="up_to_date", next_redemption_date="2026-09-30")], IDLE_JOB)
        self.assertIn("2026-09-30", html)

    def test_passed_date_is_flagged(self):
        html = pages.fund_list_page(
            [fund(status="up_to_date", next_redemption_date="2026-01-01",
                  date_passed=True)], IDLE_JOB)
        self.assertIn("passed", html.lower())

    def test_error_status_shows_the_message(self):
        html = pages.fund_list_page(
            [fund(status="error", check_error="HTTP 403 from data.sec.gov")], IDLE_JOB)
        self.assertIn("HTTP 403", html)

    def test_pending_review_also_shows_the_failed_check_and_escapes_its_message(self):
        html = pages.fund_list_page(
            [fund(check_error="HTTP 403 <upstream>")], IDLE_JOB)
        self.assertIn("NEEDS REVIEW", html)
        self.assertIn("1 needing review", html)
        self.assertIn("N-23C3A filed 2026-08-14", html)
        self.assertIn("check failed", html)
        self.assertIn("HTTP 403 &lt;upstream&gt;", html)
        self.assertNotIn("<upstream>", html)

    def test_inactive_fund_is_marked(self):
        html = pages.fund_list_page([fund(active=0)], IDLE_JOB)
        self.assertIn("inactive", html.lower())

    def test_fund_name_is_escaped(self):
        html = pages.fund_list_page([fund(fund_name="<script>x</script>")], IDLE_JOB)
        self.assertNotIn("<script>x</script>", html)

    def test_running_job_shows_the_progress_area(self):
        job = dict(IDLE_JOB, running=True, total=10, done=3, current="Beta Fund")
        html = pages.fund_list_page([fund()], job)
        self.assertIn("progress", html.lower())
        self.assertIn("Beta Fund", html)

    def test_needs_review_count_is_summarised(self):
        funds = [fund(cik="1"), fund(cik="2"), fund(cik="3", status="up_to_date")]
        html = pages.fund_list_page(funds, IDLE_JOB)
        self.assertIn("2", html)

    def test_message_and_error_are_rendered_and_escaped(self):
        html = pages.fund_list_page([], IDLE_JOB, message="Added <b>", error="Bad <i>")
        self.assertIn("Added &lt;b&gt;", html)
        self.assertIn("Bad &lt;i&gt;", html)


def review_fund(**overrides):
    base = fund(
        latest_accession="0001234567-26-000123",
        latest_url="https://www.sec.gov/Archives/edgar/data/1234567/x/notice.htm",
        last_reviewed_accession=None,
    )
    base.update(overrides)
    return base


class ReviewPageTest(unittest.TestCase):
    def test_rejected_draft_is_escaped_and_kept_out_of_stored_history(self):
        draft = {"accession": "0001234567-26-000123",
                 "next_redemption_date": '\"><script>bad()</script>',
                 "note": '</textarea><script>bad()</script>'}
        html = pages.review_page(review_fund(), error="Invalid date", draft=draft)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;bad()&lt;/script&gt;</textarea>", html)
        self.assertIn("Submitted date: &quot;&gt;&lt;script&gt;", html)
        self.assertNotIn("<script>bad()</script>", html)
        self.assertNotIn("Review history (", html)

    def test_missing_accession_preserves_note_but_requires_date_confirmation(self):
        html = pages.review_page(review_fund(), error="Stale review",
                                 draft={"next_redemption_date": "2026-09-12",
                                        "note": "Keep my note"})
        self.assertIn("Keep my note</textarea>", html)
        self.assertNotIn('value="2026-09-12"', html)
        self.assertIn("select its date again", html)

    def test_source_and_history_are_visible_without_prefilling_form(self):
        source = {
            "outcome": "reviewed", "recorded_date": "2026-09-12",
            "accession": "original-accession", "form": "N-23C3A",
            "filing_date": "2026-08-14", "reviewed_at": "2026-08-15T12:00:00+00:00",
            "url": "https://www.sec.gov/original", "note": "Original <note>",
        }
        later = dict(source, outcome="no_date", recorded_date=None,
                     accession="later-accession", note="Later note")
        html = pages.review_page(review_fund(next_redemption_date="2026-09-12"),
                                 reviews=[later, source])
        self.assertIn("Recorded-date source", html)
        source_section = html.split("Recorded-date source</summary>")[1].split("</details>")[0]
        self.assertIn("original-accession", source_section)
        self.assertNotIn("later-accession", source_section)
        self.assertIn("Original &lt;note&gt;", html)
        self.assertNotIn("Original <note>", html)
        self.assertIn("Review history (2)", html)
        self.assertIn("Later note", html)
        self.assertIn('<textarea name="note" rows="3" cols="32"></textarea>', html)
        self.assertNotIn('value="2026-09-12"', html)

    def test_older_date_without_history_has_no_invented_source(self):
        html = pages.review_page(review_fund(next_redemption_date="2026-09-12"))
        self.assertIn("Source history is unavailable", html)

    def test_shows_the_fund_and_filing_header(self):
        html = pages.review_page(review_fund())
        self.assertIn("Acme Interval Fund", html)
        self.assertIn("N-23C3A", html)
        self.assertIn("2026-08-14", html)

    def test_embeds_the_filing_frame_for_this_fund(self):
        html = pages.review_page(review_fund())
        self.assertIn('src="/filing/1234567"', html)

    def test_frame_is_sandboxed_without_same_origin(self):
        html = pages.review_page(review_fund())
        self.assertIn("allow-scripts", html)
        self.assertNotIn("allow-same-origin", html)

    def test_frame_lets_sec_links_open_in_a_normal_tab(self):
        # the sanitizer only keeps sec.gov links, so anything popping out of
        # the sandbox is a sec.gov tab. without allow-popups target="_blank"
        # links do nothing at all.
        html = pages.review_page(review_fund())
        self.assertIn("allow-popups", html)
        self.assertIn("allow-popups-to-escape-sandbox", html)

    def test_links_to_the_filing_on_sec_gov(self):
        html = pages.review_page(review_fund())
        self.assertIn("https://www.sec.gov/Archives/edgar/data/1234567/x/notice.htm",
                      html)

    def test_omits_the_sec_gov_link_when_there_is_no_url(self):
        html = pages.review_page(review_fund(latest_url=""))
        self.assertNotIn('href=""', html)
        self.assertNotIn("View on sec.gov", html)

    def test_no_chips_anywhere(self):
        html = pages.review_page(review_fund(next_redemption_date="2026-09-12"))
        self.assertNotIn("chip", html)

    def test_note_box_is_never_prefilled(self):
        html = pages.review_page(review_fund(note="a note from a previous filing"))
        self.assertNotIn("a note from a previous filing", html)
        self.assertIn("<textarea", html)

    def test_date_field_is_never_prefilled(self):
        # the rule: the tool can be confident about where to look, never
        # about what the answer is. a stored date sitting in the field is one
        # click from being resubmitted as this filing's answer.
        html = pages.review_page(review_fund(next_redemption_date="2026-09-12"))
        self.assertIn('id="date-input"', html)
        self.assertNotIn('value="2026-09-12"', html)
        self.assertRegex(html, r'id="date-input"[^>]*value=""')

    def test_has_all_four_submit_buttons(self):
        html = pages.review_page(review_fund())
        self.assertIn('name="outcome" value="reviewed"', html)
        self.assertIn('name="outcome" value="no_date"', html)
        self.assertIn('name="outcome" value="not_applicable"', html)
        self.assertIn('name="outcome_next" value="reviewed"', html)

    def test_the_form_carries_the_accession_it_was_rendered_for(self):
        html = pages.review_page(review_fund())
        self.assertIn('<input type="hidden" name="accession" '
                      'value="0001234567-26-000123">', html)

    def test_the_accession_field_is_escaped(self):
        html = pages.review_page(
            review_fund(latest_accession='"><script>x</script>'))
        self.assertNotIn("<script>x</script>", html)

    def test_a_previously_recorded_date_is_shown_as_text_not_as_a_value(self):
        html = pages.review_page(review_fund(next_redemption_date="2026-09-12"))
        self.assertIn("Previously recorded", html)
        self.assertIn(">2026-09-12<", html)
        self.assertNotIn('value="2026-09-12"', html)

    def test_no_previously_recorded_line_without_a_stored_date(self):
        html = pages.review_page(review_fund(next_redemption_date=None))
        self.assertNotIn("Previously recorded", html)

    def test_has_the_match_navigation_controls(self):
        html = pages.review_page(review_fund())
        self.assertIn('id="match-prev"', html)
        self.assertIn('id="match-next"', html)
        self.assertIn('id="match-count"', html)

    def test_error_is_shown_and_escaped(self):
        html = pages.review_page(review_fund(), error="Bad <date>")
        self.assertIn("Bad &lt;date&gt;", html)


class FocusedTileTest(unittest.TestCase):
    """Spec section 8: the filing pane can't report focus across an opaque
    origin, so the parent sets the class when the frame posts a message."""

    def static(self, name):
        return (pathlib.Path(__file__).resolve().parent.parent
                / "static" / name).read_text(encoding="utf-8")

    def test_both_review_tiles_are_addressable(self):
        html = pages.review_page(review_fund())
        self.assertIn('id="form-tile"', html)
        self.assertIn('id="filing-tile"', html)

    def test_the_parent_sets_the_class_on_a_message_from_the_frame(self):
        js = self.static("app.js")
        self.assertIn("filing-tile", js)
        self.assertIn("form-tile", js)
        self.assertIn("'focused'", js)

    def test_the_focused_class_is_styled(self):
        self.assertIn(".tile.focused", self.static("app.css"))

    def test_a_finished_check_does_not_reload_over_typed_input(self):
        # the fund list reloads itself when a check finishes. if I'm halfway
        # through typing a cik into the add box the reload eats it. the script
        # has to check for typed input and if there is any, say the check
        # finished and leave the reload to me.
        js = self.static("app.js")
        self.assertIn("Check finished", js)
        self.assertIn("typedInput", js)


class AmendmentWarningTest(unittest.TestCase):
    def test_shown_for_an_amendment(self):
        html = pages.review_page(review_fund(latest_form="SC TO-I/A"))
        self.assertIn("amendment", html.lower())
        self.assertIn("original filing", html.lower())

    def test_absent_for_a_normal_filing(self):
        html = pages.review_page(review_fund(latest_form="N-23C3A"))
        self.assertNotIn("amendment", html.lower())

    def test_links_to_edgar_filtered_by_the_base_form(self):
        html = pages.review_page(review_fund(latest_form="SC TO-I/A", cik="1234567"))
        self.assertIn("CIK=1234567", html)
        self.assertIn("type=SC+TO-I", html)
        self.assertNotIn("type=SC+TO-I%2FA", html)

    def test_base_form_is_derived_not_hardcoded(self):
        html = pages.review_page(review_fund(latest_form="N-23C3A/A"))
        self.assertIn("type=N-23C3A", html)

    def test_missing_form_is_not_treated_as_an_amendment(self):
        html = pages.review_page(review_fund(latest_form=None))
        self.assertNotIn("amendment", html.lower())


class AddFundCopyTest(unittest.TestCase):
    def test_the_add_box_leads_with_cik(self):
        html = pages.fund_list_page([], IDLE_JOB)
        self.assertIn("CIK", html)

    def test_it_says_tickers_rarely_resolve(self):
        html = pages.fund_list_page([], IDLE_JOB)
        self.assertIn("ticker", html.lower())
        self.assertIn("rarely", html.lower())


class TilingMarkupTest(unittest.TestCase):
    def test_regions_are_tiles(self):
        html = pages.fund_list_page([fund()], IDLE_JOB)
        self.assertIn('class="tile"', html)

    def test_data_columns_are_monospace(self):
        html = pages.fund_list_page([fund(ticker="ACME")], IDLE_JOB)
        self.assertIn('class="mono"', html)

    def test_pending_rows_are_marked_for_the_accent_bar(self):
        html = pages.fund_list_page([fund(status="needs_review")], IDLE_JOB)
        self.assertIn('class="pending"', html)

    def css(self):
        return (pathlib.Path(__file__).resolve().parent.parent
                / "static" / "app.css").read_text(encoding="utf-8")

    def test_the_stylesheet_is_boxy_and_gap_driven(self):
        css = self.css()
        # not vacuous: the tokens have to exist, and any radius has to be zero
        self.assertIn("--gap", css)
        self.assertIn("--accent", css)
        for line in css.splitlines():
            if "border-radius" in line:
                self.assertRegex(line, r"border-radius:\s*0",
                                 f"radius must be 0: {line.strip()}")

    def test_the_empty_message_sits_inside_the_tile_body(self):
        # a direct child of .tile gets no padding - the message would sit
        # flush against the border while every other tile's content is padded
        html = pages.fund_list_page([], IDLE_JOB)
        self.assertRegex(html, r'<div class="body">\s*<p class="empty">')

    def test_the_page_margin_is_applied_once(self):
        # spec section 8: --gap is the page margin and the space between
        # tiles, nowhere else. body already pads by --gap, so padding <main>
        # too put the topbar tile at 8px from the edge and every content tile
        # at 16px and the left edges didn't line up.
        css = self.css()
        self.assertNotRegex(css, r'\bmain\s*\{[^}]*padding')

    def test_review_tiles_are_spaced_by_the_grid_alone(self):
        css = self.css()
        self.assertRegex(css, r'\.review\s+\.tile\s*\{[^}]*margin-bottom:\s*0')

    def test_the_filing_pane_has_a_single_border(self):
        # .tile already draws one, a second on .review .frame gave the filing
        # pane a 2px double edge no other tile has
        css = self.css()
        self.assertNotRegex(css, r'\.review\s+\.frame\s*\{[^}]*border:\s*1px')

    def test_no_dead_selectors(self):
        # review_page has no <h1> and no .row
        html = pages.review_page(review_fund())
        self.assertNotIn("<h1", html)
        self.assertNotIn('class="row"', html)
        css = self.css()
        self.assertNotIn(".review h1", css)
        self.assertNotRegex(css, r'(?m)^\.row\s*\{')

    def test_the_passed_badge_does_not_collide_with_the_warn_panel_rule(self):
        # .warn is the full bordered/padded block rule for the amendment
        # panel. a badge that also uses `warn` as its modifier picks up that
        # border and padding by accident, so the badge needs its own modifier.
        html = pages.fund_list_page(
            [fund(status="up_to_date", next_redemption_date="2026-01-01",
                  date_passed=True)], IDLE_JOB)
        self.assertIn('class="tag passed"', html)
        self.assertNotIn('class="tag warn"', html)


TODAY = datetime.date(2026, 9, 10)


class ShellTest(unittest.TestCase):
    """The sidebar shell: nav, active section, breadcrumb, date."""

    def test_sidebar_links_to_funds_and_settings(self):
        html = pages.layout("t", "")
        self.assertIn('class="sidebar"', html)
        self.assertIn('href="/"', html)
        self.assertIn('href="/settings"', html)

    def test_the_current_section_is_marked_active(self):
        html = pages.settings_page("UA", "8765")
        self.assertRegex(html, r'<a[^>]*href="/settings"[^>]*class="active"')
        self.assertNotRegex(html, r'<a[^>]*href="/"[^>]*class="active"')

    def test_breadcrumb_names_the_page(self):
        html = pages.layout("Acme Fund", "", section="funds")
        self.assertRegex(html, r'class="breadcrumb".*Acme Fund')

    def test_topbar_shows_the_date(self):
        html = pages.layout("t", "", today=TODAY)
        self.assertIn("10 Sep 2026", html)

    def test_pending_badge_only_when_known(self):
        self.assertNotIn("nav-badge", pages.layout("t", ""))
        self.assertIn('class="nav-badge">2<', pages.layout("t", "", pending=2))
        self.assertNotIn("nav-badge", pages.layout("t", "", pending=0))

    def test_main_is_a_skip_target(self):
        html = pages.layout("t", "")
        self.assertIn('href="#main"', html)
        self.assertIn('id="main"', html)


class OverviewTest(unittest.TestCase):
    """The hero, the stat tiles, and the deadline rail on the fund list."""

    def funds(self):
        return [
            fund(cik="1", fund_name="Acme Interval Fund"),
            fund(cik="2", fund_name="Beta Credit Fund", status="up_to_date",
                 next_redemption_date="2026-09-18"),
            fund(cik="3", fund_name="Gamma Fund", status="up_to_date",
                 next_redemption_date="2026-11-01"),
            fund(cik="4", fund_name="Delta Fund", status="error",
                 check_error="HTTP 403"),
            fund(cik="5", fund_name="Old Fund", status="up_to_date",
                 next_redemption_date="2026-09-01", date_passed=True),
        ]

    def test_hero_counts_the_queue_and_links_to_its_head(self):
        html = pages.fund_list_page(self.funds(), IDLE_JOB, today=TODAY)
        self.assertIn('class="tile hero"', html)
        self.assertIn("1 filing is waiting", html)
        self.assertRegex(html, r'<a[^>]*class="text-action"[^>]*href="/fund/1"')
        self.assertIn("Start reviewing", html)

    def test_hero_with_nothing_pending(self):
        html = pages.fund_list_page([self.funds()[1]], IDLE_JOB, today=TODAY)
        self.assertIn("Nothing is waiting", html)
        self.assertNotIn("Start reviewing", html)

    def test_hero_plural(self):
        html = pages.fund_list_page([fund(cik="1"), fund(cik="2")], IDLE_JOB,
                                    today=TODAY)
        self.assertIn("2 filings are waiting", html)

    def test_orbital_graphic_is_svg_not_radius(self):
        html = pages.fund_list_page(self.funds(), IDLE_JOB, today=TODAY)
        self.assertIn('class="orbital"', html)
        self.assertIn("<ellipse", html)
        self.assertIn('id="orbit-count">1<', html)

    def test_orbit_callout_names_the_next_deadline(self):
        html = pages.fund_list_page(self.funds(), IDLE_JOB, today=TODAY)
        self.assertRegex(html, r'class="orbit-callout".*?Next deadline.*?18 Sep')

    def test_stat_tiles(self):
        html = pages.fund_list_page(self.funds(), IDLE_JOB, today=TODAY)
        self.assertRegex(html, r'data-stat="tracked"[^>]*>5<')
        self.assertRegex(html, r'data-stat="pending"[^>]*>1<')
        self.assertRegex(html, r'data-stat="deadlines"[^>]*>1<')
        self.assertRegex(html, r'data-stat="issues"[^>]*>1<')

    def test_issues_counts_a_pending_fund_whose_check_failed(self):
        funds = [fund(cik="1", check_error="HTTP 500"),
                 fund(cik="2", status="error", check_error="HTTP 403")]
        html = pages.fund_list_page(funds, IDLE_JOB, today=TODAY)
        self.assertRegex(html, r'data-stat="issues"[^>]*>2<')

    def test_deadline_rail_lists_only_the_next_30_days(self):
        html = pages.fund_list_page(self.funds(), IDLE_JOB, today=TODAY)
        rail = html.split('class="deadlines"')[1]
        self.assertIn("Beta Credit Fund", rail)
        self.assertIn("in 8 days", rail)
        self.assertNotIn("Gamma Fund", rail)
        self.assertNotIn("Old Fund", rail)

    def test_deadline_rail_today_and_tomorrow(self):
        funds = [fund(cik="1", status="up_to_date", next_redemption_date="2026-09-10"),
                 fund(cik="2", status="up_to_date", next_redemption_date="2026-09-11")]
        html = pages.fund_list_page(funds, IDLE_JOB, today=TODAY)
        self.assertIn("today", html.split('class="deadlines"')[1])
        self.assertIn("tomorrow", html.split('class="deadlines"')[1])

    def test_deadline_rail_empty_state(self):
        html = pages.fund_list_page([fund()], IDLE_JOB, today=TODAY)
        self.assertIn("No recorded dates fall in the next 30 days", html)

    def test_unparseable_dates_are_ignored_not_fatal(self):
        html = pages.fund_list_page(
            [fund(status="up_to_date", next_redemption_date="soon")], IDLE_JOB,
            today=TODAY)
        self.assertIn("soon", html)

    def test_rows_carry_filter_state_and_search_text(self):
        html = pages.fund_list_page(self.funds(), IDLE_JOB, today=TODAY)
        self.assertRegex(html, r'<tr[^>]*data-state="pending"[^>]*data-search="acme interval fund 1 acme"')
        self.assertRegex(html, r'<tr[^>]*data-state="error"')
        html = pages.fund_list_page([fund(check_error="HTTP 500")], IDLE_JOB)
        self.assertRegex(html, r'<tr[^>]*data-state="pending error"')

    def test_filter_tabs_and_search_are_present(self):
        html = pages.fund_list_page(self.funds(), IDLE_JOB, today=TODAY)
        for state in ("all", "pending", "error"):
            self.assertIn(f'data-filter="{state}"', html)
        self.assertIn('id="search"', html)
        self.assertIn('id="row-count"', html)

    def test_every_row_has_an_open_link(self):
        html = pages.fund_list_page(self.funds(), IDLE_JOB, today=TODAY)
        self.assertEqual(html.count('class="row-open"'), 5)

    def test_fund_avatar_uses_initials(self):
        html = pages.fund_list_page([fund(fund_name="Acme Interval Fund")], IDLE_JOB)
        self.assertIn('class="fund-avatar">AI<', html)
        html = pages.fund_list_page([fund(fund_name="")], IDLE_JOB)
        self.assertIn('class="fund-avatar">12<', html)

    def test_script_filters_rows(self):
        js = (pathlib.Path(__file__).resolve().parent.parent
              / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("data-filter", js)
        self.assertIn("dataset.search", js)
        self.assertIn("row-count", js)


class ReviewLayoutTest(unittest.TestCase):
    def test_form_comes_first_then_the_document(self):
        html = pages.review_page(review_fund())
        self.assertLess(html.index('id="form-tile"'), html.index('id="filing-tile"'))

    def test_filing_toolbar_has_meta_and_legend(self):
        html = pages.review_page(review_fund())
        self.assertIn("Source filing", html)
        self.assertIn('class="legend"', html)
        self.assertIn("deadline language", html)

    def test_no_h1_on_the_review_page(self):
        self.assertNotIn("<h1", pages.review_page(review_fund()))


class SettingsLayoutTest(unittest.TestCase):
    def test_settings_are_rows(self):
        html = pages.settings_page("UA", "8765")
        self.assertEqual(html.count('class="settings-row"'), 3)
        self.assertIn("Test connection", html)


class ResearchNavTest(unittest.TestCase):
    def test_research_sits_between_funds_and_settings(self):
        html = pages.layout("t", "", section="research")
        funds = html.index('href="/"')
        research = html.index('href="/research"')
        settings = html.index('href="/settings"')
        self.assertLess(funds, research)
        self.assertLess(research, settings)
        self.assertIn('<a href="/research" class="active">Research</a>', html)

    def test_breadcrumb_names_the_section(self):
        html = pages.layout("Research", "", section="research")
        self.assertIn("<strong>Research</strong>", html)


FUND = {"cik": "1783964", "fund_name": "Accordant ODCE Index Fund",
        "ticker": "ODCAX; ODCEX", "active": 1}
OTHER = {"cik": "1748680", "fund_name": "1WS Credit Income Fund", "ticker": "OWSCX",
         "active": 1}


def _filing(form, date, acc, doc="a.htm", description=""):
    return {"form": form, "filing_date": date, "accession": acc,
            "primary_document": doc, "description": description}


GROUPS = [
    {"key": "prospectus", "label": "prospectus", "tracked": False, "filings": [
        _filing("N-2/A", "2026-05-15", "0001783964-26-000017", "n2a.htm", "PROSPECTUS"),
        _filing("497", "2026-05-15", "0001783964-26-000018", "supp.htm")]},
    {"key": "repurchase", "label": "repurchase notices", "tracked": True, "filings": [
        _filing("N-23C3A", f"2026-0{m}-01", f"0001783964-26-0000{m:02d}") for m in range(9, 0, -1)] + [
        _filing("N-23C3A", "2025-12-01", "0001783964-25-000099"),
        _filing("N-23C3A", "2025-11-01", "0001783964-25-000098"),
        _filing("N-23C3A", "2025-10-01", "0001783964-25-000097")]},
]


class ResearchPageTest(unittest.TestCase):
    def test_lists_every_fund_as_a_collapsed_root(self):
        html = pages.research_page([OTHER, FUND])
        self.assertEqual(html.count('data-tree-root'), 2)
        self.assertIn('data-cik="1783964"', html)
        self.assertIn('href="/research/1783964"', html)
        self.assertIn('data-tree-expand="/research/1783964?fragment=1"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('data-search="accordant odce index fund 1783964 odcax; odcex"', html)

    def test_has_a_search_box_and_an_empty_viewer(self):
        html = pages.research_page([FUND])
        self.assertIn('id="research-search"', html)
        self.assertIn('id="research-frame"', html)
        self.assertIn('sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"', html)
        self.assertIn('id="research-title"', html)

    def test_expanded_fund_renders_open_with_the_subtree_inside(self):
        html = pages.research_page([OTHER, FUND], expanded=FUND, subtree="<p>SUBTREE</p>")
        self.assertIn("<p>SUBTREE</p>", html)
        root = html[html.index('data-cik="1783964"'):]
        self.assertIn('aria-expanded="true"', root[:root.index("</pre>")])
        other = html[html.index('data-cik="1748680"'):html.index('data-cik="1783964"')]
        self.assertIn('aria-expanded="false"', other)

    def test_no_funds_says_so(self):
        self.assertIn("No funds on the roster yet", pages.research_page([]))


class ResearchSubtreeTest(unittest.TestCase):
    def test_groups_render_in_order_with_counts_and_labels(self):
        html = pages.research_subtree(FUND, GROUPS)
        self.assertLess(html.index("prospectus"), html.index("repurchase notices"))
        self.assertIn('<span class="tree-count">2</span>', html)
        self.assertIn('<span class="tree-count">12</span>', html)

    def test_small_groups_are_open_and_big_ones_closed(self):
        html = pages.research_subtree(FUND, GROUPS)
        prospectus = html[html.index('data-group="prospectus"'):html.index('data-group="repurchase"')]
        repurchase = html[html.index('data-group="repurchase"'):]
        self.assertIn("<details", prospectus)
        self.assertIn(" open>", prospectus)
        self.assertNotIn(" open>", repurchase.split("</summary>")[0])

    def test_tracked_group_is_marked(self):
        html = pages.research_subtree(FUND, GROUPS)
        repurchase = html[html.index('data-group="repurchase"'):]
        self.assertIn('class="tree-tracked"', repurchase.split("</summary>")[0])

    def test_filing_rows_link_to_the_filing_route_and_fragment(self):
        html = pages.research_subtree(FUND, GROUPS)
        self.assertIn('href="/research/1783964/0001783964-26-000017"', html)
        self.assertIn('data-tree-expand="/research/1783964/0001783964-26-000017?fragment=1"', html)
        self.assertIn('<span class="tree-form">N-2/A</span>', html)
        self.assertIn('<span class="tree-date">2026-05-15</span>', html)
        self.assertIn("prospectus", html)

    def test_more_than_ten_filings_fold_behind_a_more_row(self):
        html = pages.research_subtree(FUND, GROUPS)
        repurchase = html[html.index('data-group="repurchase"'):]
        self.assertIn("… 2 more", repurchase)
        self.assertEqual(repurchase.count('data-tree-rest'), 2)
        self.assertIn('data-tree-more', repurchase)

    def test_connectors_use_the_last_branch_on_the_last_row(self):
        html = pages.research_subtree(FUND, GROUPS)
        prospectus = html[html.index('data-group="prospectus"'):html.index('data-group="repurchase"')]
        self.assertIn("├─", prospectus)
        self.assertIn("└─", prospectus)

    def test_note_and_error_render_inline(self):
        html = pages.research_subtree(FUND, GROUPS, note="filing list is 2 days old")
        self.assertIn("filing list is 2 days old", html)
        html = pages.research_subtree(FUND, [], error="Could not reach data.sec.gov")
        self.assertIn("Could not reach data.sec.gov", html)
        self.assertIn('href="/research/1783964"', html)  # retry link

    def test_open_filing_gets_its_documents_inside(self):
        html = pages.research_subtree(FUND, GROUPS, open_accession="0001783964-26-000017",
                                      documents_html="<p>DOCS</p>")
        row = html[html.index("0001783964-26-000017"):]
        self.assertIn("<p>DOCS</p>", row)
        self.assertIn('aria-expanded="true"', row[:row.index("<p>DOCS</p>")])

    def test_escapes_edgar_text(self):
        groups = [{"key": "other", "label": "other", "tracked": False, "filings": [
            _filing("<b>", "2026-01-01", "0001783964-26-000001", "x.htm", "<script>")]}]
        html = pages.research_subtree(FUND, groups)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<b>", html)

    def test_group_holding_the_open_filing_renders_open(self):
        html = pages.research_subtree(FUND, GROUPS, open_accession="0001783964-26-000005",
                                      documents_html="<p>DOCS</p>")
        repurchase = html[html.index('data-group="repurchase"'):]
        self.assertIn(" open>", repurchase.split("</summary>")[0])
        self.assertIn("<p>DOCS</p>", repurchase)

    def test_open_filing_in_the_hidden_tail_is_not_hidden(self):
        html = pages.research_subtree(FUND, GROUPS, open_accession="0001783964-25-000097",
                                      documents_html="<p>DOCS</p>")
        repurchase = html[html.index('data-group="repurchase"'):]
        self.assertNotIn("data-tree-rest", repurchase)
        self.assertNotIn("more", repurchase)
        self.assertIn("<p>DOCS</p>", repurchase)

    def test_a_filing_with_a_non_html_primary_opens_on_sec_gov(self):
        groups = [{"key": "holdings", "label": "holdings", "tracked": False, "filings": [
            _filing("N-PORT", "2026-07-29", "0001783964-26-000029", "nport.xml")]}]
        html = pages.research_subtree(FUND, groups)
        self.assertIn('data-external href="https://www.sec.gov/Archives/edgar/data/1783964/000178396426000029/nport.xml"', html)
        self.assertIn("sec.gov ↗", html)
        self.assertNotIn("data-tree-expand", html)


class ResearchDocumentsTest(unittest.TestCase):
    FILING = _filing("N-2/A", "2026-05-15", "0001783964-26-000017", "n2a.htm", "PROSPECTUS")

    def test_one_html_document_carries_data_open(self):
        docs = [{"name": "n2a.htm", "url": "https://www.sec.gov/x/n2a.htm", "is_html": True,
                 "kind": "HTML", "description": "PROSPECTUS", "primary": True}]
        html = pages.research_documents(FUND, self.FILING, docs)
        self.assertIn('data-open="/research/1783964/0001783964-26-000017/n2a.htm"', html)
        self.assertIn('data-doc', html)
        self.assertIn('data-title="Accordant ODCE Index Fund · N-2/A · 2026-05-15 · n2a.htm"', html)

    def test_several_documents_list_each_and_carry_no_data_open(self):
        docs = [{"name": "n2a.htm", "url": "u1", "is_html": True, "kind": "HTML",
                 "description": "PROSPECTUS", "primary": True},
                {"name": "ex99a.htm", "url": "u2", "is_html": True, "kind": "HTML",
                 "description": "", "primary": False},
                {"name": "fees.pdf", "url": "https://www.sec.gov/x/fees.pdf", "is_html": False,
                 "kind": "PDF", "description": "", "primary": False}]
        html = pages.research_documents(FUND, self.FILING, docs)
        self.assertNotIn("data-open", html)
        self.assertEqual(html.count("data-doc"), 2)
        self.assertIn('data-external href="https://www.sec.gov/x/fees.pdf" target="_blank" rel="noopener"', html)
        self.assertIn("PDF", html)
        self.assertIn("sec.gov ↗", html)

    def test_a_single_non_html_document_carries_no_data_open(self):
        docs = [{"name": "nport.xml", "url": "u", "is_html": False, "kind": "XML",
                 "description": "", "primary": True}]
        html = pages.research_documents(FUND, self.FILING, docs)
        self.assertNotIn("data-open", html)
        self.assertIn("data-external", html)

    def test_error_renders_with_the_sec_link(self):
        html = pages.research_documents(FUND, self.FILING, [], error="HTTP 503")
        self.assertIn("HTTP 503", html)
        self.assertIn("https://www.sec.gov/Archives/edgar/data/1783964/000178396426000017/", html)


class ResearchScriptHooksTest(unittest.TestCase):
    def test_page_carries_the_hooks_the_script_looks_for(self):
        html = pages.research_page([FUND])
        for hook in ('id="tree"', 'id="research-search"', 'id="research-frame"',
                     'id="research-title"', 'id="research-external"', 'data-tree-expand',
                     '/static/app.js'):
            self.assertIn(hook, html)


REVIEW_FUND = {
    "cik": "1783964", "fund_name": "Accordant ODCE Index Fund", "ticker": "ODCAX",
    "active": 1, "latest_form": "N-23C3A", "latest_filing_date": "2026-08-04",
    "latest_url": "https://www.sec.gov/Archives/edgar/data/1783964/000178396426000031/notice_q3.htm",
    "latest_accession": "0001783964-26-000031", "next_redemption_date": None,
    "review_status": None, "note": "", "check_error": "", "checked_at": "2026-08-05T00:00:00",
    "reviewed_at": None, "status": "needs_review", "date_passed": False,
    "ticker_revision": 0, "tickers": [],
}


class ReviewPageLayoutTest(unittest.TestCase):
    def test_form_tile_comes_before_the_filing_tile(self):
        html = pages.review_page(REVIEW_FUND)
        self.assertLess(html.index('id="form-tile"'), html.index('id="filing-tile"'))

    def test_toolbar_links_to_research_for_this_fund(self):
        html = pages.review_page(REVIEW_FUND)
        toolbar = html[html.index('class="filing-toolbar"'):html.index("</header>", html.index('class="filing-toolbar"'))]
        self.assertIn('<a href="/research/1783964">Filings ›</a>', toolbar)

    def test_stylesheet_puts_the_form_column_first(self):
        css = pathlib.Path(__file__).resolve().parents[1].joinpath("static", "app.css").read_text()
        self.assertIn(".review { display: grid; grid-template-columns: 22rem minmax(0, 1fr);", css)


if __name__ == "__main__":
    unittest.main()
