"""
One shell, one top bar, one type scale — and two maps that are actually
pointed at the network they draw.

These are the corrections that came back after the Overview rebuild, and they
are all defects of the SHELL rather than of any one screen:

  * **The top bar changed shape between pages.** Facility and Period sat in
    the top bar on Home and one row lower, in the sub-topbar, on every other
    tab. Two positions for one pair of controls, and a reader who had just set
    a facility on Home looking for it in the wrong row on the Digital Twin.

  * **Every page but Home stopped short of the right-hand edge.**
    `.main-content` capped at 1280px and Home alone opted out, so on any wide
    window the other four pages ended in a band of empty page.

  * **The shell sized to its own content.** `.app-shell` was `min-height:
    100vh` with an auto height, `.app-body` inside it was `flex: 1`, and the
    two sized off each other. Home — a page built to fit exactly — came out
    56px too tall, and the Digital Twin ran away completely: a 1140px map
    panel in a 1050px window. Every `height: 100%` inside the shell inherited
    the wrong number.

  * **Scenario Planning's baseline map was never framed.** `initMap` called
    `fitToNetwork` only on the branch that draws a plain network, so the one
    map built through the scenario branch kept the literal `center: [22.5,
    79.5]` it was constructed with. Its nodes and lanes were drawn correctly
    the whole time, several thousand kilometres off the edge of the viewport —
    which reads, exactly, as "the nodes and flows are not working".

Asset-level, for the same reason as the rest of this directory: the defects
live in a stylesheet rule and a missing call, and a browser test would report
"the map looks empty" without naming which of the two.
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


def _rule(css: str, selector: str) -> str:
    """The declarations of one rule, comments stripped."""
    body = _without_comments(css)
    start = body.index(selector)
    open_brace = body.index("{", start)
    return body[open_brace + 1:body.index("}", open_brace)]


class TestTheShellIsExactlyOneWindow:
    """
    A page that fits must not scroll, and a page that does not fit must scroll
    the page area rather than stretch the shell.
    """

    def test_the_shell_has_a_height_not_merely_a_minimum(self):
        css = _asset("css", "style.css")
        shell = _rule(css, ".app-shell {")
        assert "height: 100vh" in shell
        assert "min-height: 100vh" not in shell, (
            "a minimum with an auto height is what let a tall page push the "
            "whole shell taller"
        )

    def test_the_body_row_fills_that_and_does_not_scroll(self):
        css = _asset("css", "style.css")
        body = _rule(css, ".app-body {")
        assert "flex: 1" in body
        assert "min-height: 0" in body
        assert "overflow: hidden" in body

    def test_the_page_area_is_the_scroll_container(self):
        css = _asset("css", "style.css")
        main = _rule(css, ".main-content {")
        assert "overflow-y: auto" in main
        assert "min-height: 0" in main

    def test_going_to_a_tab_scrolls_the_page_area_not_the_window(self):
        """
        `window.scrollTo` is a no-op once the window is not what scrolls, and
        a reader arriving on a new tab would land wherever the last one had
        been scrolled to.
        """
        js = _without_comments(_asset("js", "app.js"))
        assert "function scrollPageToTop()" in js
        assert "window.scrollTo({ top: 0" not in js, (
            "a tab route still scrolls the window"
        )
        fn = js[js.index("function scrollPageToTop()"):]
        fn = fn[:fn.index("\n}\n")]
        assert ".main-content" in fn


class TestEveryPageIsTheSameWidth:
    def test_no_page_is_capped_short_of_the_edge(self):
        css = _asset("css", "style.css")
        main = _rule(css, ".main-content {")
        assert "max-width: none" in main
        assert "max-width: 1280px" not in main, (
            "the cap that left a band of empty page on every tab but Home"
        )

    def test_a_tab_panel_can_reach_the_bottom_of_the_shell(self):
        css = _asset("css", "style.css")
        main = _rule(css, ".main-content {")
        assert "flex-direction: column" in main


class TestOneTopBarOnEveryScreen:
    def test_the_scope_pair_is_declared_once_in_the_top_bar(self):
        html = _asset("index.html")
        topbar = html[html.index('class="app-global-topbar"'):]
        topbar = topbar[:topbar.index("</header>")]
        for control in ('id="project-select-btn"', 'id="home-top-facility"',
                        'id="home-top-period"', 'id="btn-topbar-upload"'):
            assert control in topbar, control

    def test_the_second_pair_in_the_sub_topbar_is_never_shown(self):
        js = _without_comments(_asset("js", "app.js"))
        tail = js[js.index("const controls = document.getElementById('topbar-controls');"):]
        tail = tail[:tail.index("}") + 1]
        assert "controls.style.display = 'none'" in tail, tail

    def test_the_page_title_is_drawn_at_homes_own_scale(self):
        """
        Home's `.ov-title` is the reference — 22px / 800 / -0.025em — so a
        reader moving between tabs sees one heading scale, not a 21px one here
        and a 22px one there.
        """
        home_css = _asset("css", "home-overview.css")
        title = _rule(home_css, ".ov-title {")
        assert "font-size: 22px" in title
        assert "font-weight: 800" in title

        css = _asset("css", "style.css")
        sub = _rule(css, ".sub-topbar-main-title,")
        assert "font-size: 22px" in sub
        assert "font-weight: 800" in sub

        strapline = _rule(home_css, ".ov-subtitle {")
        assert "font-size: 13px" in strapline
        assert "font-size: 13px" in _rule(css, ".sub-topbar-sub-title,")

    def test_the_title_row_paints_above_the_panel_that_overlaps_it(self):
        """
        Every tab panel is pulled up by a negative margin so its background
        bleeds to the edges of the shell. That also pulls it over the bottom
        of this row, and being later in the document it won — the bottom of
        "Digital Twin" was painted over.
        """
        css = _asset("css", "style.css")
        bar = _rule(css, ".app-sub-topbar {")
        assert "position: relative" in bar
        assert "z-index: 1" in bar


class TestTheDigitalTwinPageIsOneScreen:
    def test_the_legend_states_how_many_of_each_node_there_are(self):
        """
        The legend is no longer written out in the markup: both views build it
        from `twinLegendHtml()`, so the counts are asserted where they are now
        written. See `test_one_legend_serves_both_views`.
        """
        js = _without_comments(_asset("js", "twin-legend.js"))
        for kind in ("plant", "dc", "market"):
            assert "facilityRow('%s'" % kind in js, kind
        assert 'data-legend-count="${kind}"' in js, js

    def test_one_legend_serves_both_views(self):
        """
        There were two: `.twin3d-legend` written out in index.html, and a
        block of inline styles inside map.js's `addLegend`. Two hand-kept
        copies of one key, on a page whose two views the reader switches
        between with a single button — and they had already drifted, the 2D
        one keying the DC ring in #dc2626/#f59e0b/#22c55e and the 3D one
        keying the identical bands in #b91c1c/#b45309/#047857.
        """
        html = _asset("index.html")
        assert 'id="twin3d-legend"' in html
        assert 'class="twin3d-legend"' not in html, (
            "the 3D legend is hand-written in the markup again")

        js = _without_comments(_asset("js", "twin-legend.js"))
        assert "export function twinLegendHtml" in js
        for consumer in ("map.js", "app.js"):
            assert "twinLegendHtml" in _without_comments(_asset("js", consumer)), consumer

    def test_the_legend_keys_the_line_weight_it_actually_draws(self):
        """
        Both views sized a corridor by dividing its flow by a constant —
        `flow/1500` in 2D and `flow/4000` in 3D — so the same corridor was
        drawn at two different weights, and neither legend mentioned line
        weight at all. One set of bands now decides both, and the legend
        prints the same boundaries.
        """
        js = _without_comments(_asset("js", "twin-legend.js"))
        assert "export function flowBands" in js
        assert "weight2d" in js and "radius3d" in js

        for consumer, field in (("map.js", "weight2d"), ("twin3d.js", "radius3d")):
            text = _without_comments(_asset("js", consumer))
            assert "bandForFlow" in text, consumer
            assert field in text, (consumer, field)
            assert "/ 1500" not in text, consumer
            assert "/ 4000" not in text, consumer

    def test_the_legend_claims_nothing_the_map_does_not_draw(self):
        """
        The mockup carries a "Constrained route" key as a red dashed line.
        Nothing here knows which corridors are constrained: a lane reaches the
        browser as {from, to, cost, distance, leadTime, flow, mode} and
        carries neither its capacity nor a binding-constraint flag. A key for
        it would describe a line the map never draws, and a reader who never
        saw a red lane would conclude their network has none — a finding
        nothing established.
        """
        js = _asset("js", "twin-legend.js")
        body = _without_comments(js)
        assert "Constrained" not in body, body
        # And the reason is written down where the next person will look.
        assert "Constrained route" in js, (
            "the omission is deliberate and must say why")

    def test_both_maps_count_from_the_same_place(self):
        """
        The 3D legend and the Leaflet one carry `data-legend-count` rows and
        one function fills them, so "how many DCs" has a single answer and the
        two views cannot disagree.
        """
        js = _without_comments(_asset("js", "map.js"))
        assert "export function renderMapLegendCounts()" in js
        fn = js[js.index("export function renderMapLegendCounts()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "PLANTS.length" in fn and "DCS.length" in fn and "MARKETS.length" in fn
        # And the Leaflet legend uses the same attribute.
        assert 'data-legend-count="${kind}"' in js

    def test_the_counts_are_refreshed_when_the_network_changes(self):
        js = _without_comments(_asset("js", "map.js"))
        assert js.count("renderMapLegendCounts();") >= 3, (
            "a legend filled once, at init, reports the network as it was at "
            "boot: zero"
        )
        app_js = _without_comments(_asset("js", "app.js"))
        assert "renderMapLegendCounts();" in app_js

    def test_the_three_asset_tables_are_gone(self):
        """
        Plants, Distribution Centres and Demand Markets: three cards listing
        every node in the network, each scrolling inside a stated height so
        the row stayed level.

        They ignored the Facility and Period controls at the top of the screen
        completely — a reader who selected one DC got the same three full
        tables — and every figure in them is already in the node's own tooltip
        and on the KPI screen.
        """
        html = _asset("index.html")
        twin = html[html.index('id="tab-twin"'):html.index('id="tab-forecast"')]
        for gone in ('id="table-plants"', 'id="table-dcs"', 'id="table-markets"',
                     'class="grid-3'):
            assert gone not in twin, gone

    def test_what_replaced_them_answers_the_controls_at_the_top(self):
        """
        One band, about the site the reader has selected, in the period they
        have selected — which is the question those two controls ask and the
        one the page could not previously answer.
        """
        html = _asset("index.html")
        assert 'id="twin-metrics"' in html

        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function renderTwinMetrics("):]
        fn = fn[:fn.index("\n}\n")]
        assert "state.selectedFacility" in fn, fn
        assert "state.selectedPeriod" in fn, fn
        # Read through the accessor the KPI screen uses, so the two screens
        # cannot report different numbers for the same site.
        assert "getKpisForFacility(facility.id, periodId)" in fn, fn
        # And nothing is computed here (§9): the whole-network case reads the
        # authoritative base case rather than summing the facilities.
        assert "getOptimizedBaseCase()" in fn, fn

    def test_the_band_is_redrawn_when_the_selection_moves(self):
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function renderForSelection()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "renderTwinTables()" in fn, fn
        # `renderTwinTables` keeps its name — projects.js and the hydration
        # path already call it — and draws the band.
        block = js[js.index("function renderTwinTables()"):]
        block = block[:block.index("\n}\n")]
        assert "renderTwinMetrics()" in block, block

    def test_a_figure_the_solve_did_not_report_is_a_dash_not_a_zero(self):
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function twinMetricHtml("):]
        fn = fn[:fn.index("\n}\n")]
        assert "'\u2014'" in fn or "—" in fn, fn
        assert "title=" in fn, "a dash must carry the reason it is a dash"

    def test_the_map_card_has_a_stated_height(self):
        """
        `flex: 1 1 auto` on the panel with `height: 100%` on the WebGL
        container below it fed a loop: no definite height meant the canvas
        fell back to its own drawing buffer, that became the panel's content
        height, the panel grew to it, and the next resize set a bigger buffer.
        It settled at a 1361px map in a 1050px window. A number breaks it.
        """
        css = _without_comments(_asset("css", "style.css"))
        panel = _rule(css, "#tab-twin .twin-view-panel {")
        assert "flex: 0 0 auto" in panel, panel
        assert "height: clamp(" in panel, panel

        inner = _rule(css, "#tab-twin .twin-view-panel .twin3d-container,")
        assert "height: 100%" in inner
        assert "min-height: 0" in inner, (
            "a min-height as tall as the panel is the loop again"
        )

    def test_the_page_grows_for_a_taller_map_rather_than_squeezing_it(self):
        css = _without_comments(_asset("css", "style.css"))
        panel = _rule(css, "#tab-twin.active {")
        assert "flex: 1 0 auto" in panel, panel
        assert "display: flex" in panel and "flex-direction: column" in panel

    def test_the_legend_is_readable(self):
        """It was 10px — two steps below the application's smallest body size,
        on the one thing on the map that explains the picture."""
        css = _without_comments(_asset("css", "style.css"))
        legend = _rule(css, ".tw-legend {")
        assert "font-size: 12px" in legend, legend
        assert "backdrop-filter" not in legend, (
            "a backdrop filter over a WebGL canvas is a blur snapshot per paint"
        )

    def test_no_overlay_on_the_map_costs_a_backdrop_snapshot(self):
        """
        `backdrop-filter` makes the compositor snapshot and blur what is
        behind the element on every paint. These three sit on top of a WebGL
        canvas, so every scroll frame paid for them — and over an opaque map
        the blur was not visible in the first place.
        """
        css = _without_comments(_asset("css", "style.css"))
        for sel in (".twin3d-stat {", ".twin3d-hint span {", ".tw-legend {"):
            assert "backdrop-filter" not in _rule(css, sel), sel

    def test_the_three_figures_are_in_one_corner_on_both_views(self):
        css = _without_comments(_asset("css", "style.css"))
        overlay = _rule(css, ".twin3d-stats-overlay {")
        assert "top: 16px" in overlay and "left: 16px" in overlay
        html = _asset("index.html")
        block = html[html.index('id="map2d-stats"'):]
        block = block[:block.index(">") + 1]
        assert "right:16px" not in block, (
            "the 2D copy is overriding the shared position again: " + block
        )

    def test_the_zoom_control_is_not_under_them(self):
        js = _without_comments(_asset("js", "map.js"))
        assert "zoomControl: false" in js
        assert "L.control.zoom({ position: 'bottomleft' })" in js


class TestTheTwinStopsRenderingWhenNobodyIsLooking:
    """
    The scroll lag. `animate()` re-armed itself with `requestAnimationFrame`
    and `disposeTwin3D` — the only thing that cancelled it — was exported and
    never called, so a full WebGL draw plus a raycast over every node ran for
    the rest of the session on pages that do not show the canvas.
    """

    def test_the_loop_is_gated_on_the_canvas_being_visible(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "function watchVisibility()" in js
        fn = js[js.index("function watchVisibility()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "IntersectionObserver" in fn
        assert "io.observe(renderer.domElement)" in fn, (
            "the observed element must be the canvas: the container it sits "
            "in changes as the scene is re-parented between Home and this tab"
        )
        assert "visibilitychange" in fn, "a background tab reports no intersection change"

    def test_it_is_watched_from_the_first_frame(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        init = js[js.index("export function initTwin3D("):]
        init = init[:init.index("\n}\n")]
        assert "watchVisibility();" in init, init

    def test_the_raycast_only_runs_when_something_moved(self):
        """
        A raycast against every node's hit mesh, on a still scene under a
        still cursor, can only return the answer it returned last frame.
        """
        js = _without_comments(_asset("js", "twin3d.js"))
        animate = js[js.index("function animate()"):]
        animate = animate[:animate.index("\n}\n")]
        assert "if (pointerDirty)" in animate, animate
        # And both things that can change the answer set the flag.
        assert "pointerDirty = true;" in js
        assert js.count("pointerDirty = true;") >= 2, (
            "turning the scene moves the nodes under a stationary cursor"
        )

    def test_the_camera_frames_the_country(self):
        """
        SUPERSEDED, deliberately.

        This asserted the opposite — that the camera framed the node cluster
        and let the ground sheet run off the edges — and it was right while
        the sheet was the network's bounding box padded 12%: fitting a
        rectangle shaped like nothing in particular into a card shaped like
        nothing in particular left the network as a thin band.

        `networkWindow` now returns the whole COUNTRY (the instruction was
        that the twin must show the entire country, not the part the nodes are
        in), so the sheet is a shape the reader recognises and is the thing
        they are meant to be looking at. Framing on the sites inside it would
        crop the country back off the screen — exactly what the wider window
        exists to stop. The nodes cannot be framed out: the window is the
        union of the country outline and the sites.
        """
        js = _without_comments(_asset("js", "twin3d.js"))
        fn = js[js.index("function frameCameraToPlane()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "PROJECTION.width" in fn, fn
        assert "nodeMeshes.length" not in fn, (
            "the camera is framing the sites again, which crops the country")

    def test_the_window_covers_the_whole_country(self):
        """
        One window, and it is the country's.

        Both views project onto `networkWindow`, so this is the single place
        that decides whether the twin shows a country or a crop of one.
        """
        js = _asset("js", "world-basemap.js")
        assert "export function countryWindow(" in js
        assert "export function networkCountryLabel(" in js
        fn = js[js.index("export function networkWindow("):]
        fn = fn[:fn.index("\n}\n")]
        # The country box is unioned in, never substituted: a site just off a
        # coastline has to stay inside the frame.
        assert "countryWindow(pts)" in fn, fn
        for line in ("Math.min(latMin, country.latMin)",
                     "Math.max(latMax, country.latMax)",
                     "Math.min(lngMin, country.lngMin)",
                     "Math.max(lngMax, country.lngMax)"):
            assert line in fn, line

    def test_the_country_is_named_under_the_stats(self):
        """
        The map paints the network's countries white; this says which they are
        in words, for the reader who does not recognise the coastline.
        """
        html = _asset("index.html")
        for stats, wrap, name in (("map2d-stats", "map2d-country", "map2d-country-name"),
                                  ("twin3d-stats", "twin3d-country", "twin3d-country-name")):
            assert f'id="{wrap}"' in html, wrap
            assert f'id="{name}"' in html, name
            # Inside the stats overlay and AFTER the last stat card, so it
            # wraps onto the line under them rather than sitting at a
            # hand-measured offset that the cards could grow past.
            block = html[html.index(f'id="{stats}"'):]
            block = block[:block.index(f'id="{wrap}"')]
            assert "twin3d-stat-label" in block, (
                f"{wrap} is not inside the {stats} overlay")
            assert block.count("twin3d-stat-label") == 3, (
                f"{wrap} does not follow all three stat cards")

        css = _asset("css", "style.css")
        assert "flex-wrap: wrap;" in _rule(css, ".twin3d-stats-overlay {")
        assert "flex: 0 0 100%;" in _rule(css, ".twin3d-country {")
        # `hidden` loses to a class rule with an explicit display, so the
        # caption needs its own.
        assert ".twin3d-country[hidden] { display: none; }" in css

        js = _asset("js", "app.js")
        assert "networkCountryLabel" in js
        # Named from the geometry, never from a stored label that could
        # disagree with the land drawn underneath it.
        assert "networkCountryLabel([...PLANTS, ...DCS, ...MARKETS])" in js


class TestTheForecastScreenIsTheMockup:
    """Dump/Demand forecast.png."""

    @staticmethod
    def _section():
        html = _asset("index.html")
        start = html.index('id="tab-forecast"')
        return html[start:html.index("</section>", start)]

    def test_it_has_the_mockups_two_rows(self):
        fc = self._section()
        order = [fc.index(c) for c in
                 ('class="fc-main"', 'class="fc-chart-card"', 'id="fc-signals-card"')]
        assert order == sorted(order), order

    def test_the_shared_cards_are_still_one_renderer_each(self):
        """
        The alert and the signals row ARE the Overview's own, drawn by the same
        function into this page's containers. A second alert card would be a
        second place for the same finding to be worded differently, and that
        has not changed.
        """
        fc = self._section()
        assert 'class="ov-alert" id="fc-alert"' in fc
        assert 'class="ov-attn-card"' in fc
        assert 'id="fc-attn-body"' in fc
        assert 'class="ov-signals-card"' in fc

        js = _without_comments(_asset("js", "app.js"))
        # The default follows the caller. Home's own full alert is gone — it
        # keeps only an error-only notice, `#ov-notice` — so a default naming
        # `#ov-alert` would make every bare call a silent no-op, including
        # ingestion.js's, which is how a shortfall notice reaches a screen.
        assert "function renderOverviewAlert(elId = 'fc-alert'" in js
        # Same rule for the signals row: this renderer's only caller is the
        # Forecast tab, and a default naming an element that no longer exists
        # is a trap for the next reader.
        assert "function renderHomeSignals(rowId = 'fc-signals-row')" in js
        page = js[js.index("function renderForecastPage()"):]
        page = page[:page.index("\n}\n")]
        for call in ("renderOverviewAlert('fc-alert')",
                     "renderHomeSignals('fc-signals-row')"):
            assert call in page, call

    def test_the_attention_card_is_about_the_forecast_not_the_network(self):
        """
        It used to be `renderHomeAttentionFeed('fc-attn-body')` — the same
        NETWORK-scoped finding the Overview shows, in a second container. Every
        word of it was correct and it was the answer to the Overview's
        question, printed under the Forecast's.

        Reported as: "what needs your attention card is repeated, which does
        not make any sense."
        """
        js = _without_comments(_asset("js", "app.js"))
        page = js[js.index("function renderForecastPage()"):]
        page = page[:page.index("\n}\n")]
        assert "renderForecastAttention('fc-attn-body')" in page
        assert "renderHomeAttentionFeed" not in page, (
            "the Forecast screen must not draw the Overview's attention feed")
        # And the feed itself is gone from the build entirely: the Overview
        # replaced it with three insight tiles, which left this renderer with
        # no caller at all. A dead renderer still aimed at a LIVE container is
        # how the defect above comes back.
        assert "function renderHomeAttentionFeed" not in js, (
            "the Overview's attention feed renderer is still in app.js")

    def test_the_forecast_card_reads_the_forecasts_own_briefing(self):
        js = _without_comments(_asset("js", "app.js"))
        block = js[js.index("function renderForecastAttention("):]
        block = block[:block.index("\n}\n")]
        assert "FORECAST_BRIEFING.explanation" in block
        assert "FORECAST_BRIEFING.outlook" in block
        # And says so when there is nothing to read, rather than falling back
        # to the network's briefing — which is what it did before.
        assert "No forecast has been produced" in block

    def test_it_offers_the_scenario_the_forecast_recommends(self):
        """
        A forecast briefing ending "monitor demand" has told a planner nothing
        they can act on. This application can test the network against the
        demand the forecast projects, at the rate it projects.
        """
        js = _without_comments(_asset("js", "app.js"))
        block = js[js.index("function forecastActions(outlook)"):]
        block = block[:block.index("\n}\n")]
        assert "outlook.growth_pct" in block
        assert "openScenarioFromForecast(" in block
        # Gated on there being a real movement to test.
        assert "Math.abs(growth) >= 1" in block

    def test_nothing_is_submitted_on_the_readers_behalf(self):
        js = _without_comments(_asset("js", "app.js"))
        block = js[js.index("function openScenarioFromForecast("):]
        block = block[:block.index("\n}\n")]
        assert "openScenarioBuilderWith" in block
        for forbidden in ("runScenarioCreation", "btn-run-toolbox-scenario",
                          "simulate"):
            assert forbidden not in block, forbidden

    def test_two_cards_on_two_pages_cannot_share_an_id(self):
        """
        The alert's link and the attention card's call to action were
        addressed by id. With the same card on Home and here, the second one
        would be wired to the first one's button.
        """
        js = _asset("js", "app.js")
        assert 'id="ov-alert-link"' not in js
        assert 'id="ov-run-scenario"' not in js
        assert "el.querySelector('.ov-alert-link')" in js
        # The attention card's call to action was the other one addressed by
        # class for this reason. That card is gone; the Overview's tiles are
        # wired the same way — queried within the tile they belong to, never
        # by an id that a second copy on another page would answer to.
        assert "tile.querySelector('.ov-tile-cta')" in js
        assert 'id="ov-tile-cta"' not in js

    def test_the_chart_title_is_the_series_picker(self):
        """
        The mockup's title names one market-product pair. The engine forecasts
        every pair it has history for — 60 on the test workbook — so the title
        is the control that chooses which, rather than a label with the other
        59 hidden behind a menu.
        """
        fc = self._section()
        assert 'class="fc-chart-title"' in fc
        title = fc[fc.index('class="fc-chart-title"'):fc.index('fc-chart-actions')]
        assert 'id="fc-series-select"' in title, title
        css = _without_comments(_asset("css", "style.css"))
        assert "flex-wrap: nowrap" in _rule(css, ".fc-chart-title {"), (
            "a wrapping title puts \"Demand —\" on a line of its own"
        )

    def test_the_forecast_summary_survived_as_the_methodology_panel(self):
        """
        Every figure the old "Forecast Summary" card listed is still on the
        page, behind the button the mockup puts in the header.
        """
        fc = self._section()
        for field in ('id="fc-model"', 'id="fc-horizon"', 'id="fc-accuracy"',
                      'id="fc-series"', 'id="fc-periods"', 'id="fc-series-count"',
                      'id="fc-chart-tag"', 'id="fc-chart-subtitle"'):
            assert field in fc, field
        assert 'id="fc-methodology-btn"' in fc

    def test_the_detailed_signal_cards_survived_too(self):
        """
        The compact three-card row is the mockup's summary; the rationale,
        geography, direction, magnitude and confidence the upload carried are
        what "View all signals" opens.
        """
        fc = self._section()
        assert 'id="external-signals"' in fc
        assert 'id="fc-view-all-signals"' in fc

    def test_every_control_in_the_header_does_something(self):
        js = _without_comments(_asset("js", "app.js"))
        wire = js[js.index("function wireForecastPage()"):]
        wire = wire[:wire.index("\n}\n")]
        for handler in ("fc-methodology-btn", "fc-more-btn", "fc-menu-methodology",
                        "fc-menu-download", "fc-menu-signals", "fc-view-all-signals",
                        "fc-refresh-btn"):
            assert handler in wire, handler
        assert "e.key !== 'Escape'" in wire, "a menu that cannot be dismissed is a trap"

    def test_the_download_is_the_engines_own_numbers(self):
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function downloadForecastSeriesCsv()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "'period', 'observed', 'forecast_mean', 'forecast_p10', 'forecast_p90'" in fn
        assert "Number.isFinite(v)) ? String(v) : ''" in fn, (
            "a missing value must be blank, never a zero"
        )

    def test_no_forecast_recommendation_is_invented(self):
        """
        The reasoning agent has no FORECAST scope and /api/forecast reports
        `llm_used: false`. The card therefore shows the agent's NETWORK
        recommendation, and says so, rather than a forecast-shaped sentence
        nothing produced.
        """
        js = _asset("js", "app.js")
        # The scope the recommendation belongs to is stated where the page is
        # drawn, so the next person to read it knows what they are showing.
        doc = js[:js.index("function renderForecastPage()")]
        doc = doc[doc.rindex("/**"):]
        assert "no FORECAST scope" in doc, doc[-600:]
        assert "llm_used: false" in doc

        # And nothing on this page composes a recommendation of its own: the
        # card is drawn by the Overview's renderer, off the agent's own text.
        page = js[js.index("function renderForecastPage()"):]
        page = page[:page.index("function renderForecastAxisNote")]
        assert "NETWORK_RECOMMENDATION" not in page

        html = _asset("index.html")
        fc = html[html.index('id="tab-forecast"'):]
        fc = fc[:fc.index("</section>")]
        assert "reasoning agent has no FORECAST scope" in html[:html.index('id="tab-forecast"')]             or "FORECAST scope" in html[max(0, html.index('id="tab-forecast"') - 1600):
                                        html.index('id="tab-forecast"')], (
            "the markup should say the same thing the code does"
        )

    def test_the_axis_is_not_under_the_floating_chat_button(self):
        css = _without_comments(_asset("css", "style.css"))
        assert "--ng-fab-clearance" in css
        main = _rule(css, ".fc-main {")
        assert "--ng-fab-clearance" in main, main
        assert "max-height: calc(100vh" in main, (
            "unbounded, the row grows to the findings list and pushes the "
            "x-axis back under the button"
        )

    def test_the_y_axis_cannot_print_the_same_label_twice(self):
        """
        `toFixed(0)` on a 500-unit step printed "8K" for both 8,000 and 8,500
        — two identical labels one gridline apart.
        """
        js = _without_comments(_asset("js", "charts.js"))
        assert "callback: v => (v / 1000).toFixed(0) + 'K'" not in js
        assert "Math.abs(k) >= 10" in js

    def test_the_forecast_boundary_is_drawn_and_named(self):
        js = _without_comments(_asset("js", "charts.js"))
        assert "ngForecastAnnotations" in js
        assert "'Forecast starts'" in js
        assert "splitIndex: histLabels.length - 1" in js

    def test_the_key_never_names_a_line_that_is_not_drawn(self):
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function renderForecastCapacityKey()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "Number.isFinite(cap)" in fn
        css = _without_comments(_asset("css", "style.css"))
        assert ".fc-legend-item[hidden] { display: none; }" in css, (
            "`hidden` loses to the class's own display, so the key stayed on "
            "screen for a chart with no capacity line"
        )


class TestTheFirstScreenIsMeasuredNotGuessed:
    def test_the_forecast_body_grid_is_sized_from_a_measured_offset(self):
        """
        SUPERSEDED: this covered BOTH body grids. The Overview does not have
        one any more — with the twin gone it is four rows that flow, and a
        page that flows needs no measurement. The Forecast page's is
        unchanged, and the helper is still shared.
        """
        js = _without_comments(_asset("js", "app.js"))
        assert "function sizePageToWindow(selector, varName)" in js
        assert "'--fc-main-top'" in js
        assert "setProperty('--ov-main-top'" not in js, (
            "a retired grid offset is being written again")
        assert "requestAnimationFrame(" in js, "coalesced to one per frame"

    def test_the_overview_grid_is_gone_rather_than_left_sized(self):
        css = _without_comments(_asset("css", "home-overview.css"))
        assert ".ov-main {" not in css, css[:200]
        assert "var(--ov-main-top" not in css, (
            "the stylesheet still reads a measured offset nothing writes")

    def test_and_the_page_may_grow_past_the_first_screen(self):
        css = _without_comments(_asset("css", "home-overview.css"))
        panel = _rule(css, "#tab-home.active {")
        assert "flex: 1 0 auto" in panel, (
            "`flex: 1` pins Home to exactly one screen, so every row has to "
            "compete with every other for it"
        )



class TestTheScenarioMapIsPointedAtItsNetwork:
    def test_every_map_is_framed_after_it_is_drawn(self):
        """
        `fitToNetwork` used to live inside the `else` of the scenario branch,
        so the one map built through the other branch was never framed at all.
        """
        js = _without_comments(_asset("js", "map.js"))
        init = js[js.index("export function initMap("):]
        init = init[:init.index("\n}\n")]
        branch = init[init.index("if (options.initialScenario)"):]
        assert branch.count("fitToNetwork(containerId);") == 1, branch
        # ...and it is AFTER the if/else, not inside either arm.
        assert branch.index("fitToNetwork(containerId);") > branch.index("} else {")
        assert branch.index("fitToNetwork(containerId);") > branch.index("renderNetwork(")

    def test_a_network_refresh_reaches_the_scenario_map_too(self):
        """
        It used to `return` on this container outright, so after an upload the
        scenario map kept the basemap and the viewport of whatever had been
        loaded before it.
        """
        js = _without_comments(_asset("js", "map.js"))
        fn = js[js.index("export function refreshAllMaps()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "if (id === 'scenario-leaflet-map') return;" not in fn, fn
        assert "rebuildBaseLayer(id);" in fn
        assert "fitToNetwork(id);" in fn
        # Its NODES still belong to the selected scenario, not to the baseline.
        assert "if (id !== 'scenario-leaflet-map') {" in fn

    def test_the_map_re_measures_and_re_frames_when_the_page_is_shown(self):
        """
        `initScenarios()` runs at app boot, when the panel is `display: none`
        and the container is 0x0 — and a map framed at that size resolves to
        the minimum zoom, which is the whole planet.
        """
        js = _without_comments(_asset("js", "scenarios.js"))
        fn = js[js.index("function updateScenarioMap()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "invalidateMapSize('scenario-leaflet-map');" in fn
        assert "revealMap('scenario-leaflet-map')" in fn, (
            "invalidateSize corrects the viewport and leaves the zoom where "
            "it was"
        )

    def test_switching_scenario_redraws_from_that_scenarios_own_solve(self):
        js = _without_comments(_asset("js", "scenarios.js"))
        fn = js[js.index("function updateScenarioMap()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "mapActiveId === BASELINE_SCENARIO_ID ? 'baseline' : 'scenario'" in fn
        assert "renderScenarioDigitalTwin('scenario-leaflet-map', mapActiveId, mode);" in fn

    def test_the_map_is_tall_enough_to_frame_a_wide_network(self):
        """
        `fitBounds` fits in BOTH dimensions, so the short one sets the zoom: a
        1368x358 box framed the continental United States by its height and
        filled the spare width with the Atlantic.
        """
        css = _without_comments(_asset("css", "style.css"))
        wrap = _rule(css, ".scn-map-wrap {")
        assert "clamp(" in wrap and "height:" in wrap
        assert "height: 360px;" not in wrap


class TestTheTwinCameraFollowsItsContainer:
    def test_the_fit_is_measured_rather_than_hard_coded(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "function frameCameraToPlane()" in js
        fn = js[js.index("function frameCameraToPlane()"):]
        fn = fn[:fn.index("\n}\n")]
        assert ".project(camera)" in fn, (
            "the fit must be read from the projected corners, not assumed"
        )
        assert "PROJECTION.width" in fn and "PROJECTION.height" in fn

    def test_a_view_the_reader_set_is_never_overwritten(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "cameraIsUsers = true;" in js
        resize = js[js.index("export function resizeTwin3D()"):]
        resize = resize[:resize.index("\n}\n")]
        assert "if (!cameraIsUsers) frameCameraToPlane();" in resize, resize

    def test_a_new_network_starts_from_a_fresh_frame(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        fn = js[js.index("export function rebuildTwin3D()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "cameraIsUsers = false;" in fn, fn

    def test_the_scene_still_does_not_turn_on_its_own(self):
        """The standing rule from the last round, re-checked here."""
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "controls.autoRotate = false;" in js
        assert "controls.enableDamping = false;" in js
