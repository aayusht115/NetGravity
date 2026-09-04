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
        html = _asset("index.html")
        legend = html[html.index('class="twin3d-legend"'):]
        legend = legend[:legend.index("</div>\n          </div>")]
        for kind in ("plant", "dc", "market"):
            assert 'data-legend-count="%s"' % kind in legend, kind

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

    def test_the_three_boxes_below_the_map_are_one_height_and_scroll(self):
        css = _without_comments(_asset("css", "style.css"))
        scroller = _rule(css, "#tab-twin .grid-3 > .card > .card-table-scroll {")
        assert "overflow: auto" in scroller
        assert "height: clamp(" in scroller, (
            "a stated height is what makes three boxes with 3, 9 and 12 rows "
            "the same height"
        )
        card = _rule(css, "#tab-twin .grid-3 > .card {")
        assert "display: flex" in card and "flex-direction: column" in card

    def test_the_table_headers_do_not_scroll_away(self):
        css = _without_comments(_asset("css", "style.css"))
        head = _rule(css, "#tab-twin .grid-3 .ng-table thead th {")
        assert "position: sticky" in head

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
        legend = _rule(css, ".twin3d-legend {")
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
        for sel in (".twin3d-stat {", ".twin3d-hint span {", ".twin3d-legend {"):
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

    def test_the_left_column_is_the_overviews_own_two_cards(self):
        """
        Not a copy — the same classes, drawn by the same two functions into
        this page's containers. A second alert card is a second place for the
        same finding to be worded differently.
        """
        fc = self._section()
        assert 'class="ov-alert" id="fc-alert"' in fc
        assert 'class="ov-attn-card"' in fc
        assert 'id="fc-attn-body"' in fc
        assert 'class="ov-signals-card"' in fc

        js = _without_comments(_asset("js", "app.js"))
        assert "function renderOverviewAlert(elId = 'ov-alert')" in js
        assert "function renderHomeAttentionFeed(listId = 'ov-attn-body')" in js
        assert "function renderHomeSignals(rowId = 'ov-signals-row')" in js
        page = js[js.index("function renderForecastPage()"):]
        page = page[:page.index("\n}\n")]
        for call in ("renderOverviewAlert('fc-alert')",
                     "renderHomeAttentionFeed('fc-attn-body')",
                     "renderHomeSignals('fc-signals-row')"):
            assert call in page, call

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
        assert "list.querySelector('.ov-attn-cta')" in js

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
    def test_both_body_grids_are_sized_from_a_measured_offset(self):
        js = _without_comments(_asset("js", "app.js"))
        assert "function sizePageToWindow(selector, varName)" in js
        assert "'--ov-main-top'" in js
        assert "'--fc-main-top'" in js
        assert "requestAnimationFrame(" in js, "coalesced to one per frame"

    def test_the_overview_body_owns_the_first_screen(self):
        css = _without_comments(_asset("css", "home-overview.css"))
        main = _rule(css, ".ov-main {")
        assert "min-height: calc(100vh - var(--ov-main-top" in main, main
        assert "max-height: calc(100vh - var(--ov-main-top" in main, main

    def test_and_the_page_may_grow_past_it_for_the_signals(self):
        css = _without_comments(_asset("css", "home-overview.css"))
        panel = _rule(css, "#tab-home.active {")
        assert "flex: 1 0 auto" in panel, (
            "`flex: 1` pinned Home to exactly one screen, so the body grid "
            "and the signals row had to share it"
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
