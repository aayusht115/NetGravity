"""
Phase 2 — Workflow B: user what-if scenario (§10, §11, §20).

    User → NLP → Scenario Planner → Scenario Override → Validation
      → MILP → Scenario Result → Reasoning → Grounding → Governance → User

The invariant this file exists to defend:

    A SCENARIO MUST NEVER MODIFY THE OBSERVED NETWORK SNAPSHOT.

Everything else here supports that: provenance so a scenario result cannot be
mistaken for current state, and validation so an override never reaches the
solver unchecked.

The MILP is real throughout. The only double is the LLM gateway, used to prove
that a model-proposed scenario is validated rather than trusted.
"""

from __future__ import annotations

import inspect
import json

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.engines.scenario_builder import ScenarioBuilder
from netgravity.orchestrator.exceptions import InvalidScenarioError
from netgravity.orchestrator.schemas.plans import StepStatus
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.validation.validators import ScenarioValidator

from .conftest import FakeGateway, build_delhi_network, reasoning_json

TOL = 1e-9

#: "reduce Delhi capacity by 2,000 units/day" against a 5,000-unit facility.
DELHI_MINUS_2000 = ScenarioIntentSpec(
    action=ScenarioActionType.CHANGE_CAPACITY,
    facility_ids=["DC_DELHI"],
    capacity_delta_units=-2_000.0,
    label="Reduce DC_DELHI capacity by 2,000 units",
)


def _run_scenario(orch, spec, actor, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input=kwargs.pop("input", "What happens if Delhi NCR DC capacity "
                                  "is reduced by 2,000 units/day?"),
        explicit_intent=Intent.SCENARIO_ANALYSIS,
        explicit_scenarios=[spec],
        actor=actor, disable_llm=True, **kwargs,
    ))


# ===========================================================================
# §10 / §20 — the workflow runs end to end on the real MILP
# ===========================================================================

class TestScenarioWorkflow:

    def test_the_full_chain_executes(self, orch, planner_actor):
        response = _run_scenario(orch, DELHI_MINUS_2000, planner_actor)
        by_step = {s["step_id"]: s["status"] for s in response.steps}

        for step in ("load", "baseline", "create_scenario", "validate_scenario",
                     "optimize_scenario", "kpi", "reason", "govern"):
            assert by_step[step] == StepStatus.COMPLETED.value, f"{step} did not complete"

    def test_the_authoritative_milp_produced_the_scenario_result(self, orch, planner_actor):
        response = _run_scenario(orch, DELHI_MINUS_2000, planner_actor)
        result = response.results["network"]

        assert result["is_feasible"] is True
        assert result["solver_status"] == "OPTIMAL"
        assert result["run_id"], "a real solver run stands behind this"
        # Delhi keeps 3,000 units — still ample for the 100 units it actually
        # ships to MKT_NORTH — so the optimum is unchanged. A zero delta is the
        # CORRECT answer here, and saying so is more useful than engineering the
        # fixture until the number moves.
        assert result["business_network_cost"] == pytest.approx(1200.0, abs=1e-6)
        assert result["business_cost_delta"] == pytest.approx(0.0, abs=1e-6)

    def test_a_binding_capacity_cut_actually_changes_the_optimum(self, orch, planner_actor):
        """
        Delhi serves MKT_NORTH only — 100 units at baseline. Cut it to 50 and
        half that flow must reroute via Mumbai @6 instead of Delhi @2:

            50 units move from (1+2)=3 to (1+6)=7  ⇒  +4 × 50 = +200
            1,200 → 1,400, i.e. +16.67%
        """
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY,
            facility_ids=["DC_DELHI"],
            capacity_delta_units=-4_950.0,          # 5,000 → 50
            label="Cut DC_DELHI to 50 units",
        )
        response = _run_scenario(orch, spec, planner_actor)
        result = response.results["network"]

        assert result["is_feasible"] is True
        assert result["business_network_cost"] == pytest.approx(1400.0, abs=1e-6)
        assert result["business_cost_delta"] == pytest.approx(200.0, abs=1e-6)
        assert result["business_cost_delta_pct"] == pytest.approx(16.6667, abs=1e-3)

    def test_reasoning_is_grounded_in_the_scenario_result(self, orch, planner_actor):
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY,
            facility_ids=["DC_DELHI"], capacity_delta_units=-4_950.0,
        )
        response = _run_scenario(orch, spec, planner_actor)

        assert response.reasoning is not None
        assert "200.00" in response.reasoning.summary
        assert response.reasoning.grounding_status in ("GROUNDED", "NO_CLAIMS")

    def test_no_rf_is_calculated_for_a_plain_what_if(self, orch, planner_actor):
        """§20: risk assessment is not part of this workflow unless asked for."""
        response = _run_scenario(orch, DELHI_MINUS_2000, planner_actor)
        assert response.risk is None
        assert not any(s["capability"] == "risk.compute_rf" for s in response.steps)


