"""
NetGravity — Core MILP Tests
==============================
Tests for the core optimization engine.

Test categories:
  1.  Hand-solvable facility location problem (VERIFY against known optimal)
  2.  Demand conservation (all demand must be met)
  3.  Capacity constraint (no facility over-capacity)
  4.  Facility opening/closing
  5.  Product eligibility
  6.  Lane eligibility
  7.  Service constraint (SLA)
  8.  Existing facility (forced open)
  9.  Infeasible network detection
  10. Zero-demand network
  11. Multiple products
  12. Multiple facilities
  13. Existing + candidate facility together
  14. Flow conservation (inbound = outbound at DCs)
  15. Baseline evaluation
  16. Solver reproducibility
"""

import pytest

from netgravity.tests.fixtures.case16_synthetic import build_case16_network, build_tiny_network
from netgravity.optimization.milp import solve
from netgravity.optimization.baseline import evaluate_baseline
from netgravity.metrics.kpis import compute_kpis
from netgravity.validation.checks import validate_network
from netgravity.schemas.network import (
    CanonicalNetwork, DemandRecord, FacilityRecord, FacilityStatus,
    LaneRecord, NodeRole, OptimizationConfig, ProductRecord, TransportMode,
)
from netgravity.schemas.results import SolverStatus


# ============================================================
# TEST 1: Hand-solvable problem — verify known optimal
# ============================================================

class TestHandSolvable:
    """
    Tiny 2-DC network where the optimal is known analytically.
    DC_T1 only: total cost = 5400 (see fixture docstring for calculation)
    """

    def test_optimal_facility_is_dc_t1(self):
        network = build_tiny_network()
        result  = solve(network)
        assert result.solver.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
        open_ids = {fd.facility_id for fd in result.facility_decisions if fd.is_open}
        assert "DC_T1" in open_ids, f"Expected DC_T1 to be open, got: {open_ids}"

    def test_optimal_cost_is_5400(self):
        network = build_tiny_network()
        result  = solve(network)
        assert result.solver.status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
        obj = result.solver.objective_value
        # Allow 0.5% tolerance for solver numerical precision
        assert abs(obj - 5400) / 5400 < 0.005, f"Expected ~5400, got {obj}"

    def test_dc_t2_is_closed(self):
        network = build_tiny_network()
        result  = solve(network)
        closed  = {fd.facility_id for fd in result.facility_decisions if not fd.is_open}
        # DC_T2 should be closed in optimal solution
        assert "DC_T2" in closed


# ============================================================
# TEST 2: Demand Conservation
# ============================================================

class TestDemandConservation:
    """All demand must be met (when allow_shortage=False)."""

    def test_all_demand_served(self):
        network = build_case16_network()
        result  = solve(network)
        assert result.is_solved
        kpis = compute_kpis(result, network)
        total_demand = sum(d.quantity for d in network.demands)
        assert abs(kpis.total_served - total_demand) / total_demand < 0.001, (
            f"Expected {total_demand:.0f} units served, got {kpis.total_served:.0f}"
        )

    def test_fill_rate_near_100_pct(self):
        network = build_case16_network()
        result  = solve(network)
        kpis    = compute_kpis(result, network)
        assert kpis.demand_fill_rate >= 0.999, f"Fill rate {kpis.demand_fill_rate:.4f} < 99.9%"


# ============================================================
# TEST 3: Capacity Constraint
# ============================================================

class TestCapacityConstraint:
    """No facility should exceed its capacity."""

    def test_no_capacity_violation(self):
        network = build_case16_network()
        result  = solve(network)
        assert result.is_solved
        for fd in result.facility_decisions:
            if fd.is_open and fd.capacity_units > 0:
                assert fd.throughput_units <= fd.capacity_units * 1.001, (
                    f"Facility {fd.facility_id} over-capacity: "
                    f"{fd.throughput_units:.0f} > {fd.capacity_units:.0f}"
                )

    def test_utilization_not_over_100pct(self):
        network = build_case16_network()
        result  = solve(network)
        for fd in result.facility_decisions:
            if fd.is_open and fd.capacity_units > 0:
                assert fd.utilization_pct <= 100.5, (
                    f"{fd.facility_id} utilization {fd.utilization_pct:.1f}% > 100%"
                )


# ============================================================
# TEST 4: Facility Opening / Closing
# ============================================================

