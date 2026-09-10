"""
How figures and charts are presented to the people who sign things off.

TWO PROPERTIES, held here because both were asked for by name and both are the
kind of thing that decays silently.

    MONEY CARRIES NO CENTS.   "₹768,768,999.42" is not a more precise answer
                              than "₹768,768,999"; it is the same answer with
                              two digits of solver residue on the end, and on
                              a board slide it reads as false precision about
                              the output of a relaxation. Rates keep their
                              decimals, because at that scale the decimals ARE
                              the measurement.

    EVERY AXIS SAYS WHAT IT   A chart whose value axis is unlabelled is a
    MEASURES.                 chart whose numbers mean whatever the reader
                              assumes, and a category axis of NAMES belongs on
                              the vertical so the names read the way they are
                              written.

The third test class is the one that made the first property safe to have:
rounding money changed what the numeric-claim validator was willing to police.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from netgravity.orchestrator.reasoning.evidence import (
    _RATE_THRESHOLD,
    _display,
    format_money,
    format_money_compact,
)
from netgravity.orchestrator.validation.numeric_grounding import (
    ClaimKind,
    extract_numeric_claims,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CHARTS = REPO_ROOT / "app" / "frontend" / "js" / "charts.js"


class TestMoneyIsWrittenForALeader:

    @pytest.mark.parametrize("value,expected", [
        (150627.7036, "₹150,628"),
        (768768999.42, "₹768,768,999"),
        (1435985.0, "₹1,435,985"),
        (0.0, "₹0.00"),
    ])
    def test_an_amount_above_the_rate_threshold_loses_its_cents(self, value, expected):
        assert format_money(value, "INR") == expected

    def test_a_per_unit_rate_keeps_them(self):
        """
        A handling cost of ₹2.45 a unit becomes ₹2 under a blanket rounding —
        and the decimals there are not residue, they are the quantity being
        measured. Almost every caller of the exact formatter is a rate.
        """
        assert format_money(2.45, "INR") == "₹2.45"
        assert format_money(99.99, "INR") == "₹99.99"
        assert format_money(_RATE_THRESHOLD, "INR") == "₹100"

    def test_the_sign_goes_outside_the_symbol(self):
        """
        "₹-45,890" reads as a currency nobody uses before it reads as a
        negative amount — and a cost DELTA is the field most likely to be
        negative and the one most worth reading correctly at a glance.
        """
        assert format_money(-45890.5, "INR") == "-₹45,890"
        assert format_money(-45890.5, "SEK") == "SEK -45,890"

    def test_an_upload_that_named_no_currency_still_renders_bare(self):
        assert format_money(150627.70, None) == "150,628"

    def test_the_compact_form_is_for_headlines_and_keeps_one_decimal(self):
        """"₹15.13 Cr" is a headline pretending to be a ledger."""
        assert format_money_compact(768768999.42, "INR") == "₹76.9Cr"
        assert format_money_compact(768768999.42, "USD") == "$768.8M"
        assert format_money_compact(1435985.0, "INR") == "₹14.4L"

    def test_the_scale_words_follow_the_currency(self):
        """Stamping "L" and "Cr" on dollars is wrong twice."""
        assert "Cr" in format_money_compact(2e8, "INR")
        assert "M" in format_money_compact(2e8, "USD")


class TestEveryDisplayedQuantityIsAtAReadablePrecision:

    @pytest.mark.parametrize("key,value,expected", [
        ("utilization_pct", 92.374, "92.4%"),
        ("total_carbon_kg", 18455.223, "18,455 kg"),
        ("distance_km", 412.6667, "412.7 km"),
        ("business_network_cost", 150627.7036, "₹150,628"),
    ])
    def test_a_figure_is_shown_at_the_precision_it_was_measured_to(
            self, key, value, expected):
        assert _display(value, key, "INR")[0] == expected

    def test_an_absent_figure_is_said_rather_than_zeroed(self):
        assert _display(None, "business_network_cost", "INR")[0] == "Not available"


class TestRoundingMoneyDidNotSwitchTheValidatorOff:
    """
    THE REGRESSION THIS CLASS EXISTS FOR, and it was silent.

    While every amount printed as "216,594,606.26" the decimal point sent it to
    the UNKNOWN branch of the claim classifier and it was policed. The moment
    the cents came off for readability the same figure fell through to COUNT —
    which `_is_policeable` deliberately ignores, because that branch is there
    to skip "three facilities" and "2026". So a model could assert "the cost is
    216,594,606" and nothing checked it, and the narrative came back NO_CLAIMS,
    which reads like a clean result and is the validator having been disabled.
    """

    def test_a_grouped_number_is_still_a_claim(self):
        claims = extract_numeric_claims("a business network cost of 216,594,606")
        policed = [c for c in claims if c.kind is not ClaimKind.COUNT]
        assert policed, [c.kind for c in claims]
        assert policed[0].value == pytest.approx(216594606.0)

    def test_bare_small_integers_are_still_ignored(self):
        """The exclusion that branch is actually for."""
        claims = extract_numeric_claims("three facilities and 2 scenarios in 2026")
        assert all(c.kind is ClaimKind.COUNT for c in claims), \
            [(c.raw_text, c.kind) for c in claims]

    def test_a_rounded_cost_narrative_still_grounds(self):
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        result = ReasoningAgent().reason({"network_state": {
            "business_network_cost": 216594606.26,
            "periods_modelled": 12,
            "cost_per_period": 18049550.52,
            "cost_components": {"transport_cost": 10138267.14},
        }}, allow_llm=False, scope=ReasoningScope.NETWORK)
        assert result.grounding_status == "GROUNDED"
        assert result.validation_warnings == []

    def test_the_per_period_figure_is_formatted_like_every_other_amount(self):
        """
        It printed `f"{value:,.2f}"`, so one sentence carried the same quantity
        twice — once with a symbol and no cents, once with cents and no symbol.
        """
        from netgravity.orchestrator.agents.reasoning_agent import ReasoningAgent
        from netgravity.orchestrator.schemas.reasoning import ReasoningScope

        result = ReasoningAgent().reason({"network_state": {
            "business_network_cost": 216594606.26,
            "periods_modelled": 12,
            "cost_per_period": 18049550.52,
            "currency": "INR",
        }}, allow_llm=False, scope=ReasoningScope.NETWORK)
        assert "18,049,551 per period" in result.summary
        assert ".52 per period" not in result.summary


class TestEveryChartSaysWhatItMeasures:
    """
    Read from the source rather than the screen, because the question is about
    the CONFIG. A chart that renders correctly today with an unlabelled value
    axis renders just as correctly tomorrow against a different quantity.
    """

    def _renderers(self):
        source = CHARTS.read_text(encoding="utf-8")
        starts = [(m.start(), m.group(1)) for m in
                  re.finditer(r"export function (render\w+)\(", source)]
        starts.append((len(source), None))
        for (pos, name), (nxt, _) in zip(starts, starts[1:]):
            if name:
                yield name, source[pos:nxt]

    def _bar_charts(self):
        for name, block in self._renderers():
            if re.search(r"type:\s*'bar'", block):
                yield name, block

    def test_there_is_at_least_one_bar_chart_to_check(self):
        assert list(self._bar_charts())

    def test_every_bar_chart_titles_both_axes(self):
        for name, block in self._bar_charts():
            for axis in ("x", "y"):
                assert re.search(rf"\b{axis}:\s*\{{(?:[^{{}}]|\{{[^{{}}]*\}})*"
                                 rf"title:\s*\{{[^}}]*display:\s*true", block), \
                    f"{name}: the {axis} axis carries no title"

    def test_no_bar_chart_puts_names_along_the_bottom(self):
        """
        A site is called "Bengaluru Distribution Centre" and a column has one
        column-width to write that in. Every such chart had the same three
        symptoms: labels wrapped to three lines, or set at an angle, or
        trimmed until two sites read the same.
        """
        for name, block in self._bar_charts():
            assert "indexAxis: 'y'" in block, \
                f"{name} is still a column chart"

    def test_no_axis_rotates_its_labels(self):
        """
        Angled tick labels are the commonest reason a chart reads as
        unfinished, and on a period axis they are never necessary.
        """
        source = CHARTS.read_text(encoding="utf-8")
        rotations = re.findall(r"maxRotation:\s*(\d+)", source)
        assert rotations, "the guard is checking a setting nobody uses"
        assert all(int(r) == 0 for r in rotations), rotations

    def test_the_threshold_marker_follows_the_value_axis(self):
        """
        It drew a horizontal rule at `scales.y`, which was right while the
        chart was columns of facilities. On a bar chart the percentages run
        along X, and a horizontal rule marks a POSITION IN THE LIST OF SITES
        rather than a utilisation — it would go on drawing, in the right
        colour, in the wrong place.
        """
        source = CHARTS.read_text(encoding="utf-8")
        block = source[source.index("id: 'ngUtilisationThreshold'"):]
        block = block[:block.index("\n};")]
        assert "indexAxis === 'y'" in block
        assert "horizontal ? scales.x : scales.y" in block

    def test_the_demo_era_scenario_charts_are_gone(self):
        """
        Four renderers nothing imported, and none of them merely unused: the
        cost chart pinned its axis to `min: 10.0, max: 14.5` — a lakh range
        fitted to one demo network — so a network costing ₹150,627 plotted
        below the floor as an empty chart with no error.
        """
        source = CHARTS.read_text(encoding="utf-8")
        code = re.sub(r"/\*[\s\S]*?\*/", "", source)
        for dead in ("renderScenarioCostImpactChart",
                     "renderScenarioCapacityRiskChart",
                     "renderScenarioRadar"):
            assert dead not in code, dead
        assert "min: 10.0" not in code


class TestABarChartGrowsWithItsBars:
    """
    THE SECOND HALF OF TURNING THESE CHARTS ON THEIR SIDE.

    Making them horizontal fixed labels being rotated and stacked, and created
    a different problem: on a column chart every extra category takes width
    from the plot, on a bar chart it takes VERTICAL room from its neighbours —
    and the container heights are literals in the markup (300px, 260px, 220px)
    chosen against a five-site demo.

    Measured on the running application before this was fixed:

        utilisation, 12 sites    243px axis,  20px between ticks, 13px labels
        utilisation, 20 sites    243px axis,  12px between ticks  -> OVERLAP
        corridor flows, 9 lanes  163px axis,  18px between ticks

    Seven pixels of clearance at twelve sites is luck, not a margin — and a
    name past the single-line budget wraps to two lines, which is 26px against
    that 20px gap and overlaps at the size the chart is drawn at today.

    After: 341px at twelve sites and 549px at twenty, holding the gap at 27-29px
    whichever it is.
    """

    def _charts_js(self) -> str:
        return CHARTS.read_text(encoding="utf-8")

    def test_the_sizing_helper_exists_and_is_bounded(self):
        js = self._charts_js()
        assert "export function sizeBarChartHost(" in js
        # Bounded at both ends: never a strip, never taller than a screen.
        assert "BAR_MIN_PX" in js and "BAR_MAX_PX" in js
        assert "Math.max(BAR_MIN_PX, Math.min(BAR_MAX_PX" in js

    def test_the_row_height_clears_a_two_line_label(self):
        """
        A tick label is 13px at the 10.5px font these charts use, and 26px when
        it wraps to two lines. The row height has to clear the second case or
        the fix only works on short names.
        """
        js = self._charts_js()
        row = int(re.search(r"const BAR_ROW_PX = (\d+)", js).group(1))
        assert row >= 26, row

    def test_every_horizontal_bar_chart_sizes_its_own_box(self):
        js = self._charts_js()
        for renderer in ("renderWarehouseUtilisationChart",
                         "renderWarehouseHeadroomChart",
                         "renderWarehouseStockChart",
                         "renderFacilityLaneFlowsChart"):
            start = js.index(f"export function {renderer}(")
            block = js[start:js.index("\nexport function ", start + 10)
                       if "\nexport function " in js[start + 10:] else len(js)]
            assert "sizeBarChartHost(" in block, renderer

    def test_the_corridor_chart_stops_before_its_labels_touch(self):
        """
        The only one with no upstream cap: it drew every corridor attached to
        the site into a 220px box, and a hub with twenty would have run them
        together. The telemetry table below carries all of them.
        """
        js = self._charts_js()
        limit = int(re.search(r"const LANE_CHART_LIMIT = (\d+)", js).group(1))
        assert 8 <= limit <= 14, limit
        block = js[js.index("export function renderFacilityLaneFlowsChart("):]
        block = block[:block.index("\n/**")]
        assert "slice(0, LANE_CHART_LIMIT)" in block
        # Busiest first, so what falls off the end is what matters least.
        assert "sort(" in block
        # And it reports what it held back, so the caption can say so.
        assert "return { shown: shown.length, total: connectedLanes.length };" in block

    def test_the_caption_says_when_corridors_were_held_back(self):
        app = (REPO_ROOT / "app" / "frontend" / "js"
               / "app.js").read_text(encoding="utf-8")
        block = app[app.index("function captionCorridorChart("):]
        block = block[:block.index("\n  captionCorridorChart(null);")]
        assert "drawn.total > drawn.shown" in block
        assert "table below" in block

    def test_the_deep_dive_chart_sizes_too(self):
        """
        `.insd-chart-canvas-wrap` is a flat 300px and the entities plan draws
        every entity a finding was computed over, with no cap.
        """
        js = (REPO_ROOT / "app" / "frontend" / "js"
              / "insight-detail.js").read_text(encoding="utf-8")
        assert "function sizeInsightChartHost(" in js
        assert "INSD_BAR_ROW_PX" in js
        block = js[js.index("function renderInsightChart("):]
        block = block[:block.index("\n  if (plan.kind === 'evidence') {")
                      if "\n  if (plan.kind === 'evidence') {" in block
                      else block.index("\n  const grid =")]
        assert "sizeInsightChartHost(canvasId, plan.cited.length)" in block
        assert "sizeInsightChartHost(canvasId, plan.entities.length)" in block
        # A time-series plan hands the height back rather than staying tall.
        assert "resetInsightChartHost(canvasId)" in block

    def test_it_is_the_chart_wrap_that_is_resized_not_the_canvas(self):
        """
        Chart.js owns the canvas element's own width and height attributes; a
        renderer that set them directly would have them overwritten on the next
        responsive reflow. The CONTAINER is what holds the height.
        """
        js = self._charts_js()
        block = js[js.index("export function sizeBarChartHost("):]
        block = block[:block.index("\nexport function axisFacilityLabels")]
        assert "canvas.parentElement" in block
        assert "classList.contains('chart-wrap')" in block
        assert "host.style.height" in block
