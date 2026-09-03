"""
The mapping-review screen — the one place a user sees what was read from
their file before any of it reaches the optimiser.

Two kinds of check live here.

  * Contract: the parse response has to carry everything the screen renders
    from — a per-sheet role on every row, a canonical field list, the four
    counts. These run against the real Flask app and a real workbook.

  * Assets: the screen must not size itself in the landing page's reference
    pixel, must not advertise a control it does not implement, and must not
    describe the file's contents from anywhere but the parse. These read the
    shipped JS and CSS, because that is where those defects live and a
    browser test would not name them.

Background: the screen was laid out in `--u`, clamp(0.5px, min(0.0598vw,
0.1063vh), 1.32px). At 1366x768 that unit is 0.816, so its 13px table body
rendered at 10.6px — the type shrank with the window and never agreed with
the application it leads into. It also showed 147 columns as one flat list
with no way to reach the ones needing a decision, and a sidebar that listed
the four techniques the mapper uses rather than what it had concluded about
this file.
"""

from __future__ import annotations

import io
import pathlib
import re
import uuid

import pytest

from app.backend.app import app


PASSWORD = "Netgravity@2026"
FRONTEND = pathlib.Path(app.root_path).parent / "frontend"


def _asset(*parts: str) -> str:
    path = FRONTEND
    for part in parts:
        path = path / part
    return path.read_text(encoding="utf-8", errors="replace")