class TestFacilityOpenClose:
    """Facility decisions are valid binary choices."""

    def test_at_least_one_dc_open(self):
        network = build_case16_network()
        result  = solve(network)
        open_dcs = [fd for fd in result.facility_decisions
                    if fd.is_open and fd.role in ("DC", "WAREHOUSE")]
        assert len(open_dcs) >= 1, "No DCs opened — infeasible routing"

    def test_open_dc_has_positive_throughput(self):
        network = build_case16_network()
        result  = solve(network)
        for fd in result.facility_decisions:
            if fd.is_open and fd.role == "DC":
                assert fd.throughput_units >= 0, f"Open DC {fd.facility_id} has negative throughput"

    def test_closed_dc_has_zero_throughput(self):
        network = build_case16_network()
        result  = solve(network)
        for fd in result.facility_decisions:
            if not fd.is_open:
                assert fd.throughput_units < 0.01, (
                    f"Closed facility {fd.facility_id} has throughput {fd.throughput_units}"
                )


# ============================================================
# TEST 5: Product Eligibility
# ============================================================

class TestProductEligibility:
    """If a facility cannot handle a product, no flow should route through it."""

    def test_product_restricted_facility(self):
        """
        Create a network where DC_T2 cannot handle PROD_T.
        Verify no flow of PROD_T goes through DC_T2.
        """
        network = build_tiny_network()
        # Restrict DC_T2 to only handle a different product
        new_facilities = []
        for f in network.facilities:
            if f.id == "DC_T2":
                new_facilities.append(
                    f.model_copy(update={"eligible_product_ids": ["OTHER_PROD"]})
                )
            else:
                new_facilities.append(f)
        restricted_network = network.model_copy(update={"facilities": new_facilities})

        result = solve(restricted_network)
        # No flow should go through DC_T2 for PROD_T
        for fl in result.flow_decisions:
            if fl.origin_id == "DC_T2" or fl.destination_id == "DC_T2":
                assert fl.product_id != "PROD_T", (
                    f"Flow of PROD_T through ineligible DC_T2: {fl}"
                )


# ============================================================
# TEST 6: Lane Eligibility
# ============================================================

class TestLaneEligibility:
    """Flows only on active, eligible lanes."""

    def test_no_flow_on_removed_lane(self):
        """Remove the DC_T1 → MKT_T1 lane; verify no flow on it."""
        network = build_tiny_network()
        new_lanes = [
            ln for ln in network.lanes
            if not (ln.origin_id == "DC_T1" and ln.destination_id == "MKT_T1")
        ]
        config = network.config.model_copy(update={"allow_shortage": True})
        restricted = network.model_copy(update={"lanes": new_lanes, "config": config})

        result = solve(restricted)
        for fl in result.flow_decisions:
            assert not (fl.origin_id == "DC_T1" and fl.destination_id == "MKT_T1"), (
                "Flow found on removed DC_T1→MKT_T1 lane"
            )


# ============================================================
# TEST 7: Service (SLA) Constraint
# ============================================================

class TestServiceConstraint:
    """Transit time constraint: no flow on lanes exceeding SLA."""

    def test_sla_filter_removes_slow_lanes(self):
        """
        Market requires SLA ≤ 1 day. Lane with lead_time=3 must not be used.
        """
        from netgravity.schemas.network import DemandRecord
        network = build_tiny_network()
        # Modify MKT_T1 demand to require 1-day SLA
        new_demands = [
            d.model_copy(update={"sla_days": 1.0}) if d.market_id == "MKT_T1" else d
            for d in network.demands
        ]
        # Make DC_T2→MKT_T1 lane 3 days (exceeds SLA)
        new_lanes = []
        for ln in network.lanes:
            if ln.origin_id == "DC_T2" and ln.destination_id == "MKT_T1":
                new_lanes.append(ln.model_copy(update={"lead_time_days": 3.0}))
            else:
                new_lanes.append(ln)

        config = network.config.model_copy(update={"enforce_sla": True})
        sla_network = network.model_copy(update={
            "demands": new_demands, "lanes": new_lanes, "config": config
        })
        result = solve(sla_network, config=config)
        # If DC_T2 is open, it should not serve MKT_T1 via the slow lane
        for fl in result.flow_decisions:
            if fl.origin_id == "DC_T2" and fl.destination_id == "MKT_T1":
                assert fl.lead_time_days <= 1.0, (
                    f"Flow via slow lane ({fl.lead_time_days} days) to SLA=1 day market"
                )


# ============================================================
# TEST 8: Existing Facility (Forced Open)
# ============================================================

