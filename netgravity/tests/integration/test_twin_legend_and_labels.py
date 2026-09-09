"""
The Digital Twin's key, its labels, and the figures under it.

Laid out against `Dump/digital twin-updated.png`. Four claims.

  * **One key, for both views, describing what is actually drawn.** There
    were two legends — one written out in index.html, one built from inline
    styles in map.js — and they had already drifted. Neither mentioned line
    weight, though both views varied it; and they varied it by different
    constants, so the same corridor was drawn at two thicknesses depending
    on which button the reader had pressed.

  * **A facility is named on the map, and its id is in the name.** Every name
    lived in a hover tooltip, so reading the map meant pointing at one site
    at a time; and no view ever showed the id, which is the column the
    reader's own workbook is keyed on and the one thing that does not repeat
    between two sites both called "Central DC".

  * **The labels stay readable.** Twenty-six pills over southern Canada
    overlap into a block of text, which is worse than the tooltips they
    replaced.

  * **The figures below the map answer the controls above it.** Three tables
    listing every node ignored the Facility and Period selectors completely.

Asset-level, like the other two frontend suites, because these are defects in
bundled JavaScript: a browser test would report "the map looks different"
without naming which half had drifted.
"""

from __future__ import annotations

import pathlib
import re

from app.backend.app import app


FRONTEND = pathlib.Path(app.root_path).parent / "frontend"


def _asset(*parts: str) -> str:
    path = FRONTEND
    for part in parts:
        path = path / part
    return path.read_text(encoding="utf-8", errors="replace")


