"""
One loading screen, and a form that is not pinned to one country.

THE LOADING STATE
-----------------
The application had three. The agent dialog, which reports real dispatches;
a scenario "execution view" drawn inside the scenario modal, six phases with a
spinner, showing the same wait the dialog was already covering the screen to
show; and `agent-reasoning.js` — a modal that advanced four stages on 450ms
timers, filled a progress bar to 100%, and printed lines like "NetGravity
Agentic Kernel v2.4 initialized" into a fake terminal, in front of a tab
change with no work happening behind it at all.

The chatbot is the deliberate exception. A modal over a conversation would
hide the thing the reader is waiting on, and a chat turn is a message in a
thread rather than a screen being rebuilt — so it waits inline, with a word
that changes so a long wait is legible.

THE NEW-SITE FORM
-----------------
"Jump to a city" offered thirty-two Indian cities on every network in every
country, and the latitude and longitude inputs carried India's bounding box as
their `min`/`max` — so on a US network the form opened at its own network's
centroid with the field already out of range, and the browser refused to submit
a scenario the solver would have accepted.
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


def _exists(*parts: str) -> bool:
    path = FRONTEND
    for part in parts:
        path = path / part
    return path.exists()


def _without_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


class TestOnlyOneLoadingScreen:
    def test_the_fabricated_reasoning_pipeline_is_gone(self):
        assert not _exists("js", "agent-reasoning.js")
        assert not _exists("css", "agent-reasoning.css")
        html = _asset("index.html")
        assert "agent-reasoning" not in html
        assert "agent-reasoning-modal-overlay" not in html
        # Its terminal, its stage cards and its progress bar with it.
        for gone in ("agent-terminal-content", "agent-stage-card",
                     "agent-progress-fill", "Agentic Kernel"):
            assert gone not in html, gone

    def test_nothing_still_calls_it(self):
        for name in ("app.js", "actions.js", "scenarios.js", "chatbot.js"):
            js = _asset("js", name)
            assert "triggerAgentReasoning" not in js, name
            assert "completeReasoningSequence" not in js, name
            assert "agent-reasoning.js" not in js, name

    def test_exploring_in_the_twin_just_navigates(self):
        js = _without_comments(_asset("js", "actions.js"))
        fn = js[js.index("exploreInTwin:"):]
        fn = fn[:fn.index("},") + 2]
        assert "navigateToTab" in fn, fn
        assert "Reasoning" not in fn, fn

    def test_the_scenario_page_has_no_loading_state_of_its_own(self):
        """
        It drew six phases with a spinner INSIDE the scenario modal while the
        agent dialog was on screen in front of it, reporting the same request.
        Two loading states for one wait, free to disagree about how far along
        it was.
        """
        html = _asset("index.html")
        assert "solver-phase-steps" not in html
        assert "step-phase-" not in html
        assert "telemetry-spinner" not in html
        assert "agent-pulse-header" not in html
        js = _asset("js", "scenarios.js")
        assert "renderCreationProgress" not in js
        assert "CREATION_STEPS" not in js
        # The panel survives, for the one thing it is still needed for.
        assert 'id="agent-execution-view"' in html
        assert "showCreationError" in js

    def test_the_dead_ingestion_popup_mount_is_gone(self):
        assert "loading-modal-overlay" not in _asset("index.html")
        assert "loading-modal-overlay" not in _asset("js", "ingestion.js")
        # The CODE, not the prose: a comment there records that the fake
        # progress ring existed and why it was removed, which is worth keeping.
        assert "runLoadingOverlay" not in _without_comments(_asset("js", "ingestion.js"))

    def test_the_styles_those_screens_used_went_with_them(self):
        css = _asset("css", "style.css")
        for gone in (".telemetry-spinner", ".agent-pulse-header",
                     ".agent-pulse-icon", ".agent-progress-bar-fill"):
            assert gone not in css, gone

    def test_the_home_refresh_raises_it_too(self):
        """
        It re-runs `hydrateFromBackend` — the whole analysis, twenty to forty
        seconds of it on a cold solve — and the only sign was a 24px icon
        spinning, with the dashboard behind it still showing the previous
        figures as though nothing were happening.
        """
        js = _without_comments(_asset("js", "app.js"))
        # The LISTENER, not the first mention: another line clicks the same
        # button programmatically.
        fn = js[js.index("getElementById('home2-refresh-btn')?.addEventListener"):]
        fn = fn[:fn.index("\n  });")]
        assert "beginAnalysisLoading" in fn, fn
        assert "endAnalysisLoading" in fn, fn
        assert "reportAnalysisStage" in fn, fn

    def test_every_full_screen_wait_raises_the_same_dialog(self):
        """
        The two flows that hold the whole application — opening a project and
        running a scenario — both go through `agent-loading.js`. Nothing else
        may raise a screen-covering wait.
        """
        for name in ("analysis-loading.js", "scenarios.js"):
            js = _asset("js", name)
            assert "agent-loading.js" in js, name
        overlays = []
        for path in sorted((FRONTEND / "js").glob("*.js")):
            body = _without_comments(path.read_text(encoding="utf-8", errors="replace"))
            if "position: fixed" in body and "inset: 0" in body:
                overlays.append(path.name)
        assert overlays == [], overlays


class TestTheChatbotWaitsInline:
    """The one place that does NOT raise the dialog, and why."""

    def test_it_shows_a_word_that_changes(self):
        js = _asset("js", "chatbot.js")
        assert "THINKING_WORDS" in js
        words = js[js.index("const THINKING_WORDS = ["):]
        words = words[:words.index("];")]
        names = re.findall(r"'([^']+)'", words)
        assert len(names) >= 12, names
        # Whimsical, and deliberately so. The alternative — plausible-sounding
        # stage names — is the exact failure this codebase has twice undone.
        for banned in ("Solving", "Optimising", "Optimizing", "Running MILP",
                       "Executing", "Computing", "Analysing your network"):
            assert banned not in names, banned

    def test_it_does_not_raise_the_loading_dialog(self):
        js = _asset("js", "chatbot.js")
        assert "agent-loading.js" not in js
        assert "mountAgentLoading" not in js

    def test_the_timer_is_cleared_on_every_exit(self):
        js = _without_comments(_asset("js", "chatbot.js"))
        assert js.count("clearInterval(thinkingTimer)") >= 1
        fn = js[js.index("function removeTypingIndicator()"):]
        fn = fn[:fn.index("\n}\n")]
        assert "clearInterval" in fn, fn
        # Both the success path and the failure path call it.
        assert js.count("removeTypingIndicator();") >= 3, js.count("removeTypingIndicator();")

    def test_a_screen_reader_is_not_read_the_whole_word_list(self):
        js = _asset("js", "chatbot.js")
        assert 'aria-label="Waiting for a reply"' in js
        assert 'class="chat-thinking-word" aria-hidden="true"' in js

    def test_the_elapsed_count_is_a_real_number(self):
        js = _asset("js", "chatbot.js")
        assert "Date.now() - startedAt" in js
        assert "THINKING_ELAPSED_AFTER_MS" in js


class TestTheNewSiteFormFollowsTheData:
    def test_no_hardcoded_city_list_remains(self):
        js = _asset("js", "scenarios.js")
        assert "INDIA_SITE_PRESETS" not in js
        code = _without_comments(js)
        for city in ("Coimbatore", "Guwahati", "Visakhapatnam", "Bhubaneswar",
                     "Madurai", "Vadodara", "Siliguri"):
            assert city not in code, city

    def test_the_places_come_from_the_uploaded_network(self):
        js = _asset("js", "scenarios.js")
        assert "function networkPlacePresets()" in js
        fn = _without_comments(js)
        fn = fn[fn.index("function networkPlacePresets()"):]
        fn = fn[:fn.index("\n}\n")]
        # Markets first: they are the named places a network serves.
        assert "MARKETS.forEach" in fn
        assert "DCS.forEach" in fn and "PLANTS.forEach" in fn
        assert fn.index("MARKETS.forEach") < fn.index("DCS.forEach")

    def test_the_regions_come_from_the_networks_own_country(self):
        js = _asset("js", "scenarios.js")
        assert "countriesContaining" in js and "loadAdmin1" in js
        assert "hydrateSitePresetRegions" in js
        # Called, not merely defined.
        assert js.count("hydrateSitePresetRegions") >= 2, js.count("hydrateSitePresetRegions")

    def test_the_coordinate_inputs_accept_the_whole_globe(self):
        """
        The real defect. `min="6" max="38"` on latitude and `min="67"
        max="98"` on longitude is India's bounding box: on a US network the
        form opened at longitude -98, already outside its own field's range,
        and the browser blocked a submission the solver would have accepted.
        """
        js = _asset("js", "scenarios.js")
        lat = re.search(r'id="toolbox-site-lat"[^>]*min="(-?[\d.]+)" max="(-?[\d.]+)"', js)
        lng = re.search(r'id="toolbox-site-lng"[^>]*min="(-?[\d.]+)" max="(-?[\d.]+)"', js)
        assert lat, "latitude input not found"
        assert lng, "longitude input not found"
        assert float(lat.group(1)) <= -84 and float(lat.group(2)) >= 84, lat.groups()
        assert float(lng.group(1)) <= -180 and float(lng.group(2)) >= 180, lng.groups()

    def test_the_form_no_longer_claims_one_country(self):
        code = _without_comments(_asset("js", "scenarios.js"))
        assert "anywhere in India" not in code
        assert "in India" not in code, [
            line for line in code.splitlines() if "in India" in line]
