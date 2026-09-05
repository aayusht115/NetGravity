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


def _fn(js: str, signature: str) -> str:
    """One function body, from its signature to the next top-level close."""
    start = js.index(signature)
    return js[start:js.index("\n}\n", start)]


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

    def test_the_panel_that_held_it_is_gone_too(self):
        """
        SUPERSEDED FORM: this used to assert the panel SURVIVED, "for the one
        thing it is still needed for" — carrying a creation error. It was
        still being revealed on every run, and by then it was empty, so each
        scenario opened a blank white card. In front of the loading dialog,
        because `.modal-overlay` is z-index 999999 and the dialog was 99995.

        A refusal belongs beside the form that has to change, with the user's
        inputs still in it, so the error moved into the form body and the
        panel went with the display it used to hold.
        """
        html = _asset("index.html")
        js = _asset("js", "scenarios.js")
        assert "agent-execution-view" not in html
        # The CODE, not the prose: a comment there records what the panel was
        # and why it went, which is the part worth keeping.
        assert "agent-execution-view" not in _without_comments(js)
        assert "showCreationError" in js
        # The refusal is written into the form, and the form is put back up.
        fn = js[js.index("function showCreationError(message, fieldId = null)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "formBody.appendChild(banner)" in fn, fn
        assert "modal-create-toolbox" in fn, fn
        # And the swap that "Back to the form" existed to undo is gone with it.
        assert "scn-creation-back" not in js

    def test_a_run_closes_the_modal_that_started_it(self):
        js = _asset("js", "scenarios.js")
        fn = js[js.index("async function runScenarioCreation()"):]
        fn = fn[:fn.index("  let solved;")]
        assert "classList.remove('visible')" in fn, fn
        # And it does not close before the form has been accepted, or a
        # refusal would have nowhere to be read.
        assert fn.index("readScenarioForm()") < fn.index("classList.remove('visible')")

    def test_the_loading_dialog_is_above_everything(self):
        """
        It was below every modal in the application. Any modal open when a run
        started drew over the screen that had just blurred the workspace and
        taken every click — measured on the scenario planner, where the modal
        was still up and had been emptied.
        """
        css = _asset("css", "agent-loading.css")
        style = _asset("css", "style.css")
        agl = int(re.search(r"z-index: (\d+);", css).group(1))
        # ".modal-overlay" is the competitor, and the one that caused the bug.
        # (".modal-content" carries a higher number, but it is nested INSIDE
        # that overlay's stacking context and cannot climb out of it.)
        rule = style[style.index(".modal-overlay {"):]
        overlay = int(re.search(r"z-index: (\d+)", rule[:rule.index("}")]).group(1))
        assert agl > overlay, (agl, overlay)

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

    def test_there_is_no_clock(self):
        """
        SUPERSEDED FORM: this used to assert the elapsed count WAS a real
        number rather than a fabricated progress figure, which it was. The
        objection to it is different — the moving words and the dots already
        say the request is alive, and a number climbing beside them turns
        waiting into watching it climb.
        """
        js = _asset("js", "chatbot.js")
        code = _without_comments(js)
        for gone in ("THINKING_ELAPSED_AFTER_MS", "chat-thinking-elapsed",
                     "startedAt"):
            assert gone not in code, gone
        assert ".chat-thinking-elapsed" not in _without_comments(
            _asset("css", "chatbot.css"))
        # What says the wait is alive is still there.
        assert "THINKING_WORDS" in js and "typing-dot" in js

    def test_no_version_or_engine_mode_above_the_thread(self):
        """
        It read "NetGravity v2.0.0 - grounded answers, deterministic". Both
        halves were true and neither was the reader's: a build number and
        whether a language model happened to be reachable are facts about the
        deployment. What grounds an answer is said by the answer.
        """
        code = _without_comments(_asset("js", "chatbot.js"))
        assert "engineLabel" not in code, code
        assert "__ngServerStatus" not in code
        assert "grounded answers, deterministic" not in code

    def test_the_welcome_card_goes_when_the_conversation_starts(self):
        """
        "Hi there! I'm Netgravity AI, here to help you explore your network"
        sat above every thread for its whole length, introducing an assistant
        the reader was already talking to. Three functions set the FAQ list
        and the chat view by hand and none of them touched it — which is why
        there is now one switch rather than a fourth thing to remember.
        """
        js = _asset("js", "chatbot.js")
        assert "function showConversation(on)" in js
        fn = _fn(js, "function showConversation(on)")
        assert "chatbot-welcome-banner" in fn, fn
        assert "chatbot-faq-section" in fn and "chatbot-chat-view" in fn, fn
        # Every path through the panel uses it.
        code = _without_comments(js)
        assert code.count("showConversation(") >= 4, code.count("showConversation(")
        assert "faqSection.style.display = 'none'" not in code, (
            "a view switch is bypassing the one that hides the welcome card")


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
