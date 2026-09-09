"""
Four corrections to screens that were already right about the figures.

  * **The scenario map's key did not fit its map.** It is the twin's own key
    plus three scenario rows — 456px — inside a panel that clamps to 360px on
    a short window. The panel is `overflow: hidden`, so the top of the key was
    simply cut off, taking the first group's heading with it.

  * **The Forecast screen carried a Facility and a Period picker.** Neither
    moves a demand forecast: a forecast is per market-product series, and the
    screen has its own picker for that. Two controls that look like scope and
    change nothing are worse than none.

  * **A facility on the map was a 13px emoji.** At the zoom a national network
    is framed at, a reader could see that a marker was there and not what it
    was — and the 3D scene said the same thing with a different vocabulary
    (a shape), while the key beside it printed an emoji that appeared nowhere
    in the scene.

  * **The recommendation card scrolled out from under the top bar.** It is
    `position: sticky` inside the same scroll container as the sticky top bar,
    pinned at 16px from the scrollport — behind it. And its column was sized
    to the card, so it had no travel and did not stick at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_FRONTEND = Path(__file__).resolve().parents[3] / "app" / "frontend"


def _asset(*parts) -> str:
    return _FRONTEND.joinpath(*parts).read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    """One CSS rule body, by the selector that opens it."""
    start = css.index(selector)
    return css[start:css.index("}", start)]


@pytest.fixture(scope="module")
def style_css() -> str:
    return _asset("css", "style.css")


@pytest.fixture(scope="module")
def scenarios_js() -> str:
    return _asset("js", "scenarios.js")


class TestOneIconInEveryView:
    """
    The map, the 3D scene and the key show the same mark at the same size.
    """

    def test_the_size_lives_with_the_glyph_and_the_colour(self):
        legend = _asset("js", "twin-legend.js")
        for kind in ("plant", "dc", "market"):
            block = legend[legend.index(f"  {kind}:"):]
            block = block[:block.index("},")]
            assert "radius:" in block, kind
        assert "export function glyphSize(" in legend

    def test_the_glyph_is_a_fraction_of_the_marker_not_an_offset(self):
        """
        It was `size - 3`. A DC's marker grows with its utilisation, so
        subtracting a constant made the glyph shrink relative to its own
        circle exactly as the circle grew — the busiest site, which is the one
        a reader is looking for, had the least legible icon on the map.
        """
        legend = _asset("js", "twin-legend.js")
        fn = legend[legend.index("export function glyphSize("):]
        fn = fn[:fn.index("\n}")]
        assert "GLYPH_SCALE" in fn
        assert "*" in fn, fn
        assert "- 3" not in fn

    def test_the_2d_map_reads_the_shared_size(self):
        js = _asset("js", "map.js")
        assert "NODE_STYLE.plant.radius" in js
        assert "glyphSize(adjustedSize)" in js
        # No independent number left to drift.
        assert "sizeMap = { plant: 16" not in js
        assert "Math.max(11, adjustedSize - 3)" not in js

    def test_a_lightly_loaded_dc_is_never_smaller_than_a_market(self):
        """
        The DC band is derived from the shared radii rather than written as
        literals, so raising the base sizes cannot leave a DC drawn smaller
        than the market dots around it.
        """
        js = _asset("js", "map.js")
        block = js[js.index("  if (isDc) {"):]
        block = block[:block.index("\n  }")]
        assert "NODE_STYLE.market.radius" in block
        assert "NODE_STYLE.dc.radius" in block
        assert "Math.max(12," not in block

    def test_the_3d_scene_wears_the_same_glyph(self):
        """
        It said what a node was by its SHAPE — a hexagonal pedestal, a drum.
        Legible, and it transfers nowhere: a reader who learnt the key on the
        map arrived at the scene and had to learn it again, while the key
        printed beside the scene showed an emoji that was not in it.
        """
        js = _asset("js", "twin3d.js")
        assert "twin3d-node-glyph" in js
        assert "NODE_STYLE[node.type]" in js
        assert "glyphSize(style.radius)" in js
        assert "style.glyph" in js

    def test_the_badge_cannot_make_a_node_unhoverable(self):
        """
        It sits over the node, and the scene picks nodes by raycasting the
        canvas underneath. A badge that took the pointer would cover the thing
        it labels.
        """
        css = _asset("css", "style.css")
        rule = _rule(css, ".twin3d-node-glyph {")
        assert "pointer-events: none" in rule

    def test_a_glyph_is_never_culled_the_way_a_name_pill_is(self):
        """
        A name pill is a convenience and is dropped when it would collide with
        another. The glyph IS the node's identity — hiding it would hide what
        the node is.
        """
        js = _asset("js", "twin3d.js")
        fn = js[js.index("function updateNodeLabels()"):]
        fn = fn[:fn.index("\nfunction ")]
        glyph_part = fn[:fn.index("if (!nodeLabels.length) return;")]
        assert "clash" not in glyph_part
        assert "placed" not in glyph_part

    def test_the_key_draws_the_chip_at_the_marker_size(self):
        legend = _asset("js", "twin-legend.js")
        fn = legend[legend.index("function facilityRow(kind) {"):]
        fn = fn[:fn.index("\n}")]
        assert "style.radius * 2" in fn
        assert "glyphSize(style.radius)" in fn


class TestTheScenarioKeySitsBelowItsMap:
    """
    It used to be a Leaflet control anchored bottom-right, which is the corner
    a national network's southern sites occupy. It was also taller than the
    panel at the short end of `clamp(360px, 46vh, 520px)`, so it had to be
    bounded and scrolled — and a key you scroll to read has stopped being one.
    """

    def test_the_scenario_map_mounts_no_legend_control(self):
        """
        The compact branch returns before `L.control` is reached. If it fell
        through, the map would carry BOTH keys.
        """
        js = _asset("js", "map.js")
        fn = js[js.index("function addLegend(map, isCompact = false) {"):]
        fn = fn[:fn.index("\n/** Every legend control")]
        compact = fn[:fn.index("const legend = L.control(")]
        assert "scenarioLegendHost()" in compact
        assert "return;" in compact

    def test_the_key_has_a_host_under_the_map_in_the_markup(self):
        html = _asset("index.html")
        wrap = html.index('id="scenario-map-wrap"')
        host = html.index('id="scenario-map-legend"')
        assert host > wrap, "the key must follow the map it explains"
        # Inside the same card, not adrift at the foot of the page.
        card = html.rindex('class="scn-visual-context-card"', 0, wrap)
        assert host - card < 2000

    def test_it_redraws_as_a_scenario_key_not_a_twin_key(self):
        """
        `refreshTwinMapLegend` runs on every network refresh. Rewriting the
        host with `twinLegendHtml` would strip the three rows that are true
        only on a scenario map.
        """
        js = _asset("js", "map.js")
        fn = js[js.index("export function refreshTwinMapLegend() {"):]
        fn = fn[:fn.index("\n}")]
        assert "scenarioLegendHtml(perPeriodLabel())" in fn
        assert "scenarioLegendHost()" in fn

    def test_the_groups_run_across_the_strip_not_down_it(self, style_css):
        """Four stacked groups is the shape that was too tall for the map."""
        rule = _rule(style_css, ".tw-legend-below {")
        assert "display: flex" in rule
        assert "max-width: none" in rule
        group = _rule(style_css, ".tw-legend-below .tw-legend-group {")
        assert "flex: 1 1" in group

    def test_it_is_not_bounded_or_scrolled_any_more(self, style_css):
        """
        Nothing constrains it to the map's height now, because it is no longer
        inside the map.
        """
        rule = _rule(style_css, ".tw-legend-below {")
        assert "max-height" not in rule
        assert "overflow-y: auto" not in rule

    def test_the_map_still_publishes_one_height(self, style_css):
        rule = _rule(style_css, ".scn-map-wrap {")
        assert "--scn-map-h: clamp(360px, 46vh, 520px)" in rule
        assert "height: var(--scn-map-h)" in rule

    def test_the_key_still_states_every_encoding_the_map_draws(self):
        """
        Moved, not shortened. A key that omits an encoding the map is drawing
        is the problem it was built to fix.
        """
        legend = _asset("js", "twin-legend.js")
        fn = legend[legend.index("export function scenarioLegendHtml("):]
        fn = fn[:fn.index("\n}")]
        assert "twinLegendHtml(perPeriod)" in fn
        assert "Corridor this plan moves volume on" in fn
        assert "Corridor unchanged from today" in fn
        assert "Site this scenario adds" in fn


class TestTheForecastScreenDropsDeadScope:
    def test_the_facility_and_period_pair_is_hidden_there(self):
        js = _asset("js", "app.js")
        fn = js[js.index("function updateTopBarLayout(tab) {"):]
        fn = fn[:fn.index("\n/**")]
        assert "tab !== 'forecast'" in fn
        assert "tab !== 'scenarios'" in fn

    def test_the_screen_keeps_the_picker_that_does_narrow_it(self):
        """
        A forecast is per market-product series, and that picker is the
        chart's own title. Removing the two dead controls must not remove the
        live one.
        """
        html = _asset("index.html")
        assert 'id="fc-series-select"' in html


class TestTheRecommendationCardStaysWhereItIsPut:
    def test_it_pins_below_the_bar_rather_than_behind_it(self, style_css):
        """
        `.app-global-topbar` is sticky at `top: 0` in the same scroll
        container, so a card at `top: 16px` pinned sixteen pixels from the top
        of the SCROLLPORT — underneath it. The heading and the first line of
        the verdict scrolled under the bar and stayed there.
        """
        rule = _rule(style_css, ".scn-take-card {")
        assert "var(--global-topbar-h" in rule
        assert "top: 16px" not in rule
        # And it is bounded by what is left of the window under that bar.
        assert "max-height: calc(100vh - var(--global-topbar-h" in rule

    def test_the_bar_height_is_measured_rather_than_assumed(self):
        js = _asset("js", "app.js")
        assert "function publishTopBarHeight()" in js
        fn = js[js.index("function publishTopBarHeight()"):]
        fn = fn[:fn.index("\n}")]
        assert "getBoundingClientRect().height" in fn
        assert "--global-topbar-h" in fn
        # Re-measured per tab: the bar carries different things on each.
        assert "requestAnimationFrame(publishTopBarHeight)" in js

    def test_its_column_is_tall_enough_to_stick_inside(self, style_css):
        """
        `align-items: start` sized the column to the card, and a sticky
        element travels only inside its own containing block — so with zero
        travel it did not stick at all. It scrolled up under the bar and out
        of the window while its own `max-height` made it look pinned.
        """
        rule = _rule(style_css, ".scn-single-right {")
        assert "align-self: stretch" in rule

    def test_the_map_is_in_the_evidence_column(self):
        """
        The map was a full-width card AFTER the grid, so the row ended at the
        taller of the table and the card and the card had nothing to stick
        across. In the left column it gives the card travel for the whole
        length of the evidence — which is the point: a reader looking at the
        network should still be able to read the recommendation about it.
        """
        html = _asset("index.html")
        left = html[html.index('<div class="scn-single-left">'):]
        left = left[:left.index('<div class="scn-single-right">')]
        assert 'id="scenario-leaflet-map"' in left
        assert 'class="scn-visual-context-card"' in left


class TestTheCardSaysTheAnswerBeforeItScrolls:
    def test_the_working_moved_to_the_detail_view(self, scenarios_js):
        """
        1,519px of card in a 966px window put the recommended action — the
        thing this card exists to say — below the fold. Everything cut is one
        press away in "View full detail".
        """
        card = scenarios_js[scenarios_js.index("container.innerHTML = takeHeadHtml("):]
        card = card[:card.index("container.querySelectorAll(")]
        # What the card BUILDS. Each of these survives here as a comment
        # recording what moved and why, which is the opposite of the defect
        # and must not fail its own test.
        built = re.sub(r"//[^\n]*", "", card)
        for gone in ("capacityResponseHtml(", "takeFiguresHtml(",
                     "Also compared", "Next step"):
            assert gone not in built, gone
        # The answer, and the way to the rest of it.
        assert "takeActionsHtml(actions)" in card
        assert "takeFooterHtml()" in card

    def test_nothing_was_left_defined_but_uncalled(self, scenarios_js):
        """
        A helper nothing calls is a second definition of a thing that moved,
        waiting to be wired back to a screen that no longer wants it.
        """
        for dead in ("function takeFiguresHtml(", "function takeCheckItem("):
            assert dead not in scenarios_js, dead

    def test_the_briefing_s_own_next_step_reads_beside_the_derived_list(
            self, scenarios_js):
        """
        It sat on the card ABOVE the recommended actions. When the two agreed
        the reader was told the same thing twice; when they did not, they had
        two recommendations and nothing to choose between them.
        """
        drawer = scenarios_js[scenarios_js.index("export function openScenarioDrawer"):]
        assert "drawerNextStep" in drawer
        assert "The briefing's own next step" in drawer

    def test_the_meaning_is_one_paragraph_not_three_phrasings(self, scenarios_js):
        """
        The verdict states the conclusion; the model's headline restated it;
        the meaning explained it. Three passes at one finding, and the bold
        "Across the 3 compared:" lead broke the wrap early — a four-line
        paragraph whose longest line used half the column.
        """
        fn = scenarios_js[scenarios_js.index("function narrativeHtml("):]
        fn = fn[:fn.index("\n}\n")]
        assert fn.count("scn-take-para") == 1, fn
        assert "<strong>" not in fn

    def test_it_does_not_print_the_verdict_a_second_time(self, scenarios_js):
        """
        Measured on a live +15% demand run: the briefing's `meaning` opened
        "Simulating Demand +15% produced a feasible plan that serves all
        demand with open facilities…" — word for word the verdict printed
        directly above it.

        The duplication guard ran against the HEADLINE, because that is what
        the section used to lead with. Rendering one paragraph carried the
        check past the string it now prints, and where both restate the
        verdict the section is omitted rather than filled: a heading reading
        "What this means" over a sentence just read is worse than no heading.
        """
        fn = scenarios_js[scenarios_js.index("function narrativeHtml("):]
        fn = fn[:fn.index("\n}\n")]
        assert "saysTheSameThing(body, verdict)" in fn
        assert "saysTheSameThing(headline, verdict)" in fn
        assert "if (!body) return '';" in fn

    def test_the_recommendations_are_still_the_servers(self, scenarios_js):
        """
        Trimming the card must not have moved any decision back into it.
        """
        fn = scenarios_js[scenarios_js.index("function recommendedActions("):]
        fn = fn[:fn.index("\n}\n")]
        assert "comparison.recommended_actions" in fn
        assert "at_ceiling" not in fn


class TestTheDrawerTablesFit:
    """
    Four columns in the 460px content column of a 520px drawer, at the shared
    table's 14px cell padding: 112px of the width was padding, and the figures
    were squeezed into what was left.

    The Shift column then wrapped BETWEEN the arrow and the number — "↑" on
    one line and "2,100" on the next, in the one column whose job is to say
    how much moved — and rows came out at different heights depending on
    whether their corridor name wrapped, so a column of figures stopped
    scanning as a column.
    """

    def test_a_figure_never_wraps_away_from_its_sign(self, style_css):
        rule = _rule(style_css, "#scenario-drawer .scn-drawer-table .num {")
        assert "white-space: nowrap" in rule
        assert "text-align: right" in rule

    def test_the_arrow_is_bound_to_its_figure_in_the_markup(self):
        """
        CSS alone is not enough: the template put a newline between the arrow
        and the number, which is a break opportunity wherever the cell is
        allowed to wrap. The non-breaking space removes it at the source.
        """
        import re
        js = _asset("js", "scenarios.js")
        # Every arrow that leads a figure is bound to it.
        assert js.count("'↑'}&nbsp;${") + js.count("'↓'}&nbsp;${") >= 5
        # And no TABLE CELL separates them by anything a line may break at.
        # Prose elsewhere may wrap; a column of figures may not.
        cells = re.findall(r"<td[^>]*>.*?</td>", js, re.S)
        loose = [c for c in cells if re.search(r"'[↑↓]'\}\s+\$\{", c)]
        assert not loose, f"{len(loose)} table cell(s) can break an arrow off its figure"

    def test_the_drawer_buys_the_width_back_from_padding(self, style_css):
        rule = _rule(style_css, "#scenario-drawer .scn-drawer-table th,")
        assert "padding: 8px 9px" in rule

    def test_only_the_corridor_may_wrap_and_never_inside_a_name(self, style_css):
        """
        A corridor is the one cell with a natural break in it — after the
        arrow. Breaking inside "PLANT_NORTH" would not be a wrap, it would be
        a different identifier on each line.
        """
        rule = _rule(style_css, "#scenario-drawer .scn-drawer-table .scn-corridor-cell {")
        assert "word-break: normal" in rule
        assert "break-all" not in rule

    def test_both_drawer_tables_opt_in(self):
        """The rules are scoped to the class, so a table without it keeps the
        shared padding and the defect."""
        js = _asset("js", "scenarios.js")
        assert js.count('class="scn-data-table scn-drawer-table"') == 2


class TestAnAxisLabelIsReadable:
    def test_it_wraps_on_words_rather_than_cutting_one_short(self):
        """
        A flat seven-character cut drew "Norther…" and "Souther…" — one letter
        short of words that had room, which reads as a misspelling rather than
        an abbreviation, and hid whether a bar was a plant or a DC.
        """
        js = _asset("js", "charts.js")
        fn = js[js.index("function wrapLabel(name, perLine = AXIS_LINE_CHARS) {"):]
        fn = fn[:fn.index("\n}")]
        assert "split(/" in fn, "it must break on whitespace"
        # A single over-long word is the only thing cut mid-word.
        assert "word.length > perLine" in fn

    def test_the_width_grows_until_the_labels_are_distinct(self):
        """
        Two sites whose names agree for the first two lines would draw two
        identical labels. Width is spent only where it buys a distinction.
        """
        js = _asset("js", "charts.js")
        fn = js[js.index("export function axisFacilityLabels(names) {"):]
        fn = fn[:fn.index("\n}")]
        assert "new Set" in fn
        assert "perLine += " in fn

    def test_every_facility_axis_uses_it(self):
        """Three charts label a facility axis. One rule for all of them."""
        js = _asset("js", "charts.js")
        assert js.count("axisFacilityLabels(") == 4   # 1 definition + 3 uses
        # The old per-name cut is no longer applied to an axis directly.
        assert "labels: rows.map((r) => shortFacilityLabel(" not in js


class TestTheFacilityDetailRenders:
    def test_it_calls_an_escaper_that_exists(self):
        """
        `esc(stockReason)` was called in `app.js`, which defines no `esc`. It
        threw a ReferenceError on any site whose stock the solve did not
        report — which aborted the whole facility render mid-string, so the
        detail below it was simply never drawn. Nothing logged a failure; the
        page just ended early.
        """
        js = _asset("js", "app.js")
        import re
        bare = re.findall(r"[^A-Za-z_.]esc\(", js)
        assert not bare, f"{len(bare)} call(s) to an undefined esc()"
        assert "escAttr(stockReason)" in js

    def test_the_escaper_it_uses_is_defined_in_that_file(self):
        js = _asset("js", "app.js")
        assert "function escAttr(value) {" in js
