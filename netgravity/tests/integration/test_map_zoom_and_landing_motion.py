"""
Zoom on the scenario planner's twin card, and the motion on the landing hero.

THE CARD YOU COULD NOT ZOOM
---------------------------
Every map in this product is the same Leaflet map with the same controls,
except one: the scenario planner's "Digital Twin — Visual Context" card is
built with `isCompact: true`, and that flag turned the wheel off and never
turned it back on. It was turned off for a real reason — the card is the
bottom row of a page that scrolls, and a map that takes the wheel the moment
the pointer crosses it swallows the page scroll. So the wheel is now *armed*
by a click and handed back when the pointer leaves, which gives the card the
twin page's zoom without taking the page's scroll.

THE HERO THAT LOOKED PRINTED
----------------------------
Three rectangles of a dot pattern is a regular grid drawn three times, and a
still image of a network is a picture of one. What moves now: 132 scattered
points on their own clocks, six panels breathing, two rings per hub at
different speeds, and fourteen packets travelling the corridor geometry
itself. None of it carries information, which is exactly why all of it stops
under `prefers-reduced-motion`.
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
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


class TestTheScenarioTwinCanBeZoomed:
    def test_the_wheel_is_armed_by_a_click_not_by_the_pointer(self):
        """
        Enabling `scrollWheelZoom` outright would restore the trap the flag
        was added to avoid: the card sits at the bottom of a scrolling page.
        """
        js = _without_comments(_asset("js", "map.js"))
        fn = js[js.index("function armCompactZoom("):]
        fn = fn[:fn.index("\n}\n")]
        assert "mousedown" in fn and "arm" in fn, fn
        assert "map.scrollWheelZoom.enable()" in fn, fn
        # And handed back when the pointer leaves.
        assert "mouseleave" in fn, fn
        assert "map.scrollWheelZoom.disable()" in fn, fn

    def test_it_is_the_compact_maps_that_get_it(self):
        js = _without_comments(_asset("js", "map.js"))
        init = js[js.index("export function initMap("):]
        init = init[:init.index("\n}\n")]
        assert "if (options.isCompact) armCompactZoom(map, container);" in init, init

    def test_an_unarmed_wheel_says_why_nothing_happened(self):
        """Nielsen #1. A map that silently ignores the wheel reads as broken."""
        js = _asset("js", "map.js")
        fn = js[js.index("function armCompactZoom("):]
        fn = fn[:fn.index("\n}\n")]
        assert "Click the map to zoom" in fn, fn
        assert "'wheel'" in fn, fn
        css = _asset("css", "style.css")
        assert ".map-zoom-hint {" in css
        assert ".map-zoom-hint.is-visible {" in css
        # It must never eat a click meant for the map underneath it.
        rule = css[css.index(".map-zoom-hint {"):]
        rule = rule[:rule.index("}")]
        assert "pointer-events: none" in rule, rule

    def test_the_wheel_listener_does_not_block_the_page(self):
        """
        A non-passive wheel listener on a scrolling page is a jank source even
        when it does nothing. This one only shows a hint.
        """
        js = _asset("js", "map.js")
        fn = js[js.index("function armCompactZoom("):]
        fn = fn[:fn.index("\n}\n")]
        wheel = fn[fn.index("'wheel'"):]
        assert "{ passive: true }" in wheel[:400], wheel[:400]

    def test_every_map_gets_a_zoom_to_the_network(self):
        """
        Both halves of a zoom. The default frame is the whole COUNTRY — a
        deliberate decision, and the right one for a reader orienting on a
        coastline they know — but Canada's outline reaches 83N, and Mercator
        stretches that strip so hard that a network in the southern provinces
        frames at zoom 2. Measured on the running app: the sites occupied
        about a seventh of a 1368px card. That is the view you need a way
        into.

        And a way back out: zooming into the corner of a card this size with
        no reset is a trap. The twin page gets the same control — one
        behaviour on every map is the point of the ask.
        """
        js = _without_comments(_asset("js", "map.js"))
        init = js[js.index("export function initMap("):]
        init = init[:init.index("\n}\n")]
        assert "addFitControl(map, containerId);" in init, init
        # Unconditional: not inside the isCompact branch.
        assert init.index("addFitControl") < init.index("if (options.isCompact)")
        fn = js[js.index("function addFitControl("):]
        fn = fn[:fn.index("\n}\n")]
        assert "fitToNetwork(containerId, { sites: true })" in fn, fn
        # Beside Leaflet's own +/- bar, not in the corner the network's own
        # figures occupy (see .twin3d-stats-overlay).
        assert "'bottomleft'" in fn, fn
        assert "L.DomEvent.disableClickPropagation" in fn, fn

    def test_the_default_country_framing_is_untouched(self):
        """
        The whole-country window was asked for and is asserted elsewhere
        (test_screen_consistency). The control reads the SAME function with
        one flag set, so the two framings cannot drift apart — and every
        existing caller still gets the country.
        """
        js = _without_comments(_asset("js", "map.js"))
        fn = js[js.index("export function fitToNetwork("):]
        fn = fn[:fn.index("\n}\n")]
        assert "networkWindow(nodes, { wholeCountry: false })" in fn, fn
        assert "networkWindow(nodes)" in fn, fn
        assert "{ sites = false } = {}" in fn, fn
        # Nothing else asks for the site window.
        assert js.count("wholeCountry: false") == 1, js.count("wholeCountry: false")

    def test_the_control_is_reachable_without_a_mouse(self):
        js = _asset("js", "map.js")
        fn = js[js.index("function addFitControl("):]
        fn = fn[:fn.index("\n}\n")]
        assert "setAttribute('role', 'button')" in fn, fn
        assert "aria-label" in fn, fn

    def test_the_maps_are_inspectable(self):
        """
        "What zoom is that card at" had no answer from outside the module, so
        the only available check was a pane transform — which Leaflet resets
        when a zoom animation settles, and which therefore reads identical
        before and after a zoom that did happen.
        """
        js = _asset("js", "map.js")
        assert "window.__ngMaps = maps;" in js
        # The existing handle is kept, not replaced.
        assert "window.__ngTwinMap = map;" in js