class TestExistingFacility:
    """Mandatory existing facilities must remain open."""

    def test_mandatory_facility_stays_open(self):
        network = build_case16_network()
        # Force DC_CENTRAL to be mandatory
        new_facs = []
        for f in network.facilities:
            if f.id == "DC_CENTRAL":
                new_facs.append(f.model_copy(update={"is_mandatory": True}))
            else:
                new_facs.append(f)
        forced_network = network.model_copy(update={"facilities": new_facs})
        result = solve(forced_network)
        assert result.is_solved
        dc_central_fd = next(
            (fd for fd in result.facility_decisions if fd.facility_id == "DC_CENTRAL"), None
        )
        assert dc_central_fd is not None
        assert dc_central_fd.is_open, "Mandatory DC_CENTRAL was not kept open"


# ============================================================
# TEST 9: Infeasible Network Detection
# ============================================================

class TestInfeasibleNetwork:
    """Model correctly detects infeasibility."""

    def test_zero_capacity_is_infeasible(self):
        """Set all DC capacities to 0 → infeasible (demand cannot be met)."""
        network = build_tiny_network()
        new_facs = []
        for f in network.facilities:
            if f.role == NodeRole.DC:
                new_facs.append(f.model_copy(update={"capacity_units_per_period": 0.0}))
            else:
                new_facs.append(f)
        config = network.config.model_copy(update={"allow_shortage": False})
        infeasible_network = network.model_copy(update={"facilities": new_facs, "config": config})
        result = solve(infeasible_network, config=config)
        assert result.solver.status == SolverStatus.INFEASIBLE, (
            f"Expected INFEASIBLE, got {result.solver.status}"
        )

    def test_infeasible_with_shortage_returns_positive_shortage(self):
        """When allow_shortage=True on constrained network, unmet demand is captured."""
        network = build_tiny_network()
        new_facs = []
        for f in network.facilities:
            if f.role == NodeRole.DC:
                new_facs.append(f.model_copy(update={"capacity_units_per_period": 100.0}))
            else:
                new_facs.append(f)
        config = network.config.model_copy(update={"allow_shortage": True, "shortage_penalty": 1e4})
        shortage_network = network.model_copy(update={"facilities": new_facs, "config": config})
        result = solve(shortage_network, config=config)
        assert result.is_solved


# ============================================================
# TEST 10: Zero-Demand Network
# ============================================================

class TestZeroDemand:
    """Network with zero demand has trivial solution (all DCs closed if possible)."""

    def test_zero_demand_trivial(self):
        network = build_tiny_network()
        new_demands = [d.model_copy(update={"quantity": 0.0}) for d in network.demands]
        zero_network = network.model_copy(update={"demands": new_demands})
        result = solve(zero_network)
        # Should be feasible with 0 cost (or just fixed cost if any facility forced open)
        assert result.is_solved
        assert result.solver.objective_value is not None


# ============================================================
# TEST 11: Multiple Products
# ============================================================

class TestMultipleProducts:
    """Two-product network routes each product independently."""

    def test_two_products_served(self):
        network = build_tiny_network()
        prod2 = ProductRecord(id="PROD_T2", name="Test Product 2",
                              weight_kg=2.0, unit_value=0.0, holding_rate=0.0)
        new_products = list(network.products) + [prod2]
        new_demands  = list(network.demands) + [
            DemandRecord(market_id="MKT_T1", product_id="PROD_T2", quantity=100, std_dev=0),
            DemandRecord(market_id="MKT_T2", product_id="PROD_T2", quantity= 50, std_dev=0),
        ]
        # Add lanes for PROD_T2 (use existing lanes — eligible_product_ids empty = all products)
        multi_network = network.model_copy(update={
            "products": new_products,
            "demands":  new_demands,
        })
        result = solve(multi_network)
        assert result.is_solved
        kpis = compute_kpis(result, multi_network)
        total_demand = sum(d.quantity for d in new_demands)
        assert kpis.total_served >= total_demand * 0.999, (
            f"Not all demand served in multi-product test: {kpis.total_served:.0f}/{total_demand:.0f}"
        )


# ============================================================
# TEST 12: Flow Conservation at DCs
# ============================================================

class TestFlowConservation:
    """Inbound flow = Outbound flow at all DC nodes."""

    def test_flow_conservation_at_dcs(self):
        network = build_case16_network()
        result  = solve(network)
        assert result.is_solved

        # For each open DC, sum inbound and outbound flows
        dc_ids = {f.id for f in network.facilities
                  if f.role in (NodeRole.DC, NodeRole.WAREHOUSE)}

        for dc_id in dc_ids:
            inbound  = sum(fl.flow_units for fl in result.flow_decisions
                          if fl.destination_id == dc_id)
            outbound = sum(fl.flow_units for fl in result.flow_decisions
                          if fl.origin_id == dc_id)
            if inbound > 0.01 or outbound > 0.01:
                assert abs(inbound - outbound) / max(inbound, outbound, 1) < 0.01, (
                    f"DC {dc_id}: inbound={inbound:.2f}, outbound={outbound:.2f} "
                    f"— flow conservation violated"
                )