# ===========================================================================
# §11 — data integrity and provenance
# ===========================================================================

class TestScenarioDataIntegrity:

    def test_every_required_provenance_field_is_present(self, orch, planner_actor):
        response = _run_scenario(orch, DELHI_MINUS_2000, planner_actor)
        result = response.results["network"]

        assert result["baseline_snapshot_id"] == orch.snapshots.current_id
        assert result["scenario_id"].startswith("scn_")
        assert result["scenario_overrides"] == [
            "CHANGE_CAPACITY DC_DELHI -2,000 units/period"
        ]
        assert result["model_version"]
        assert result["execution_id"] == response.execution_id
        assert result["scenario_version"] == 1

    def test_the_result_identifies_itself_as_hypothetical(self, orch, planner_actor):
        response = _run_scenario(orch, DELHI_MINUS_2000, planner_actor)
        result = response.results["network"]

        assert result["result_kind"] == "SCENARIO_RESULT"
        assert result["is_hypothetical"] is True
        assert response.is_hypothetical is True
        assert response.scenario_id == result["scenario_id"]

    def test_an_observed_run_identifies_itself_differently(self, orch):
        """The contrast that makes the marker meaningful."""
        response = orch.run_sync(OrchestratorRequest(
            input="What does the network look like now?",
            explicit_intent=Intent.NETWORK_STATE_QUERY, disable_llm=True,
        ))
        result = response.results["network"]

        assert result["result_kind"] == "OBSERVED_RESULT"
        assert result["is_hypothetical"] is False
        assert response.is_hypothetical is False

    # ---- the regression test §11 asks for --------------------------------

    def test_baseline_is_byte_for_byte_unchanged_after_a_scenario(self, orch, planner_actor):
        before = orch.snapshots.current().network.model_dump_json()
        before_id = orch.snapshots.current_id

        _run_scenario(orch, DELHI_MINUS_2000, planner_actor)

        after = orch.snapshots.current().network.model_dump_json()
        assert after == before, "the observed snapshot was mutated by a what-if"
        assert orch.snapshots.current_id == before_id
        assert orch.snapshots.list_ids() == [before_id]

    def test_the_scenario_network_really_did_change(self, orch, planner_actor):
        """The mirror of the test above: isolation, not inaction."""
        response = _run_scenario(orch, DELHI_MINUS_2000, planner_actor)
        record = orch.scenarios.get(response.scenario_id)

        scenario_delhi = next(f for f in record.network.facilities if f.id == "DC_DELHI")
        observed_delhi = next(f for f in orch.snapshots.current().network.facilities
                              if f.id == "DC_DELHI")

        assert scenario_delhi.capacity_units_per_period == pytest.approx(3_000.0)
        assert observed_delhi.capacity_units_per_period == pytest.approx(5_000.0)

    def test_two_scenarios_on_one_baseline_do_not_contaminate_each_other(
        self, orch, planner_actor,
    ):
        first = _run_scenario(orch, DELHI_MINUS_2000, planner_actor, request_id="s1")
        second = _run_scenario(
            orch,
            ScenarioIntentSpec(action=ScenarioActionType.CHANGE_CAPACITY,
                               facility_ids=["DC_MUMBAI"], capacity_delta_units=-1_000.0),
            planner_actor, request_id="s2",
        )

        rec_a = orch.scenarios.get(first.scenario_id)
        rec_b = orch.scenarios.get(second.scenario_id)

        def capacity(record, fid):
            return next(f for f in record.network.facilities
                        if f.id == fid).capacity_units_per_period

        assert capacity(rec_a, "DC_DELHI") == pytest.approx(3_000.0)
        assert capacity(rec_a, "DC_MUMBAI") == pytest.approx(5_000.0)
        assert capacity(rec_b, "DC_DELHI") == pytest.approx(5_000.0)
        assert capacity(rec_b, "DC_MUMBAI") == pytest.approx(4_000.0)

    def test_a_scenario_record_cannot_be_promoted_to_observed(self, orch, planner_actor):
        """There is deliberately no such API. Asserted so nobody adds one quietly."""
        _run_scenario(orch, DELHI_MINUS_2000, planner_actor)
        assert not hasattr(orch.scenarios, "promote_to_observed")
        assert not hasattr(orch.scenarios, "commit")
        assert all(r.is_hypothetical for r in
                   [orch.scenarios.get(i) for i in orch.scenarios.list_ids()])

    def test_mutating_a_handed_out_scenario_network_does_not_reach_the_store(
        self, orch, planner_actor,
    ):
        response = _run_scenario(orch, DELHI_MINUS_2000, planner_actor)
        copy = orch.scenarios.network_for(response.scenario_id)
        stored_before = orch.scenarios.get(response.scenario_id).network.model_dump_json()

        copy.facilities[1].capacity_units_per_period = 1.0

        assert orch.scenarios.get(response.scenario_id).network.model_dump_json() == \
            stored_before