class TestTheLandingHeroMoves:
    def test_the_scattered_field_is_seeded_so_it_does_not_reshuffle(self):
        """
        A field that lands somewhere new on every render reads as a glitch,
        and nothing can be asserted about it.
        """
        js = _without_comments(_asset("js", "landing.js"))
        assert "function seeded(seed)" in js
        fn = js[js.index("function starMarkup()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "seeded(20260905)" in fn, fn
        assert "Math.random" not in fn, fn
        assert "const STAR_COUNT = 132;" in js

    def test_the_panelled_fields_doubled_and_come_in_two_grades(self):
        js = _asset("js", "landing.js")
        block = js[js.index('<g class="lw-dotfields"'):]
        block = block[:block.index("</g>")]
        assert block.count("<rect") == 6, block
        assert 'url(#lw-dots-fine)' in block
        assert 'id="lw-dots-fine"' in js

    def test_the_freight_follows_the_corridor_geometry(self):
        """
        A dashed beam slides; it does not travel. These are on the corridor's
        own path, so a packet leaves one hub and arrives at the other.
        """
        js = _without_comments(_asset("js", "landing.js"))
        fn = js[js.index("function packetMarkup()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "<animateMotion" in fn, fn
        assert "<mpath" in fn, fn
        assert 'href="#lw-c${i}"' in fn, fn
        # The id it points at is written by the corridor that drew the path.
        corr = js[js.index("function corridorMarkup()"):]
        corr = corr[:corr.index("\n}\n")]
        assert 'id="lw-c${i}"' in corr, corr

    def test_the_packets_move_at_one_speed_not_one_duration(self):
        """
        A fixed duration sends the short hops crawling and the long ones
        sprinting, on the same map, at the same time.
        """
        js = _without_comments(_asset("js", "landing.js"))
        fn = js[js.index("function packetMarkup()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "Math.hypot" in fn, fn
        assert "Math.min(13, Math.max(5.5," in fn, fn

    def test_the_hubs_carry_two_rings_at_different_speeds(self):
        js = _asset("js", "landing.js")
        assert "lw-hub-pulse-slow" in js
        css = _asset("css", "landing.css")
        assert "@keyframes hubPulseRingWide" in css
        rule = css[css.index("#landing-page .lw-hub-pulse-slow {"):]
        rule = rule[:rule.index("}")]
        assert "animation-duration: 5.4s" in rule, rule
        # Not a multiple of the fast ring's 3.2s, so they never land together.
        assert "3.2s" not in rule

    def test_none_of_it_runs_for_someone_who_asked_for_less_motion(self):
        """
        Every one of these is decoration. Someone who has set "reduce motion"
        at the OS level has said that is not welcome, and nothing here carries
        information — so it all stops, and the hero is the same picture.
        """
        css = _asset("css", "landing.css")
        block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
        block = block[:block.index("/* ─── Narrow / squat")]
        for stopped in (".lw-dotfields rect", ".lw-star", ".minimal-hub-pulse",
                        ".lw-hub-glow", ".minimal-route-beam",
                        ".landing-map-container::after"):
            assert stopped in block, stopped
        assert "animation: none !important;" in block
        # SMIL is not a CSS animation and does not stop with one.
        assert ".lw-packets { display: none; }" in block, block
        # The dots stay ON the page — stopping the twinkle must not blank them.
        assert "opacity: var(--lw-star-peak" in block, block

    def test_the_movement_is_slow_enough_to_be_peripheral(self):
        """
        Decoration behind a sign-in form is welcome at one speed. Nothing here
        cycles faster than 2.6s, and the panels take nine seconds.
        """
        css = _asset("css", "landing.css")
        block = css[css.index("#landing-page .lw-dotfields {"):]
        block = block[:block.index("/* ─── Corridors")] if "/* ─── Corridors" in block else block
        assert "lwFieldBreathe 9s" in css
        js = _asset("js", "landing.js")
        fn = js[js.index("function starMarkup()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "(2.6 + rand() * 4.2)" in fn, fn

    def test_the_new_marks_use_the_brand_purple(self):
        """
        A white bead on a white corridor is nothing, and a pale tint on a
        near-white page is measurably present and visually absent — which is
        what the first cut of both of these was.
        """
        css = _asset("css", "landing.css")
        for rule_name in ("#landing-page .lw-star {",
                          "#landing-page .lw-packet-glow {"):
            rule = css[css.index(rule_name):]
            rule = rule[:rule.index("}")]
            assert "146, 24, 234" in rule, (rule_name, rule)
