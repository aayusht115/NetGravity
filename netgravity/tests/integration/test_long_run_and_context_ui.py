"""
Three things a reader could not do on the Scenario Planner.

Each was measured on a live run, and each is a different kind of failure:

  * THEY COULD NOT LEAVE. A demand scenario scoped to one product category
    across every region took over 700 seconds. For all of it the workspace sat
    behind a blurred scrim with no exit — the Digital Twin, the assistant and
    every other screen unreachable — and nothing on the dialog said the wait
    would be that long. A reader with no way out and no end in sight concludes
    the application has hung. The work never needed the dialog: aborting a
    fetch does not abort an optimiser, which is exactly why
    `findScenarioCreatedSince` exists.

  * THE ACTIONS ANSWERED A DIFFERENT QUESTION. Raising demand by half produced
    the same two entries as raising it by five percent — review the changes,
    ask the assistant. Neither says which sites have to carry the volume, how
    hard they have to run, or where the network has nothing left. The
    per-facility values were on the record already; nothing read them.

  * THE ASSISTANT DID NOT OPEN. "Ask Netgravity about this scenario" called
    `askChatbotPrompt`, which renders into an overlay it does not open. The
    button did nothing visible — and sent a question to the orchestrator
    anyway, which knows nothing about a saved scenario and would have answered
    about the baseline network minutes later.

These read the shipped sources, in the style of the other screen tests here:
the behaviour lives in template strings and event handlers a headless run can
only reach through a full solve, and the property worth protecting is that the
branch exists at all and says the right thing.
"""

from __future__ import annotations

import pathlib

import pytest

JS = pathlib.Path(__file__).resolve().parents[3] / "app" / "frontend" / "js"
CSS = pathlib.Path(__file__).resolve().parents[3] / "app" / "frontend" / "css"


