"""
Phase 2 — §25, §26, §27: provenance, observability, and the absence of hidden
fallbacks.

§25 sets the bar precisely: a reviewer must be able to answer

    "Why did NetGravity produce this risk?"

from the audit record alone, without inspecting internal Python state. So the
test reads the sealed trace, serialises it, and reconstructs the answer from
the JSON.

§27 is tested two ways: behaviourally (feed the system a gap, prove no value
appears) and structurally (read the integration code and prove the dangerous
patterns are absent).
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from netgravity.orchestrator import build_orchestrator, registry as registry_module
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.audit.audit_logger import AuditLogger
from netgravity.orchestrator.risk import risk_assessment, risk_factor
from netgravity.orchestrator.schemas.requests import Intent, OrchestratorRequest
from netgravity.resilience import service as rei_service

from .conftest import build_delhi_network, flood_signal

TOL = 1e-9


def _risk_run(orch, signal=None, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input="Flood warning issued for the Delhi NCR region.",
        explicit_intent=Intent.EXTERNAL_EVENT,
        external_signal=signal if signal is not None else flood_signal(),
        disable_llm=True, **kwargs,
    ))


# ===========================================================================
# §25 — a complete workflow is reconstructable
# ===========================================================================

class TestProvenanceCompleteness:

    def test_every_required_field_is_recorded(self, orch, planner_actor):
        response = _risk_run(orch, actor=planner_actor)
        record = orch.get_trace(response.execution_id).to_dict()

        assert record["execution_id"] == response.execution_id
        assert record["workflow_id"] == "wf_external_event"
        assert record["data_references"]["baseline_snapshot_id"] == \
            orch.snapshots.current_id
        assert record["data_references"]["data_version"]

        risk = record["risk_calculation"]
        [row] = risk["results"]
        assert row["facility_id"] == "DC_DELHI"
        assert row["likelihood"] == pytest.approx(0.7, abs=TOL)
        assert row["rei"] == pytest.approx(0.8, abs=TOL)
        assert row["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert row["formula"] == "RF = P + REI - P*REI"

        rei_out = record["engine_results"]["resilience.assess"]
        assert rei_out["model_version"]
        assert rei_out["batch_id"]

        assert record["governance_decision"]["classification"] == "HUMAN_ONLY"
        assert record["outcome"]["status"] == "REQUIRES_HUMAN"
        assert record["intent"]["interpreted"] == "EXTERNAL_EVENT"

    def test_reasoning_and_grounding_status_are_both_recorded(self, orch):
        response = _risk_run(orch)
        trace = orch.get_trace(response.execution_id)

        [reasoning] = trace.events_of(events.REASONING_COMPLETED)
        [grounding] = trace.events_of(events.GROUNDING_COMPLETED)
        assert reasoning.detail["source"] == "template"
        assert grounding.detail["grounding_status"] in ("GROUNDED", "NO_CLAIMS")

    def test_the_question_why_is_answerable_from_json_alone(self, orch):
        """
        The §25 acceptance test, performed literally: no Python objects, only
        the serialised record, and every element of the answer recovered.
        """
        response = _risk_run(orch)
        record = json.loads(orch.get_trace(response.execution_id).to_json())

        risk_row = record["risk_calculation"]["results"][0]
        answer = {
            "node": risk_row["facility_id"],
            "event_probability": risk_row["likelihood"],
            "probability_source": risk_row["provenance"]["likelihood"],
            "rei": risk_row["rei"],
            "rei_source": risk_row["provenance"]["rei"],
            "formula": risk_row["formula"],
            "risk_factor": risk_row["risk_factor"],
            "snapshot": record["data_references"]["baseline_snapshot_id"],
            "verdict": record["governance_decision"]["classification"],
            "rule": record["governance_decision"]["triggered_rules"],
        }

        assert answer["node"] == "DC_DELHI"
        assert answer["event_probability"] == pytest.approx(0.7, abs=TOL)
        assert "india_met_department" in answer["probability_source"]
        assert answer["rei"] == pytest.approx(0.8, abs=TOL)
        assert "rei_registry:rei_" in answer["rei_source"]
        assert answer["risk_factor"] == pytest.approx(0.94, abs=TOL)
        # The arithmetic reproduces from the recorded inputs alone.
        p, rei = answer["event_probability"], answer["rei"]
        assert p + rei - p * rei == pytest.approx(answer["risk_factor"], abs=1e-9)
        assert answer["verdict"] == "HUMAN_ONLY"
        assert answer["rule"] == ["R6_RISK_FACTOR_HUMAN"]

    def test_a_refusal_is_as_reconstructable_as_a_result(self, orch):
        response = _risk_run(orch, flood_signal(probability=None))
        record = json.loads(orch.get_trace(response.execution_id).to_json())

        [row] = record["risk_calculation"]["not_computable"]
        assert row["status"] == "NOT_COMPUTABLE"
        assert row["not_computable_reason"] == "NO_EVENT_PROBABILITY"
        assert row["risk_factor"] is None
        assert any("never substituted" in n for n in row["notes"])

    def test_scenario_overrides_are_recorded_verbatim(self, orch, planner_actor):
        from netgravity.orchestrator.schemas.requests import (
            ScenarioActionType, ScenarioIntentSpec,
        )
        response = orch.run_sync(OrchestratorRequest(
            input="Cut Delhi.", explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=["DC_DELHI"], capacity_delta_units=-2_000.0,
            )], actor=planner_actor, disable_llm=True,
        ))
        record = orch.get_trace(response.execution_id).to_dict()

        assert record["data_references"]["scenario_overrides"] == [
            "CHANGE_CAPACITY DC_DELHI -2,000 units/period"
        ]
        assert record["data_references"]["scenario_ids"] == [response.scenario_id]

    def test_the_explain_narrative_renders(self, orch):
        response = _risk_run(orch)
        text = orch.get_trace(response.execution_id).explain()

        assert response.execution_id in text
        assert "wf_external_event" in text
        assert "HUMAN_ONLY" in text
        assert "R6_RISK_FACTOR_HUMAN" in text


# ===========================================================================
# §26 — structured observability
# ===========================================================================

class TestObservability:

    def test_the_full_canonical_event_set_is_reachable(self, orch, planner_actor):
        """
        Not every event fires on every run, so coverage is accumulated across a
        representative set of runs. Anything never emitted is a constant nobody
        wired up.
        """
        from .test_failure_propagation import TimingOutREIClient
        from netgravity.orchestrator.schemas.requests import (
            ScenarioActionType, ScenarioIntentSpec,
        )

        seen: set = set()

        def collect(o, response):
            trace = o.get_trace(response.execution_id)
            seen.update(e.event_type for e in trace.events)

        healthy = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        collect(healthy, _risk_run(healthy, request_id="ok"))

        degraded = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        degraded.services["rei"] = TimingOutREIClient()
        collect(degraded, _risk_run(degraded, request_id="degraded"))

        infeasible_net = build_delhi_network()
        blocked = build_orchestrator(network=infeasible_net, enable_llm=False)
        from .test_failure_propagation import FailingOptimizationClient
        blocked.services["optimization"] = FailingOptimizationClient()
        collect(blocked, blocked.run_sync(OrchestratorRequest(
            input="Cut Delhi.", explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=["DC_DELHI"], capacity_delta_units=-2_000.0,
            )], actor=planner_actor, disable_llm=True,
        )))

        from .conftest import build_infeasible_network
        infeasible = build_orchestrator(network=build_infeasible_network(),
                                        enable_llm=False)
        collect(infeasible, infeasible.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        )))

        # STEP_EXCEPTION covers a raise from OUTSIDE the tool wrapper, which the
        # engine-error paths above never reach because `CapabilityTool` converts
        # engine failures into a failed ToolResult. Capability-level
        # authorization is the real route: it is checked before the tool runs.
        unauthorized = build_orchestrator(network=build_delhi_network(),
                                          enable_llm=False)
        unauthorized.registry.get("resilience.assess").required_roles = ("ADMIN",)
        collect(unauthorized, _risk_run(unauthorized, request_id="unauthorized"))

        missing = events.CANONICAL_EVENTS - seen
        assert not missing, f"declared but never emitted: {sorted(missing)}"

    def test_an_unauthorized_capability_is_refused_and_recorded(self):
        """
        The authorization path in its own right: a VIEWER cannot invoke a
        capability restricted to ADMIN, and the refusal is a recorded exception
        rather than a silently skipped step.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        orch.registry.get("resilience.assess").required_roles = ("ADMIN",)
        response = _risk_run(orch)

        trace = orch.get_trace(response.execution_id)
        [event] = trace.events_of(events.STEP_EXCEPTION)
        assert event.detail["step_id"] == "rei"
        assert event.detail["error_type"] == "AuthorizationError"
        assert "may not invoke capability" in event.detail["error"]
        assert "VIEWER" in event.detail["error"]

        # And the consequence is honest: no REI, so RF refuses.
        assert response.risk["not_computable"][0]["not_computable_reason"] == "NO_REI"
        assert response.risk["not_computable"][0]["rei"] is None

    def test_every_event_carries_the_four_correlation_keys(self, orch):
        response = _risk_run(orch)
        trace = orch.get_trace(response.execution_id)

        for event in trace.events:
            for key in events.CORRELATION_KEYS:
                assert key in event.detail, f"{event.event_type} lacks {key}"
            assert event.detail["execution_id"] == response.execution_id

    def test_workflow_scoped_events_carry_the_workflow_id(self, orch):
        response = _risk_run(orch)
        trace = orch.get_trace(response.execution_id)

        for name in (events.WORKFLOW_STARTED, events.STEP_STARTED,
                     events.STEP_COMPLETED, events.WORKFLOW_COMPLETED):
            for event in trace.events_of(name):
                assert event.detail["workflow_id"] == "wf_external_event"

    def test_step_events_name_their_step_and_capability(self, orch):
        response = _risk_run(orch)
        trace = orch.get_trace(response.execution_id)

        started = {e.detail["step_id"] for e in trace.events_of(events.STEP_STARTED)}
        completed = {e.detail["step_id"] for e in trace.events_of(events.STEP_COMPLETED)}
        assert started == {"load", "interpret_signal", "rei", "risk", "reason", "govern"}
        assert completed == started, "every started step also reported completion"

        for event in trace.events_of(events.STEP_COMPLETED):
            assert event.detail["capability"]
            assert event.detail["duration_seconds"] >= 0.0

    def test_events_are_ordered_and_sequenced(self, orch):
        response = _risk_run(orch)
        trace = orch.get_trace(response.execution_id)

        sequences = [e.sequence for e in trace.events]
        assert sequences == sorted(sequences)
        assert sequences == list(range(1, len(sequences) + 1))

        types = [e.event_type for e in trace.events]
        assert types[0] == events.EXECUTION_STARTED
        assert types[-1] == events.EXECUTION_COMPLETED
        assert types.index(events.WORKFLOW_STARTED) < types.index(events.STEP_STARTED)
        assert types.index(events.RF_CALCULATED) < \
            types.index(events.GOVERNANCE_DECISION)

    def test_an_unclassifiable_request_records_no_workflow_events(self, orch):
        """
        The reason EXECUTION_* and WORKFLOW_* are separate scopes: a run that
        never resolves an intent has no workflow to report on, and saying it
        completed one would be false.
        """
        response = orch.run_sync(OrchestratorRequest(
            input="zzzz qqqq", disable_llm=True,
        ))
        trace = orch.get_trace(response.execution_id)

        assert trace.has_event(events.EXECUTION_STARTED)
        assert trace.has_event(events.EXECUTION_COMPLETED)
        assert not trace.has_event(events.WORKFLOW_STARTED)
        assert not trace.has_event(events.WORKFLOW_COMPLETED)

    def test_no_credential_material_reaches_the_trail(self, orch):
        response = _risk_run(orch)
        serialised = orch.get_trace(response.execution_id).to_json().lower()

        for forbidden in ("authorization", "bearer ", "text_api_token",
                          "api_key", "password", "secret"):
            assert forbidden not in serialised, f"'{forbidden}' leaked into the trail"

    def test_the_trace_buffer_is_bounded(self):
        """A long-running control plane must not grow without limit."""
        logger = AuditLogger(max_traces=3)

        class _Ctx:
            def __init__(self, i):
                self.execution_id = f"e{i}"
                self.request_id = f"r{i}"
                self.raw_input = ""
                self.baseline_snapshot_id = None
                self.actor = type("A", (), {"actor_id": "a",
                                            "role": type("R", (), {"value": "VIEWER"})()})()

        for i in range(6):
            logger.start(_Ctx(i))

        assert len(logger.list_ids()) == 3
        assert logger.list_ids() == ["e3", "e4", "e5"]