# ===========================================================================
# §10 — override validation happens before the solver
# ===========================================================================

class TestScenarioOverrideValidation:

    def test_absolute_and_relative_capacity_are_mutually_exclusive(self, delhi_network):
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
            capacity_multiplier=0.6, capacity_delta_units=-2_000.0,
        )
        with pytest.raises(InvalidScenarioError, match="mutually exclusive"):
            ScenarioValidator().validate(spec, delhi_network)

    def test_a_capacity_change_with_no_quantity_is_rejected(self, delhi_network):
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
        )
        # Matched on the field names rather than the sentence: Phase 3.2 added
        # capacity_set_units to the message and the assertion should not care.
        with pytest.raises(InvalidScenarioError, match="capacity_delta_units"):
            ScenarioValidator().validate(spec, delhi_network)

    def test_a_delta_that_would_go_negative_is_refused_not_clamped(self, delhi_network):
        """
        Clamping −9,000 to zero would silently convert a capacity reduction into
        a facility closure — an action governed HUMAN_ONLY. Refuse instead.
        """
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
            capacity_delta_units=-9_000.0,
        )
        with pytest.raises(InvalidScenarioError, match="CLOSE_FACILITY"):
            ScenarioValidator().validate(spec, delhi_network)

    def test_relative_capacity_still_works(self, delhi_network):
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
            capacity_multiplier=0.6,
        )
        ScenarioValidator().validate(spec, delhi_network)
        network, overrides = ScenarioBuilder().build(delhi_network, spec)

        delhi = next(f for f in network.facilities if f.id == "DC_DELHI")
        assert delhi.capacity_units_per_period == pytest.approx(3_000.0)
        assert overrides == ["CHANGE_CAPACITY DC_DELHI x0.6"]

    def test_an_increase_is_expressed_as_a_positive_delta(self, delhi_network):
        spec = ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_DELHI"],
            capacity_delta_units=2_000.0,
        )
        ScenarioValidator().validate(spec, delhi_network)
        network, overrides = ScenarioBuilder().build(delhi_network, spec)

        delhi = next(f for f in network.facilities if f.id == "DC_DELHI")
        assert delhi.capacity_units_per_period == pytest.approx(7_000.0)
        assert overrides == ["CHANGE_CAPACITY DC_DELHI +2,000 units/period"]

    def test_the_builder_never_touches_the_network_it_was_given(self, delhi_network):
        before = delhi_network.model_dump_json()
        ScenarioBuilder().build(delhi_network, DELHI_MINUS_2000)
        assert delhi_network.model_dump_json() == before


# ===========================================================================
# §10 — NLP → scenario planner
# ===========================================================================

