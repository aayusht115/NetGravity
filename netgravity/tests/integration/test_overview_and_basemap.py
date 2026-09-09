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



def _rule(css: str, selector: str) -> str:
    """One CSS rule's body, by its opening selector."""
    start = css.index(selector)
    return css[start:css.index("}", start)]


class TestTheOverviewPageIsShapedLikeTheMockup:
    """
    Row order and, above all, where the KPIs are. Everything else on this page
    can move; a headline KPI below the fold is the defect that was reported.
    """

    @staticmethod
    def _home_section():
        html = _asset("index.html")
        start = html.index('id="tab-home"')
        # To the NEXT tab panel, not to the first `</section>`. The KPI strip
        # is a `<section>` of its own now — it has a heading, so it is one —
        # and slicing at the first closing tag cut the page off after its
        # first row.
        return html[start:html.index('id="tab-insights"', start)]

    def test_the_rows_are_in_the_mockups_order(self):
        """
        Figures, then findings, then the data the analysis did not have.

        SUPERSEDED ORDER: head, body grid, KPI strip. The figures were last,
        on the reasoning that findings outrank figures; the first question a
        reader has on opening this page is "is the network alright", and five
        figures answer it in a glance. Dump/Home Overview-updated1.png puts
        them at the top and this asserts that they stay there.
        """
        home = self._home_section()
        order = [home.index(cls) for cls in
                 ('class="ov-head"', 'class="home2-kpi-strip"',
                  'class="ov-insights-head"', 'id="ov-tiles"',
                  'id="ov-data-strip"', 'class="ov-foot"')]
        assert order == sorted(order), order
        assert 'class="ov-signals-card"' not in home, (
            "the signals card is back on Home"
        )

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

    def test_the_digital_twin_is_not_on_this_page(self):
        """
        SUPERSEDED: this was `test_the_kpi_band_is_gone_and_the_twin_took_its_
        height`, and it protected the twin's height against a KPI band sitting
        on top of it.

        The twin is not on this page at all now. It took the right half of the
        first screen to draw a network the sidebar opens in full, and it left
        the findings a 500px column with an internal scroller — so on a
        1050px window 204px of that card, including the recommendation a
        reader is meant to act on, sat below its own bottom edge.

        Nothing was removed from the product: Baseline > Digital Twin is the
        same scene, drawn by the same engine. What this test protects is that
        no copy of it comes back to this page, and that its renderers are gone
        rather than left behind as a second, unreachable view of one scene.
        """
        home = self._home_section()
        for gone in ('id="home-map-twin"', 'id="home-twin-callout"',
                     'class="ov-twin-card"', 'id="ov-kpis"', 'class="ov-band"',
                     'class="ov-main"'):
            assert gone not in home, "%s is still on Home" % gone

        js = _asset("js", "app.js")
        for gone in ("function renderHomeDigitalTwin",
                     "function renderHomeTwinCallout"):
            assert gone not in js, "%s is still in app.js" % gone

        css = _asset("css", "home-overview.css")
        for gone in (".ov-twin-card {", ".home2-twin-card {",
                     ".home-twin-map-container {", ".home-twin-callout {",
                     ".ov-main {"):
            assert gone not in css, "%s is still in home-overview.css" % gone

        # And the scene itself is untouched — the Digital Twin tab still
        # initialises it.
        assert "initTwin3D" in js

    def test_the_page_flows_and_measures_no_grid(self):
        """
        SUPERSEDED: this was `test_the_grid_takes_the_whole_first_screen`, and
        it required `.ov-main` to be sized from a MEASURED top offset so its
        two cards filled exactly one screen and its own scroller worked.

        There is no grid to size. Home is four rows that flow — figures,
        findings, data requests, timestamp — and a page that flows needs no
        measurement. What is left is the one thing that still has to be
        measured: how much of the bottom rows the fixed "Ask Netgravity"
        button covers.

        Both retired variables are actively CLEARED rather than merely no
        longer written, because a cached stylesheet holding the old
        subtraction would otherwise keep applying it to a page that no longer
        expects it.
        """
        # `var(--...)`, not the bare name: the stylesheet still NAMES both
        # variables, in the note recording that they are gone and why. What
        # must not come back is a rule that reads one.
        css = _asset("css", "home-overview.css")
        assert "var(--ov-main-top" not in css, (
            "the stylesheet is still subtracting a measured grid offset"
        )
        assert "var(--ov-strip-h" not in css, (
            "the stylesheet is still buying the strip a place on the first screen"
        )

        js = _asset("js", "app.js")
        fn = js[js.index("function sizeOverviewToWindow()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "removeProperty('--ov-strip-h')" in fn, fn
        assert "removeProperty('--ov-main-top')" in fn, fn
        assert "setProperty('--ov-strip-h'" not in fn, fn
        assert "setProperty('--ov-main-top'" not in fn, fn
        # The chat button's gutter is still measured, from a real rect.
        assert "--ov-fab-reserve" in fn, fn
        assert "getBoundingClientRect()" in fn, fn

    def test_the_headline_is_a_conclusion_and_the_prose_explains_it(self):
        """
        A headline alone was not readable. Half the deterministic template's
        were noun phrases - "Capacity headroom across the footprint" - which
        tell a reader what the card is FILED under rather than what was found,
        and the tile had nothing under them but figures.

        Two changes, and this guards both: the template writes clauses, and
        the tile carries the engine's own prose between the headline and the
        figures.
        """
        import netgravity.orchestrator.agents.reasoning_agent as _ra
        agent = pathlib.Path(_ra.__file__).read_text(encoding="utf-8")
        for label in ('headline="I see capacity headroom across the footprint"',
                      'headline="I see the transport emissions this plan implies"',
                      'headline="The cost this network runs at today"',
                      'headline=f"I see {label} as the largest cost line"'):
            assert label not in agent, label
        # "3 site(s)" is a print statement, not a sentence a person reads.
        assert "site(s)" not in agent, "machine plurals are back in the prose"
        assert "facility(ies)" not in agent

        js = _asset("js", "app.js")
        fn = js[js.index("function insightTileHtml("):]
        fn = fn[:fn.index("\n}\n")]
        assert "insightDescription(rec, item.title)" in fn, fn

    def test_the_description_never_repeats_the_headline(self):
        """
        The engine's headline is usually one of its own narrative's sentences.
        Printed together they put the same sentence on the tile twice, which
        is what "No open site reaches the 90% threshold, so capacity is not
        what limits this plan" did - as the heading, and again under it.
        """
        js = _without_comments(_asset("js", "insight-presentation.js"))
        fn = js[js.index("export function insightDescription(record, headline)"):]
        fn = fn[:fn.index("\n}\n")]
        # Compared with punctuation and case removed, and in BOTH directions:
        # the headline is often a trimmed version of the sentence rather than
        # a copy of it.
        assert "replace(/[^a-z0-9]+/g" in fn, fn
        assert "n.includes(head) || head.includes(n)" in fn, fn

    def test_every_outstanding_request_is_on_this_page(self):
        """
        The strip showed two per group behind a "+N more" link into another
        screen. A reader cannot tell whether the third one matters without
        opening that screen, and "3 more optional fields" is a statistic
        rather than something anyone can act on.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function dataStripCardHtml(group, items)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "items.map(" in fn, fn
        for gone in ("items.slice(", "ov-data-more", "data-open-insights"):
            assert gone not in fn, gone
        # The size of the job is stated up front instead.
        assert "ov-data-card-count" in fn, fn

    def test_a_request_offers_the_upload_as_well_as_the_email(self):
        """
        There was one button per row, labelled "Request this data", and it
        opened a page offering two things: send the request, or upload the
        file yourself. Most of the time the person reading this HAS the
        workbook - they uploaded the last one - so the likeliest action was
        behind a button whose label described the other one. Nielsen #4.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function dataStripCardHtml(group, items)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "data-upload-for" in fn, fn
        assert "Upload it" in fn, fn
        assert "Ask for it" in fn, fn
        assert "Request this data" not in fn, (
            "the single button that promised only an email is back")

        # And the upload button calls the same thing the detail page's does,
        # rather than routing through the page about emailing someone.
        binder = js[js.index("function renderHomeDataStrip"):]
        binder = binder[:binder.index("\n}\n")]
        assert "window.showUploadData" in binder, binder

    def test_a_request_already_sent_says_so_on_the_row(self):
        """
        `/api/actions` sends `last_sent` for every action and the band ignored
        it, so four requests looked identical whether one had been emailed an
        hour ago or never. The mistake that invites is asking the same person
        for the same column twice.

        Absence is the default state: a row with nothing sent carries no chip
        at all rather than one reading "not yet".
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function dataSentChip(item)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "lastSent" in fn, fn
        assert "if (!sent || !sent.sent_at) return null;" in fn, fn
        # A stub is reported as saved, never as sent: no message left the
        # machine, and a stub described as a send is the one outcome that
        # makes this feature worse than not having it.
        assert "'stubbed'" in fn and "Saved" in fn, fn
        assert "'failed'" in fn and "'partial'" in fn, fn

        card = js[js.index("function dataStripCardHtml(group, items)"):]
        card = card[:card.index("\n}\n")]
        assert "dataSentChip(it)" in card, card
        assert "'Ask again' : 'Ask for it'" in card, card

    def test_the_bands_take_the_width_rather_than_sharing_it(self):
        """
        Two side-by-side cards are only ever the same height by accident: with
        two required fields and three optional ones the shorter one ended in a
        block of empty tint, and the longer the lists got the bigger that
        block became. One band per group, each the full width, each ending
        where its own last request ends.
        """
        css = _asset("css", "home-overview.css")
        strip = _rule(css, ".ov-data-strip {")
        assert "flex-direction: column" in strip, strip
        assert "grid-template-columns" not in strip, (
            "the data bands are sharing the width again")

    def test_the_strip_reports_the_solve_and_computes_nothing(self):
        """
        §9. Five figures, each read from the authoritative baseline, each
        rendering a dash when the solve did not report it. No zero fallback,
        and no delta — this build computes no previous-period baseline, so an
        arrow here would compare against a number that does not exist.
        """
        # The CODE, not the prose: the comments here explain what is NOT
        # done, and a bare scan for "/ " matches every "//" in them.
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function renderHomeKpiStrip("):]
        fn = fn[:fn.index("\n}\n")]
        assert "getOptimizedBaseCase()" in fn, fn
        assert "Number.isFinite(raw)" in fn, fn
        assert "—" in fn, fn
        # Nothing is derived. The tile formatters round and label ONE
        # value; they never combine two, which is what deriving would take.
        for arithmetic in ("* 100", " / ", " - ", " + "):
            assert arithmetic not in fn, (arithmetic, fn)
        tiles = js[js.index("const HOME_KPI_TILES = ["):]
        tiles = tiles[:tiles.index("\n];")]
        # SUPERSEDED COUNT: three, then four. A fifth was asked for, and the
        # fill rate is the one headline figure the solve reports that cost,
        # utilisation, service level and carbon do not cover — a network can
        # meet its service level on the demand it chooses to serve and strand
        # the rest, which is exactly what the test network does. What this
        # test protects is unchanged: every tile is a key read from the
        # authoritative baseline, and none is derived here.
        assert tiles.count("key:") == 5, tiles
        for key in ("totalCost", "avgUtilization", "fillRate", "sla",
                    "carbonKgCo2e"):
            assert f"key: '{key}'" in tiles, key

    def test_the_way_to_the_rest_of_the_figures_is_at_the_foot_of_the_strip(self):
        """
        "View all KPIs" sits on the strip's bottom edge, not in its top-right
        corner: the corner competes with the first figure for the first
        fixation, and the foot is where the eye leaves the row.
        """
        home = self._home_section()
        strip = home[home.index('class="home2-kpi-strip"'):]
        strip = strip[:strip.index("</section>")]
        row = strip.index('id="ov-kpi-strip-row"')
        foot = strip.index('class="home2-kpi-strip-foot"')
        assert row < foot, "the link is above the figures it summarises"
        assert 'id="btn-view-all-kpis"' in strip[foot:], strip[foot:]
        assert 'data-arg="facility-dashboard"' in strip[foot:], strip[foot:]

    def test_the_bottom_rows_leave_room_for_the_chat_button(self):
        """
        "Ask Netgravity" is `position: fixed` at the bottom right of the
        VIEWPORT. The KPI strip used to be the bottom row of this page and
        was what ended up underneath it; the strip is at the TOP now, and the
        data requests and the "last analysed" line are the rows that share
        that band of screen.

        Measured from the button's rect, not from `offsetParent`: that
        property is null for every fixed element, visible or not, so the
        obvious test reported the button hidden and reserved nothing. This
        cost one run of the harness to find.
        """
        js = _asset("js", "app.js")
        fn = js[js.index("function sizeOverviewToWindow()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "floating-chatbot-fab" in fn, fn
        assert "--ov-fab-reserve" in fn, fn
        assert "offsetParent" not in fn.split("floating-chatbot-fab")[1], (
            "a fixed element's visibility cannot be read from offsetParent"
        )
        # AT THE FOOT OF THE PAGE, not down the side of every row.
        #
        # The data bands used to reserve `--ov-fab-reserve` on their right,
        # which on a 1680px window is about 280px of blank margin down each of
        # them - a permanent hole in the layout to avoid an overlap that only
        # happens mid-scroll. The page reserves the button's own clearance at
        # its foot instead, so at the bottom of the scroll nothing comes to
        # REST underneath it, and the bands take the full width they were
        # given.
        css = _asset("css", "home-overview.css")
        panel = _rule(css, "#tab-home.active {")
        assert "padding-bottom: var(--ng-fab-clearance" in panel, panel
        strip = _rule(css, ".ov-data-strip {")
        assert "var(--ov-fab-reserve" not in strip, (
            "the data bands are reserving a blank margin again")

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

    def test_a_tile_says_the_mockups_four_things_in_its_order(self):
        """
        SUPERSEDED: the attention card's three sections were "Why it matters",
        "Impact" and "Recommended next step", stacked down one column.

        Dump/Home Overview-updated1.png asks for four, on each of three tiles,
        in one order: the conclusion in bold, why it matters in figures, what
        to do about it with a button that goes there, and the way into the
        full finding. All three tiles say the same four things in the same
        order, so a reader learns the shape once (Nielsen #4).
        """
        js = _asset("js", "app.js")
        fn = js[js.index("function insightTileHtml("):]
        fn = fn[:fn.index("\n}\n")]
        # The TEMPLATE the function returns, not the order the blocks were
        # built in: `actionHtml` is assembled before the return and rendered
        # after the highlight, so reading the file top to bottom would report
        # the wrong order for a correct page.
        tpl = fn[fn.index("  return `"):]
        order = [tpl.index(marker) for marker in
                 ("ov-tile-eyebrow", "${figureHtml}", "ov-tile-highlight",
                  "${descriptionHtml}", "${bodyHtml}", "ov-tile-detail")]
        assert order == sorted(order), (order, tpl)
        # And within the body, why before what to do about it.
        body = fn[fn.index("const bodyHtml ="):fn.index("  return `")]
        assert body.index("whyHtml") < body.index("actionHtml"), body
        # And the blocks say what they are, in the tile's own words.
        assert ">Why it matters<" in fn, fn
        assert ">Recommended action<" in fn, fn
        assert ">View detailed finding<" in fn, fn

    def test_the_recommendation_on_a_tile_is_never_written_here(self):
        """
        §9, applied to prose. A recommendation composed in the browser is a
        recommendation nothing verified. `recommendedAction` is what
        `/api/insights` sent — the Reasoning Agent's own line where it wrote
        one, and that theme's default where it did not — and an empty one
        drops the block rather than filling it in.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function insightTileHtml("):]
        fn = fn[:fn.index("\n}\n")]
        assert "rec.recommendedAction" in fn, fn
        assert "action ?" in fn, "an absent action must drop its own block"

        data = _without_comments(_asset("js", "data.js"))
        rec = data[data.index("export function toInsightRecord("):]
        rec = rec[:rec.index("\n}\n")]
        assert "apiInsight.recommended_action" in rec, rec

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
        SUPERSEDED: the Facility control used to earn its place on Home by
        scoping the twin's own snapshot. The twin is not on this page.

        It earns it on the findings instead, and more directly than before:
        the tiles are drawn from the network's findings PLUS the selected
        facility's own, and selecting one asks the reasoning layer for that
        facility's briefing. Both halves are asserted, because the second
        without the first would be a request whose answer nothing reads.
        """
        js = _asset("js", "app.js")
        fetch = js[js.index("function ensureFacilityInsights()"):]
        fetch = fetch[:fetch.index("\n}\n")]
        assert "state.selectedFacility" in fetch, fetch
        assert "loadFacilityInsights" in fetch, fetch

        ranked = js[js.index("function rankedAttentionInsights()"):]
        ranked = ranked[:ranked.index("\n}\n")]
        assert "ensureFacilityInsights()" in ranked, ranked
        assert "getInsightsForFacility(state.selectedFacility)" in ranked, ranked

        tiles = js[js.index("function renderHomeInsightTiles("):]
        tiles = tiles[:tiles.index("\n}\n")]
        assert "rankedAttentionInsights()" in tiles, tiles

    def test_a_missing_figure_is_never_rendered_as_a_number(self):
        """
        The rule outlives the card it was written for. A tile's headline
        figure is a value the engine REPORTED: the first evidence row that
        carries one. A finding that cites none has no figure line — never a
        zero, and never the engine's own "Not available" printed at 26px as
        though it were a reading.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function insightTileHtml("):]
        fn = fn[:fn.index("\n}\n")]
        # The figure is chosen, not fabricated: an evidence row with a real
        # display value, or nothing at all.
        assert "display_value !== 'Not available'" in fn, fn
        assert "lead ?" in fn, fn
        assert "figureHtml" in fn, fn
        # And nothing on the tile is computed from two values.
        for arithmetic in ("* 100", " / "):
            assert arithmetic not in fn, (arithmetic, fn)
        assert "?? 95" not in js, "a fabricated service target is back"

    def test_the_run_that_fills_the_savings_figure_is_still_one_click_away(self):
        """
        The savings tile carried the "Run optimization" button. With the tile
        gone the run must still be reachable from this page without hunting —
        it is the call to action on any finding the planner can act on, and
        the destination the tile map falls back to.
        """
        js = _asset("js", "insight-presentation.js")
        assert "Open scenario planner" in js
        cta = js[js.index("export const INSIGHT_CTA = {"):]
        cta = cta[:cta.index("\n};")]
        assert "tab: 'scenarios'" in cta, cta
        default = js[js.index("export const INSIGHT_CTA_DEFAULT ="):]
        default = default[:default.index("\n")]
        assert "scenarios" in default, default

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

class TestTheAffectedDemandIsShown:
    """
    "View affected demand" named a thing and opened a different screen.

    It called `navigateToTab('twin')` — the Digital Twin, which draws the
    network and says nothing about a shortfall. A reader pressing a link that
    names the demand it cannot serve was shown a map instead, and the detail
    the link promised existed nowhere in the product.

    The engine had it the whole time: when the strict model proves infeasible
    it returns the best plan that serves as much as the network physically
    can, and attaches a note carrying `short_markets` — demand at a market
    minus what reached it, read off that plan's own flows — plus its reason
    and the proposed sites it opened to get that far. Hydration dropped all of
    it.
    """

    def test_the_link_opens_the_detail_rather_than_another_screen(self):
        js = _asset("js", "app.js")
        block = js[js.index("function renderOverviewAlert("):]
        block = block[:block.index("\nfunction ")]
        assert "openDemandShortfallDetail" in block
        # The screen it used to change to.
        assert "navigateToTab('twin')" not in block

    def test_the_breakdown_survives_hydration(self):
        """`short_markets` and `would_open_candidates` were read off the plan
        and then discarded, so Home could report a shortfall and offer no
        affected demand to view."""
        hy = _asset("js", "integration", "hydrate.js")
        assert "DEMAND_SHORTFALL" in hy
        block = hy[hy.index("Object.assign(DEMAND_SHORTFALL,"):]
        block = block[:block.index("});")]
        assert "relaxedMeta.short_markets" in block
        assert "relaxedMeta.would_open_candidates" in block
        assert "relaxedMeta.relaxation_reason" in block

    def test_a_new_network_clears_the_old_ones_shortfall(self):
        js = _asset("js", "data.js")
        assert "export const DEMAND_SHORTFALL" in js
        # Reset with every other narrative field, or the previous project's
        # markets are listed under this one's figures.
        block = js[js.index("SCENARIO_COMPARISON_ACTIONS.length = 0;"):]
        block = block[:block.index("AGENT_STATE")]
        assert "Object.assign(DEMAND_SHORTFALL," in block
        assert "shortMarkets: []" in block

    def test_the_detail_names_markets_and_never_apportions(self):
        """
        A total split across markets by the screen would read exactly like one
        the engine computed. Where no breakdown travelled with the run, the
        drawer says so.
        """
        js = _asset("js", "app.js")
        block = js[js.index("function openDemandShortfallDetail()"):]
        block = block[:block.index("\nconst OV_ICONS")]
        assert "DEMAND_SHORTFALL.shortMarkets" in block
        assert "r.market_id" in block and "r.unserved" in block
        assert "No market breakdown travelled with this run" in block
        # Nothing divides the total by a market count anywhere in it.
        assert "/ rows.length" not in block
        assert "/ MARKETS.length" not in block

    def test_the_reason_is_the_engines_own(self):
        js = _asset("js", "app.js")
        block = js[js.index("function openDemandShortfallDetail()"):]
        block = block[:block.index("\nconst OV_ICONS")]
        assert "DEMAND_SHORTFALL.reason" in block
        # ...and its absence is stated rather than filled in.
        assert "has not stated a" in block

    def test_it_opens_the_drawer_that_already_exists(self):
        """No second overlay for one more panel: the drawer, its close button
        and its overlay-click dismissal are already on the page."""
        html = _asset("index.html")
        assert 'id="action-drawer-overlay"' in html
        assert 'id="action-drawer-content"' in html
        js = _asset("js", "app.js")
        block = js[js.index("function openDemandShortfallDetail()"):]
        block = block[:block.index("\nconst OV_ICONS")]
        assert "action-drawer-content" in block
        assert "action-drawer-overlay" in block
        assert "export function closeActionDrawer()" in js


class TestTheInsightsListReadsAsAList:
    """
    Seven findings on one screen, each legible as a different finding.

    Rendered against a real solved network the list was six rows of exactly
    157px, and about a third of every one of them was empty: the recommended
    action sat in a tinted box the width of the left column — some 1,180px —
    holding one sentence of roughly 700px, and the figure and the link were
    pinned to the top right leaving the bottom right blank.
    """

    def test_the_finding_the_action_and_the_figure_are_three_columns(self):
        css = _without_comments(_asset("css", "home-overview.css"))
        row = _rule(css, ".insp-row {")
        assert "display: grid" in row, row
        # STATED tracks. Each row is its own grid, so an `auto` end column
        # sized to that row's own content — a row showing a currency total and
        # one showing a percentage got different widths, and the action boxes
        # beside them started at different x.
        assert "grid-template-columns: 20px" in row, row
        assert "auto" not in row.split("grid-template-columns:")[1].split(";")[0], row

    def test_the_action_is_beside_the_finding_not_under_it(self):
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function insightRowHtml(item)"):]
        fn = fn[:fn.index("\n}\n")]
        order = [fn.index(m) for m in
                 ("insp-row-main", "insp-row-action", "insp-row-right")]
        assert order == sorted(order), fn
        # And a finding with no step keeps the column, so the figures stay in
        # the same place on every row.
        assert "insp-row-action is-empty" in fn, fn

    def test_one_figure_per_row_so_the_column_is_a_column(self):
        """
        A second figure was tried here and taken out: the engine's own
        description already carries it ("Average utilisation is 56.23% and the
        busiest site at 77.14%"), and two right-aligned figures moved the
        PRIMARY one left on the rows that had two — so the figure a reader
        scans the page for was in a different place on every other row.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function insightRowHtml(item)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "insp-row-figures" not in fn, fn
        assert fn.count("insp-row-figure-value") == 1, fn

    def test_a_risk_is_tinted_the_way_the_overview_tints_one(self):
        """
        A left border three pixels wide is the difference between "read this
        first" and "read this eventually", and every other row carries the
        same three pixels. The tint is the Overview lead tile's own
        `--red-bg`, so the finding looks the same on both screens.
        """
        css = _without_comments(_asset("css", "home-overview.css"))
        rule = _rule(css, ".insp-row.tone-risk {\n  background")
        assert "--red-bg" in rule, rule
        # Its action block has to separate from the tint rather than blend in.
        action = _rule(css, ".insp-row.tone-risk .insp-row-action {")
        assert "rgba(255, 255, 255" in action, action

    def test_the_row_shows_the_description_not_the_headline_again(self):
        """
        The row printed `subtitle` — the narrative's FIRST SENTENCE — which on
        a finding whose headline IS that sentence was the heading again, in
        grey, directly under itself.
        """
        js = _without_comments(_asset("js", "app.js"))
        fn = js[js.index("function insightRowHtml(item)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "insightDescription(rec, item.title)" in fn, fn