# ===========================================================================
# §27 — no hidden fallbacks
# ===========================================================================

class TestNoHiddenFallbacks:
    """
    §27 enumerates eight forbidden fallbacks. Each is tested behaviourally
    where an end-to-end path exists, and structurally otherwise.
    """

    def test_missing_rei_does_not_become_zero(self, orch):
        from .test_failure_propagation import TimingOutREIClient

        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch)

        assert all(r["rei"] is None for r in response.risk["not_computable"])
        assert response.governance.evaluated["rei"] is None

    def test_missing_p_does_not_become_zero(self, orch):
        response = _risk_run(orch, flood_signal(probability=None))
        assert all(r["likelihood"] is None for r in response.risk["not_computable"])

    def test_missing_rf_does_not_become_low_risk(self, orch):
        """
        The most dangerous fallback of the eight: "no RF" reading as "no risk".
        The refusal is loud, and governance sees None rather than a small number.
        """
        from .test_failure_propagation import TimingOutREIClient

        orch.services["rei"] = TimingOutREIClient()
        response = _risk_run(orch)

        assert response.risk["max_risk_factor"] is None
        assert response.governance.evaluated["risk_factor"] is None
        assert response.risk["not_computable"], "the gap is stated, not implied"

    def test_a_solver_failure_does_not_reuse_a_previous_result(self, delhi_network):
        from .test_failure_propagation import FailingOptimizationClient

        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        good = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
            request_id="good",
        ))
        assert good.results["network"]["business_network_cost"] == pytest.approx(1200.0)

        orch.services["optimization"] = FailingOptimizationClient()
        bad = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
            request_id="bad",
        ))
        assert "network" not in bad.results, "the previous result must not be served"
        assert bad.status == "FAILED"

    def test_stale_rei_is_not_used_anyway(self, orch):
        from .test_failure_propagation import StaleREIClient

        orch.services["rei"] = StaleREIClient("snap_ANCIENT")
        response = _risk_run(orch)
        assert response.risk["results"] == []
        assert response.risk["not_computable"][0]["not_computable_reason"] == "STALE_REI"

    def test_an_unmappable_node_does_not_fall_back_to_the_first_match(self, orch):
        response = _risk_run(orch, flood_signal(nodes=["DC_UNKNOWN"], location="Nowhere"))
        assert response.risk["results"] == []
        assert response.risk["not_computable"][0]["facility_id"] is None

    def test_an_llm_number_never_becomes_authoritative(self, planner_actor):
        from .conftest import FakeGateway, reasoning_json
        from netgravity.orchestrator.schemas.requests import (
            ScenarioActionType, ScenarioIntentSpec,
        )

        gateway = FakeGateway({"reasoning": reasoning_json(
            summary="Cost increased by 50%.",
            claims=[{"type": "business_cost_delta_pct", "value": 50,
                     "unit": "percent", "text": "50%"}],
        )})
        orch = build_orchestrator(network=build_delhi_network(), gateway=gateway)
        response = orch.run_sync(OrchestratorRequest(
            input="Cut Delhi hard.", explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=["DC_DELHI"], capacity_delta_units=-4_950.0,
            )], actor=planner_actor,
        ))

        assert response.results["network"]["business_cost_delta_pct"] == pytest.approx(
            16.6667, abs=1e-3
        )
        assert response.reasoning.grounding_status == "GROUNDING_FAILED"

    def test_a_scenario_override_never_mutates_the_baseline(self, orch, planner_actor):
        from netgravity.orchestrator.schemas.requests import (
            ScenarioActionType, ScenarioIntentSpec,
        )
        before = orch.snapshots.current().network.model_dump_json()
        orch.run_sync(OrchestratorRequest(
            input="Cut Delhi.", explicit_intent=Intent.SCENARIO_ANALYSIS,
            explicit_scenarios=[ScenarioIntentSpec(
                action=ScenarioActionType.CHANGE_CAPACITY,
                facility_ids=["DC_DELHI"], capacity_delta_units=-4_950.0,
            )], actor=planner_actor, disable_llm=True,
        ))
        assert orch.snapshots.current().network.model_dump_json() == before

    # ---- structural check ------------------------------------------------

    def test_the_integration_code_contains_no_zero_defaulting_on_risk_inputs(self):
        """
        A source-level guard against the fallback being reintroduced. Scans the
        modules that actually carry P, REI and RF for patterns that would
        substitute a number for a missing one.
        """
        modules = [registry_module, risk_assessment, risk_factor, rei_service]
        forbidden = [
            re.compile(r"""\.get\(\s*['"](rei|risk_factor|likelihood|"""
                       r"""event_probability|max_rei)['"]\s*,\s*0"""),
            re.compile(r"\b(rei|likelihood|probability)\s*=\s*\w+\s+or\s+0(?!\.\d*[1-9])"),
            re.compile(r"\brei\s*=\s*0\.0\b"),
        ]

        for module in modules:
            source = inspect.getsource(module)
            # Sort keys legitimately use `or 0.0` on already-computed rows.
            body = "\n".join(
                line for line in source.splitlines()
                if "key=lambda" not in line and ".sort(" not in line
            )
            for pattern in forbidden:
                match = pattern.search(body)
                assert match is None, (
                    f"{module.__name__} defaults a risk input to zero: "
                    f"{match.group(0)!r}"
                )

    def test_the_fabricating_registry_rebuild_is_gone(self):
        """
        Regression guard. `_registry_from_rei_output` reconstructed a typed REI
        registry from the flattened dict and defaulted every node's
        `calculation_status` to OK, so a FAILED node was recorded as healthy.
        It was deleted rather than patched; this asserts it stays deleted.
        """
        source = inspect.getsource(registry_module)
        assert "_registry_from_rei_output" not in source
        assert "baseline_solver_status=SolverStatus.OPTIMAL" not in source
        assert not hasattr(registry_module, "_registry_from_rei_output")