def _without_comments(text: str) -> str:
    """
    Drop comments before scanning.

    Several of these rules are explained in a comment that quotes the thing
    being banned. Scanning raw text would fail on the explanation, which
    teaches the next reader to delete the explanation.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def auth(client):
    email = f"map-{uuid.uuid4().hex[:10]}@example.com"
    res = client.post("/api/auth/signup", json={
        "name": "Mapping Review", "email": email, "password": PASSWORD})
    assert res.status_code == 201, res.get_data(as_text=True)[:300]
    token = res.get_json()["token"]
    res = client.post("/api/projects", json={"name": "Mapping review"},
                      headers={"Authorization": f"Bearer {token}"})
    assert res.status_code in (200, 201), res.get_data(as_text=True)[:300]
    return token, res.get_json().get("id") or res.get_json().get("project", {}).get("id")


def _workbook() -> bytes:
    """
    A two-sheet workbook: one the extractor recognises, one it does not.

    Both are needed. The screen's sheet tabs report `used/total` per sheet,
    and a sheet contributing nothing is a finding the user has to be able to
    see — so a fixture with only recognisable sheets would let a build that
    silently drops unrecognised ones pass.
    """
    pd = pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame({
            "Facility_ID": ["F1", "F2"],
            "Facility_Name": ["Mumbai DC", "Delhi DC"],
            "Capacity_Units": [1000, 2000],
            "Fixed_Cost": [50.0, 60.0],
            "Latitude": [19.07, 28.61],
            "Longitude": [72.87, 77.20],
        }).to_excel(writer, sheet_name="Facilities", index=False)
        pd.DataFrame({
            "Ledger_Ref": ["X1", "X2"],
            "Internal_Note": ["alpha", "beta"],
            "Batch_Tag": ["b-1", "b-2"],
        }).to_excel(writer, sheet_name="Back_Office", index=False)
    return buf.getvalue()


def _parse(client, auth):
    token, project_id = auth
    res = client.post(
        "/api/ingestions/preview/upload-and-parse",
        data={
            "files": (io.BytesIO(_workbook()), "network.xlsx"),
            "project_id": project_id,
        },
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)[:400]
    return res.get_json()


class TestTheParseCarriesWhatTheScreenRenders:
    """
    Every figure and every control on the review screen is derived from this
    one response. A field missing here is not a smaller screen; it is a
    screen that invents the missing part or renders it blank.
    """

    def test_every_row_says_which_sheet_it_came_from_and_what_that_sheet_is(
            self, client, auth):
        rows = _parse(client, auth)["mapping"]["network.xlsx"]
        assert rows, "no columns were returned for a workbook with two sheets"
        for row in rows:
            assert row["sheet"], f"a column with no sheet: {row}"
            assert "sheetRole" in row, (
                "the sheet tabs group by sheet and label each with what the "
                f"parser decided it is; this row cannot be placed: {row}"
            )

    def test_a_column_means_what_its_sheet_says_it_means(self, client, auth):
        rows = _parse(client, auth)["mapping"]["network.xlsx"]
        by_sheet = {}
        for row in rows:
            by_sheet.setdefault(row["sheet"], set()).add(row["sheetRole"])
        assert by_sheet["Facilities"] == {"facilities"}
        assert by_sheet["Back_Office"] != {"facilities"}

    def test_a_sheet_nothing_is_read_from_is_reported_not_dropped(
            self, client, auth):
        """
        The tab for such a sheet reads 0/3 — which is the finding. Dropping
        the sheet instead would leave the user believing their file was
        fully understood.
        """
        rows = _parse(client, auth)["mapping"]["network.xlsx"]
        back = [r for r in rows if r["sheet"] == "Back_Office"]
        assert len(back) == 3, f"the unrecognised sheet's columns went missing: {back}"
        assert all(r["status"] == "ignored" for r in back), back

    def test_the_counts_are_the_rows(self, client, auth):
        """
        The stats strip and the summary's "N of M columns" both come from
        mapStats; the table comes from `mapping`. If the two disagree the
        screen contradicts itself in two places at once.
        """
        data = _parse(client, auth)
        rows = data["mapping"]["network.xlsx"]
        stats = data["mapStats"]
        assert stats["detected"] == len(rows)
        assert stats["auto"] == sum(1 for r in rows if r["status"] == "auto")
        assert stats["review"] == sum(1 for r in rows if r["status"] == "review")
        assert stats["ignored"] == sum(1 for r in rows if r["status"] == "ignored")

    def test_every_suggested_field_is_offered_by_the_dropdown(self, client, auth):
        """
        A `<select>` whose value is absent from its options silently falls
        back to the first option. That is how every row of a real workbook
        once rendered as "Customer ID".
        """
        data = _parse(client, auth)
        offered = set(data["schemaFields"])
        for row in data["mapping"]["network.xlsx"]:
            assert row["mapped"] in offered, (
                f"{row['source']} is suggested as {row['mapped']!r}, which the "
                "dropdown does not offer"
            )

    def test_each_row_carries_samples_from_the_file(self, client, auth):
        rows = _parse(client, auth)["mapping"]["network.xlsx"]
        facility = next(r for r in rows if r["source"] == "Facility_Name")
        assert "Mumbai DC" in facility["sample"], facility


class TestOneUnreadableFileDoesNotTakeTheBatchWithIt:
    """
    The uploader accepts `.pdf` — the file input says so, the help says so,
    and there is a review screen for it — but this endpoint parses TABLES and
    its allow-list has never contained `.pdf`. The check that enforced that
    sat outside the per-file try, so one PDF raised straight past every other
    file in the request: the workbook uploaded beside it was never read, and
    the mapping screen opened with no columns and a disabled Continue.
    """

    def test_a_rejected_file_is_named_rather_than_fatal(self, client, auth):
        token, project_id = auth
        res = client.post(
            "/api/ingestions/preview/upload-and-parse",
            data={
                "files": [
                    (io.BytesIO(_workbook()), "network.xlsx"),
                    (io.BytesIO(b"%PDF-1.4 not a table"), "rate_card.pdf"),
                ],
                "project_id": project_id,
            },
            content_type="multipart/form-data",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 200, (
            "one unsupported file aborted the whole upload: "
            + res.get_data(as_text=True)[:300]
        )
        data = res.get_json()
        assert data["mapping"]["network.xlsx"], "the workbook was not read"
        rejected = [e["file"] for e in data["parse_errors"]]
        assert "rate_card.pdf" in rejected, (
            "the file that could not be read has to be named, not dropped"
        )

    def test_a_request_of_nothing_readable_still_fails(self, client, auth):
        """
        Reporting per file must not become swallowing. With no table anywhere
        in the request there is nothing to review, and the call says so.
        """
        token, project_id = auth
        res = client.post(
            "/api/ingestions/preview/upload-and-parse",
            data={
                "files": (io.BytesIO(b"%PDF-1.4 not a table"), "rate_card.pdf"),
                "project_id": project_id,
            },
            content_type="multipart/form-data",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert res.status_code == 422, res.get_data(as_text=True)[:300]
        ctx = res.get_json()["error"]["context"]["parse_errors"]
        assert any(e["file"] == "rate_card.pdf" for e in ctx), ctx

    def test_the_client_does_not_post_a_pdf_to_a_table_parser(self):
        """
        The other half of the fix. A PDF has no tables and this build has no
        contract parser, so posting one here only ever produced a refusal.
        """
        js = _asset("js", "ingestion.js")
        fn = js[js.index("async function addFiles"):js.index("function bindUploadData")]
        assert "const parseable = validRawFiles.filter" in fn
        assert "f.kind !== 'pdf'" in fn
        assert "parseable.forEach(item => {" in fn and "formData.append('files'" in fn, (
            "the request is still built from the unfiltered list"
        )
        assert "if (parseable.length) flow.parsing += 1;" in fn, (
            "a PDF-only upload would hold Continue open waiting for a parse "
            "that is never requested"
        )


class TestTheReviewScreenIsOnTheApplicationsScale:
    """
    Type on this screen used to be sized in `--u`, the landing page's
    reference pixel, which is a fraction of a CSS pixel below a ~1670px
    viewport. The rules that lay the screen out now live in a block scoped
    to `#ingestion-page` and are in fixed px.
    """

    SCOPED = "#ingestion-page"

    @staticmethod
    def _scoped_rules(css: str):
        """Every rule whose selector is scoped to the review page."""
        out = []
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            selector = selector.strip()
            if selector.startswith("#ingestion-page") or "\n#ingestion-page" in selector:
                out.append((selector, body))
        return out

    def test_the_review_screen_does_not_size_itself_in_the_landing_unit(self):
        css = _without_comments(_asset("css", "ingestion.css"))
        offenders = [
            (sel, body) for sel, body in self._scoped_rules(css)
            if "var(--u)" in body
        ]
        assert not offenders, (
            "these review-screen rules still scale with the viewport rather "
            f"than with the application: {offenders[:4]}"
        )

    @pytest.mark.parametrize("prop,expected", [
        (".ing-map-table td", "13px"),   # style.css .ng-table td
        (".ing-map-table th", "11px"),   # style.css .ng-table th
        (".ing-card-title", "15px"),     # style.css .card-title
        (".ing-title", "22px"),          # home-overview card headline
    ])
    def test_it_uses_the_applications_own_sizes(self, prop, expected):
        css = _without_comments(_asset("css", "ingestion.css"))
        rules = [body for sel, body in self._scoped_rules(css)
                 if prop in sel and "font-size" in body]
        assert rules, f"no scoped font-size for {prop}"
        assert any(f"font-size: {expected}" in body for body in rules), (
            f"{prop} is not at the application's {expected}: {rules}"
        )


class TestNothingOnTheScreenClaimsMoreThanTheBuildDoes:
    """
    The rules from the standing brief that this screen can break: a control
    that looks live and is not, and a number about the user's file that came
    from somewhere other than their file.
    """

    def test_every_rail_step_is_reachable_or_says_why_not(self):
        """
        The rail shows three steps. One is where you are, one is behind you,
        and one is ahead — and the one ahead is disabled and carries the
        reason as its title, rather than being a button that eats clicks.
        """
        js = _asset("js", "ingestion.js")
        rail = js[js.index("function flowRailHtml"):js.index("function bindFlowRail")]
        assert "class=\"ing-rail-item state-${state}\"" in rail
        assert "'blocked'" in rail, "no step is ever marked out of reach"
        assert "disabled aria-disabled" in rail, (
            "a step that cannot be taken must be disabled, not merely dimmed"
        )
        assert "Confirm your mapping to continue" in rail, (
            "a disabled step has to say what would unblock it"
        )
        css = _without_comments(_asset("css", "ingestion.css"))
        blocked = css[css.index(".ing-rail-item.state-blocked"):]
        assert "cursor: not-allowed" in blocked[:200], blocked[:200]

    def test_the_rail_offers_no_destination_that_does_not_exist_yet(self):
        """
        During a first ingestion there is no solved network, so Home, KPIs,
        Digital Twin and Scenarios are not places to go. The rail carries
        the setup steps only; the workspace entry appears solely when the
        flow was entered from a workspace that already exists.
        """
        js = _asset("js", "ingestion.js")
        rail = js[js.index("function flowRailHtml"):js.index("function bindFlowRail")]
        for absent in ("Overview", "Scenarios", "Digital Twin", "KPIs"):
            assert absent not in rail, (
                f"the rail offers {absent!r}, which has no network behind it "
                "until this flow finishes"
            )
        assert "flow.cameFromApp" in rail

    def test_the_summary_is_computed_from_the_parsed_rows(self):
        """
        Not authored copy about a hypothetical workbook. Every list in the
        summary is a filter over the same array the table renders.
        """
        js = _asset("js", "ingestion.js")
        fn = js[js.index("function mappingSummaryHtml"):js.index("function statsRowHtml")]
        assert "rows.filter(r => r.status === 'review')" in fn
        assert "rows.filter(r => r.status === 'ignored')" in fn
        assert "stats.auto" in fn
        for invented in ("12,480", "48 columns", "customer_id", "origin_dc"):
            assert invented not in fn, (
                f"{invented!r} is prototype content, not this file's"
            )

    def test_the_table_opens_showing_everything(self):
        """
        A view that opens pre-filtered shows a subset while looking like the
        whole. The flagged rows are advertised by a callout and a tab; they
        are not achieved by hiding the other 144.
        """
        js = _asset("js", "ingestion.js")
        view = js[js.index("const mapView = {"):js.index("function resetMapView")]
        assert "tab: 'all'" in view
        reset = js[js.index("function resetMapView"):js.index("function activeFilterCount")]
        assert "mapView.tab = 'all'" in reset

    def test_a_filtered_table_says_it_is_filtered(self):
        """
        Nielsen #1. The Filters control counts what is active — including the
        search box — so a table showing 3 of 147 rows always has a visible
        reason on screen.
        """
        js = _asset("js", "ingestion.js")
        fn = js[js.index("function activeFilterCount"):js.index("function sheetTabs")]
        assert "mapView.search" in fn and "confidence.size" in fn and "status.size" in fn

    def test_an_empty_result_offers_a_way_out(self):
        """Nielsen #9: a dead end with no exit is the failure, not the
        empty result itself."""
        js = _asset("js", "ingestion.js")
        assert "ing-map-clear-view" in js
        assert "Show all columns" in js

    def test_the_primary_action_does_not_depend_on_scrolling(self):
        """
        With 147 columns the page used to be about 10,000px tall, so
        confirming a mapping meant scrolling past every row you had just
        decided not to change. The screen is now one screenful: the table
        scrolls inside its own box and the footer never moves.

        Every element between the page and the table needs `min-height: 0` —
        a flex item's default `min-height: auto` refuses to shrink below its
        content, so one missing declaration puts the footer back off-screen.
        """
        css = _without_comments(_asset("css", "ingestion.css"))
        # Every body for a selector, not the last one: the same selector is
        # redeclared inside media queries, and a dict would keep only the
        # narrow-window override.
        scoped = {}
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            scoped.setdefault(selector.strip(), []).append(body)

        def declares(selector, text):
            return any(text in body for body in scoped.get(selector, []))

        assert declares("#ingestion-page .proj-scroll", "overflow: hidden"), (
            "the page itself still scrolls"
        )
        assert declares("#ingestion-page #ing-map-table-slot", "overflow: auto"), (
            "the mapping table has no scroller of its own"
        )
        for selector in ("#ingestion-page .ing-body",
                         "#ingestion-page .ing-mapping-layout",
                         "#ingestion-page .ing-mapping-main",
                         "#ingestion-page #ing-map-table-slot"):
            assert declares(selector, "min-height: 0"), (
                f"{selector} will not shrink, so the table pushes the footer "
                f"off the bottom: {scoped.get(selector)}"
            )

    def test_the_column_headers_survive_the_scroll(self):
        """
        The table has its own viewport now. Scrolling 147 rows past a header
        that has left the screen removes the only labels saying which column
        is the source and which the mapped field.
        """
        css = _without_comments(_asset("css", "ingestion.css"))
        head = css[css.index("#ingestion-page .ing-map-table thead th"):]
        head = head[:head.index("}")]
        assert "position: sticky" in head and "top: 0" in head, head

    def test_back_is_at_the_top_and_names_its_destination(self):
        """
        It used to be a bare "Back" that meant the uploader on one file and
        the previous file on the next, and on the mock it sat beside the
        primary action at the foot of a 2,000px page.
        """
        js = _asset("js", "ingestion.js")
        assert "function backTargetLabel" in js
        assert "Back to upload" in js
        excel = js[js.index("function renderExcelIngestion"):js.index("function refreshMapStats")]
        crumbs = excel.index("ing-crumbs")
        footer = excel.index("ing-footer-row")
        assert crumbs < footer, "Back is rendered below the mapping table"
        assert "backPillHtml()" in excel[:crumbs + 400]

    def test_the_screen_names_the_project_it_will_commit_to(self):
        """
        Ingestion is several screens and several minutes long, and nothing on
        it used to say which project the upload was about to be written to.
        """
        js = _asset("js", "ingestion.js")
        assert "project: flow.project" in js
        chrome = _asset("js", "workspace-chrome.js")
        assert 'data-wc="project"' in chrome
        assert "showSelectProject" in chrome

    def test_help_on_this_screen_describes_this_screen(self):
        """
        Both review screens used to open the uploader's help, which explains
        file types and size limits and says nothing about confidence,
        status, or what confirming does.
        """
        chrome = _asset("js", "workspace-chrome.js")
        assert "mapping: `" in chrome
        mapping_help = chrome[chrome.index("mapping: `"):]
        mapping_help = mapping_help[:mapping_help.index("`,")]
        for concept in ("Confidence", "Status", "Not used", "Confirm mapping"):
            assert concept in mapping_help, f"the help never mentions {concept}"

    def test_the_measured_data_quality_findings_are_still_reachable(self):
        """
        The data-quality CARD was removed from the review screen on request:
        it sat between the mapping table and the footer and pushed
        "Confirm mapping & continue" below the fold on a 768px window.

        The measurements were not removed with it. How many records the parser
        could not use, and which columns are mostly empty, is the evidence a
        user needs before committing an upload, and this is the only screen
        that has it. It is one chip on the file card, opening the same list.
        """
        js = _asset("js", "ingestion.js")
        excel = js[js.index("function renderExcelIngestion"):js.index("function refreshMapStats")]
        assert "dataQualitySectionHtml()" not in excel, (
            "the full-width card is back in the page flow"
        )
        assert "qualityChipHtml()" in excel
        assert "function showQualityPanel" in js
        panel = js[js.index("function showQualityPanel"):js.index("function renderExcelIngestion")]
        for fact in ("q.valid", "q.total", "q.invalid", "q.issues"):
            assert fact in panel, f"the panel no longer reports {fact}"

    def test_the_chip_says_nothing_when_there_is_nothing_to_say(self):
        """
        A chip reading "0 issues" beside "File processed successfully" is
        noise, and a chip shown for a file nobody measured would be a claim
        about a measurement that was never taken.
        """
        js = _asset("js", "ingestion.js")
        fn = js[js.index("function qualityChipHtml"):js.index("function showQualityPanel")]
        assert "if (!q.total) return '';" in fn
        assert "if (!q.invalid && !q.issues.length) return '';" in fn
