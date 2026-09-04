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
    def test_the_five_layers_and_the_hub_are_the_designs_own(self):
        js = _asset("js", "agent-loading.js")
        for name in ('Intent Layer', 'Scenario Planner', 'Extraction Layer',
                     'Forecasting Layer', 'Reasoning Layer', 'Orchestrator Agent'):
            assert name in js, name

    def test_they_sit_clockwise_from_the_top_as_the_mockup_places_them(self):
        js = _asset("js", "agent-loading.js")
        # Sliced on the declaration that FOLLOWS it, not on the prose of the
        # comment in between: this broke once because a docblock was expanded,
        # which says nothing about where the layers sit.
        meta = js[js.index("const LAYER_META = {"):js.index("const STATE_LABEL = {")]
        angles = dict(re.findall(r"(\w+): \{\s*\n\s*name: '[^']+', angle: (\d+)", meta))
        assert angles == {'intent': '0', 'scenario': '72', 'extraction': '144',
                          'forecasting': '216', 'reasoning': '288'}, angles

    def test_the_rings_and_particles_say_nothing_and_barely_move(self):
        """
        The decoration is inert, and slow enough not to be watched.

        It was neither. Every particle was given its own orbit at its own
        rate and two rings turned in opposite directions, which is subtle by
        the numbers and reads as a solar system: the eye tracks anything
        going round in a circle, and it tracked the decoration instead of the
        layer that was working. One ring still turns, at three minutes a
        revolution; the particles hold station and only change brightness.
        """
        css = _asset("css", "agent-loading.css")
        body = _without_comments(css)
        assert "@keyframes agl-spin" in css
        assert "@keyframes agl-twinkle" in css

        # Nothing travels in a circle any more.
        assert "@keyframes agl-orbit" not in body, (
            "the particles are orbiting again")
        assert "agl-orbit" not in _rule(css, ".agl-particles i {"), (
            "a particle is animating its own transform again")

        # Whatever still rotates does so slowly enough to be felt, not watched.
        for rule in re.findall(r"animation:\s*agl-spin\s+([\d.]+)s", body):
            assert float(rule) >= 60, f"agl-spin at {rule}s is fast enough to track"
        # Decoration must be inert to state: no rule may key a ring or a
        # particle off an agent's state class.
        for line in _without_comments(css).splitlines():
            if ".agl-ring" in line or ".agl-particles" in line:
                assert "state-" not in line, line
        js = _asset("js", "agent-loading.js")
        # Sliced to the next element in the skeleton, not to a call that used
        # to build it: the links are inlined now that the dialog is built once.
        rings = js[js.index('class="agl-rings"'):js.index('class="agl-links"')]
        assert 'aria-hidden' in rings

    def test_an_uninvolved_layer_is_subdued_and_still_legible(self):
        css = _asset("css", "agent-loading.css")
        idle = _rule(css, ".agl-node.state-idle {")
        assert "opacity: 0.45" in idle
        assert "grayscale" in idle

    def test_someone_who_asked_for_less_motion_gets_less_motion(self):
        css = _without_comments(_asset("css", "agent-loading.css"))
        block = css[css.index("prefers-reduced-motion"):]
        assert "animation: none !important" in block
        assert ".agl-signal-dot { display: none; }" in block


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
        assert "clockTimer = setInterval(tick, 1000)" in js

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
        assert "width: min(940px" in shell, shell
        assert "max-height: min(" in shell and "100vh" in shell, shell
        # Bounded, so it can never grow past the window; scrollable inside if
        # a translation or a long message makes it taller than expected.
        assert "overflow-y: auto" in shell, shell

    def test_the_diagram_is_a_square_so_the_ring_is_a_circle(self):
        css = _asset("css", "agent-loading.css")
        stage = _rule(css, ".agl-stage {")
        assert "aspect-ratio: 1 / 1" in stage, stage
        js = _asset("js", "agent-loading.js")
        # ONE radius. Two — one for the width, one for the height — was a
        # correction for a rectangular stage, and had to be re-tuned whenever
        # the container changed shape.
        assert "const RADIUS = 40;" in js
        assert "RADIUS_X" not in js and "RADIUS_Y" not in js, (
            "the ring is being corrected for a rectangle again")
        # Node and hub are shares of that same square, so the figure holds its
        # proportions at any size.
        assert "width: 26%;" in _rule(css, ".agl-node {")
        assert "width: 30%;" in _rule(css, ".agl-hub {")

    def test_it_uses_the_applications_type_scale(self):
        """22px/800 is `.ov-title`, the Home Overview heading — the source of
        truth for scale. The dialog must not invent one of its own."""
        css = _asset("css", "agent-loading.css")
        title = _rule(css, ".agl-title {")
        assert "font-size: 22px;" in title, title
        assert "vw" not in title, "the heading is sized off the viewport again"
        assert "font-size: 13px;" in _rule(css, ".agl-sub {")

    def test_the_motion_is_slow_enough_to_follow(self):
        """
        One hand-off should read as one thing crossing between two agents.

        At a 1.15s crossing against a 620ms dwell the dot crossed the diagram
        twice per message, and the ring read as traffic rather than as a
        conversation.
        """
        css = _asset("css", "agent-loading.css")
        body = _without_comments(css)
        travel = re.search(r"animation:\s*agl-travel\s+([\d.]+)s", body)
        assert travel, "the signal no longer travels"
        crossing = float(travel.group(1))
        assert crossing >= 1.5, f"the signal crosses in {crossing}s"

        activity = _without_comments(_asset("js", "agent-activity.js"))
        dwell = re.search(r"SIGNAL_DWELL_MS\s*=\s*(\d+)", activity)
        assert dwell, "the dwell is gone"
        assert int(dwell.group(1)) >= crossing * 700, (
            "a hand-off is shown for less than one crossing: "
            f"{dwell.group(1)}ms for a {crossing}s trip")

        # Everything else that repeats forever is slow too.
        for name, floor in (("agl-dash", 2.0), ("agl-breathe", 5.0),
                            ("agl-node-breathe", 4.0), ("agl-rotate", 1.2),
                            ("agl-sweep", 2.0), ("agl-twinkle", 8.0)):
            found = [float(x) for x in
                     re.findall(name + r"\s+(?:calc\()?([\d.]+)s", body)]
            assert found, f"{name} is not used"
            assert min(found) >= floor, f"{name} runs at {min(found)}s"

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

    def test_it_is_drawn_differently_from_idle_and_from_failed(self):
        css = _asset("css", "agent-loading.css")
        blocked = _rule(css, ".agl-node.state-blocked {")
        idle = _rule(css, ".agl-node.state-idle {")
        assert blocked != idle
        assert "b45309" in blocked.lower() or "180, 83, 9" in blocked, blocked
        assert ".agl-badge.blocked" in css
        js = _asset("js", "agent-loading.js")
        assert "blocked: 'Blocked'" in js


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

    def test_update_writes_into_it_rather_than_replacing_it(self):
        js = _without_comments(_asset("js", "agent-loading.js"))
        update = js[js.index("function update(run)"):]
        update = update[:update.index("\n}\n")]
        # The only things `update` is allowed to replace wholesale are the two
        # that carry no animation of their own once written.
        replaced = re.findall(r"els\.(\w+)\.innerHTML", update)
        assert set(replaced) <= {"signalHost", "calloutHost"}, replaced
        # Badges are per-node and only rewritten when that node's state moved.
        assert "if (lastNodeState[id] === agent.state) return;" in update, update

    def test_the_signal_survives_an_unrelated_update(self):
        """Keyed on the hand-off itself. A live poll landing mid-crossing must
        not send the dot back to the start."""
        js = _without_comments(_asset("js", "agent-loading.js"))
        update = js[js.index("function update(run)"):]
        update = update[:update.index("\n}\n")]
        assert "if (key !== lastSignalKey)" in update, update
        assert "${sig.from}>${sig.to}@${sig.at}" in update, update

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
        The entrance starts at `opacity: 0` with `fill: both`. A rule that
        cancels the animation must not strand the dialog on its first frame —
        that would hand the readers who most need a legible screen a blank one.
        """
        css = _asset("css", "agent-loading.css")
        block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
        assert ".agl-shell" in block, block
        assert "animation: none !important" in block, block
        # Nothing in that block may set an opacity or a transform, which is
        # what would leave the dialog invisible or displaced.
        body = _without_comments(block)
        assert "opacity:" not in body, body
        assert "transform:" not in body, body
