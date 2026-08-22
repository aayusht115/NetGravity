"""
Phase 5 integration — the Digital Twin.

Covers the acceptance criteria in order: contract validation, baseline and
scenario state, baseline immutability, multiple scenarios, comparison,
orchestrator integration, provenance, missing/failed engine results, snapshot
consistency and concurrency. Scale benchmarks live in
`netgravity/tests/test_twin_scale_benchmark.py`.

Two things are asserted structurally rather than behaviourally, because
behaviour can be restored by accident and a structure cannot:

  * the twin package imports no engine, checked against the compiled source
    with docstrings stripped, so a module that merely *discusses* MILP does not
    read as one that calls it;
  * no engine imports the twin, so `MILP → Digital Twin` has no code path to
    travel along.

The deterministic chain is real throughout. MILP, REI and RF run their
production paths; only the LLM is disabled.
"""

from __future__ import annotations

import ast
import concurrent.futures
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest
from pydantic import ValidationError

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.schemas.requests import (
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.orchestrator.schemas.twin import (
    DeltaDirection,
    DigitalTwinState,
    FacilityState,
    FlowState,
    StorageMode,
    TwinCalculationStatus,
    TwinKPIs,
    TwinProvenance,
    TwinStateType,
    UnavailableValue,
    ValueStatus,
)
from netgravity.orchestrator.twin import (
    DigitalTwinService,
    DigitalTwinStore,
    TwinStateNotFound,
    apply_delta,
    build_flow_aggregate,
    build_unavailable_state,
    make_state_id,
    to_delta,
)
from netgravity.tests.integration.conftest import (
    build_delhi_network,
    build_infeasible_network,
    flood_signal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _close_scenario(facility_id: str = "DC_DELHI") -> ScenarioIntentSpec:
    return ScenarioIntentSpec(
        action=ScenarioActionType.CLOSE_FACILITY, facility_ids=[facility_id],
    )


def _run_scenario(orch, facility_id: str = "DC_DELHI", request_id: str = ""):
    kwargs: Dict[str, Any] = {
        "input": f"What if we close {facility_id}?",
        "explicit_intent": Intent.SCENARIO_ANALYSIS,
        "explicit_scenarios": [_close_scenario(facility_id)],
    }
    if request_id:
        kwargs["request_id"] = request_id
    return orch.run_sync(OrchestratorRequest(**kwargs))


def _run_state_query(orch):
    return orch.run_sync(OrchestratorRequest(
        input="What is our current network state?",
        explicit_intent=Intent.NETWORK_STATE_QUERY,
    ))


def _provenance(**overrides: Any) -> TwinProvenance:
    base: Dict[str, Any] = {"snapshot_id": "snap_test"}
    base.update(overrides)
    return TwinProvenance(**base)


def _facility(fid: str, *, is_open: bool = True, throughput: float = 100.0,
              utilization: float = 50.0, rei: Optional[float] = None) -> FacilityState:
    return FacilityState(
        facility_id=fid, facility_name=fid, role="DC", is_open=is_open,
        throughput_units=throughput, capacity_units=200.0,
        utilization_pct=utilization, rei=rei,
        rei_status=(ValueStatus.AVAILABLE if rei is not None
                    else ValueStatus.NOT_COMPUTED),
    )


def _flow(origin: str, dest: str, units: float) -> FlowState:
    return FlowState(origin_id=origin, destination_id=dest, flow_units=units,
                     transport_cost=units * 2.0, distance_km=100.0)


def _state(state_id: str, *, state_type: TwinStateType = TwinStateType.BASELINE,
           snapshot_id: str = "snap_test", scenario_id: Optional[str] = None,
           facilities: Optional[List[FacilityState]] = None,
           flows: Optional[List[FlowState]] = None,
           kpis: Optional[TwinKPIs] = None) -> DigitalTwinState:
    resolved_flows = flows if flows is not None else [_flow("P1", "DC_A", 100.0)]
    return DigitalTwinState(
        state_id=state_id, snapshot_id=snapshot_id, scenario_id=scenario_id,
        state_type=state_type,
        provenance=_provenance(
            snapshot_id=snapshot_id, scenario_id=scenario_id,
            is_hypothetical=state_type is not TwinStateType.BASELINE,
        ),
        facilities=facilities if facilities is not None else [_facility("DC_A")],
        flows=resolved_flows,
        # Built the same way the real builder does, so a hand-made state is not
        # quietly missing something every published state carries.
        flow_aggregate=build_flow_aggregate(resolved_flows),
        kpis=kpis,
    )


def _code_only(path: Path) -> str:
    """
    Source with every docstring removed.

    A module that explains in prose why it never imports MILP would otherwise
    fail a naive substring scan for "milp". Stripping docstrings means these
    tests read the code and nothing else.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _twin_modules() -> List[Path]:
    root = Path(__file__).resolve().parents[2] / "orchestrator" / "twin"
    return sorted(root.glob("*.py"))


# ===========================================================================
# A. Contract validation
# ===========================================================================

class TestContractValidation:

    def test_a_delta_without_a_base_is_rejected(self):
        """A delta with nothing to apply it to cannot be materialised at all."""
        with pytest.raises(ValidationError, match="requires base_state_id"):
            DigitalTwinState(
                state_id="s1", snapshot_id="snap_test",
                state_type=TwinStateType.BASELINE,
                storage_mode=StorageMode.DELTA,
                provenance=_provenance(is_hypothetical=False),
            )

    def test_a_full_state_may_not_carry_a_base_reference(self):
        """A self-contained state pointing at a base is a contradiction."""
        with pytest.raises(ValidationError, match="must not carry base_state_id"):
            DigitalTwinState(
                state_id="s1", snapshot_id="snap_test",
                state_type=TwinStateType.BASELINE,
                storage_mode=StorageMode.FULL, base_state_id="other",
                provenance=_provenance(is_hypothetical=False),
            )

    def test_scenario_type_requires_a_scenario_id(self):
        with pytest.raises(ValidationError, match="requires a scenario_id"):
            DigitalTwinState(
                state_id="s1", snapshot_id="snap_test",
                state_type=TwinStateType.SCENARIO, provenance=_provenance(),
            )

    def test_a_scenario_id_may_not_hide_under_a_non_scenario_type(self):
        """
        The pair (state_type, scenario_id) is what a viewer reads to decide
        whether it is looking at reality. They may not disagree.
        """
        with pytest.raises(ValidationError, match="must declare itself SCENARIO"):
            DigitalTwinState(
                state_id="s1", snapshot_id="snap_test", scenario_id="scn_1",
                state_type=TwinStateType.OPTIMIZED,
                provenance=_provenance(scenario_id="scn_1"),
            )

    def test_only_a_baseline_may_claim_to_be_observed(self):
        with pytest.raises(ValidationError, match="hypothetical by definition"):
            DigitalTwinState(
                state_id="s1", snapshot_id="snap_test",
                state_type=TwinStateType.OPTIMIZED,
                provenance=_provenance(is_hypothetical=False),
            )

    def test_a_published_state_is_frozen(self):
        state = _state("s1")
        with pytest.raises(ValidationError):
            state.state_id = "tampered"  # type: ignore[misc]

    def test_the_contract_has_no_field_for_a_computed_result(self):
        """
        The twin cannot carry a value it would have had to calculate.

        Structural, like `ExtractionResult`: evidence that arrives pre-scored
        by the wrong component cannot be checked by the right one.
        """
        fields = set(DigitalTwinState.model_fields)
        for banned in ("objective", "solve", "optimize", "compute"):
            assert not any(banned in f for f in fields), (
                f"DigitalTwinState has a field containing '{banned}'"
            )

    def test_extra_fields_are_forbidden_on_every_twin_model(self):
        """
        An unknown key is a contract violation, not something to pass through.

        Silent acceptance is how a value nobody validated reaches a viewer.
        """
        for model in (DigitalTwinState, TwinProvenance, FacilityState,
                      FlowState, TwinKPIs, UnavailableValue):
            assert model.model_config.get("extra") == "forbid", model.__name__

    def test_kpis_default_to_none_not_zero(self):
        """
        Zero cost and zero unmet demand describe a network that ran perfectly
        for free. Absence must not be able to say that.
        """
        kpis = TwinKPIs()
        assert kpis.business_network_cost is None
        assert kpis.unserved_demand is None
        assert kpis.demand_fill_rate is None

    def test_state_ids_are_deterministic(self):
        assert (make_state_id("snap_a", TwinStateType.BASELINE)
                == make_state_id("snap_a", TwinStateType.BASELINE))
        assert (make_state_id("snap_a", TwinStateType.BASELINE)
                != make_state_id("snap_a", TwinStateType.OPTIMIZED))
        assert (make_state_id("snap_a", TwinStateType.SCENARIO, "scn_1")
                != make_state_id("snap_a", TwinStateType.SCENARIO, "scn_2"))


# ===========================================================================
# B. Baseline state creation
# ===========================================================================

class TestBaselineState:

    def test_a_state_query_publishes_one_state(self, orch):
        response = _run_state_query(orch)
        assert response.status == "COMPLETED"
        assert len(response.twin_states) == 1
        assert response.twin_states[0]["state_type"] == "OPTIMIZED"

    def test_the_state_carries_every_facility_the_milp_decided_on(self, orch):
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])

        assert {f.facility_id for f in view.facilities} == {
            "PLANT_N", "DC_DELHI", "DC_MUMBAI", "DC_KOLKATA",
        }
        assert all(f.is_open for f in view.facilities)

    def test_facility_utilisation_is_the_engines_own_figure(self, orch):
        """
        The twin copies `utilization_pct`; it does not divide throughput by
        capacity. The engine's definition is the one the rest of the system
        reasons about, and a second definition here would diverge silently.
        """
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])
        delhi = next(f for f in view.facilities if f.facility_id == "DC_DELHI")

        # Delhi ships 100 units into MKT_NORTH against 5,000 capacity.
        assert delhi.throughput_units == pytest.approx(100.0)
        assert delhi.capacity_units == pytest.approx(5_000.0)
        assert delhi.utilization_pct == pytest.approx(2.0, abs=1e-6)

    def test_kpis_match_the_hand_calculable_baseline(self, orch):
        """
        The Delhi fixture's baseline cost is arithmetic: 100·(1+2) + 100·(1+3)
        + 100·(1+4) = 1,200. If the twin reported anything else it would be
        inventing a number.
        """
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])

        assert view.kpis is not None
        assert view.kpis.business_network_cost == pytest.approx(1_200.0)
        assert view.kpis.total_demand == pytest.approx(300.0)
        assert view.kpis.served_demand == pytest.approx(300.0)
        assert view.kpis.unserved_demand == pytest.approx(0.0)

    def test_flows_carry_lane_volumes_and_their_share(self, orch):
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])

        assert view.flows.total > 0
        assert sum(f.share_of_total_units for f in view.flows.items) == pytest.approx(1.0)
        lane = next(f for f in view.flows.items
                    if f.origin_id == "DC_DELHI" and f.destination_id == "MKT_NORTH")
        assert lane.flow_units == pytest.approx(100.0)

    def test_the_flow_aggregate_totals_match_the_lane_list(self, orch):
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"], flow_limit=0)

        assert view.flow_aggregate is not None
        assert view.flow_aggregate.total_lanes == len(view.flows.items)
        assert view.flow_aggregate.total_flow_units == pytest.approx(
            sum(f.flow_units for f in view.flows.items), abs=1e-4,
        )

    def test_an_optimized_state_is_not_marked_observed(self, orch):
        """An optimum is a proposal. Only an as-is evaluation is reality."""
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])
        assert view.provenance.is_hypothetical is True
        assert view.state_type is TwinStateType.OPTIMIZED


# ===========================================================================
# C. Scenario state creation
# ===========================================================================

class TestScenarioState:

    def test_a_scenario_run_publishes_a_baseline_and_a_scenario(self, orch):
        response = _run_scenario(orch)
        types = {t["state_type"] for t in response.twin_states}
        assert types == {"OPTIMIZED", "SCENARIO"}

    def test_the_scenario_state_reflects_the_closure(self, orch):
        response = _run_scenario(orch, "DC_DELHI")
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)

        delhi = next(f for f in view.facilities if f.facility_id == "DC_DELHI")
        assert delhi.is_open is False
        assert delhi.throughput_units == pytest.approx(0.0)

    def test_the_scenario_cost_is_the_hand_calculable_figure(self, orch):
        """
        With Delhi closed, MKT_NORTH falls back to Mumbai at rate 6:
        100·(1+6) + 100·(1+3) + 100·(1+4) = 1,600.
        """
        response = _run_scenario(orch, "DC_DELHI")
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)
        assert view.kpis is not None
        assert view.kpis.business_network_cost == pytest.approx(1_600.0)

    def test_the_scenario_state_names_its_overrides(self, orch):
        response = _run_scenario(orch, "DC_DELHI")
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)
        assert view.decisions
        assert any("DC_DELHI" in d for d in view.decisions)

    def test_a_scenario_is_stored_as_a_delta_against_the_baseline(self, orch):
        """
        Compression is the point: N scenarios on a snapshot must cost N deltas,
        not N copies of the network.
        """
        response = _run_scenario(orch, "DC_DELHI")
        scenario_ref = next(t for t in response.twin_states
                            if t["state_type"] == "SCENARIO")
        baseline_ref = next(t for t in response.twin_states
                            if t["state_type"] == "OPTIMIZED")

        assert orch.twin.store.is_delta(scenario_ref["state_id"])
        assert scenario_ref["n_facilities"] < baseline_ref["n_facilities"]

    def test_materialising_a_delta_restores_the_whole_network(self, orch):
        response = _run_scenario(orch, "DC_DELHI")
        scenario_ref = next(t for t in response.twin_states
                            if t["state_type"] == "SCENARIO")
        baseline_ref = next(t for t in response.twin_states
                            if t["state_type"] == "OPTIMIZED")

        full = orch.twin.materialize(scenario_ref["state_id"])
        assert full.storage_mode is StorageMode.FULL
        assert len(full.facilities) == baseline_ref["n_facilities"]
        assert full.facility("DC_DELHI") is not None

    def test_a_view_reports_that_it_came_from_a_delta(self, orch):
        response = _run_scenario(orch)
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)
        assert view.materialized_from_delta is True
        assert view.base_state_id is not None


# ===========================================================================
# D. Baseline immutability
# ===========================================================================

class TestBaselineImmutability:

    def test_publishing_a_scenario_does_not_alter_the_baseline_decisions(self, orch):
        """
        The baseline's decisions and economics are the fixed point a scenario is
        measured against. A scenario run may legitimately REPUBLISH the baseline
        with more evidence attached — a workflow that also ran REI knows the
        exposure the earlier one did not — but it must never move the numbers.
        """
        state_response = _run_state_query(orch)
        baseline_id = state_response.twin_states[0]["state_id"]
        before = orch.twin.materialize(baseline_id)

        _run_scenario(orch, "DC_DELHI")
        after = orch.twin.materialize(baseline_id)

        assert after.kpis == before.kpis
        assert ([(f.facility_id, f.is_open, f.throughput_units) for f in after.facilities]
                == [(f.facility_id, f.is_open, f.throughput_units) for f in before.facilities])
        assert ([(f.lane_key, f.flow_units) for f in after.flows]
                == [(f.lane_key, f.flow_units) for f in before.flows])

    def test_republishing_a_base_expands_the_deltas_that_depended_on_it(self, orch):
        """
        State ids are deterministic, so a later run republishes the same
        baseline — and the scenario run attaches REI the state query could not.
        A delta stored against the old content would otherwise materialise
        against the new content and describe a network that never existed.
        """
        scenario_response = _run_scenario(orch, "DC_DELHI")
        snapshot_id = scenario_response.network_snapshot_id
        scenario_ref = next(t for t in scenario_response.twin_states
                            if t["state_type"] == "SCENARIO")
        assert orch.twin.store.is_delta(scenario_ref["state_id"])

        before = orch.twin.materialize(scenario_ref["state_id"])

        # A state query republishes the baseline WITHOUT REI, changing the
        # facility rows the delta borrows.
        _run_state_query(orch)

        assert not orch.twin.store.is_delta(scenario_ref["state_id"])
        after = orch.twin.materialize(scenario_ref["state_id"])
        assert after.facilities == before.facilities
        assert after.flows == before.flows

    def test_mutating_a_retrieved_state_cannot_corrupt_the_store(self, orch):
        response = _run_state_query(orch)
        state_id = response.twin_states[0]["state_id"]

        first = orch.twin.store.get(state_id)
        # Frozen, so the object cannot be edited in place at all — and the copy
        # handed out is a distinct object regardless.
        second = orch.twin.store.get(state_id)
        assert first == second
        assert first is not second
        assert first.facilities is not second.facilities

    def test_mutating_the_caller_s_object_after_publishing_changes_nothing(self):
        store = DigitalTwinStore()
        facilities = [_facility("DC_A")]
        state = _state("s1", facilities=facilities)
        store.put(state)

        facilities.append(_facility("DC_SMUGGLED"))
        stored = store.get("s1")
        assert {f.facility_id for f in stored.facilities} == {"DC_A"}

    def test_the_observed_snapshot_is_untouched_by_twin_publication(self, orch):
        snapshot_id = orch.snapshots.current_id
        before = copy.deepcopy(orch.snapshots.get(snapshot_id).network)

        _run_scenario(orch, "DC_DELHI")

        after = orch.snapshots.get(snapshot_id).network
        assert after.compute_data_version() == before.compute_data_version()
        assert [f.id for f in after.facilities] == [f.id for f in before.facilities]


# ===========================================================================
# E. Multiple scenarios
# ===========================================================================

class TestMultipleScenarios:

    def test_several_scenarios_coexist_on_one_snapshot(self, orch):
        first = _run_scenario(orch, "DC_DELHI", request_id="req_a")
        second = _run_scenario(orch, "DC_MUMBAI", request_id="req_b")
        third = _run_scenario(orch, "DC_KOLKATA", request_id="req_c")

        snapshot_id = first.network_snapshot_id
        scenarios = orch.twin.list_scenarios(snapshot_id)
        assert len({r.scenario_id for r in scenarios}) == 3
        assert {second.scenario_id, third.scenario_id} <= {
            r.scenario_id for r in scenarios
        }

    def test_each_scenario_reports_its_own_closure(self, orch):
        runs = {
            fid: _run_scenario(orch, fid, request_id=f"req_{fid}")
            for fid in ("DC_DELHI", "DC_MUMBAI", "DC_KOLKATA")
        }
        for fid, response in runs.items():
            view = orch.twin.get(response.network_snapshot_id, response.scenario_id)
            closed = {f.facility_id for f in view.facilities if not f.is_open}
            assert closed == {fid}, f"{fid} scenario shows closures {closed}"

    def test_scenarios_do_not_leak_costs_into_each_other(self, orch):
        """
        Each closure has a different, hand-calculable cost. Equal costs would
        mean one scenario had overwritten another.
        """
        expected = {"DC_DELHI": 1_600.0, "DC_MUMBAI": 1_700.0, "DC_KOLKATA": 1_400.0}
        for fid, want in expected.items():
            response = _run_scenario(orch, fid, request_id=f"cost_{fid}")
            view = orch.twin.get(response.network_snapshot_id, response.scenario_id)
            assert view.kpis is not None
            assert view.kpis.business_network_cost == pytest.approx(want), fid

    def test_republishing_the_same_scenario_replaces_rather_than_accumulates(self, orch):
        response = _run_scenario(orch, "DC_DELHI", request_id="first")
        before = len(orch.twin.store)

        scenario_ref = next(t for t in response.twin_states
                            if t["state_type"] == "SCENARIO")
        state = orch.twin.materialize(scenario_ref["state_id"])
        orch.twin.update(state)

        assert len(orch.twin.store) == before


# ===========================================================================
# F. Baseline vs scenario comparison
# ===========================================================================

class TestComparison:

    def test_comparison_reports_the_closure_and_the_cost_increase(self, orch):
        response = _run_scenario(orch, "DC_DELHI")
        comparison = orch.twin.compare_scenario(
            response.network_snapshot_id, response.scenario_id,
        )

        assert comparison.facilities_closed == ["DC_DELHI"]
        assert comparison.facilities_opened == []

        cost = comparison.delta("business_network_cost")
        assert cost is not None
        assert cost.baseline_value == pytest.approx(1_200.0)
        assert cost.comparison_value == pytest.approx(1_600.0)
        assert cost.abs_delta == pytest.approx(400.0)
        assert cost.pct_delta == pytest.approx(33.333333, abs=1e-4)
        assert cost.direction is DeltaDirection.INCREASED

    def test_an_unchanged_metric_is_reported_unchanged_not_omitted(self, orch):
        response = _run_scenario(orch, "DC_DELHI")
        comparison = orch.twin.compare_scenario(
            response.network_snapshot_id, response.scenario_id,
        )
        served = comparison.delta("served_demand")
        assert served is not None
        assert served.direction is DeltaDirection.UNCHANGED

    def test_lane_changes_name_the_rerouting(self, orch):
        """Delhi→North disappears; Mumbai→North appears. Both must be visible."""
        response = _run_scenario(orch, "DC_DELHI")
        comparison = orch.twin.compare_scenario(
            response.network_snapshot_id, response.scenario_id,
        )
        changes = {(c.origin_id, c.destination_id): c.change
                   for c in comparison.lane_changes}
        assert changes.get(("DC_DELHI", "MKT_NORTH")) == "REMOVED"
        assert changes.get(("DC_MUMBAI", "MKT_NORTH")) == "ADDED"

    def test_unchanged_lanes_are_omitted_but_unchanged_facilities_are_not(self, orch):
        """
        A facility's open/closed state IS the decision, so every facility is
        reported. Lanes vastly outnumber facilities, so only changes are.
        """
        response = _run_scenario(orch, "DC_DELHI")
        comparison = orch.twin.compare_scenario(
            response.network_snapshot_id, response.scenario_id,
        )
        assert len(comparison.facility_changes) == 4
        assert any(c.change.startswith("UNCHANGED") for c in comparison.facility_changes)
        assert all(c.change in ("ADDED", "REMOVED", "INCREASED", "DECREASED")
                   for c in comparison.lane_changes)

    def test_a_metric_missing_on_one_side_is_not_comparable(self):
        """
        Subtracting from a missing value manufactures a change that never
        happened. It lands in `incomparable` with the reason instead.
        """
        service = DigitalTwinService()
        service.update(_state(
            "base", kpis=TwinKPIs(business_network_cost=100.0, total_demand=None),
        ))
        service.update(_state(
            "comp", state_type=TwinStateType.SCENARIO, scenario_id="scn_1",
            kpis=TwinKPIs(business_network_cost=120.0, total_demand=50.0),
        ))
        comparison = service.compare("base", "comp")

        demand = comparison.delta("total_demand")
        assert demand is not None
        assert demand.direction is DeltaDirection.NOT_COMPARABLE
        assert "baseline" in demand.reason
        assert demand.abs_delta is None

    def test_a_state_with_no_kpis_yields_no_fabricated_deltas(self):
        service = DigitalTwinService()
        service.update(_state("base", kpis=TwinKPIs(business_network_cost=100.0)))
        service.update(_state(
            "comp", state_type=TwinStateType.SCENARIO, scenario_id="scn_1", kpis=None,
        ))
        comparison = service.compare("base", "comp")

        assert comparison.kpi_deltas == []
        assert comparison.incomparable
        assert all(d.direction is DeltaDirection.NOT_COMPARABLE
                   for d in comparison.incomparable)
        assert any("NOT_COMPARABLE" in w for w in comparison.warnings)

    def test_a_zero_baseline_yields_no_percentage_rather_than_infinity(self):
        service = DigitalTwinService()
        service.update(_state("base", kpis=TwinKPIs(business_network_cost=0.0)))
        service.update(_state(
            "comp", state_type=TwinStateType.SCENARIO, scenario_id="scn_1",
            kpis=TwinKPIs(business_network_cost=500.0),
        ))
        delta = service.compare("base", "comp").delta("business_network_cost")

        assert delta is not None
        assert delta.abs_delta == pytest.approx(500.0)
        assert delta.pct_delta is None
        assert "zero" in delta.reason

    def test_comparing_across_snapshots_is_flagged(self):
        """
        A cross-snapshot delta blends a network change with a decision change
        and cannot be attributed to either. Permitted, but never silent.
        """
        service = DigitalTwinService()
        service.update(_state("a", snapshot_id="snap_1",
                              kpis=TwinKPIs(business_network_cost=100.0)))
        service.update(_state("b", snapshot_id="snap_2",
                              kpis=TwinKPIs(business_network_cost=200.0)))
        comparison = service.compare("a", "b")

        assert comparison.same_snapshot is False
        assert any("across snapshots" in w for w in comparison.warnings)

    def test_comparison_names_both_sides_by_id(self, orch):
        response = _run_scenario(orch, "DC_DELHI")
        comparison = orch.twin.compare_scenario(
            response.network_snapshot_id, response.scenario_id,
        )
        assert orch.twin.store.has(comparison.baseline_state_id)
        assert orch.twin.store.has(comparison.comparison_state_id)


# ===========================================================================
# G. Orchestrator → Digital Twin integration
# ===========================================================================

class TestOrchestratorIntegration:

    def test_milp_reaches_the_twin_through_the_orchestrator(self, orch):
        """
        The critical assertion: MILP → Orchestrator → Digital Twin works, and
        the figures that arrive are the solver's own.
        """
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])

        milp_output = None
        context = orch.state_store.get(response.execution_id)
        milp_output = context.output_of("optimization.solve")

        assert milp_output is not None
        assert view.kpis is not None
        assert view.kpis.business_network_cost == pytest.approx(
            milp_output["business_network_cost"],
        )
        assert view.provenance.run_id == milp_output["run_id"]

    def test_rei_reaches_the_twin_through_the_orchestrator(self, orch):
        """REI values arrive per facility, matching the registry exactly."""
        response = _run_scenario(orch, "DC_DELHI")
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)

        by_id = {f.facility_id: f.rei for f in view.facilities}
        # The fixture's documented arithmetic.
        assert by_id["DC_DELHI"] == pytest.approx(0.8)
        assert by_id["DC_MUMBAI"] == pytest.approx(1.0)
        assert by_id["DC_KOLKATA"] == pytest.approx(0.4)

    def test_rf_reaches_the_twin_through_the_orchestrator(self):
        """
        With P = 0.7 stated and REI = 0.8, RF = 0.94. The twin must show the
        risk layer's figure, not one of its own.
        """
        network = build_delhi_network()
        orch = build_orchestrator(network=network, enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="Flood warning for Delhi.",
            explicit_intent=Intent.EXTERNAL_EVENT,
            external_signal=flood_signal(probability=0.7),
        ))

        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])
        assert view.risk is not None
        assert view.risk.max_risk_factor == pytest.approx(0.94, abs=1e-6)
        assert view.risk.risk_factor_status is ValueStatus.AVAILABLE
        assert view.risk.max_rei == pytest.approx(1.0)

    def test_an_event_workflow_has_no_network_state_and_says_so(self):
        """
        The external-event graph runs REI and RF but no MILP solve, so there
        are no facility DECISIONS to represent. The twin shows the risk context
        it does have and records the absence of the rest — rather than
        fabricating an `is_open` for every node so the picture looks complete.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="Flood warning for Delhi.",
            explicit_intent=Intent.EXTERNAL_EVENT,
            external_signal=flood_signal(probability=0.7),
        ))
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])

        assert view.facilities == []
        assert view.kpis is None
        assert view.calculation_status is TwinCalculationStatus.PARTIAL
        assert any("without producing a network state" in u.reason
                   for u in view.unavailable)
        # The risk it DOES have is still published.
        assert view.risk is not None
        assert view.risk.max_risk_factor == pytest.approx(0.94, abs=1e-6)

    def test_the_response_carries_twin_references_not_payloads(self, orch):
        """
        A state grows with the network; a workflow response must not. The
        response carries handles, and the state is fetched separately.
        """
        response = _run_scenario(orch)
        for ref in response.twin_states:
            assert set(ref) == {
                "state_id", "snapshot_id", "scenario_id", "state_type",
                "calculation_status", "n_facilities", "n_flows", "generated_at",
            }
            assert "facilities" not in ref
            assert "kpis" not in ref

    def test_publication_is_recorded_in_the_audit_trail(self, orch):
        from netgravity.orchestrator.audit import events

        response = _run_scenario(orch)
        trace = orch.get_trace(response.execution_id)
        published = [e for e in trace.to_dict()["events"]
                     if e["type"] == events.TWIN_STATE_PUBLISHED]

        assert len(published) == 2
        assert {e["detail"]["state_type"] for e in published} == {
            "OPTIMIZED", "SCENARIO",
        }

    def test_the_twin_survives_a_projection_failure_without_failing_the_run(self, orch):
        """
        Representation must never break analysis. A broken twin costs the
        picture, not the answer.
        """
        class BrokenTwin:
            def update(self, state, **kwargs):
                raise RuntimeError("twin storage is down")

        orch.twin = BrokenTwin()
        response = _run_state_query(orch)

        assert response.status == "COMPLETED"
        assert response.results["network"]["business_network_cost"] == pytest.approx(1_200.0)
        assert response.twin_states == []
        assert any("Digital Twin state could not be published" in w
                   for w in response.warnings)