class TestNaturalLanguageToScenario:

    def test_the_rule_parser_reads_the_spec_sentence_verbatim(self, orch):
        """
        "What happens if Delhi NCR DC capacity is reduced by 2,000 units/day?"
        must become CHANGE_CAPACITY(−2000), offline, with no model involved.
        """
        agent = orch.services["intent_agent"]
        resolution = agent.resolve(
            "What happens if Delhi NCR DC capacity is reduced by 2,000 units/day?",
            known_facility_ids=["DC_DELHI", "DC_MUMBAI", "DC_KOLKATA", "PLANT_N"],
            allow_llm=False,
        )

        assert resolution.intent == Intent.SCENARIO_ANALYSIS
        assert resolution.source == "rules"
        [spec] = resolution.scenarios
        assert spec.action == ScenarioActionType.CHANGE_CAPACITY
        assert spec.facility_ids == ["DC_DELHI"]
        assert spec.capacity_delta_units == pytest.approx(-2_000.0)
        assert spec.capacity_multiplier is None

    def test_increase_language_flips_the_sign(self, orch):
        resolution = orch.services["intent_agent"].resolve(
            "Increase DC_DELHI capacity by 2,000 units/day.",
            known_facility_ids=["DC_DELHI"], allow_llm=False,
        )
        [spec] = resolution.scenarios
        assert spec.capacity_delta_units == pytest.approx(2_000.0)

    def test_a_percentage_becomes_a_multiplier_not_a_unit_delta(self, orch):
        resolution = orch.services["intent_agent"].resolve(
            "What if we reduce DC_DELHI capacity by 20%?",
            known_facility_ids=["DC_DELHI"], allow_llm=False,
        )
        [spec] = resolution.scenarios
        assert spec.capacity_multiplier == pytest.approx(0.8)
        assert spec.capacity_delta_units is None

    def test_capacity_language_is_not_mistaken_for_a_closure(self, orch):
        """
        "reduce" is close-adjacent, but reducing capacity is reversible and
        operational; closing a facility is structural and HUMAN_ONLY. Conflating
        them would misgovern the request.
        """
        resolution = orch.services["intent_agent"].resolve(
            "Reduce DC_DELHI capacity by 2,000 units.",
            known_facility_ids=["DC_DELHI"], allow_llm=False,
        )
        [spec] = resolution.scenarios
        assert spec.action == ScenarioActionType.CHANGE_CAPACITY
        assert spec.action != ScenarioActionType.CLOSE_FACILITY

    def test_no_quantity_means_no_fabricated_quantity(self, orch):
        """Without a number there is nothing defensible to pass to the MILP."""
        resolution = orch.services["intent_agent"].resolve(
            "What if we reduce DC_DELHI capacity?",
            known_facility_ids=["DC_DELHI"], allow_llm=False,
        )
        assert not any(s.action == ScenarioActionType.CHANGE_CAPACITY
                       for s in resolution.scenarios)

    def test_an_end_to_end_run_driven_purely_by_the_sentence(self, delhi_network,
                                                             planner_actor):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="What happens if DC_DELHI capacity is reduced by 4,950 units/day?",
            actor=planner_actor, disable_llm=True,
        ))

        assert response.intent == Intent.SCENARIO_ANALYSIS.value
        assert response.results["network"]["scenario_overrides"] == [
            "CHANGE_CAPACITY DC_DELHI -4,950 units/period"
        ]
        assert response.results["network"]["business_cost_delta"] == pytest.approx(
            200.0, abs=1e-6
        )

    def test_a_model_proposed_scenario_is_validated_not_trusted(self, delhi_network,
                                                                planner_actor):
        """The LLM may propose; the network decides. A hallucinated site dies here."""
        gateway = FakeGateway({
            "intent": json.dumps({
                "intent": "SCENARIO_ANALYSIS",
                "confidence": 0.9,
                "facility_ids": ["DC_ATLANTIS"],
                "scenarios": [{
                    "action": "CHANGE_CAPACITY",
                    "facility_ids": ["DC_ATLANTIS"],
                    "capacity_delta_units": -2000,
                    "label": "Cut Atlantis",
                }],
                "rationale": "invented site",
            }),
            "reasoning": reasoning_json(summary="No analysis was produced."),
        })
        orch = build_orchestrator(network=delhi_network, gateway=gateway)

        response = orch.run_sync(OrchestratorRequest(
            input="Trim the Atlantis distribution centre a little.",
            actor=planner_actor,
        ))

        # The invented facility was stripped before it could become a scenario.
        assert "DC_ATLANTIS" not in json.dumps(response.results)
        assert orch.scenarios.list_ids() == []
        assert response.status != ExecutionState.COMPLETED.value

    def test_a_model_supplying_both_capacity_forms_is_dropped_not_guessed(self,
                                                                          delhi_network):
        gateway = FakeGateway({"intent": json.dumps({
            "intent": "SCENARIO_ANALYSIS",
            "confidence": 0.9,
            "facility_ids": ["DC_DELHI"],
            "scenarios": [{
                "action": "CHANGE_CAPACITY",
                "facility_ids": ["DC_DELHI"],
                "capacity_multiplier": 0.5,
                "capacity_delta_units": -2000,
                "label": "ambiguous",
            }],
            "rationale": "ambiguous quantity",
        })})
        from netgravity.orchestrator.agents.intent_agent import IntentAgent

        resolution = IntentAgent(gateway)._llm_based(
            "trim delhi", ["DC_DELHI"],
        )
        [spec] = resolution.scenarios
        assert spec.capacity_multiplier is None
        assert spec.capacity_delta_units is None