def _without_comments(text: str) -> str:
    """Comments here quote the thing being banned; scan the code, not the prose."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def _fn(js: str, signature: str) -> str:
    start = js.index(signature)
    return js[start:js.index("\n}\n", start)]


class TestTheKeyDescribesWhatIsDrawn:
    def test_both_views_size_a_corridor_by_the_same_rule(self):
        shared = _without_comments(_asset("js", "twin-legend.js"))
        assert "export function flowBands" in shared
        assert "export function bandForFlow" in shared

        two_d = _without_comments(_asset("js", "map.js"))
        three_d = _without_comments(_asset("js", "twin3d.js"))
        assert "bandForFlow(flow.flow, bands).weight2d" in two_d, two_d[:0] or "2D"
        assert "bandForFlow(lane.flow, bands).radius3d" in three_d, "3D"

    def test_the_old_per_view_constants_are_gone(self):
        """
        `flow / 1500` in the 2D map and `lane.flow / 4000` in the 3D twin.
        Two consequences: the views disagreed with each other, and each
        suited one network — a network whose largest corridor carries 4,500
        units drew every lane at the minimum, one moving 500,000 drew every
        lane at the maximum, and in both the thickness carried nothing.
        """
        for module in ("map.js", "twin3d.js"):
            js = _without_comments(_asset("js", module))
            assert "/ 1500" not in js, module
            assert "/ 4000" not in js, module

    def test_the_bands_come_from_the_network_rather_than_from_a_literal(self):
        js = _without_comments(_asset("js", "twin-legend.js"))
        fn = _fn(js, "export function flowBands()")
        assert "LANES" in fn, fn
        assert "Math.max(...flows)" in fn, fn
        # And a network with no volumes says so rather than showing four
        # bands of nothing.
        assert "return null" in fn, fn

    def test_the_legend_prints_the_same_boundaries_it_draws(self):
        js = _without_comments(_asset("js", "twin-legend.js"))
        fn = _fn(js, "export function twinLegendHtml(perPeriod = 'units/period')")
        assert "flowBands()" in fn, fn
        assert "b.weight2d" in fn, (
            "the legend must sample the weight it is keying, not a fixed one")
        assert "bandLabel(b)" in fn, fn

    def test_the_ring_is_keyed_as_a_ring(self):
        """
        The old key showed a filled dot for a thing the map draws as an
        outline, so the reader had to work out that the colour they were
        looking for was on the edge of a DC and not in the middle of it.
        """
        js = _asset("js", "twin-legend.js")
        assert "tw-legend-ring" in js
        css = _asset("css", "style.css")
        assert "border: 3px solid currentColor" in css

    def test_the_key_and_the_map_draw_the_same_thing(self):
        """
        THREE PALETTES BETWEEN TWO VIEWS AND A KEY. The legend keyed a plant
        in #5b21b6, a DC in #0ea5e9 and a market in #075985; the 2D map drew
        the same three in #6B2FA0, #2563eb and #0891b2; the 3D twin used a
        third set. A reader matching a chip against a marker was comparing
        two different colours, which is the one thing a key must not ask.
        """
        shared = _without_comments(_asset("js", "twin-legend.js"))
        assert "export const NODE_STYLE" in shared
        for kind in ("plant", "dc", "market"):
            assert f"{kind}:" in shared, kind

        two_d = _without_comments(_asset("js", "map.js"))
        assert "NODE_STYLE.plant.color" in two_d, two_d
        assert "NODE_STYLE.plant.glyph" in two_d, "the marker glyph"
        # And no independent literal left to drift.
        assert "'#6B2FA0'," not in two_d.replace("NODE_STYLE", ""), two_d

        three_d = _without_comments(_asset("js", "twin3d.js"))
        assert "NODE_STYLE.plant.hex3d" in three_d, three_d
        assert "NODE_STYLE.market.hex3d" in three_d, three_d
        assert "0x5b21b6" not in three_d, "the 3D plant hue is a literal again"

    def test_the_dc_colour_is_keyed_as_the_status_it_carries(self):
        """
        A DC's ring (2D) and its body (3D) carry the utilisation band, not its
        identity — so the group that keys those colours has to say so, or a
        reader looks for the DC's blue on a site the map painted red.
        """
        js = _asset("js", "twin-legend.js")
        assert "its ring, and its colour in 3D" in js

    def test_the_key_is_readable(self):
        """It was 10px — two steps below the application's smallest body
        size, on the one thing on the map that explains the picture."""
        css = _asset("css", "style.css")
        rule = css[css.index(".tw-legend {"):]
        rule = rule[:rule.index("}")]
        assert "font-size: 12px" in rule, rule

    def test_nothing_is_keyed_that_is_never_drawn(self):
        """
        The mockup carries a "Constrained route" key. A lane reaches the
        browser as {from, to, cost, distance, leadTime, flow, mode} and
        carries neither its capacity nor a binding-constraint flag, so
        nothing here could draw one — and a reader who never saw a red lane
        would conclude their network has no constrained routes.
        """
        body = _without_comments(_asset("js", "twin-legend.js"))
        assert "Constrained" not in body
        for module in ("map.js", "twin3d.js"):
            js = _without_comments(_asset("js", module))
            assert "constrained" not in js.lower(), module


class TestAFacilityIsNamedOnTheMap:
    def test_the_id_leads_the_name(self):
        js = _without_comments(_asset("js", "twin-legend.js"))
        fn = _fn(js, "export function facilityLabel(node)")
        assert "node.id" in fn and "node.name" in fn, fn
        # Never "F006 · F006" for a network whose sites are named by their id.
        assert "name === id" in fn, fn

    def test_both_views_draw_the_label(self):
        two_d = _without_comments(_asset("js", "map.js"))
        assert "map-node-label" in two_d
        assert "facilityShortLabel(node)" in two_d

        three_d = _without_comments(_asset("js", "twin3d.js"))
        assert "twin3d-node-label" in three_d
        assert "facilityShortLabel(node.data)" in three_d

    def test_the_id_is_in_the_tooltips_too(self):
        two_d = _without_comments(_asset("js", "map.js"))
        assert "facilityLabel(node)" in two_d, "the 2D hover tooltip"
        # And on a corridor's endpoints, which is what the reader's own lane
        # sheet calls those rows.
        assert "facilityLabel(node) : id" in two_d.replace("\n", " ") \
            or "return node ? facilityLabel(node) : id;" in two_d

        # The 3D hover card puts the id on its OWN line rather than in front
        # of the name: `facilityLabel` returns "F006 · Brampton National
        # Distribution Hub", which on a 244px card wrapped to three lines and
        # pushed the type badge onto a fourth.
        three_d = _without_comments(_asset("js", "twin3d.js"))
        hud = _fn(three_d, "function renderHUDTooltip(data, type, x, y)")
        assert "hud-id" in hud, hud
        assert "String(data.id" in hud, hud
        assert "String(data.name" in hud, hud
        # And the full identity is still one hover away on the map label.
        assert "el.title = facilityLabel(node.data)" in three_d

    def test_markets_are_not_labelled(self):
        """
        A network with twenty of them would put twenty more pills over the
        corridors to name the small dots that are already the least ambiguous
        thing on the map. The mockup labels the facilities only, and a
        market's name is still one hover away.
        """
        two_d = _without_comments(_asset("js", "map.js"))
        assert "type !== 'market'" in two_d, two_d

        three_d = _without_comments(_asset("js", "twin3d.js"))
        assert "n.type !== 'market'" in three_d, three_d

    def test_the_pills_stay_off_the_scenario_planners_thumbnail(self):
        """
        `createNodeMarker` is shared with `renderScenarioDigitalTwin`, so
        labelling every marker put twenty-six pills on a card a few hundred
        pixels wide beside a comparison table — a screen this change was not
        asked to touch.
        """
        js = _without_comments(_asset("js", "map.js"))
        assert "showLabels" in js, js
        fn = _fn(js, "function createNodeMarker(node, type, containerId, overrideStats = null)")
        assert "mapOptions[containerId]?.showLabels !== false" in fn, fn

    def test_a_label_never_eats_the_click_that_opens_the_facility(self):
        css = _asset("css", "style.css")
        block = css[css.index(".map-node-label,"):]
        block = block[:block.index("}")]
        assert "pointer-events: none" in block, block


class TestTheLabelsStayReadable:
    def test_the_pill_carries_the_city_rather_than_the_full_name(self):
        """
        "F025 · Regina Global Transportation Hub Candidate DC" is right in a
        tooltip and wrong on a pin: drawn in full, twenty-six of them covered
        the middle of the canvas.
        """
        js = _without_comments(_asset("js", "twin-legend.js"))
        fn = _fn(js, "export function facilityShortLabel(node, maxName = 18)")
        assert "node.city" in fn, fn
        assert "slice(0, maxName - 1)" in fn, fn
        # The id survives the trim: it is the short half and the unique half.
        assert "if (!id) return tail;" in fn, fn

    def test_overlapping_labels_are_dropped_in_both_views(self):
        three_d = _without_comments(_asset("js", "twin3d.js"))
        fn = _fn(three_d, "function updateNodeLabels()")
        assert "placed" in fn and "clash" in fn, fn
        # Nearest first, so the label that survives a crowd is the one in
        # front rather than whichever happened to be built first.
        assert "sort((a, b) => a.z - b.z)" in fn, fn

        two_d = _without_comments(_asset("js", "map.js"))
        fn2 = _fn(two_d, "function declutterMapLabels(containerId)")
        assert "getBoundingClientRect()" in fn2, fn2
        assert "visibility" in fn2, (
            "a display:none label has no box, so the next pass cannot measure it")

    def test_the_2d_pass_runs_when_the_view_or_the_network_changes(self):
        js = _without_comments(_asset("js", "map.js"))
        assert "map.on('moveend zoomend'" in js, (
            "a pan or a zoom is the only thing that alters which pills collide")
        assert js.count("declutterMapLabels(containerId)") >= 3, js

    def test_a_hidden_label_costs_the_reader_nothing_else(self):
        """
        The marker, its tooltip and its click target are untouched: only the
        pill is hidden, and it comes back as soon as there is room.
        """
        js = _without_comments(_asset("js", "map.js"))
        fn = _fn(js, "function declutterMapLabels(containerId)")
        assert ".map-node-label" in fn, fn
        assert "removeLayer" not in fn and "remove()" not in fn, fn


class TestTheFiguresAnswerTheControlsAboveThem:
    def test_the_band_is_scoped_by_facility_and_period(self):
        js = _without_comments(_asset("js", "app.js"))
        fn = _fn(js, "function renderTwinMetrics(containerId = 'twin-metrics')")
        assert "state.selectedFacility" in fn, fn
        assert "state.selectedPeriod" in fn, fn

    def test_it_reads_the_same_accessor_the_kpi_screen_reads(self):
        """
        Two screens reporting different numbers for the same site under the
        same name is the worst failure this application has, because nothing
        about it looks broken.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = _fn(js, "function renderTwinMetrics(containerId = 'twin-metrics')")
        assert "getKpisForFacility(facility.id, periodId)" in fn, fn
        dash = _fn(js, "export function renderFacilityDashboard()")
        assert "getKpisForFacility(state.selectedFacility, state.selectedPeriod)" in dash

    def test_nothing_on_the_band_is_computed_here(self):
        """
        §9. The whole-network case reads the authoritative base case rather
        than summing the facilities, and the facility case reads the solved
        utilisation rather than dividing throughput by capacity — a division
        this file has no business doing, and one that would disagree with the
        engine the moment either figure is on a different basis.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = _fn(js, "function renderTwinMetrics(containerId = 'twin-metrics')")
        assert "getOptimizedBaseCase()" in fn, fn
        assert "/ capacity" not in fn, fn
        assert "reduce(" not in fn, fn

    def test_an_unreported_figure_is_a_dash_carrying_its_reason(self):
        js = _without_comments(_asset("js", "app.js"))
        fn = _fn(js, "function twinMetricHtml(label, value, sub, reason, tone = '')")
        assert "—" in fn, fn
        assert "title=" in fn, fn
        assert "|| 0" not in fn, "an absent figure must never become a zero"

    def test_the_period_note_says_whether_that_period_was_solved(self):
        """
        The control does not re-optimise anything. Some periods carry a
        solved reading of their own and the rest fall back to the horizon
        average, and showing those two identically is how a reader mistakes
        "not modelled" for a finding.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = _fn(js, "function twinPeriodNote(periodId)")
        assert "SOLVE_HORIZON.periodLabels" in fn, fn
        assert "outside the modelled horizon" in fn, fn