# ===========================================================================
# G2. No direct engine → twin integration
# ===========================================================================

class TestNoDirectEngineIntegration:

    ENGINE_IMPORTS = (
        "netgravity.optimization",
        "netgravity.resilience",
        "netgravity.costs",
        "netgravity.metrics",
        "orchestrator.risk",
    )

    def test_the_twin_package_imports_no_engine(self):
        """
        `MILP → Digital Twin` cannot exist if the twin cannot reach the MILP.

        Checked against compiled source with docstrings stripped, so a module
        that *explains* the boundary is not mistaken for one that crosses it.
        """
        for path in _twin_modules():
            code = _code_only(path)
            for banned in self.ENGINE_IMPORTS:
                assert f"import {banned}" not in code, f"{path.name} imports {banned}"
                assert f"from {banned}" not in code, f"{path.name} imports {banned}"

    def test_the_twin_package_never_calls_a_solver(self):
        for path in _twin_modules():
            code = _code_only(path)
            for banned in ("milp_solve", "assess_network_resilience",
                           "compute_risk_factor", "get_or_compute", ".solve("):
                assert banned not in code, f"{path.name} references {banned}"

    def test_the_twin_never_receives_a_canonical_network(self):
        """
        Accepting only result CONTRACTS is what makes it impossible for the
        builder to solve: it is never handed the inputs a solve would need.
        """
        for path in _twin_modules():
            code = _code_only(path)
            assert "CanonicalNetwork" not in code, f"{path.name} references a network"
            assert "OptimizationConfig" not in code, f"{path.name} references a config"

    def test_no_engine_imports_the_twin(self):
        """The dependency is one-directional, so an engine cannot push state."""
        root = Path(__file__).resolve().parents[2]
        engine_dirs = ["optimization", "resilience", "costs", "metrics",
                       "orchestrator/risk", "orchestrator/engines",
                       "orchestrator/governance"]
        offenders: List[str] = []
        for rel in engine_dirs:
            for path in (root / rel).rglob("*.py"):
                if "twin" in path.read_text(encoding="utf-8"):
                    offenders.append(str(path.relative_to(root)))
        assert offenders == [], f"engine modules referencing the twin: {offenders}"

    def test_only_the_orchestrator_publishes_state(self):
        """
        Exactly one non-test caller of `twin.update`. If a second appears, the
        twin has acquired a second upstream and this test is the alarm.
        """
        root = Path(__file__).resolve().parents[2]
        callers: Set[str] = set()
        for path in root.rglob("*.py"):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if "/tests/" in rel or rel.startswith("tests/") or "/twin/" in rel:
                continue
            if ".twin.update(" in path.read_text(encoding="utf-8"):
                callers.add(rel)
        assert callers == {"orchestrator/core/orchestrator.py"}, callers


