"""
A site the client has PROPOSED, on screen.

The backend half of warehouse planning is covered by
`test_candidate_data_ingestion.py` (the column reaches the model) and
`test_candidate_opening.py` (the solver opens one when it needs to). This is
about what the screens then say, and every assertion here is a sentence that
was measured wrong on a live upload carrying two proposed DCs.

  * "Closed by solver" on a warehouse the client never built. A proposed site
    and a real one the optimiser shut both arrive with `isOpen === false`.
  * A solid green tower in the 3D twin for the same site, because
    `getUtilLabel(null)` returns 'Not solved', which matched no colour branch
    and left the healthy-emerald default.
  * And the one that survived the first fix: "Utilisation 0% Healthy",
    "Headroom 21,000 units/month", "Risk / Bottleneck LOW — Healthy Headroom"
    — on a building that does not exist. The guard tested for a NULL
    utilisation; hydration writes 0 for a site the solve did not use.

These read the shipped sources, in the style of the other screen tests here:
the behaviour is in template strings a headless run can only reach through a
solve, and the property worth protecting is that the branch exists at all.
"""

from __future__ import annotations

import pathlib

import pytest

JS = pathlib.Path(__file__).resolve().parents[3] / "app" / "frontend" / "js"


def _asset(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js() -> str:
    return _asset("app.js")


class TestAProposedSiteIsNotAClosedOne:

    def test_the_status_tag_tells_them_apart(self, app_js):
        block = app_js[app_js.index("function openStatusTag(node)"):]
        block = block[:block.index("\n}\n")]
        assert "'CANDIDATE'" in block
        assert "Proposed — not opened" in block
        # The wording for a real facility is unchanged.
        assert "Closed by solver" in block

    def test_the_dc_table_makes_the_same_distinction(self, app_js):
        block = app_js[app_js.index("function renderTwinTables()"):]
        block = block[:block.index("window.openFacilityPanel")]
        assert "Proposed — not opened" in block

    def test_the_map_draws_an_outline_rather_than_a_stop_sign(self):
        js = _asset("map.js")
        block = js[js.index("function createNodeMarker("):]
        assert "isUnbuiltCandidate" in block
        assert "(isClosed && !isCandidate)" in block, (
            "the stop sign is still drawn on a site that was never built")
        assert "proposed site — not opened" in block

    def test_the_3d_twin_does_not_colour_it_healthy(self):
        js = _asset("twin3d.js")
        block = js[js.index("function createDC3D("):]
        block = block[:block.index("\n}\n")]
        assert "dcCandidate" in block
        # The healthy colour is now a BRANCH, not the default an unmatched
        # band falls through to.
        assert "else if (utilBand === 'Healthy') dcColor = THEME_COLORS.dcHealthy;" in block
        assert "else dcColor = THEME_COLORS.dcUnsolved;" in block


class TestAnUnbuiltSiteHasNoHeadroom:
    """
    Zero utilisation is not comfort. It is the absence of a building.
    """

    def test_a_site_that_is_not_operating_has_no_risk_band(self, app_js):
        block = app_js[app_js.index("window.openFacilityPanel = function"):]
        block = block[:block.index("document.getElementById('facility-panel')")]
        assert "const notOperating = fac.isOpen === false;" in block
        assert "const riskKnown = !notOperating" in block, (
            "a site the solver did not use can still reach the utilisation "
            "bands, which read 0% as Healthy")
        assert "Closed in this solve — no load to assess" in block

    def test_headroom_and_growth_follow_the_same_guard(self, app_js):
        block = app_js[app_js.index("window.openFacilityPanel = function"):]
        block = block[:block.index("document.getElementById('facility-panel')")]
        # Both are computed from `headroomBasis`, which is null unless the
        # risk band is known — so neither can be reported for a site that is
        # not operating.
        assert "const headroomBasis = riskKnown ? Number(riskBasisPct) : null;" in block
        assert "headroomUnits === null ? ''" in block
        assert "growthToBreachPct === null ? ''" in block
        # And the busiest-period row, which otherwise reported on the data
        # under a heading about a site that has no solve to have a peak in.
        assert "const peakRow = notOperating ? '' :" in block

    def test_the_utilisation_row_says_which_kind_of_absence_it_is(self, app_js):
        block = app_js[app_js.index("window.openFacilityPanel = function"):]
        block = block[:block.index("document.getElementById('facility-panel')")]
        assert "not opened in this solve" in block
        assert "closed by the solver in this solve" in block
        # And when it IS operating, the tag respects the same open/closed fact
        # the rest of the product does.
        assert "getUtilTagClass(utilPct, fac.isOpen)" in block
        assert "getUtilLabel(utilPct, fac.isOpen)" in block


class TestWhatItCostsToBuildIsSaidOrItsAbsenceIs:

    def test_a_priced_candidate_shows_the_one_time_cost(self, app_js):
        block = app_js[app_js.index("window.openFacilityPanel = function"):]
        assert "Cost to open" in block
        assert "one-time" in block

    def test_an_unpriced_candidate_says_the_optimiser_is_told_it_is_free(self, app_js):
        """
        Silence about a missing opening cost is the dangerous case: the MILP
        charges `opening_cost`, so an unpriced candidate is offered as free to
        build, and a free site that saves anything at all is always worth
        opening.
        """
        block = app_js[app_js.index("window.openFacilityPanel = function"):]
        assert "not priced in the upload" in block
        assert "treating this site as free to build" in block


class TestTheScenarioBuilderOffersThemAndLabelsThem:

    def test_a_candidate_is_in_the_list_and_marked(self):
        js = _asset("scenarios.js")
        block = js[js.index("function facilityOptionsHtml("):]
        block = block[:block.index("\n}\n")]
        assert "— proposed site" in block, (
            "a proposed DC sat unlabelled in a dropdown headed 'one of my "
            "existing sites'")

    def test_the_dropdown_no_longer_calls_them_existing(self):
        js = _asset("scenarios.js")
        assert "One of the sites already in my data" in js
        assert "One of my existing sites, pinned open" not in js