class TestTheHoverCardCanBeUsed:
    def test_the_card_takes_the_pointer(self):
        """
        `pointer-events: none` meant the card could never be reached: it was a
        picture of a control, not a control. Its footer read "Click node to
        inspect full diagnostics →" — which looks like a link, is positioned
        like one, and did nothing when clicked.
        """
        css = _asset("css", "style.css")
        rule = css[css.index(".twin3d-node-tooltip {"):]
        rule = rule[:rule.index("}")]
        assert "pointer-events: auto" in rule, rule
        # And gives it back when hidden, or it would eat drags over the canvas.
        hidden = css[css.index(".twin3d-node-tooltip.hidden {"):]
        hidden = hidden[:hidden.index("}")]
        assert "pointer-events: none" in hidden, hidden

    def test_it_is_not_torn_away_on_the_way_to_it(self):
        """
        The card sits above the node, so every route to it crosses a few
        pixels of empty canvas where the raycast misses — and it was hidden
        the instant that happened.
        """
        js = _without_comments(_asset("js", "twin3d.js"))
        fn = _fn(js, "function scheduleHudHide()")
        assert "hudHovered" in fn, fn
        assert "setTimeout" in fn, fn

        hover = _fn(js, "function updateHoverState()")
        assert "scheduleHudHide()" in hover, hover
        assert "cancelHudHide()" in hover, hover

    def test_the_way_into_the_panel_is_a_button(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "data-hud-open" in js
        assert "window.openFacilityPanel(id)" in js
        assert "Click node to inspect" not in js, (
            "the sentence pretending to be a control is back")

    def test_the_badge_is_not_the_colour_of_a_warning(self):
        """
        `.hud-badge.dc` was amber — the utilisation "Stress" colour on the
        very same screen — so a healthy DC carried a warning-coloured chip.
        """
        css = _asset("css", "style.css")
        rule = css[css.index(".hud-badge.dc"):]
        rule = rule[:rule.index("}")]
        assert "#2563eb" in rule, rule
        assert "d97706" not in rule, rule