# ===========================================================================
# H. Provenance
# ===========================================================================

class TestProvenance:

    def test_a_state_names_the_snapshot_and_solver_run_behind_it(self, orch):
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])
        p = view.provenance

        assert p.snapshot_id == response.network_snapshot_id
        assert p.data_version
        assert p.run_id
        assert p.solver_status == "OPTIMAL"
        assert p.execution_id == response.execution_id
        assert p.model_version
        assert p.generated_at

    def test_a_scenario_state_names_its_parent_and_overrides(self, orch):
        response = _run_scenario(orch, "DC_DELHI")
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)
        p = view.provenance

        assert p.scenario_id == response.scenario_id
        assert p.scenario_version == 1
        assert p.parent_snapshot_id == response.network_snapshot_id
        assert any("DC_DELHI" in o for o in p.scenario_overrides)
        assert p.is_hypothetical is True

    def test_the_source_is_always_the_orchestrator(self, orch):
        response = _run_scenario(orch)
        for ref in response.twin_states:
            view = orch.twin.get_by_id(ref["state_id"])
            assert view.provenance.source == "orchestrator"

    def test_risk_provenance_names_the_rei_batch(self, orch):
        response = _run_scenario(orch)
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)

        assert view.risk is not None
        assert view.risk.rei_batch_id
        assert view.risk.rei_snapshot_id == response.network_snapshot_id
        assert view.risk.rei_batch_status in (
            "COMPLETED", "COMPLETED_WITH_ERRORS",
        )


