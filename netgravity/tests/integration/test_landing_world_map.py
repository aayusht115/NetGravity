"""
The landing hero shows the world.

It used to be a 156 KB base64 raster of India — inlined TWICE, once in
index.html and again as `MAP_IMAGE_URI` in landing.js, the second replacing
the first on load, so every visit decoded that image in order to throw it
away. The product plans networks anywhere; one country was the wrong first
thing to see, and two copies of it was the wrong way to show it.

It is now drawn from `world-basemap.js` — the same Natural Earth outlines the
digital twin and the scenario map use. The tests here are about the four
things the change had to preserve or establish:

  * the world, from real data rather than a picture;
  * the animations that were already there, unchanged;
  * the palette, unchanged;
  * a bound that keeps the artwork off the sign-in and sign-up forms.
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
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


class TestTheHeroIsTheWorld:
    def test_the_india_raster_is_gone_from_both_places_it_lived(self):
        js = _asset("js", "landing.js")
        html = _asset("index.html")
        assert "MAP_IMAGE_URI" not in js
        assert "data:image/webp;base64" not in js
        assert "data:image/webp;base64" not in html
        # And the page is smaller for it, rather than larger.
        assert len(js) < 40_000, len(js)

    def test_it_is_drawn_from_the_data_the_application_already_ships(self):
        js = _without_comments(_asset("js", "landing.js"))
        assert "from './world-basemap.js'" in js
        assert "countryRings()" in js
        # Not a second copy of the geography, and not a second basemap module.
        assert "WORLD_COUNTRIES" not in js

    def test_the_projection_is_stated_and_confined_to_the_hero(self):
        """
        The hero stretches latitude by about 1.3 so a world reads beside a
        full-height sign-in column instead of as a stripe. That is decoration,
        and the comment says so — nothing is measured on it. The maps that DO
        carry claims project from the same data in their own modules.
        """
        js = _asset("js", "landing.js")
        assert "Equirectangular" in js
        assert "decoration behind a login form" in js
        code = _without_comments(js)
        assert "function px(lng)" in code and "function py(lat)" in code
        # The stretch is the frame's, not a fudge factor sprinkled about.
        assert "const VIEW_W = 1200;" in code
        assert "const VIEW_H = 625;" in code

    def test_antarctica_is_left_out_for_the_reason_that_it_has_to_be(self):
        """
        Its ring spans the antimeridian, so drawn flat it is a bar straight
        across the map. The rule is general — any ring wider than half the
        globe has wrapped — rather than a name check.
        """
        code = _without_comments(_asset("js", "landing.js"))
        assert "maxLng - minLng > 180" in code, code
        assert "Antarctica" not in code, "excluded by name rather than by shape"

    def test_six_hubs_at_real_coordinates_on_six_continents(self):
        code = _without_comments(_asset("js", "landing.js"))
        block = code[code.index("const HUBS = ["):code.index("];", code.index("const HUBS = ["))]
        lngs = [float(m) for m in re.findall(r"lng: (-?\d+\.?\d*)", block)]
        lats = [float(m) for m in re.findall(r"lat: (-?\d+\.?\d*)", block)]
        assert len(lngs) == 6 and len(lats) == 6, (lngs, lats)
        for v in lngs:
            assert -180 <= v <= 180, v
        for v in lats:
            assert -90 <= v <= 90, v
        # Spread across the globe, not clustered on one continent.
        assert max(lngs) - min(lngs) > 200, lngs
        assert max(lats) - min(lats) > 60, lats


class TestTheAnimationsAreTheOnesThatWereThere:
    def test_the_two_animations_are_reused_not_replaced(self):
        """The ask was for the animations as they are. Both classes, and both
        keyframes, are the ones the India hero used."""
        js = _without_comments(_asset("js", "landing.js"))
        css = _asset("css", "landing.css")
        assert "minimal-hub-pulse" in js
        assert "minimal-route-beam" in js
        assert "@keyframes hubPulseRing" in css
        assert "@keyframes routeBeamFlow" in css
        assert "@keyframes mapAmbientGlow" in css

    def test_the_hubs_do_not_pulse_in_lockstep(self):
        code = _without_comments(_asset("js", "landing.js"))
        block = code[code.index("const HUBS = ["):code.index("];", code.index("const HUBS = ["))]
        delays = re.findall(r"delay: '([^']+)'", block)
        assert len(delays) == 6, delays
        assert len(set(delays)) == 6, delays


class TestTheArtworkStaysOffTheForm:
    def test_the_width_is_taken_from_the_space_the_column_leaves(self):
        """
        The bound, not a guess. The left column is 720 reference units; the
        box subtracts that plus its gutter and right margin from the viewport,
        so its left edge lands at 790u whatever the viewport is.
        """
        css = _asset("css", "landing.css")
        rule = css[css.index("#landing-page .landing-hero-right {"):]
        rule = rule[:rule.index("}")]
        assert "calc(100vw - 845 * var(--u))" in rule, rule
        assert "min(58vw," in rule, rule
        # The old full-bleed sizing is gone with the raster it was cut for.
        assert "aspect-ratio: 952 / 941" not in css
        assert "aspect-ratio: 1200 / 625" in rule, rule
        # 845 = the column's 720 + gutter + this box's own right margin.
        column = css[css.index("#landing-page .landing-hero-left {"):]
        column = column[:column.index("}")]
        assert "width: calc(720 * var(--u));" in column, column

    def test_the_left_column_was_not_touched(self):
        """Everything on the left remains as it was."""
        css = _asset("css", "landing.css")
        for unchanged in ("#landing-page .landing-hero-left {",
                          "width: calc(720 * var(--u));",
                          "#landing-page .landing-auth-panel {",
                          "width: calc(398 * var(--u));"):
            assert unchanged in css, unchanged
        html = _asset("index.html")
        for unchanged in ('id="panel-signin"', 'id="panel-signup"',
                          'id="panel-reset"', 'class="landing-hero-left"'):
            assert unchanged in html, unchanged


class TestThePaletteIsUnchanged:
    def test_the_brand_tokens_are_what_they_were(self):
        css = _asset("css", "landing.css")
        for token, value in (("--brand-purple", "#9218EA"),
                             ("--brand-purple-dark", "#7c14c7"),
                             ("--brand-purple-light", "#f5edfc"),
                             ("--landing-bg", "#f2f1f8")):
            assert f"{token}: {value};" in css, token

    def test_the_new_artwork_introduces_no_new_colour(self):
        """
        Every fill and stroke on the map is the existing purple family or
        white. A hero that arrived with its own palette would be a second
        design system on the first screen anyone sees.
        """
        css = _asset("css", "landing.css")
        block = css[css.index("The landmass"):css.index("/* ─── Narrow / squat")]
        hexes = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", block)}
        assert hexes <= {"#ffffff"}, hexes
        js = _asset("js", "landing.js")
        grads = js[js.index("<defs>"):js.index("</defs>")]
        for stop in re.findall(r'stop-color="(#[0-9a-fA-F]{6})"', grads):
            r = int(stop[1:3], 16)
            g = int(stop[3:5], 16)
            b = int(stop[5:7], 16)
            # Purple: blue and red both above green, and blue the strongest.
            assert b > g and r > g, (stop, r, g, b)