# ---------------------------------------------------------------------------
# The footprint a scenario is allowed to change
# ---------------------------------------------------------------------------


def _network_where_closing_is_cheaper():
    """
    The shared Delhi fixture with one change: DC_KOLKATA carries a fixed cost
    large enough that shutting it and serving MKT_EAST down Delhi's backup lane
    is the cheaper plan. Every other cost is the fixture's, so the ONLY reason
    the solver has to touch the footprint is the one this test is about.
    """
    net = build_delhi_network()
    return net.model_copy(update={"facilities": [
        f.model_copy(update={"fixed_cost_per_year": 50_000.0})
        if f.id == "DC_KOLKATA" else f
        for f in net.facilities
    ]})


def _solved_open(net):
    """Which DCs the MILP opens, solved the way a scenario is solved."""
    from netgravity.optimization.milp import solve as milp_solve
    from netgravity.schemas.network import OptimizationMode

    cfg = net.config.model_copy(update={
        "optimization_mode": OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION})
    result = milp_solve(net, cfg, "footprint-probe")
    return {d.facility_id: d.is_open for d in result.facility_decisions
            if d.facility_id.startswith("DC")}


class TestOnlyTheUserClosesASite:
    """
    A scenario answers the question the planner asked. It does not redesign the
    footprint on the way past.

    A scenario solves as `BROWNFIELD_SCENARIO_OPTIMIZATION`, which honours
    facility flags as supplied — and an uploaded network supplies
    `is_closable = True`. So the MILP was free to shut any site it found
    cheaper to shut. Ask "what if demand grows 20%?" on the network below and
    the answer came back with a DC closed that the question never mentioned,
    and a cost delta that was mostly the closure.

    Closing a site is a decision. It happens when someone decides it.
    """

    def test_without_the_hold_the_solver_would_close_a_site(self):
        """The premise. If this ever stops being true the tests below prove
        nothing, because there would be no closure left to prevent."""
        assert _solved_open(_network_where_closing_is_cheaper()) == {
            "DC_DELHI": True, "DC_MUMBAI": True, "DC_KOLKATA": False}

    def test_a_demand_scenario_closes_nothing(self):
        built, _ = ScenarioBuilder().build(
            _network_where_closing_is_cheaper(),
            ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND,
                               demand_multiplier=1.2, label="Demand +20%"))
        assert _solved_open(built) == {
            "DC_DELHI": True, "DC_MUMBAI": True, "DC_KOLKATA": True}

    def test_a_capacity_scenario_closes_nothing_either(self):
        built, _ = ScenarioBuilder().build(
            _network_where_closing_is_cheaper(),
            ScenarioIntentSpec(action=ScenarioActionType.CHANGE_CAPACITY,
                               facility_ids=["DC_DELHI"],
                               capacity_multiplier=1.5,
                               label="Delhi +50%"))
        assert _solved_open(built)["DC_KOLKATA"] is True

    def test_the_users_own_closure_still_closes(self):
        """The exception the whole rule exists for. `is_forced_closed` is never
        touched, so a site the scenario closed stays closed."""
        built, overrides = ScenarioBuilder().build(
            _network_where_closing_is_cheaper(),
            ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                               facility_ids=["DC_KOLKATA"],
                               label="Close Kolkata"))
        assert overrides == ["CLOSE_FACILITY DC_KOLKATA"]
        assert _solved_open(built)["DC_KOLKATA"] is False

    def test_the_closure_still_pays_its_closure_cost(self):
        """
        `baseline_status` is what closure economics key on, and the hold must
        not disturb it — a closure that stopped being priced would be a worse
        bug than the one being fixed.
        """
        from netgravity.schemas.network import FacilityStatus

        built, _ = ScenarioBuilder().build(
            _network_where_closing_is_cheaper(),
            ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY,
                               facility_ids=["DC_KOLKATA"],
                               label="Close Kolkata"))
        shut = next(f for f in built.facilities if f.id == "DC_KOLKATA")
        assert shut.effective_baseline_status == FacilityStatus.EXISTING
        assert shut.is_forced_closed is True
        assert shut.is_mandatory is False

    def test_markets_are_never_pinned(self):
        """Demand is not footprint. A market has no open/close decision to
        hold, and writing flags onto one would be a change with no meaning."""
        base = _network_where_closing_is_cheaper()
        before = {f.id: (f.is_mandatory, f.is_closable)
                  for f in base.facilities if f.id.startswith("MKT")}
        built, _ = ScenarioBuilder().build(
            base, ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND,
                                     demand_multiplier=1.1, label="+10%"))
        after = {f.id: (f.is_mandatory, f.is_closable)
                 for f in built.facilities if f.id.startswith("MKT")}
        assert after == before

    def test_a_candidate_is_offered_not_forced_open(self):
        """
        Holding the EXISTING footprint is not the same as opening everything.
        A proposed site stays the solver's choice: forcing one open would
        answer a question nobody asked, in the opposite direction.
        """
        from netgravity.schemas.network import FacilityRecord, FacilityStatus, NodeRole

        base = _network_where_closing_is_cheaper()
        candidate = FacilityRecord(
            id="DC_PROPOSED", name="Proposed DC", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE,
            capacity_units_per_period=5_000.0, fixed_cost_per_year=0.0)
        base = base.model_copy(update={
            "facilities": [*base.facilities, candidate]})

        built, _ = ScenarioBuilder().build(
            base, ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND,
                                     demand_multiplier=1.1, label="+10%"))
        proposed = next(f for f in built.facilities if f.id == "DC_PROPOSED")
        assert proposed.is_mandatory is False
        assert proposed.is_closable is True

    def test_a_disruption_target_is_not_pinned_open(self):
        """An outage is the point of the run that carries one. Pinning the
        target open would delete the disruption being modelled."""
        base = _network_where_closing_is_cheaper()
        base = base.model_copy(update={"facilities": [
            f.model_copy(update={"is_disruption_target": True})
            if f.id == "DC_DELHI" else f
            for f in base.facilities]})
        built, _ = ScenarioBuilder().build(
            base, ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND,
                                     demand_multiplier=1.1, label="+10%"))
        target = next(f for f in built.facilities if f.id == "DC_DELHI")
        assert target.is_mandatory is False
        assert target.is_closable is True

    def test_greenfield_still_designs_a_footprint(self):
        """
        The hold is a SCENARIO rule and lives in the scenario builder. A
        greenfield design releases the footprint through its own mode policy
        and never passes through here — that is still where a planner goes to
        ask which sites should exist.
        """
        from netgravity.optimization.modes import get_mode_policy
        from netgravity.schemas.network import OptimizationMode

        policy = get_mode_policy(OptimizationMode.GREENFIELD_OPTIMIZATION)
        assert policy.release_existing_footprint is True
        assert policy.pin_existing_open is False

        builder_source = inspect.getsource(ScenarioBuilder)
        assert "_hold_the_existing_footprint" in builder_source