# ===========================================================================
# I. Missing / failed engine results
# ===========================================================================

class TestMissingAndFailedResults:

    def test_an_infeasible_run_still_publishes_a_state(self):
        """
        Publishing nothing would leave the previous state on screen — a viewer
        would see a healthy network with no sign the run collapsed.
        """
        orch = build_orchestrator(network=build_infeasible_network(), enable_llm=False)
        response = _run_state_query(orch)

        assert response.status == "INFEASIBLE"
        assert len(response.twin_states) == 1
        assert response.twin_states[0]["calculation_status"] == "INFEASIBLE"

    def test_an_infeasible_state_reports_no_kpis_rather_than_zeros(self):
        orch = build_orchestrator(network=build_infeasible_network(), enable_llm=False)
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])

        assert view.kpis is None
        assert view.facilities == []
        assert view.unavailable

    def test_a_facility_with_no_rei_is_none_never_zero(self, orch):
        """
        On a [0,1] relative scale, 0.0 is the value of the LEAST exposed node in
        the network — a specific claim about a node nobody assessed.
        """
        response = _run_scenario(orch)
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)

        plant = next(f for f in view.facilities if f.facility_id == "PLANT_N")
        assert plant.rei is None
        assert plant.rei != 0.0
        assert plant.rei_status in (ValueStatus.NOT_COMPUTABLE, ValueStatus.NOT_COMPUTED)

    def test_rf_absent_is_not_computed_rather_than_zero(self, orch):
        """A scenario run asserts no event, so no probability exists."""
        response = _run_scenario(orch)
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id)

        for facility in view.facilities:
            assert facility.risk_factor is None
            assert facility.risk_factor_status is not ValueStatus.AVAILABLE

    def test_a_signal_with_no_probability_yields_not_computable_not_a_guess(self):
        """
        Severity is not a probability. RF must refuse rather than infer one,
        and the twin must show the refusal.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        response = orch.run_sync(OrchestratorRequest(
            input="Severe flooding expected in Delhi.",
            explicit_intent=Intent.EXTERNAL_EVENT,
            external_signal=flood_signal(probability=None),
        ))
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])

        assert view.risk is not None
        assert view.risk.max_risk_factor is None
        assert view.risk.risk_factor_status is ValueStatus.NOT_COMPUTABLE
        assert view.risk.not_computable_reasons

    def test_missing_evidence_is_carried_onto_the_state(self):
        """
        The twin reports the same absences the control plane recorded, rather
        than inventing a second vocabulary for missing data.
        """
        state = build_unavailable_state(
            snapshot_id="snap_test",
            state_type=TwinStateType.OPTIMIZED,
            calculation_status=TwinCalculationStatus.FAILED,
            unavailable=[UnavailableValue(
                field="engine.optimization.solve",
                status=ValueStatus.FAILED,
                reason="solver crashed",
                capability="optimization.solve",
            )],
        )
        assert state.kpis is None
        assert state.calculation_status is TwinCalculationStatus.FAILED
        assert state.unavailable[0].capability == "optimization.solve"

    def test_an_injected_empty_store_is_actually_used(self):
        """
        The store defines `__len__`, so an EMPTY one is falsy. Built with
        `store or DigitalTwinStore()` the service would silently swap in a
        private store and every write would land somewhere the caller cannot
        see — which is exactly how a concurrency test in this file managed to
        pass while testing nothing.
        """
        injected = DigitalTwinStore()
        assert not injected, "precondition: an empty store is falsy"

        service = DigitalTwinService(injected)
        assert service.store is injected

        service.update(_state("s1"))
        assert len(injected) == 1

    def test_reading_a_state_that_was_never_published_raises(self):
        service = DigitalTwinService()
        with pytest.raises(TwinStateNotFound):
            service.get("snap_missing")

    def test_comparing_a_scenario_with_no_baseline_raises(self):
        service = DigitalTwinService()
        service.update(_state("s", state_type=TwinStateType.SCENARIO,
                              scenario_id="scn_1"))
        with pytest.raises(TwinStateNotFound, match="nothing to compare"):
            service.compare_scenario("snap_test", "scn_1")

    def test_a_delta_applied_to_the_wrong_base_is_refused(self):
        """Merging a delta onto a foreign base yields a network that never was."""
        base = _state("base_a")
        other = _state("base_b")
        delta = to_delta(
            _state("scn", state_type=TwinStateType.SCENARIO, scenario_id="s1",
                   facilities=[_facility("DC_A", is_open=False)]),
            base,
        )
        with pytest.raises(ValueError, match="declares base"):
            apply_delta(delta, other)


# ===========================================================================
# J. Snapshot consistency
# ===========================================================================

class TestSnapshotConsistency:

    def test_a_state_belongs_to_exactly_one_snapshot(self, orch):
        response = _run_scenario(orch)
        for ref in response.twin_states:
            assert ref["snapshot_id"] == response.network_snapshot_id

    def test_states_are_indexed_under_their_snapshot(self, orch):
        response = _run_scenario(orch)
        refs = orch.twin.list_states(response.network_snapshot_id)
        assert len(refs) == 2
        assert orch.twin.list_states("snap_nonexistent") == []

    def test_a_new_snapshot_gets_its_own_states(self, orch):
        """States from one network version must not answer for another."""
        first = _run_state_query(orch)

        bigger = build_delhi_network(delhi_capacity=9_000.0)
        second_snapshot = orch.register_network(bigger, label="expanded")
        second = _run_state_query(orch)

        assert second_snapshot != first.network_snapshot_id
        assert second.network_snapshot_id == second_snapshot
        assert len(orch.twin.list_states(first.network_snapshot_id)) == 1
        assert len(orch.twin.list_states(second_snapshot)) == 1

    def test_the_state_data_version_matches_the_snapshot(self, orch):
        response = _run_state_query(orch)
        view = orch.twin.get_by_id(response.twin_states[0]["state_id"])
        snapshot = orch.snapshots.get(response.network_snapshot_id)
        assert view.provenance.data_version == snapshot.data_version

    def test_retrieval_is_deterministic(self, orch):
        response = _run_state_query(orch)
        state_id = response.twin_states[0]["state_id"]
        reads = [orch.twin.get_by_id(state_id) for _ in range(5)]
        assert all(r == reads[0] for r in reads)


# ===========================================================================
# K. Concurrency
# ===========================================================================

class TestConcurrency:

    def test_simultaneous_scenarios_do_not_cross_contaminate(self):
        """
        Three closures solved at once, each with a distinct hand-calculable
        cost. A crossover would show up as the wrong figure on a scenario.
        """
        orch = build_orchestrator(network=build_delhi_network(), enable_llm=False)
        expected = {"DC_DELHI": 1_600.0, "DC_MUMBAI": 1_700.0, "DC_KOLKATA": 1_400.0}

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(_run_scenario, orch, fid, f"conc_{fid}"): fid
                for fid in expected
            }
            results = {futures[f]: f.result() for f in
                       concurrent.futures.as_completed(futures)}

        for fid, response in results.items():
            view = orch.twin.get(response.network_snapshot_id, response.scenario_id)
            closed = {f.facility_id for f in view.facilities if not f.is_open}
            assert closed == {fid}, f"{fid} shows closures {closed}"
            assert view.kpis is not None
            assert view.kpis.business_network_cost == pytest.approx(expected[fid])

    def test_concurrent_reads_return_identical_state(self, orch):
        response = _run_state_query(orch)
        state_id = response.twin_states[0]["state_id"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            views = list(pool.map(
                lambda _: orch.twin.get_by_id(state_id), range(24),
            ))
        assert all(v == views[0] for v in views)

    def test_concurrent_writes_all_land(self):
        store = DigitalTwinStore()

        def publish(index: int):
            return store.put(_state(
                f"s{index}", state_type=TwinStateType.SCENARIO,
                scenario_id=f"scn_{index}",
            ))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            refs = list(pool.map(publish, range(40)))

        assert len(store) == 40
        assert len({r.state_id for r in refs}) == 40
        assert len(store.list_refs("snap_test")) == 40

    def test_concurrent_scenario_writes_do_not_disturb_the_baseline(self):
        service = DigitalTwinService()
        baseline = _state("base", facilities=[_facility("DC_A"), _facility("DC_B")])
        service.update(baseline)

        def publish(index: int):
            service.update(_state(
                f"scn_{index}", state_type=TwinStateType.SCENARIO,
                scenario_id=f"s{index}",
                facilities=[_facility("DC_A", is_open=False), _facility("DC_B")],
            ))

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(publish, range(30)))

        assert service.store.get("base") == baseline
        assert service.store.get("base").facility("DC_A").is_open is True

    def test_a_baseline_replaced_mid_publish_cannot_corrupt_a_delta(self):
        """
        Compressing is find-diff-store, and a baseline replacement landing
        between the find and the store would leave a delta diffed against one
        baseline and stored against another — a network that never existed.

        `_release_dependents` does not cover this: it fires when a base is
        replaced *after* a delta registers, and here the replacement lands
        first. The transaction does.

        Forced rather than raced. Threading this by hand and hoping never
        reproduced it — the window is a few bytecodes wide — so the interleaving
        is injected at the exact point it matters. A concurrency test that
        passes whether or not the lock is there is not a test.
        """
        import threading

        class InterleavingStore(DigitalTwinStore):
            """Lets another writer in immediately after the baseline lookup."""

            def __init__(self) -> None:
                super().__init__()
                self.intruder = None
                self._armed = True

            def baseline_for_internal(self, snapshot_id):
                found = super().baseline_for_internal(snapshot_id)
                if self._armed and found is not None:
                    self._armed = False
                    self.intruder = threading.Thread(
                        target=self.put, args=(replacement,), daemon=True,
                    )
                    self.intruder.start()
                    # Bounded: under the transaction the intruder is blocked on
                    # the lock and this times out, which is the correct outcome.
                    self.intruder.join(timeout=0.5)
                return found

        original = [_facility(f"DC_{i}", throughput=100.0) for i in range(6)]
        replacement_facilities = [
            _facility(f"DC_{i}", throughput=999.0) for i in range(6)
        ]
        replacement = _state("base", facilities=replacement_facilities)

        store = InterleavingStore()
        service = DigitalTwinService(store)
        service.update(_state("base", facilities=original))

        service.update(_state(
            "scn_0", state_type=TwinStateType.SCENARIO, scenario_id="s0",
            facilities=[_facility("DC_0", is_open=False, throughput=0.0),
                        *original[1:]],
        ))
        if store.intruder is not None:
            store.intruder.join(timeout=5.0)

        full = service.materialize("scn_0")

        # The scenario was diffed against the ORIGINAL baseline, so it must
        # still describe the original throughputs. Reading 999.0 here would mean
        # the delta had been merged onto the replacement.
        assert {f.facility_id for f in full.facilities if not f.is_open} == {"DC_0"}
        surviving = [f for f in full.facilities if f.facility_id != "DC_0"]
        assert len(surviving) == 5
        assert all(f.throughput_units == pytest.approx(100.0) for f in surviving), (
            "the delta was merged onto a baseline it was never diffed against"
        )

    def test_repeated_identical_requests_are_stable(self, orch):
        first = _run_scenario(orch, "DC_DELHI", request_id="stable")
        second = _run_scenario(orch, "DC_DELHI", request_id="stable")

        # Idempotency returns the original execution, so the state is unchanged.
        assert first.execution_id == second.execution_id
        assert len(orch.twin.list_scenarios(first.network_snapshot_id)) == 1


# ===========================================================================
# L. Delta round-trip, pagination and aggregation
# ===========================================================================

class TestDeltaRoundTrip:

    def test_a_delta_round_trip_restores_the_original_exactly(self):
        base = _state("base", facilities=[_facility(f"DC_{i}") for i in range(10)],
                      flows=[_flow("P", f"DC_{i}", 10.0) for i in range(10)])
        full = _state(
            "scn", state_type=TwinStateType.SCENARIO, scenario_id="s1",
            facilities=[
                _facility("DC_0", is_open=False, throughput=0.0),
                *[_facility(f"DC_{i}") for i in range(1, 10)],
            ],
            flows=[_flow("P", f"DC_{i}", 10.0) for i in range(1, 10)],
        )

        delta = to_delta(full, base)
        restored = apply_delta(delta, base)

        assert {f.facility_id for f in restored.facilities} == {
            f.facility_id for f in full.facilities
        }
        assert restored.facility("DC_0").is_open is False
        assert {f.lane_key for f in restored.flows} == {f.lane_key for f in full.flows}

    def test_a_delta_stores_only_what_changed(self):
        base = _state("base", facilities=[_facility(f"DC_{i}") for i in range(50)],
                      flows=[_flow("P", f"DC_{i}", 10.0) for i in range(50)])
        full = _state(
            "scn", state_type=TwinStateType.SCENARIO, scenario_id="s1",
            facilities=[_facility("DC_0", is_open=False),
                        *[_facility(f"DC_{i}") for i in range(1, 50)]],
            flows=[_flow("P", f"DC_{i}", 10.0) for i in range(50)],
        )
        delta = to_delta(full, base)

        assert len(delta.facilities) == 1
        assert delta.flows == []
        assert delta.storage_mode is StorageMode.DELTA

    def test_a_removed_lane_is_recorded_explicitly(self):
        """
        "This lane no longer carries flow" is invisible in a changed-entries
        list — it looks identical to "this lane was not mentioned".
        """
        base = _state("base", flows=[_flow("P", "DC_A", 10.0), _flow("P", "DC_B", 5.0)])
        full = _state("scn", state_type=TwinStateType.SCENARIO, scenario_id="s1",
                      flows=[_flow("P", "DC_A", 15.0)])

        delta = to_delta(full, base)
        assert delta.removed_lane_keys == ["P->DC_B"]

        restored = apply_delta(delta, base)
        assert {f.lane_key for f in restored.flows} == {"P->DC_A"}

    def test_a_scenario_changing_everything_is_no_larger_than_a_full_state(self):
        base = _state("base", facilities=[_facility(f"DC_{i}") for i in range(20)])
        full = _state(
            "scn", state_type=TwinStateType.SCENARIO, scenario_id="s1",
            facilities=[_facility(f"DC_{i}", is_open=False) for i in range(20)],
        )
        delta = to_delta(full, base)
        assert len(delta.facilities) == len(full.facilities)


class TestPaginationAndAggregation:

    def test_flows_are_paginated_by_default(self):
        service = DigitalTwinService()
        service.update(_state(
            "s", flows=[_flow("P", f"DC_{i}", float(i + 1)) for i in range(1_200)],
        ))
        view = service.get_by_id("s")

        assert len(view.flows.items) == 500
        assert view.flows.total == 1_200
        assert view.flows.has_more is True

    def test_a_page_can_be_requested_by_offset(self):
        service = DigitalTwinService()
        service.update(_state(
            "s", flows=[_flow("P", f"DC_{i:04d}", float(i + 1)) for i in range(100)],
        ))
        page = service.get_by_id("s", flow_offset=90, flow_limit=20).flows

        assert len(page.items) == 10
        assert page.offset == 90
        assert page.has_more is False

    def test_every_lane_can_be_requested(self):
        service = DigitalTwinService()
        service.update(_state(
            "s", flows=[_flow("P", f"DC_{i}", 1.0) for i in range(700)],
        ))
        view = service.get_by_id("s", flow_limit=0)
        assert len(view.flows.items) == 700

    def test_a_summary_view_can_skip_flows_entirely(self):
        """The cheap path: aggregate without paging through any lane."""
        service = DigitalTwinService()
        service.update(_state(
            "s", flows=[_flow("P", f"DC_{i}", 2.0) for i in range(500)],
        ))
        view = service.get_by_id("s", include_flows=False)

        assert view.flows.items == []
        assert view.flows.total == 500
        assert view.flow_aggregate is not None
        assert view.flow_aggregate.total_flow_units == pytest.approx(1_000.0)

    def test_the_aggregate_of_a_materialised_delta_describes_the_merged_network(self, orch):
        """
        A delta's stored aggregate covers its own lane subset. Materialising
        must roll the merged set up, not report the fragment.
        """
        response = _run_scenario(orch, "DC_DELHI")
        view = orch.twin.get(response.network_snapshot_id, response.scenario_id,
                             flow_limit=0)

        assert view.materialized_from_delta is True
        assert view.flow_aggregate is not None
        assert view.flow_aggregate.total_lanes == len(view.flows.items)
        assert view.flow_aggregate.total_flow_units == pytest.approx(
            sum(f.flow_units for f in view.flows.items), abs=1e-4,
        )