def _js(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def scenarios_js() -> str:
    return _js("scenarios.js")


@pytest.fixture(scope="module")
def loading_js() -> str:
    return _js("agent-loading.js")


@pytest.fixture(scope="module")
def tray_js() -> str:
    return _js("background-tasks.js")


# ---------------------------------------------------------------------------
# 1. A run that can be left
# ---------------------------------------------------------------------------

class TestALongSolveCanBeLeftRunning:

    def test_the_dialog_offers_the_exit(self, loading_js):
        assert "agl-background" in loading_js
        assert "Continue in the background" in loading_js
        assert "export function offerBackgroundExit" in loading_js
        assert "export function withdrawBackgroundExit" in loading_js

    def test_the_offer_is_opt_in_per_run(self, loading_js):
        """
        Only a flow that can genuinely survive the dialog closing may offer it,
        so `startRun` must not enable it for every caller. A run whose result
        is reported by the dialog itself would be lost.
        """
        start = loading_js.index("export function offerBackgroundExit")
        assert "backgroundHandler = typeof onBackground === 'function'" in \
            loading_js[start:start + 600]
        # Nothing else turns it on: the declaration, this setter, and the two
        # places that clear it (the click handler and `withdrawBackgroundExit`).
        assert loading_js.count("backgroundHandler = ") == 4

    def test_the_offer_does_not_outlive_its_run(self, loading_js):
        block = loading_js[loading_js.index("export function dismissAgentLoading"):]
        assert "withdrawBackgroundExit()" in block

    def test_pressing_it_drains_the_pacing_first(self, loading_js):
        """
        `dismissAgentLoading` waits for the narration queue to empty before it
        closes — right when a run has ENDED, wrong here. The reader has asked
        for the screen back; holding the scrim for another few seconds of
        read-out is the opposite of giving it to them.
        """
        block = loading_js[loading_js.index("els.background.addEventListener"):]
        block = block[:block.index("  });")]
        assert "flush();" in block

    def test_the_scenario_run_offers_it(self, scenarios_js):
        block = scenarios_js[scenarios_js.index("async function runScenarioCreation"):]
        assert "offerBackgroundExit(" in block
        assert "startBackgroundTask(" in block

    def test_leaving_reports_to_the_tray_and_staying_to_the_dialog(self, scenarios_js):
        """
        Every terminal branch has to pick, and picking has to happen in one
        place — otherwise a branch gets written for the dialog and forgets that
        the dialog may not be there.
        """
        block = scenarios_js[scenarios_js.index(
            "const reportFailure = (step, message, diagnosis = null)"):]
        block = block[:block.index("\n  };")]
        assert "if (backgrounded)" in block
        assert "failBackgroundTask(taskId, message)" in block
        # It carries the solver's diagnosis to the form as well, so a refused
        # run can say how much demand the network could not serve.
        assert "showCreationError(message, null, diagnosis)" in block

    def test_a_failure_that_happened_offscreen_is_still_reported(self, scenarios_js):
        # Both terminal branches route through it: the solve that never
        # returned, and the result that could not be mapped.
        assert scenarios_js.count("reportFailure(") == 2

    def test_the_result_is_announced_where_the_reader_went(self, scenarios_js):
        block = scenarios_js[scenarios_js.index("if (backgrounded) {\n    finishBackgroundTask"):]
        block = block[:block.index("\n  }\n}")]
        assert "openLabel: 'View results'" in block
        assert "navigateToTab('scenarios')" in block

    def test_the_notice_is_not_a_modal(self, tray_js):
        """
        The reader left this run so they could work somewhere else. Taking that
        screen away the moment the solve lands would undo the thing they asked
        for.
        """
        assert "modal" not in tray_js.lower().split("WHAT IT IS NOT")[-1][:200]
        assert "aria-live" in tray_js
        assert "role" in tray_js

    def test_a_failure_notice_does_not_clear_itself(self, tray_js):
        block = tray_js[tray_js.index("export function failBackgroundTask"):]
        block = block[:block.index("\n}")]
        assert "setTimeout" not in block

    def test_the_tray_stores_no_business_value(self, tray_js):
        """
        §5 and §9: this is a title, a clock and a status for a promise the
        caller still holds. It is not a second state store, and it must never
        become a place a KPI is kept or derived.
        """
        # The prose above says what this module is not; the assertion is
        # about the code, so the header comment is cut off first.
        code = tray_js[tray_js.index("const TRAY_ID"):].lower()
        for forbidden in ("cost", "utilization", "utilisation", "fill_rate",
                          "kpi", "scenariokpis"):
            assert forbidden not in code, forbidden

    def test_nothing_is_restored_from_storage(self, tray_js):
        """
        A pill restored for a promise nobody holds counts up forever against a
        result that can never arrive on this page. What survives a reload is
        the scenario, on the server.
        """
        assert "localStorage" not in tray_js
        assert "sessionStorage" not in tray_js

    def test_the_tray_leaves_the_assistant_reachable(self):
        """
        The reason to leave a solve running is usually to go and use the
        assistant, whose floating button owns the bottom right.
        """
        css = (CSS / "style.css").read_text(encoding="utf-8")
        block = css[css.index(".ng-bg-tray {"):]
        block = block[:block.index("}")]
        assert "left:" in block
        assert "right:" not in block


# ---------------------------------------------------------------------------
# 2. Actions that answer the question that was asked
# ---------------------------------------------------------------------------

class TestTheActionsSayWhatTheChangeAsksOfTheNetwork:

    def test_the_capacity_account_is_in_the_detail_view(self, scenarios_js):
        """
        WHICH SITES THE PLAN FILLS moved off the card and into the drawer.

        It was three rows on the card and five in the drawer, on the reasoning
        that a recommendation needs its supporting detail beside it. In use it
        was 203px of a 966px card, and the card ran to 1,519px — so the thing
        it exists to say, the recommended action, was below the fold. That is
        the same defect the three rows were trimmed to avoid, one level up.

        It is the WORKING behind the recommendation, and the drawer is where
        the working goes: one press, under the heading it already had.
        """
        assert "function capacityResponseHtml(scn, { title = true, rows = 5 } = {})" \
            in scenarios_js
        drawer = scenarios_js[scenarios_js.index("export function openScenarioDrawer"):]
        assert "capacityResponseHtml(scn, { title: false })" in drawer
        # And no longer on the card.
        card = scenarios_js[scenarios_js.index("container.innerHTML = takeHeadHtml("):]
        card = card[:card.index("container.querySelectorAll(")]
        assert "capacityResponseHtml(" not in card, card

    def test_it_renders_nothing_when_the_backend_supplied_nothing(self, scenarios_js):
        block = scenarios_js[scenarios_js.index("function capacityResponseHtml(scn,"):]
        head = block[:block.index("const units =")]
        assert "if (!cr) return '';" in head

    def test_the_drawer_answers_the_same_question_from_the_same_block(
            self, scenarios_js):
        """
        "Review the proposed changes" opened onto a corridor table — the audit
        of which lanes moved volume, and the wrong grain for a scenario that
        raised demand by half. Same figures as the card, from the same backend
        block, so the two cannot disagree.
        """
        block = scenarios_js[scenarios_js.index("export function openScenarioDrawer"):]
        assert "capacityResponseHtml(scn, { title: false })" in block
        # The lane table is not removed: nothing else reports it.
        assert "Corridor" in block

    def test_the_mapper_carries_it_through(self):
        mapper = (JS / "integration" / "mappers" / "scenario-mapper.js").read_text(
            encoding="utf-8")
        assert "capacityResponse: raw.capacity_response || null," in mapper

    def test_the_screen_renders_the_recommendations_it_was_given(
            self, scenarios_js):
        """
        WHICH actions to recommend is now decided on the server, and the
        substance of that decision is held in `test_scenario_document.py`
        against `_recommended_actions` itself — a full site gets an action
        naming it, reopening is offered before building, a new site only where
        a region has no room left.

        It moved because deriving it here meant the reasoning behind a
        recommendation to spend money lived in a render function, could not be
        audited, and had to be written a second time for the document. What is
        left to assert HERE is what this file is about: that the screen renders
        what it was given and decides nothing of its own.
        """
        block = scenarios_js[scenarios_js.index("function recommendedActions(scn, comparison)"):]
        block = block[:block.index("\n/**")]
        # The comparison's list first — recomputed against the record as it now
        # stands — with the record's own as the fallback.
        assert "comparison.recommended_actions" in block
        assert "scn.recommendedActions" in block
        # No threshold, no capacity arithmetic, no site selection here.
        for derived in ("cap.regions_without_room", "at_ceiling", "util_pct",
                        "capacityResponse", "unserved"):
            assert derived not in block, derived

    def test_every_action_key_opens_the_form_that_performs_it(self, scenarios_js):
        """
        The one thing the screen still owns: what pressing a recommendation
        does. A key the server can send and the screen cannot open would be a
        recommendation with no way to act on it.
        """
        block = scenarios_js[scenarios_js.index("function recommendedActions(scn, comparison)"):]
        block = block[:block.index("\n/**")]
        assert "openCreateToolboxWith('CHANGE_CAPACITY'" in block
        assert "openCreateToolboxWith('OPEN_FACILITY'" in block
        assert "openCreateToolboxWith('CHANGE_DEMAND')" in block
        # Reopening names the site it reopens; building names no facility,
        # because there is not one yet.
        assert "openMode: 'EXISTING'" in block
        assert "openMode: 'NEW'" in block

    def test_a_statement_is_not_given_a_button(self, scenarios_js):
        """
        "No network change is indicated" is an answer to "what should I do",
        and a control under it would open a form contradicting the sentence.
        """
        block = scenarios_js[scenarios_js.index("function recommendedActions(scn, comparison)"):]
        block = block[:block.index("\n/**")]
        assert "row.key === 'NO_ACTION'" in block
        assert "statement:" in block

    def test_an_uploaded_site_name_cannot_inject_markup(self, scenarios_js):
        """
        These labels name sites now — "Add capacity at Toronto DC" — and the
        name came out of somebody's spreadsheet. Both the action list and the
        capacity rows are written with innerHTML.
        """
        for fn in ("function takeActionsHtml(actions)",
                   "function capacityResponseHtml(scn,"):
            block = scenarios_js[scenarios_js.index(fn):]
            block = block[:block.index("\n}\n")]
            assert "const esc = (t) =>" in block, fn

    def test_the_builder_opens_pointed_at_the_site_the_action_named(
            self, scenarios_js):
        block = scenarios_js[scenarios_js.index(
            "function openCreateToolboxWith(type, options = {})"):]
        block = block[:block.index("\n}\n")]
        assert "options.facilityId" in block
        assert "options.openMode" in block
        # An unknown id must not silently leave another site selected.
        assert "some((o) => o.value === options.facilityId)" in block


# ---------------------------------------------------------------------------
# 3. An assistant that opens, onto something it can actually answer from
# ---------------------------------------------------------------------------

class TestTheAssistantOpensOnThisScenario:

    def test_the_assistant_is_reachable_from_the_card(self, scenarios_js):
        """
        Talking about a scenario is not a thing to DO about the network, so it
        is no longer offered as a recommended action — it sits in the footer
        with the other ways into the analysis. It still has to open the panel:
        `askChatbotPrompt` alone renders into an overlay it does not open, so
        the button did nothing visible and sent a question to the orchestrator
        anyway.
        """
        footer = scenarios_js[scenarios_js.index("function takeFooterHtml()"):]
        footer = footer[:footer.index("\n}\n")]
        assert "scn-open-chat" in footer
        assert "Ask about this scenario" in footer

        assert "openChatAboutScenario(focus, comparison)" in scenarios_js
        block = scenarios_js[scenarios_js.index("function openChatAboutScenario("):]
        block = block[:block.index("\n}\n")]
        assert "window.openChatbotWithBriefing(" in block

    def test_the_chatbot_exposes_a_way_in_that_does_not_restore_a_thread(self):
        """
        `openChatbotModal` brings back whatever conversation the tab was last
        having — right when the reader opens the assistant themselves, wrong
        when they arrive from a specific result: the thing they pressed would
        be replaced by an unrelated conversation.
        """
        chat = _js("chatbot.js")
        assert "export function openChatbotWithBriefing" in chat
        block = chat[chat.index("export function openChatbotWithBriefing"):]
        block = block[:block.index("\n}")]
        assert "restoreConversation" not in block
        assert "storeConversationId(null)" in block

    def test_it_spends_no_request_of_its_own(self):
        chat = _js("chatbot.js")
        block = chat[chat.index("export function openChatbotWithBriefing"):]
        block = block[:block.index("\n}")]
        for forbidden in ("chatService", "sendMessage", "fetch(", "await "):
            assert forbidden not in block, forbidden

    def test_the_follow_up_is_offered_not_sent(self):
        """
        Putting a question in the box and leaving it there is the difference
        between suggesting one and asking it — and asking it would spend a
        request from a shared budget that the reader did not ask to spend.
        """
        chat = _js("chatbot.js")
        block = chat[chat.index("export function openChatbotWithBriefing"):]
        block = block[:block.index("\n}")]
        assert "input.value = question" in block
        assert "askChatbotPrompt" not in block

    def test_the_briefing_is_composed_only_from_what_the_backend_returned(
            self, scenarios_js):
        block = scenarios_js[scenarios_js.index("function openChatAboutScenario(scn, comparison)"):]
        block = block[:block.index("\n/** One authoritative KPI value")]
        # The scenario's own briefing, the ranking's verdict, the capacity
        # account. Nothing derived here.
        assert "scn.explanation && scn.explanation.card" in block
        assert "scn.capacityResponse" in block
        assert "comparison.verdict" in block

    def test_it_says_which_voice_wrote_the_briefing(self, scenarios_js):
        """
        A briefing that reads as the model's when the deterministic template
        wrote it is the one thing this surface must not do. The card says so
        above the fold and the assistant has to say it too.
        """
        block = scenarios_js[scenarios_js.index("function openChatAboutScenario(scn, comparison)"):]
        block = block[:block.index("\n/** One authoritative KPI value")]
        assert "card.source === 'llm'" in block
        # Reworded away from "the deterministic template", which names a code
        # path rather than telling a reader who wrote the words.
        assert "without a model" in block.lower()

    def test_every_uploaded_value_is_escaped_into_the_thread(self, scenarios_js):
        """
        The thread is rendered with innerHTML and these values came out of a
        spreadsheet somebody uploaded.
        """
        block = scenarios_js[scenarios_js.index("function openChatAboutScenario(scn, comparison)"):]
        block = block[:block.index("\n/** One authoritative KPI value")]
        assert "const esc = (t) =>" in block
        assert "esc(name)" in block
        assert "esc(r.name)" in block
