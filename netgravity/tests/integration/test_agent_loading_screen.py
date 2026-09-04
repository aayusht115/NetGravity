"""
The agent loading screen, and the one property that matters about it.

The approved design (`Dump/loading page.png`) draws five specialist layers
around an orchestrator with a signal travelling between whichever two are
talking. That picture is a claim about the system: it says these agents exist,
that this one is working now, and that this one is waiting on it. A claim has
to be true.

This repository has had to delete TWO animated agent pipelines that were not.
The first named a solver the product does not use, with a fabricated runtime
and a verdict no engine had produced. The second, `js/agent-reasoning.js`,
advanced four stages on 450ms timers and filled a progress bar to 100% in front
of a tab change — no request, no wait, no work. The replacement must not be
able to drift back into either, so the tests here are about PROVENANCE rather
than appearance:

  * the view holds no sequence of its own;
  * the recorder holds no sequence of its own either — every step comes from
    the caller that is about to make the request;
  * progress is settled dispatches over planned dispatches, never a timer;
  * the server's own execution trace is what supplies capability names,
    retries and failures;
  * a layer nobody dispatched to is drawn subdued, and stays that way.
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
    body = _without_comments(css)
    start = body.index(selector)
    open_brace = body.index("{", start)
    return body[open_brace + 1:body.index("}", open_brace)]


def _fn(js: str, signature: str) -> str:
    """One function body, from its signature to the next top-level close."""
    start = js.index(signature)
    return js[start:js.index("\n}\n", start)]


class TestTheVisualisationIsWhatTheDesignApproved:
    """
    `Dump/avengersloading.html`: a vertical phase tree in a small rectangular
    dialog — a node per phase on a connecting line, a spinner while it runs, a
    green tick when it lands, the title typed out and the detail streaming
    underneath, finished phases collapsing away.

    SUPERSEDED, deliberately. This class previously described the circular
    visualisation from `Dump/loading page.png`: five layer circles round an
    orchestrator with a signal travelling between whichever two were talking.
    That design was replaced wholesale. What did NOT change is the rule the
    rest of this file exists to enforce — the picture is drawn from what the
    application actually dispatched, and from the orchestrator's own record of
    what ran.
    """

    def test_the_reference_is_a_demo_and_none_of_it_was_copied(self):
        """
        The reference advances six hardcoded phases on a 1250ms `setTimeout`
        and prints invented logs — "Recruiting Iron Man", "Simulating
        14,000,605 outcomes". It is a mock-up of a loading screen, not a
        loading screen, and this repository has twice had to delete exactly
        that.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        for invented in ("Iron Man", "Black Widow", "S.H.I.E.L.D", "OMEGA",
                         "AES-256", "14,000,605", "threat matrix", "PHASES"):
            assert invented not in js, invented
        # No phase list of any kind: the tree is built from the run's steps.
        assert "run.steps.map(phaseHtml)" in js

    def test_a_timer_may_choose_when_a_fact_is_shown_never_what(self):
        """
        SUPERSEDED FORM: this used to ban `setTimeout` outright, bar two named
        uses. That was a proxy for the real property, and it stopped being a
        usable one when the reveals were paced — releasing a queue at reading
        speed is a timer, and a necessary one.

        The property itself is unchanged and is asserted directly instead:
        every item that reaches the screen is created in ONE place, `update`,
        by reading the recorder. The pump chooses the moment. It cannot choose
        the content, because it has none to choose from.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        update = _fn(js, "function update(run)")
        # Every push is inside `update`…
        assert js.count("queue.push(") == update.count("queue.push("), (
            "something outside `update` is putting items on the queue")
        assert update.count("queue.push(") >= 3, update
        # …and every one of them carries a step or a run, never a literal.
        for push in update.split("queue.push(")[1:]:
            body = push[:push.index("});") + 1]
            assert "step." in body or "run." in body or "prog." in body, body
        # The pump moves what it is handed and creates nothing.
        for mover in ("function reveal(item)", "function revealState(item)",
                      "function revealLog(item)", "function pump()"):
            fn = _fn(js, mover)
            assert "queue.push" not in fn, (mover, fn)

    def test_a_phase_is_a_dispatch(self):
        js = _without_comments(_asset("js", "agent-loading.js"))
        assert "run.steps.map(phaseHtml)" in js, (
            "the tree is built from something other than the run's steps")
        fn = js[js.index("function phaseState(step, runEnded)"):]
        fn = fn[:fn.index("\n}\n")]
        # Every phase state is a read of the step, not a schedule.
        for status in ("'active'", "'done'", "'failed'"):
            assert f"step.status === {status}" in fn, status

    def test_a_phase_reports_its_own_dispatch_and_not_another(self):
        """
        The recorder keeps one set of lines per LAYER, and a phase folds its
        layer's lines in. Two steps on the same layer therefore both read
        whatever that layer last said, and each printed the other's detail —
        measured, a five-step run on one layer put fifty items on the reveal
        queue instead of twenty-two, the same line repeating once more with
        every phase.

        A layer's lines belong to the last dispatch that went out on it, which
        is the one whose answer they describe. Every plan in the application
        today puts one step on a layer, so a real run is unchanged; a plan
        that shares one now reads correctly instead of repeating itself.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        logs = _fn(js, "function logsFor(run, step, index)")
        assert "ownsLayer(run, step)" in logs, logs
        owns = _fn(js, "function ownsLayer(run, step)")
        assert "s.layer !== step.layer" in owns, owns
        assert "s.startedAt" in owns, owns
        assert "owner.id === step.id" in owns, owns

    def test_the_four_states_are_drawn_differently(self):
        css = _asset("css", "agent-loading.css")
        for state in ("active", "done", "failed", "blocked"):
            assert f".agl-phase.{state} .agl-node" in css, state
        # A spinner only while something is running; a tick only when it landed.
        assert ".agl-phase.active .agl-node-spin { display: block; }" in css
        assert ".agl-phase.done .agl-node-tick { display: block; }" in css
        assert ".agl-phase.failed .agl-node-cross { display: block; }" in css

    def test_a_finished_phase_is_read_before_it_collapses(self):
        """
        SUPERSEDED FORM: this used to assert `max-height: 0` on `.agl-phase.done`
        itself, which is what made a fast dispatch unreadable — the phase wrote
        its lines and folded them away inside the same animation frame, so the
        detail existed only in the DOM. `.done` now stays open; the fold is a
        second class the view adds once the phase has had its time.

        The dialog is small and shows what is happening now, so it does still
        fold. The reason a run STOPPED is the exception, and stays.
        """
        css = _asset("css", "agent-loading.css")
        done = _rule(css, ".agl-phase.done {")
        assert "max-height: 0" not in done, done
        folded = _rule(css, ".agl-phase.done.collapsed {")
        assert "max-height: 0" in folded, folded
        # And the fold is the view's decision, taken after a stated hold.
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "function holdThenCollapse(id)")
        assert "PHASE_HOLD_MS" in fn, fn
        assert "classList.add('collapsed')" in fn, fn
        failed = _rule(css, ".agl-phase.failed {")
        assert "max-height: 0" not in failed, failed
        assert "opacity: 1" in failed, failed
        blocked = _rule(css, ".agl-phase.blocked {")
        assert "max-height: 0" not in blocked, blocked

    def test_someone_who_asked_for_less_motion_gets_less_motion(self):
        css = _asset("css", "agent-loading.css")
        block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
        assert "animation: none !important" in block
        for stilled in (".agl-orb::after", ".agl-node-spin", ".agl-caret",
                        ".agl-log", ".agl-shell"):
            assert stilled in block, stilled
        # And the title is not typed out a character at a time.
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = js[js.index("function typeTitle(el, text)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "prefersReducedMotion()" in fn, fn


class TestTheViewHoldsNoSequence:
    """
    The whole safety property. If the drawing knows an order, it can draw one
    that did not happen.
    """

    def test_there_is_no_stage_list_in_the_view(self):
        js = _without_comments(_asset("js", "agent-loading.js"))
        for banned in ("STAGES", "advance", "nextStage", "setTimeout(next"):
            assert banned not in js, f"{banned} is back in the view"
        # One interval only, and the next test pins what it is for.
        assert js.count("setInterval") == 1, (
            "a second timer in the view is a stage advancing on a clock"
        )

    def test_its_only_clock_counts_real_seconds(self):
        js = _asset("js", "agent-loading.js")
        fn = _fn(_without_comments(js), "function elapsedText(run)")
        assert "run.startedAt" in fn
        assert "Date.now()" in fn
        # The one interval in the file updates that clock and nothing else.
        assert _without_comments(js).count("setInterval") == 1
        # 100ms, because the elapsed figure now carries a tenth of a second —
        # a four-second run is the common case and "0s → 4s" says nothing
        # while it is happening.
        assert "clockTimer = setInterval(tick, 100)" in js
        assert "toFixed(1)" in js, "the clock stopped showing tenths"

    def test_everything_it_draws_comes_from_the_recorder(self):
        js = _asset("js", "agent-loading.js")
        assert "from './agent-activity.js'" in js
        assert "subscribe((run) => {" in js
        assert "This file draws. It decides nothing." in js


class TestTheRecorderOnlyRecords:
    def test_the_plan_is_the_callers_own_dispatches(self):
        js = _asset("js", "agent-activity.js")
        assert "export function startRun({ title, verb, subtitle, plan })" in js
        # No default plan, anywhere: a plan the recorder supplies is a script.
        assert "plan = [" not in _without_comments(js)

    def test_progress_is_settled_dispatches_over_planned_ones(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = _fn(js, "export function progress(snapshot)")
        assert "s.status === 'done' || s.status === 'failed'" in fn
        assert "r.steps.length" in fn
        assert "return null" in fn, (
            "with nothing to divide the view must show an indeterminate bar, "
            "not a number"
        )

    def test_layers_engaged_counts_layers_that_were_engaged(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = _fn(js, "export function layersEngaged(snapshot)")
        assert "st !== 'idle'" in fn

    def test_a_failure_is_recorded_as_a_failure(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = _fn(js, "export function stepFail(id,")
        assert "status = 'failed'" in fn
        assert "'failed'" in fn
        # Nothing is filled in for a step that did not produce anything.
        assert "detail = error" in fn

    def test_waiting_completion_failure_and_retry_are_all_expressible(self):
        js = _asset("js", "agent-activity.js")
        for state in ("'waiting'", "'active'", "'done'", "'failed'", "'retrying'"):
            assert state in js, state
        assert "export function stepRetry(id, attempt)" in js

    def test_parallel_dispatches_are_shown_in_parallel(self):
        """
        `hydrateFromBackend` issues three KPI requests inside one `Promise.all`.
        A layer with another step still open stays active rather than being
        marked complete by the first one to return.
        """
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = _fn(js, "export function stepDone(id,")
        assert "s.layer === step.layer && s.status === 'active'" in fn
        assert "busy ? 'active' : 'done'" in fn


class TestTheServersOwnRecordIsWhatFillsInTheDetail:
    def test_the_trace_endpoint_is_the_source(self):
        js = _asset("js", "agent-activity.js")
        assert "/orchestrator/executions/${encodeURIComponent(executionId)}/trace" in js

    def test_capabilities_are_mapped_by_the_registrys_own_names(self):
        """
        The live registry holds `extraction.parse`, `network.load_snapshot`,
        `forecast.demand`, `scenario.create`, `reasoning.synthesise` and
        eleven more. A layer lights up because one of those ran.
        """
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = _fn(js, "export function layerForCapability(name)")
        for prefix in ("'extraction.'", "'network.'", "'forecast.'", "'scenario.'",
                       "'reasoning.'", "'governance.'"):
            assert prefix in fn, prefix
        assert "return HUB" in fn, "an unknown capability is the orchestrator's own work"

    def test_a_retry_is_reported_because_the_trace_reported_it(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = js[js.index("async function absorbTrace(executionId)"):]
        assert "(inv.attempts || 1) > 1" in fn
        assert "inv.success === false" in fn
        assert "trace.errors" in fn

    def test_a_missing_trace_changes_nothing(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = js[js.index("async function absorbTrace(executionId)"):]
        assert "return;   // no trace is not an error" in _asset("js", "agent-activity.js")
        assert "catch (e) {" in fn

    def test_hydration_hands_over_the_execution_id(self):
        js = _without_comments(_asset("js", "integration", "hydrate.js"))
        assert "const stage = (id, state, detail, executionId) =>" in js
        assert "onStage(id, state, detail, executionId)" in js
        assert "networkRes && networkRes.execution_id" in js


class TestEveryCallerReportsItsOwnRealWork:
    def test_the_analysis_run_is_hydrations_own_five_awaits(self):
        js = _without_comments(_asset("js", "analysis-loading.js"))
        assert "HYDRATION_STAGES.map(" in js, (
            "the plan must be the list hydration actually executes"
        )
        assert "startRun({" in js

    def test_each_stage_names_the_layer_that_owns_its_route(self):
        js = _asset("js", "analysis-loading.js")
        block = js[js.index("const STAGE_LAYER = {"):js.index("/** What is being passed")]
        assert "structure: 'extraction'" in block
        assert "insights: 'reasoning'" in block
        assert "scenarios: 'scenario'" in block
        assert "forecast: 'forecasting'" in block
        assert "solve: 'orchestrator'" in block, (
            "the MILP is the orchestrator's own work; the design names no "
            "optimisation layer on the ring and inventing one would not be true"
        )

    def test_a_scenario_run_engages_only_what_a_scenario_run_uses(self):
        js = _without_comments(_asset("js", "scenarios.js"))
        fn = js[js.index("async function runScenarioCreation()"):]
        # Ends at the second dispatch. This used to slice on a call into the
        # scenario page's own step list, which no longer exists: that list was
        # a second loading state drawn behind the dialog for the same wait.
        fn = fn[:fn.index("stepStart('compare');")]
        assert "startRun({" in fn
        assert "layer: 'scenario'" in fn
        # And nothing else is claimed.
        for absent in ("layer: 'forecasting'", "layer: 'extraction'", "layer: 'reasoning'"):
            assert absent not in fn, f"{absent} did not run and must not be lit"

    def test_a_failed_scenario_is_reported_as_failed(self):
        js = _without_comments(_asset("js", "scenarios.js"))
        fn = js[js.index("async function runScenarioCreation()"):]
        assert "stepFail('simulate'" in fn
        assert "finishRun({ error: message })" in fn

    def test_the_fabricated_pre_analysis_popup_is_gone(self):
        """
        `finishIngestion` opened with a 2.2-second overlay whose progress ring
        was a `setInterval` counting its own forty ticks, and only then began
        the real work behind the analysis screen.
        """
        js = _without_comments(_asset("js", "ingestion.js"))
        fn = js[js.index("function finishIngestion()"):]
        fn = fn[:fn.index("function showNetworkNotice")]
        assert "runLoadingOverlay" not in fn, fn[:400]
        assert "beginAnalysisLoading(" in fn


class TestTheHomeCardsAreTheSizesAsked:
    def test_the_warning_card_is_compact(self):
        css = _without_comments(_asset("css", "home-overview.css"))
        alert = _rule(css, ".ov-alert {")
        assert "padding: 12px 15px" in alert, alert
        icon = _rule(css, ".ov-alert-icon {")
        assert "width: 30px" in icon
        fig = _rule(css, ".ov-alert-figure {")
        assert "font-size: 23px" in fig

    def test_what_it_gives_up_the_attention_card_gets(self):
        """
        They are the two rows of one column with a fixed total, so the height
        trimmed above is height the recommended next step and the further
        findings gain.
        """
        css = _without_comments(_asset("css", "home-overview.css"))
        rows = css[css.index(".ov-main {"):css.index(".ov-attn-card,")]
        assert "grid-template-rows: auto minmax(0, 1fr)" in rows
        assert "max-height: calc(100vh" in rows

    def test_the_further_findings_are_listed_rather_than_collapsed(self):
        js = _asset("js", "app.js")
        assert '<details class="ov-attn-rest" open>' in js


class TestTheTwinTooltipFollowsItsCanvas:
    def test_the_tooltip_is_re_parented_with_the_scene(self):
        """
        The canvas is a singleton shared by Home's preview and the Digital Twin
        tab. The tooltip was created inside whichever container initialised
        first and left there, so hovering a node on the other page wrote the
        facility's figures into an element in a hidden container — and
        returning to Home cleared that container and detached it for good.
        """
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "function attachHudTooltip()" in js
        fn = _fn(js, "function attachHudTooltip()")
        assert "hudTooltipEl.isConnected" in fn
        assert "hudTooltipEl.parentElement !== containerEl" in fn
        # Called on the re-parent path, not only on first init.
        init = js[js.index("export function initTwin3D(containerId)"):]
        init = init[:init.index("export function setTwin3DState")]
        assert init.count("attachHudTooltip();") >= 1
        assert "attachHudTooltip();" in _fn(js, "function setupInteraction()")

    def test_the_hover_test_still_runs_when_the_pointer_moves(self):
        js = _without_comments(_asset("js", "twin3d.js"))
        assert "canvas.addEventListener('mousemove', updateMouseCoords)" in js
        assert "pointerDirty = true;" in _fn(js, "function setupInteraction()")


class TestTheLoadingScreenIsADialog:
    """
    A pop-up over the workspace, not a page that replaces it.

    It was a full-screen page painted in the workspace's own colours, which
    made a wait look like a navigation: the screen the user was on
    disappeared, and there was nothing to say it was coming back. It is now a
    bounded dialog over a blurred copy of that screen — the work is happening
    TO what is behind it, and what is behind it stays visible.
    """

    def test_the_scrim_blurs_the_workspace_behind_it(self):
        css = _asset("css", "agent-loading.css")
        overlay = _rule(css, ".agl-overlay {")
        assert "backdrop-filter" in overlay, overlay
        assert "blur(" in overlay, overlay
        # A scrim, not an opaque page: the workspace has to read through it.
        assert "rgba(" in overlay, overlay
        assert "position: fixed" in overlay
        # Centred, so the dialog sits in the middle of the screen.
        assert "align-items: center" in overlay
        assert "justify-content: center" in overlay

    def test_the_dialog_is_smaller_than_the_screen_in_both_axes(self):
        css = _asset("css", "agent-loading.css")
        shell = _rule(css, ".agl-shell {")
        assert "min-height: 100vh" not in shell, (
            "the dialog is a full-height page again")
        # 560px: a small rectangular pop-up, not a page and not the 940px
        # canvas the circular design needed.
        assert "width: min(560px" in shell, shell
        assert "max-height: min(" in shell and "100vh" in shell, shell
        # Bounded, so it can never grow past the window; scrollable inside if
        # a long run puts more phases in it than the height allows.
        assert "overflow-y: auto" in shell, shell
        # And a floor, because finished phases collapse: without one the
        # dialog shrinks and grows every time a phase lands.
        assert "min-height:" in shell, shell

    def test_it_is_a_rectangle_holding_a_list(self):
        """
        SUPERSEDED. This asserted a square stage, because the previous design
        was a circle and a circle needs one. The finalised design is a
        rectangular dialog holding a vertical tree, and nothing in it is
        positioned by geometry at all.
        """
        css = _asset("css", "agent-loading.css")
        js = _asset("js", "agent-loading.js")
        assert ".agl-stage" not in css, "the circular stage is back"
        assert "aspect-ratio" not in css, css
        for gone in ("RADIUS", "pointFor", "LAYER_META", "agl-signal",
                     "agl-callout", "agl-hub"):
            assert gone not in js, gone
        # A column of phases, laid out by flow.
        tree = _rule(css, ".agl-tree {")
        assert "flex-direction: column" in tree, tree

    def test_it_uses_the_applications_type_scale(self):
        """
        The dialog must not invent a scale of its own. 15px/800 is the
        application's card-heading size; the reference's own 19px belongs to a
        720px page, not to a 560px pop-up.
        """
        css = _asset("css", "agent-loading.css")
        title = _rule(css, ".agl-title {")
        assert "font-size: 15px;" in title, title
        assert "vw" not in title, "the heading is sized off the viewport again"
        assert "font-size: 12px;" in _rule(css, ".agl-sub {")
        # Anchored at the start of a line: `.agl-phase.blocked
        # .agl-phase-title` contains the same substring and comes first.
        assert "font-size: 13.5px;" in _rule(css, "\n.agl-phase-title {")
        # And the reference's palette is not used: the app's token is.
        # The CODE, not the prose: the header comment names the reference's
        # purple in order to say it was not used.
        assert "#7c3aed" not in _without_comments(css), (
            "the reference's purple was copied in")

    def test_nothing_moves_without_a_cause(self):
        """
        The previous design had ambient motion — rings turning, particles
        twinkling — and the work needed to be told apart from it. This one has
        almost none: a spinner on a phase that is genuinely running, an
        entrance on a line that genuinely arrived, and a bar moving to a
        fraction that genuinely changed.
        """
        css = _asset("css", "agent-loading.css")
        body = _without_comments(css)
        for gone in ("agl-orbit", "agl-twinkle", "agl-travel", "agl-breathe",
                     "agl-node-breathe", "agl-dash", "agl-spin "):
            assert gone not in body, gone
        # What is left, and what causes each.
        assert ".agl-phase.active .agl-node-spin { display: block; }" in css
        assert "animation: agl-log-in" in body
        assert "transition: width" in _rule(css, ".agl-bar > span {")
        # The one exception is stated as one: a sweep across the mark, with no
        # state class on it, saying only that the dialog is live.
        orb = body[body.index(".agl-orb::after"):]
        orb = orb[:orb.index("}")]
        assert "agl-sweep-orb" in orb, orb
        for line in body.splitlines():
            if ".agl-orb" in line:
                assert "state-" not in line and ".active" not in line, line

    def test_a_hub_step_draws_no_hand_off_to_itself(self):
        """The solve is the orchestrator's own work — a step whose layer IS
        the hub. A line from the centre of the hub to the centre of the hub
        draws as a dot and reads as a fault."""
        js = _without_comments(_asset("js", "agent-activity.js"))
        push = _fn(js, "function pushSignal(signal)")
        assert "signal.from === signal.to" in push, push


class TestTheEventModelIsAFacade:
    """
    A vocabulary, not a second state machine.

    The frontend consumes named agent events — `agent_started`, `signal_sent`,
    `agent_completed` and the rest — so a producer does not have to know which
    function to call. The risk in that is a second store: an event path that
    keeps its own idea of what each agent is doing, drifting from the one the
    direct calls maintain, and a screen that shows whichever was written last.
    So every branch routes through the same recorder.
    """

    def test_it_understands_every_named_event(self):
        js = _asset("js", "agent-activity.js")
        for name in ("agent_started", "signal_sent", "agent_progress",
                     "agent_completed", "agent_waiting", "agent_failed",
                     "retry_started", "orchestration_updated",
                     "workflow_completed"):
            assert f"'{name}'" in js, name
        # Blocked is a state the orchestrator genuinely reports
        # (REQUIRES_APPROVAL, REQUIRES_HUMAN, its own blocked_steps), so it is
        # an event here too.
        assert "'agent_blocked'" in js

    def test_there_is_still_exactly_one_store(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        assert js.count("let run = null;") == 1, "a second run store appeared"
        # The event handler calls the recorder rather than reimplementing it.
        fn = js[js.index("export function applyAgentEvent(event)"):]
        fn = fn[:fn.index("\n}\n")]
        for call in ("stepStart(", "stepDone(", "stepFail(", "stepRetry(",
                     "finishRun(", "pushSignal(", "absorbTrace("):
            assert call in fn, f"applyAgentEvent does not go through {call}"

    def test_an_unknown_event_is_ignored_rather_than_guessed_at(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = js[js.index("export function applyAgentEvent(event)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "default:" in fn and "break" in fn, fn[-400:]


class TestItWatchesTheRunWhileItRuns:
    """
    The one thing the client cannot know on its own: what is happening inside
    a request that has not come back yet.
    """

    def test_it_reads_the_orchestrators_live_state(self):
        js = _asset("js", "agent-activity.js")
        assert "/orchestrator/executions/live" in js
        assert "correlation_id" in js
        # Mapped from the orchestrator's own enums, not from names invented here.
        for state in ("RUNNING", "WAITING", "REQUIRES_APPROVAL", "REQUIRES_HUMAN",
                      "INFEASIBLE", "COMPLETED", "FAILED"):
            assert state in js, state
        for status in ("SUCCESS", "PARTIAL", "RETRYABLE_FAILURE",
                       "NON_RETRYABLE_FAILURE", "INVALID_OUTPUT",
                       "INSUFFICIENT_EVIDENCE"):
            assert status in js, status

    def test_it_claims_nothing_about_the_capability_still_running(self):
        """
        `capability_status` holds OUTCOMES. Reading "three have finished, so
        the fourth must be running now" would be a guess dressed as a reading
        — and wrong the moment a plan runs two steps at once.
        """
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = js[js.index("function absorbLive(executions)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "LIVE_CAPABILITY[" in fn
        # Only settled outcomes reach a layer's state; a capability with no
        # recorded status is skipped.
        assert "if (!outcome) return;" in fn, fn

    def test_it_never_watches_its_own_polls(self):
        js = _asset("js", "agent-activity.js")
        assert "/orchestrator/executions" in js
        watcher = js[js.index("function ensureRequestObserver()"):]
        assert "indexOf('/orchestrator/executions') === 0" in watcher, (
            "polling the live route would announce itself and poll the poll")

    def test_the_first_poll_is_one_interval_away(self):
        """A request that answers quickly is never polled at all, so the
        screen costs nothing for the common case."""
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = js[js.index("function startWatching(correlationId)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "setTimeout(() => pollLive(correlationId), LIVE_POLL_MS)" in fn, fn

    def test_watching_stops_when_the_run_does(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        for fname in ("export function finishRun(", "export function clearRun("):
            fn = js[js.index(fname):]
            fn = fn[:fn.index("\n}\n")]
            assert "stopAllWatching()" in fn, fname

    def test_a_failed_poll_changes_nothing_on_screen(self):
        """The client's own account of its dispatches is already true. The
        live view only adds detail to it, so losing it is not an error."""
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = js[js.index("async function pollLive(correlationId)"):]
        fn = fn[:fn.index("\n}\n")]
        assert "catch (e)" in fn and "stopWatching(correlationId)" in fn, fn


class TestBlockedIsNotIdle:
    """
    A dark layer says "nobody asked this one to do anything". A blocked layer
    was part of the request and never got to run. Drawing them the same way
    turns a failure into an absence.
    """

    def test_a_stranded_step_ends_blocked(self):
        js = _without_comments(_asset("js", "agent-activity.js"))
        fn = js[js.index("export function finishRun("):]
        fn = fn[:fn.index("\n}\n")]
        assert "strandedLayers" in fn and "'blocked'" in fn, fn
        assert "x.status === 'pending'" in fn, fn

    def test_it_is_drawn_differently_from_pending_and_from_failed(self):
        """
        In the tree the dangerous confusion is with PENDING, not idle: a
        pending phase is one whose turn has not come, and drawing a step that
        never ran the same way says it is still coming.
        """
        css = _asset("css", "agent-loading.css")
        blocked = _rule(css, ".agl-phase.blocked {")
        pending = _rule(css, ".agl-phase.pending {")
        assert blocked != pending
        assert "max-height: 0" in pending, pending
        assert "max-height: 0" not in blocked, blocked
        # Amber, not the red of a failure: it did not fail, it never ran.
        assert "#b45309" in _rule(css, ".agl-phase.blocked .agl-node {")
        js = _without_comments(_asset("js", "agent-loading.js"))
        assert "runEnded ? 'blocked' : 'pending'" in js, js

    def test_a_blocked_phase_says_so_in_its_own_words(self):
        """
        The line was there, behind a condition that almost never held: it was
        written only if the phase had no lines of its own. A step whose layer
        IS the hub inherits the hub's lines, so a stranded `compare` step read
        "Continuing with 1 remaining step, without its evidence" — the
        orchestrator's account of what it did NEXT — and never said that this
        was the step that did not run. Measured in the browser, not reasoned
        about: the line was absent from a real stranded step.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "function revealState(item)")
        assert "!blockedNoted[item.id]" in fn, fn
        assert "phase.logs.children.length" not in fn, fn
        assert "Did not run" in fn, fn


class TestTheScreenIsPacedToBeRead:
    """
    The screen was correct and unreadable.

    Every fact on it was true and every one of them arrived at machine speed:
    a network that had been solved before reported five dispatches and twenty
    lines of detail inside two seconds, so each line held the screen for about
    a twentieth of a second and a phase wrote its detail and folded it away
    inside one animation frame. Nielsen's first heuristic asks for visibility
    of system status; a line nobody could read was never visible, and a
    loading screen that cannot be read is decoration with a true caption.

    So reveals are queued and released at reading speed. The tests here are
    about the two things that keeps honest:

      * the queue holds only what already happened, in the order it happened
        (`TestTheVisualisationIsWhatTheDesignApproved` above pins that);
      * pacing a person's wait is a cost, so it is bounded, it speeds up when
        it falls behind, it never touches the one figure that claims to be a
        duration, and there is a way out of it.
    """

    def test_a_revealed_line_holds_the_screen_long_enough_to_be_read(self):
        js = _without_comments(_asset("js", "agent-loading.js"))
        base = int(re.search(r"BASE_STEP_MS = (\d+)", js).group(1))
        floor = int(re.search(r"FLOOR_STEP_MS = (\d+)", js).group(1))
        hold = int(re.search(r"PHASE_HOLD_MS = (\d+)", js).group(1))
        # Reading one short line of text is a few hundred milliseconds. The
        # floor is what the screen falls back to when it is behind, and it is
        # still above a flicker.
        assert floor >= 300, floor
        assert base >= 800, base
        assert floor < base, (floor, base)
        # A finished phase is held open on top of that, so its detail is not
        # taken away on the frame it arrived.
        assert hold >= 1000, hold

    def test_it_speeds_up_rather_than_falling_further_behind(self):
        """
        A screen thirty seconds behind the system is describing a past. The
        pace shortens as the backlog grows, so what is waiting is shown within
        a bounded time however much of it there is.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "function stepMs()")
        assert "queue.length" in fn, fn
        assert "FLOOR_STEP_MS" in fn and "BASE_STEP_MS" in fn, fn
        budget = int(re.search(r"BACKLOG_BUDGET_MS = (\d+)", js).group(1))
        assert budget <= 12000, budget
        # A budget needs a DEADLINE, not a ratio. Dividing the whole budget by
        # the current backlog after every reveal gives each remaining item more
        # time than the one before it — the queue shrinks, so the divisor does
        # — and the total comes out as the budget times a harmonic sum.
        # Measured that way, 777ms of work held the dialog for 21.4 seconds.
        assert "drainBy - Date.now()" in fn, fn
        assert "BACKLOG_BUDGET_MS" not in fn, fn
        update = _fn(js, "function update(run)")
        assert "drainBy = Date.now() + BACKLOG_BUDGET_MS" in update, update
        assert "drainBy < Date.now()" in update, update

    def test_a_failure_does_not_queue_behind_its_own_preamble(self):
        """
        Measured before this existed: a scenario that failed 300ms in put
        seven items on the queue, and at the reading pace the reason reached
        the screen 3.6 seconds later — behind "Turning this request into a
        plan" and "Passing a change to the scenario planner". Nielsen's ninth
        heuristic is about recognising and diagnosing an error, and pacing the
        diagnosis behind the pleasantries is the one place the pacing makes
        the screen worse rather than better.

        The recorder already states this rule for hand-offs: `stepFail` stops
        the signal queue outright because "a failure jumps the queue: it is
        the thing the reader needs to see". This is the same rule for reveals.
        Nothing is dropped and nothing is reordered — the queue stops WAITING,
        so everything ahead of the failure arrives with it.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        pace = _fn(js, "function stepMs()")
        assert "item.urgent" in pace, pace
        assert "return 0;" in pace, pace
        update = _fn(js, "function update(run)")
        assert "urgent: state === 'failed'" in update, update
        # And the pump is re-armed rather than left sitting out the wait it
        # was scheduled for before the failure arrived.
        pump = _fn(js, "function startPump()")
        assert "item.urgent" in pump, pump
        assert "clearTimeout(pumpTimer)" in pump, pump

    def test_a_heading_finishes_before_its_own_detail_arrives(self):
        """
        A title still typing itself while the lines underneath it are already
        landing reads as two things happening at once when only one is.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        budget = int(re.search(r"TYPE_BUDGET_MS = (\d+)", js).group(1))
        floor = int(re.search(r"FLOOR_STEP_MS = (\d+)", js).group(1))
        assert budget <= floor + 200, (budget, floor)
        fn = _fn(js, "function typeTitle(el, text)")
        assert "TYPE_BUDGET_MS / Math.max(1, text.length)" in fn, fn

    def test_the_elapsed_clock_is_never_paced(self):
        """
        The pacing makes the SCREEN later than the work. Exactly one figure on
        the dialog claims to be a duration, and it has to stay the true one —
        otherwise the dialog is quietly reporting its own animation as the
        system's response time.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "function elapsedText(run)")
        assert "run.startedAt" in fn and "run.endedAt" in fn, fn
        for paced in ("queue", "revealFloor", "instant", "stepMs"):
            assert paced not in fn, (paced, fn)

    def test_each_phase_states_the_time_it_actually_took(self):
        """
        A dispatch that took four hundredths of a second is held open for
        eight tenths so it can be read. The screen says so, in the phase's own
        lines, from the recorder's own timestamps.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "function logsFor(run, step, index)")
        assert "step.endedAt - step.startedAt" in fn, fn
        assert "'Took'" in fn, fn

    def test_the_dialog_waits_for_the_queue_and_not_for_ever(self):
        """
        Closing on the last event would undo the pacing for the phase it
        matters most for — the final one, whose lines would be drawn and
        removed in the same breath. Waiting without a bound would let a
        backlog hold a workspace shut, which is worse than either.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "export function dismissAgentLoading(holdMs = 0)")
        assert "queue.length" in fn, fn
        assert "MAX_DRAIN_MS" in fn, fn
        assert "deadline" in fn, fn
        cap = int(re.search(r"MAX_DRAIN_MS = (\d+)", js).group(1))
        assert cap <= 30000, cap

    def test_there_is_a_way_out_of_the_pacing(self):
        """
        Nielsen's third heuristic. A hold with no exit is a trap, and someone
        who has seen this screen fifty times should not have to watch it read
        itself out.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        flush = _fn(js, "function flush()")
        assert "instant = true;" in flush, flush
        assert "while (queue.length" in flush, flush
        # Reachable by pointer and by keyboard.
        assert "els.skip.addEventListener('click', flush);" in js, js
        esc = js[js.index("document.addEventListener('keydown'"):]
        esc = esc[:esc.index("});")]
        assert "'Escape'" in esc and "flush()" in esc, esc
        # And it says what it does not do: it is a button on a modal over
        # running work, which reads as a cancel until it is told otherwise.
        skeleton = js[js.index("function skeletonHtml()"):js.index("function phaseHtml")]
        assert "does not change the work" in skeleton, skeleton

    def test_the_way_out_is_offered_only_when_there_is_something_to_skip(self):
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "function updateSkip()")
        assert "queue.length" in fn, fn
        assert "hidden" in fn, fn

    def test_the_fraction_agrees_with_the_tree_under_it(self):
        """
        A bar reading "5 of 5" above a tree still showing the third phase is a
        dialog disagreeing with itself, and the reader believes the half that
        is moving. The denominator is still the recorder's.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "function drawProgress()")
        assert "progress(run)" in fn, fn
        assert "prog.total" in fn, fn
        assert "lastState[s.id]" in fn, fn

    def test_the_newest_line_is_never_left_below_the_fold(self):
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = _fn(js, "function keepLatestInView()")
        assert "scrollTop" in fn and "scrollHeight" in fn, fn

    def test_the_motion_is_slow_enough_to_follow(self):
        """
        Every duration on the dialog was tuned next to a screen that changed
        every few hundred milliseconds. With the reveals paced there is room
        for the motion to be legible instead of merely unobtrusive.
        """
        css = _asset("css", "agent-loading.css")
        log = _rule(css, "\n.agl-log {")
        seconds = float(re.search(r"agl-log-in (\d*\.?\d+)s", log).group(1))
        assert seconds >= 0.45, seconds
        spin = _rule(css, ".agl-node-spin {")
        assert float(re.search(r"agl-rotate (\d*\.?\d+)s", spin).group(1)) >= 1.0
        phase = _rule(css, "\n.agl-phase {")
        assert float(re.search(r"max-height (\d*\.?\d+)s", phase).group(1)) >= 0.6


class TestTheDialogIsBuiltOnceAndUpdatedInPlace:
    """
    Replacing an element restarts every CSS animation inside it.

    The view was a template string re-evaluated into `innerHTML` on every
    state change — the obvious way to write it, and wrong here for that one
    reason. The dialog's own entrance replayed on each emit, and with the live
    poll and the signal queue both firing it never got past the opening frames
    of its fade: a screenshot of a busy run showed a blurred workspace with no
    dialog on it at all. The travelling signal had the same fault in a form
    that mattered more — a 1.9-second crossing restarted from the beginning
    whenever anything changed, so the dot never once arrived at the other node.

    Everything below is about IDENTITY: which elements are allowed to be
    replaced, and when.
    """

    def test_the_structure_is_built_once(self):
        js = _without_comments(_asset("js", "agent-loading.js"))
        assert "function skeletonHtml()" in js
        assert "function buildSkeleton(overlay)" in js
        # The skeleton carries no run-dependent content: it is built before
        # there is a run to describe.
        skeleton = js[js.index("function skeletonHtml()"):js.index("let els = null;")]
        # The INTERPOLATION forms, not the bare words: "prog" also spells the
        # class name `agl-progress-card`, which is structure, not state.
        for forbidden in ("${run.", "${agent", "state-${", "${prog",
                          "${hub", "${message"):
            assert forbidden not in skeleton, (forbidden, skeleton[:400])

    def test_nothing_on_the_dialog_is_replaced_wholesale(self):
        """
        SUPERSEDED FORM: this read the whole property out of `update`, which
        was where the drawing happened. The drawing moved to `reveal` when the
        screen was paced. The property did not move: neither function replaces
        anything, and a phase's class is still written only when its state
        genuinely moved.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        update = _fn(js, "function update(run)")
        assert "innerHTML" not in update, update
        # `update` queues on a real move, and on nothing else.
        assert "const moved = queuedState[step.id] !== state;" in update, update
        for guarded in ("if (moved && state === 'active')",
                        "if (moved && state !== 'active')"):
            assert guarded in update, update
        # And the reveal writes a class, never a subtree.
        state = _fn(js, "function revealState(item)")
        assert "innerHTML" not in state, state
        assert "phase.root.className = `agl-phase ${item.state}`;" in state, state

    def test_a_title_is_typed_once_and_logs_are_only_appended(self):
        """
        A reveal happens throughout a run — a live poll, a trace arriving, a
        step settling, each adding to the queue. Re-typing a title from its
        first character, or rewriting the log list, would replay both
        animations each time and the phase would never finish saying anything.
        """
        js = _without_comments(_asset("js", "agent-loading.js"))
        state = _fn(js, "function revealState(item)")
        assert "if (!typedFor[item.id])" in state, state
        assert "typedFor[item.id] = true;" in state, state
        # Appended, never rewritten.
        log = _fn(js, "function revealLog(item)")
        assert "insertAdjacentHTML('beforeend'" in log, log
        assert "innerHTML" not in log, log
        # And a line already queued is never queued twice.
        update = _fn(js, "function update(run)")
        assert "if (already.includes(key)) return;" in update, update

    def test_text_is_only_written_when_it_changed(self):
        js = _without_comments(_asset("js", "agent-loading.js"))
        assert "if (el && el.textContent !== value) el.textContent = value;" in js

    def test_taking_it_down_does_not_destroy_it(self):
        """The overlay is hidden, not emptied: the next run reuses the same
        elements, and the entrance plays once per open rather than per emit."""
        js = _without_comments(_asset("js", "agent-loading.js"))
        fn = js[js.index("export function dismissAgentLoading("):]
        fn = fn[:fn.index("\n}\n")]
        assert "classList.remove('active')" in fn, fn
        assert "innerHTML = ''" not in fn, fn

    def test_reduced_motion_leaves_the_dialog_drawn(self):
        """
        Two entrances start at `opacity: 0` — the dialog's and every log
        line's. Cancelling those animations without care would hand the
        readers who most need a legible screen a blank box.
        """
        css = _asset("css", "agent-loading.css")
        block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
        assert ".agl-shell" in block, block
        assert "animation: none !important" in block, block
        body = _without_comments(block)
        # The shell is never given an opacity or a transform here, so it
        # cannot be stranded on its first frame.
        for line in body.splitlines():
            if ".agl-shell" in line:
                assert "opacity" not in line and "transform" not in line, line
        # The log lines DO need restoring, because their base rule starts them
        # at zero and it is the animation that brings them in.
        assert ".agl-log { opacity: 1; transform: none; }" in body, body
