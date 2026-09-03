"""
Home Overview, and the geography both maps stand on.

Two things are held here.

  * **The Overview page's shape.** The mockup (`Dump/home overview.png`) puts
    the three headline KPIs in the same band as the alert, above the fold. They
    used to sit in a strip BELOW both columns — off the bottom of a 1050px
    window, under a 712px map, with the floating chat button on top of them —
    so the three numbers the page exists to show were the three you had to
    scroll to reach.

  * **One set of coordinates for both maps.** The 2D map and the 3D twin each
    used to carry their own basemap: a raster photograph of India, applied only
    when the network happened to sit inside 4-39N / 65-100E. Anywhere else the
    2D map fell back to a bare graticule and the 3D plane to blank white, so a
    US network's twelve facilities floated over nothing while the counters
    beside them correctly reported 24 nodes and 51 corridors. Both now read
    `js/world-basemap.js`, and the 3D ground plane is the 2D map's own country
    rings projected through the same Mercator maths onto the same window.

These are asset-level checks because that is where the defects live: the
geometry is bundled JavaScript, and a browser test would report "the map looks
different" without naming which of the two views had drifted.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

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


class TestTheBundledGeographyIsUsable:
    """
    177 country polygons, bundled, public domain, no key and no network. If
    this file is wrong every map in the product is wrong with it.
    """

    @staticmethod
    def _feature_collection():
        src = _asset("js", "world-basemap.js")
        start = src.index("export const WORLD_COUNTRIES = ") + len("export const WORLD_COUNTRIES = ")
        end = src.index(";\n", start)
        return json.loads(src[start:end])

    def test_every_country_is_present_and_closed(self):
        gj = self._feature_collection()
        assert gj["type"] == "FeatureCollection"
        assert len(gj["features"]) == 177, len(gj["features"])
        for f in gj["features"]:
            assert f["geometry"]["type"] == "MultiPolygon", f["properties"]
            for poly in f["geometry"]["coordinates"]:
                for ring in poly:
                    assert len(ring) >= 4, f["properties"]["name"]
                    assert ring[0] == ring[-1], (
                        f"{f['properties']['name']} has an unclosed ring — "
                        "an open ring triangulates into a torn ground plane"
                    )

    def test_coordinates_are_lng_lat_and_on_the_planet(self):
        """
        GeoJSON order, which is the REVERSE of Leaflet's own [lat, lng]. A
        swapped pair puts every coastline on its side and is invisible in a
        diff; it is not invisible here.
        """
        gj = self._feature_collection()
        for f in gj["features"]:
            for poly in f["geometry"]["coordinates"]:
                for ring in poly:
                    for lng, lat in ring:
                        assert -180.0 <= lng <= 180.0, (f["properties"]["name"], lng)
                        assert -90.0 <= lat <= 90.0, (f["properties"]["name"], lat)

    def test_a_few_countries_are_where_they_belong(self):
        """A spot check that survives a re-generation of the file."""
        gj = self._feature_collection()
        by_name = {f["properties"]["name"]: f for f in gj["features"]}
        for name in ("United States of America", "India", "Brazil", "Australia"):
            assert name in by_name, sorted(by_name)[:12]

        def bbox(feature):
            xs, ys = [], []
            for poly in feature["geometry"]["coordinates"]:
                for ring in poly:
                    for x, y in ring:
                        xs.append(x)
                        ys.append(y)
            return min(xs), min(ys), max(xs), max(ys)

        # India: roughly 68-98E, 6-36N.
        x0, y0, x1, y1 = bbox(by_name["India"])
        assert 60 < x0 < 75 and 90 < x1 < 100, (x0, x1)
        assert 5 < y0 < 10 and 32 < y1 < 38, (y0, y1)

    def test_it_carries_no_licence_obligation_we_are_not_meeting(self):
        """
        Natural Earth is public domain — no attribution required. The file says
        where it came from anyway, because a bundled dataset with no stated
        provenance is a dataset nobody can re-derive.
        """
        src = _asset("js", "world-basemap.js")
        assert "Natural Earth" in src
        assert "PUBLIC" in src.upper()


class TestBothMapsStandOnTheSameGeometry:
    """
    "The 2D map becomes the guide for the 3D map" has to be true in the code,
    not just in a comment: one geometry source, one framing rule.
    """

    def test_the_2d_map_draws_the_bundled_countries(self):
        js = _without_comments(_asset("js", "map.js"))
        assert "world-basemap.js" in js
        assert "L.geoJSON(WORLD_COUNTRIES" in js
        assert "INDIA_BASEMAP_DATA_URI" not in js, (
            "the India raster is still being drawn"
        )

    def test_the_3d_twin_builds_its_ground_from_those_same_rings(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "world-basemap.js" in js
        assert "WORLD_COUNTRIES" in js
        assert "THREE.ShapeGeometry" in js, (
            "the ground plane is not triangulated from polygons"
        )
        assert "INDIA_BASEMAP_DATA_URI" not in js
        assert "TextureLoader" not in js, (
            "the twin is still texturing its plane with a photograph"
        )

    def test_both_frame_themselves_with_the_same_rule(self):
        """
        `networkWindow()` lives in world-basemap.js and is called by both. Two
        copies of the framing maths would agree until one of them was edited.
        """
        shared = _asset("js", "world-basemap.js")
        assert "export function networkWindow" in shared
        for module in ("map.js", "twin3d.js"):
            js = _without_comments(_asset("js", module))
            assert "networkWindow" in js, module

    def test_the_3d_plane_is_cut_at_its_own_edge(self):
        """
        A country crossing the plate boundary is clipped, not drawn past it.
        Canada hanging off the side of the ground plane reads as a rendering
        fault even when the geometry underneath is correct.
        """
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "clipRingToBounds" in js
        assert "ringIntersects" in js

    def test_land_and_water_are_actually_distinguishable(self):
        """
        The first pass had land #eef2f7 on water #f8fafc — a 4% luminance
        difference. The coastline was drawn, and invisible.
        """
        def luminance(hex_str):
            h = hex_str.lstrip("#").lstrip("0x")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        css_map = _asset("js", "map.js")
        land = re.search(r"land:\s*'(#[0-9a-fA-F]{6})'", css_map).group(1)
        water = re.search(r"water:\s*'(#[0-9a-fA-F]{6})'", css_map).group(1)
        assert abs(luminance(land) - luminance(water)) >= 12, (
            f"land {land} and water {water} are too close to tell apart"
        )

    def test_the_two_views_use_one_palette(self):
        """The 3D twin's colours are the 2D map's, as numbers."""
        map_js = _asset("js", "map.js")
        twin_js = _asset("js", "twin3d.js")
        for key in ("water", "land", "landActive"):
            hexed = re.search(rf"{key}:\s*'#([0-9a-fA-F]{{6}})'", map_js).group(1).lower()
            assert f"0x{hexed}" in twin_js.lower(), (
                f"{key} is #{hexed} on the 2D map and something else in 3D"
            )


class TestThePoliticalLayer:
    """
    Countries alone answer "which country is this network in". They cannot
    answer "which state is this facility in", which is the question a reader
    actually has — and the level their operations are organised at. With
    coastline and national borders only, twelve US facilities sat on one
    undifferentiated shape.
    """

    @staticmethod
    def _admin1():
        src = _asset("js", "world-admin1.js")
        start = src.index("export const ADMIN1 = ") + len("export const ADMIN1 = ")
        return json.loads(src[start:src.index(";\n", start)])

    def test_the_whole_world_is_subdivided(self):
        gj = self._admin1()
        assert len(gj["features"]) > 4500, len(gj["features"])
        admins = {f["properties"]["admin"] for f in gj["features"]}
        assert len(admins) > 240, len(admins)
        counts = {}
        for f in gj["features"]:
            a = f["properties"]["admin"]
            counts[a] = counts.get(a, 0) + 1
        # The federal countries a logistics network is most likely to be in.
        assert counts.get("United States of America") == 51, counts.get("United States of America")
        assert counts.get("India") == 36, counts.get("India")
        assert counts.get("Japan") == 47, counts.get("Japan")

    def test_no_subdivision_was_rounded_out_of_existence(self):
        """
        At a flat 2dp (~1.1 km) Vatican City, six Maldivian atolls and three
        Maltese towns collapsed below four distinct points and vanished. A
        shape smaller than the grid gets a finer grid.
        """
        gj = self._admin1()
        names = {(f["properties"]["admin"], f["properties"]["name"])
                 for f in gj["features"]}
        for pair in (("Vatican", "Vatican"), ("Malta", "Mdina"),
                     ("Maldives", "Raa")):
            assert pair in names, pair
        for f in gj["features"]:
            for poly in f["geometry"]["coordinates"]:
                for ring in poly:
                    assert len(ring) >= 4 and ring[0] == ring[-1], f["properties"]

    def test_it_is_loaded_on_demand_not_bundled(self):
        """
        1.6 MB is worth having and not worth putting in front of a sign-in
        screen. A static import would put it in the initial bundle.
        """
        shared = _without_comments(_asset("js", "world-basemap.js"))
        assert "import('./world-admin1.js')" in shared, (
            "the subdivisions are not dynamic-imported"
        )
        for module in ("map.js", "twin3d.js", "app.js"):
            js = _without_comments(_asset("js", module))
            assert "from './world-admin1.js'" not in js, module
            assert 'from "./world-admin1.js"' not in js, module

    def test_a_map_still_draws_when_the_layer_cannot_be_loaded(self):
        """
        Degraded is a map with national borders; failed is no map at all.
        """
        shared = _without_comments(_asset("js", "world-basemap.js"))
        loader = shared[shared.index("export function loadAdmin1"):]
        loader = loader[:loader.index("\n" + "}" + "\n")]
        assert ".catch(" in loader
        assert "return null" in loader

    def test_both_views_draw_the_subdivisions(self):
        for module, marker in (("map.js", "addSubdivisions"),
                               ("twin3d.js", "addSubdivisionBorders")):
            js = _without_comments(_asset("js", module))
            assert marker in js, module
            assert "loadAdmin1" in js, module

    def test_a_facility_can_be_resolved_to_its_state(self):
        """
        Answered by the same rings the maps draw, so "which state is this in"
        cannot disagree with what is on screen.
        """
        shared = _without_comments(_asset("js", "world-basemap.js"))
        assert "export function subdivisionsContaining" in shared


class TestTheTwinHoldsStill:
    """
    A map that drifts while you read it moves the thing you are pointing at,
    and every reading of a node's position is taken against a frame that has
    since moved.
    """

    def test_nothing_turns_the_camera_on_its_own(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "controls.autoRotate = false" in js
        assert "controls.autoRotate = true" not in js, (
            "something still switches auto-rotation on"
        )
        assert "setTimeout(() => { if (controls) controls.autoRotate = true; }" not in js, (
            "the idle timer that re-started the spin five seconds after you "
            "let go is back"
        )

    def test_the_user_can_still_rotate_it(self):
        """Stillness is not the same as a frozen control."""
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "controls.rotateSpeed" in js
        assert "controls.enablePan" in js

    def test_release_means_stop(self):
        """
        Inertia is a nice feel on a globe you are browsing and the wrong one
        on a map you read coordinates off: it keeps turning after you let go.
        Its decay is asymptotic too, so "it has stopped" was never exactly
        true — invisible at 60fps, seconds of drift on a software renderer.
        """
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "controls.enableDamping = false" in js
        assert "controls.enableDamping = true" not in js

    def test_decoration_does_not_animate(self):
        """
        The base rings breathed and plant cores spun. Neither encoded
        anything — a plant turning faster did not mean a plant doing more.
        """
        js = _without_comments(_asset("js", "twin3d.js"))
        animate = js[js.index("function animate()"):]
        animate = animate[:animate.index("\n" + "}" + "\n")]
        assert "rotation.y = time" not in animate
        assert "Math.sin(time" not in animate
        # The corridor flow stays: it shows which way goods travel.
        assert "photonStreams" in animate


class TestTheOverviewPageIsShapedLikeTheMockup:
    """
    Row order and, above all, where the KPIs are. Everything else on this page
    can move; a headline KPI below the fold is the defect that was reported.
    """

    @staticmethod
    def _home_section():
        html = _asset("index.html")
        start = html.index('id="tab-home"')
        return html[start:html.index("</section>", start)]

    def test_the_rows_are_in_the_mockups_order(self):
        home = self._home_section()
        order = [home.index(cls) for cls in
                 ('class="ov-head"', 'class="ov-main"', 'class="ov-signals-card"')]
        assert order == sorted(order), order

    def test_the_headline_and_its_strapline_are_one_line(self):
        """
        "Overview" and "Your network health and key actions at a glance." read
        as one line, so they share a baseline-aligned row rather than stacking.
        """
        css = _asset("css", "home-overview.css")
        head = css[css.index(".ov-head {"):css.index(".ov-title {")]
        assert "align-items: baseline" in head, head
        text = css[css.index(".ov-head-text {"):]
        text = text[:text.index("}")]
        assert "display: flex" in text and "align-items: baseline" in text

    def test_the_kpi_band_is_gone_and_the_twin_took_its_height(self):
        """
        The three KPI tiles sat on top of the digital twin and cost it half
        its height. They are gone; the alert is a card of its own in the left
        column and the twin spans BOTH rows of that column, so it runs from
        the top of the alert to the bottom of the attention card.
        """
        home = self._home_section()
        assert 'id="ov-kpis"' not in home, "the KPI band is still in the markup"
        assert 'class="ov-band"' not in home, "the old status band is still there"
        assert "home2-kpi-strip" not in home, (
            "the older below-the-fold KPI strip is still in the markup"
        )
        main = home[home.index('class="ov-main"'):]
        assert 'id="ov-alert"' in main, (
            "the alert must be inside the body grid, beside the twin"
        )

        css = _asset("css", "home-overview.css")
        rows = css[css.index(".ov-main {"):]
        rows = rows[:rows.index(".ov-attn-card,")]
        assert "grid-row: 1 / span 2" in rows, (
            "the twin does not span both rows, so it cannot reach the alert"
        )
        assert "grid-template-rows: auto minmax(0, 1fr)" in rows

    def test_no_dead_kpi_renderer_was_left_behind(self):
        """
        Removing a band from the markup and leaving its renderer in the file
        is how a second, unreachable KPI engine gets born. Nothing that drew
        those tiles survives.
        """
        js = _asset("js", "app.js")
        for gone in ("function renderHomeKPIs", "function renderFacilityKpiBand",
                     "function ovKpiHtml", "OV_KPI_ICONS", "state.overviewView"):
            assert gone not in js, "%s is still in app.js" % gone

    def test_the_scope_controls_live_with_the_project_selector(self):
        """
        Project, then facility, then period — the three answers to "what am I
        looking at", in order of how much they narrow, in one place. They sit
        in the top bar beside the project button rather than a row apart.
        """
        html = _asset("index.html")
        topbar = html[html.index('class="app-global-topbar"'):]
        topbar = topbar[:topbar.index("</header>")]
        assert 'id="project-select-btn"' in topbar
        assert 'id="home-top-facility"' in topbar
        assert 'id="home-top-period"' in topbar

        home = self._home_section()
        assert 'id="ov-facility"' not in home, (
            "a second Facility control on the same screen is two chances to "
            "disagree about one value"
        )
        assert 'id="ov-period"' not in home

    def test_the_page_local_view_selector_is_gone(self):
        """
        `View: network summary / selected facility` switched the KPI band that
        no longer exists. The page carries no selector of its own now — scope
        is the top bar's, on every screen.
        """
        home = self._home_section()
        assert 'id="ov-view"' not in home
        assert 'class="ov-head-controls"' not in home

    def test_one_scope_pair_and_it_is_the_same_one_on_every_page(self):
        """
        Facility and Period used to be in the top bar on Home and one row
        lower on every other tab: the same two controls in two positions,
        which is Nielsen #4 twice over. There is one pair, in the top bar,
        and the sub-topbar's copy is never shown.
        """
        js = _without_comments(_asset("js", "app.js"))
        block = js[js.index("const scopeApplies"):]
        block = block[:block.index("if (isHomeOverview) return;")]
        assert "tab !== 'scenarios'" in block, block
        assert "scopeApplies ? 'flex' : 'none'" in block, block

        tail = js[js.index("const controls = document.getElementById('topbar-controls');"):]
        tail = tail[:tail.index("}") + 1]
        assert "controls.style.display = 'none'" in tail, tail

    def test_scenario_planning_gets_no_facility_or_period(self):
        """
        A scenario is solved over the whole network for the horizon it was
        built with. A facility or period picker there would be a control that
        changes nothing, and a dead control is worse than none.
        """
        js = _without_comments(_asset("js", "app.js"))
        assert "const scopeApplies = (tab !== 'scenarios');" in js

    def test_upload_data_is_on_every_page_like_the_rest_of_the_bar(self):
        js = _without_comments(_asset("js", "app.js"))
        block = js[js.index("const btnUpload = document.getElementById('btn-topbar-upload');"):]
        block = block[:block.index("}") + 1]
        assert "btnUpload.style.display = 'flex';" in block, block

    def test_the_attention_card_has_the_mockups_three_sections(self):
        js = _asset("js", "app.js")
        assert "Why it matters" in js
        assert ">Impact<" in js
        assert "Recommended next step" in js

    def test_the_signals_row_shows_only_what_the_upload_carried(self):
        """
        `EXTERNAL_SIGNALS` is emptied and refilled by hydration and this build
        ships no demo signals, so an empty row says the upload had none rather
        than inventing three.
        """
        js = _asset("js", "app.js")
        fn = js[js.index("function renderHomeSignals"):js.index("const OV_SIGNAL_CHIP")]
        assert "EXTERNAL_SIGNALS" in fn
        assert "No external signals were found in your upload" in fn


class TestNothingOnTheOverviewClaimsMoreThanTheBuildDoes:
    """The standing rules this page is most able to break."""

    def test_the_facility_selector_still_changes_something_on_this_page(self):
        """
        The KPI band it used to switch is gone, so the Facility control has to
        keep earning its place: the twin's own snapshot is scoped by it, and
        reads `fac.utilPct` — the field hydration writes, and the same one the
        rest of the app reads.
        """
        js = _asset("js", "app.js")
        fn = js[js.index("function renderHomeTwinCallout"):]
        fn = fn[:fn.index("\n}\n")]
        assert "state.selectedFacility" in fn
        assert "fac.utilPct" in fn

    def test_a_missing_figure_is_never_rendered_as_a_number(self):
        """
        The rule outlives the band it was written for. Utilisation is a solver
        output; until there is one the snapshot shows an em dash, never a
        zero, and never the literal string "undefined%".
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function renderHomeTwinCallout"):]
        fn = fn[:fn.index("\n}\n")]
        assert "Number.isFinite(fac.utilPct)" in fn
        assert "'—'" in fn or '"—"' in fn
        assert "?? 95" not in js, "a fabricated service target is back"

    def test_the_run_that_fills_the_savings_figure_is_still_one_click_away(self):
        """
        The savings tile carried the "Run optimization" button. With the tile
        gone the run must still be reachable from this page without hunting —
        it is the attention card's own call to action.
        """
        js = _asset("js", "app.js")
        assert "Open scenario planner" in js
        assert "ov-attn-cta" in js

    def test_the_alert_no_longer_prints_every_market_in_prose(self):
        """
        `showNetworkNotice` used to concatenate `res.issues` into the banner
        sentence: six lines and ~900 characters above the fold, with the
        figure a reader needed buried mid-paragraph. The per-market detail is
        kept — on `detail`, for the view that wants it.
        """
        js = _asset("js", "ingestion.js")
        fn = js[js.index("function showNetworkNotice"):]
        fn = fn[:fn.index("\n}\n")]
        assert "__ngNetworkNotice" in fn
        assert "detail" in fn
        caller = js[js.index("} else if (report?.relaxed) {"):]
        caller = caller[:caller.index("} else {")]
        assert "(res.issues || []).join" in caller, (
            "the per-market detail was dropped rather than moved"
        )
        assert "${why}" not in caller, (
            "the issues are still being concatenated into the headline"
        )