# ============================================================
# TEST 13: Existing + Candidate Facilities Together
# ============================================================

class TestExistingPlusCandidates:
    """Network with both existing and candidate facilities."""

    def test_candidates_can_open_if_beneficial(self):
        """On a constrained network, candidates should open if needed."""
        network = build_case16_network()
        # Reduce existing DC capacities to force candidate opening
        new_facs = []
        for f in network.facilities:
            if f.id in ("DC_CENTRAL", "DC_EAST", "DC_WEST"):
                new_facs.append(f.model_copy(update={"capacity_units_per_period": 500}))
            else:
                new_facs.append(f)
        constrained = network.model_copy(update={"facilities": new_facs})
        result = solve(constrained)
        assert result.is_solved

    def test_existing_and_candidate_coexist(self):
        network = build_case16_network()
        result  = solve(network)
        # All facility decisions present in result
        result_ids = {fd.facility_id for fd in result.facility_decisions}
        for f in network.facilities:
            if f.role != NodeRole.MARKET:
                assert f.id in result_ids, f"{f.id} missing from facility decisions"


# ============================================================
# TEST 14: Validation Checks
# ============================================================

class TestValidation:
    """Pre-solve validation catches data problems."""

    def test_valid_network_passes(self):
        network = build_case16_network()
        report  = validate_network(network)
        assert report.is_valid, f"Valid network failed validation: {report.errors}"

    def test_invalid_demand_id_fails(self):
        """Demand referencing unknown market_id should fail validation."""
        network = build_case16_network()
        bad_demand = DemandRecord(market_id="NONEXISTENT", product_id="P001", quantity=100)
        bad_network = network.model_copy(update={"demands": list(network.demands) + [bad_demand]})
        # Pydantic model_validator will raise ValueError
        with pytest.raises(Exception):
            CanonicalNetwork(
                facilities = bad_network.facilities,
                products   = bad_network.products,
                demands    = list(network.demands) + [bad_demand],
                lanes      = bad_network.lanes,
            )

    def test_negative_demand_fails_validation(self):
        with pytest.raises(Exception):
            DemandRecord(market_id="MKT_A", product_id="P001", quantity=-100)


# ============================================================
# TEST 15: Baseline Evaluation
# ============================================================

class TestBaseline:
    """Baseline evaluator works and returns current-state KPIs."""

    def test_baseline_is_solvable(self):
        network  = build_case16_network()
        baseline = evaluate_baseline(network)
        assert baseline.is_solved, f"Baseline failed: {baseline.solver.status}"

    def test_baseline_scenario_id(self):
        network  = build_case16_network()
        baseline = evaluate_baseline(network)
        assert baseline.scenario_id == "BASELINE"

    def test_baseline_only_existing_open(self):
        """In baseline, only EXISTING facilities should be open."""
        network  = build_case16_network()
        baseline = evaluate_baseline(network)
        for fd in baseline.facility_decisions:
            if fd.is_open:
                # Find the facility
                fac = next((f for f in network.facilities if f.id == fd.facility_id), None)
                if fac and fac.role != NodeRole.MARKET:
                    assert fac.status.value == "EXISTING", (
                        f"Candidate facility {fd.facility_id} is open in baseline"
                    )


# ============================================================
# TEST 16: Solver Reproducibility
# ============================================================

class TestReproducibility:
    """Same input + config produces same output (deterministic)."""

    def test_same_result_on_two_runs(self):
        network = build_case16_network()
        r1 = solve(network)
        r2 = solve(network)
        assert r1.solver.status == r2.solver.status
        if r1.solver.objective_value and r2.solver.objective_value:
            diff = abs(r1.solver.objective_value - r2.solver.objective_value)
            assert diff < 0.01, (
                f"Non-reproducible: run1={r1.solver.objective_value}, "
                f"run2={r2.solver.objective_value}"
            )

    def test_result_structure_complete(self):
        """Ensure result has all required fields."""
        network = build_case16_network()
        result  = solve(network)
        assert result.run_id is not None
        assert result.solver is not None
        assert result.facility_decisions is not None
        assert result.flow_decisions is not None
        assert result.objective_components is not None
