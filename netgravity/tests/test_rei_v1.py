"""
NetGravity — REI V1 Test Suite
===============================

Covers the frozen REI V1 scope: deterministic, scenario-based economic exposure
of the network to FACILITY failure, computed by re-optimising with the existing
MILP.

Sections map to the V1 acceptance list:

    A  baseline                     G  solver failure isolation
    B  single facility disruption   H  baseline immutability
    C  multiple facilities          I  snapshot consistency
    D  zero impact                  J  idempotency
    E  negative incremental cost    K  material-change invalidation
    F  infeasible disruption        L  normalisation bounds
                                    M  regression (existing suites)

Hand-checkable networks are used wherever an exact number is asserted, so REI
values are verified against arithmetic rather than against the engine's own
output.
"""

from __future__ import annotations

import time
from typing import List, Optional

import pytest

from netgravity.resilience.fingerprint import (
    compute_material_fingerprint,
    material_config_view,
    networks_are_materially_equal,
)
from netgravity.resilience.registry_store import (
    REICacheKey,
    REIRegistryStore,
    disruption_signature,
)
from netgravity.resilience.rei import (
    BaselineSolveError,
    NoEligibleFacilitiesError,
    assess_network_resilience,
    compute_baseline,
    discover_eligible_facilities,
    economic_impact_of,
    normalize_rei,
)
from netgravity.resilience.service import REIService
from netgravity.schemas.network import (
    CanonicalNetwork,
    CostPeriod,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    OptimizationConfig,
    ProductRecord,
    TransportMode,
)
from netgravity.schemas.resilience import DisruptionConfig
from netgravity.schemas.results import (
    CalculationStatus,
    REIBatchStatus,
    REIStatus,
    SolverStatus,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network

TOL = 1e-6


# ---------------------------------------------------------------------------
# Hand-checkable fixtures
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> OptimizationConfig:
    """Inventory and SLA off so hand arithmetic is exact."""
    base = dict(
        solver_name="HiGHS", enable_inventory=False, enforce_sla=False,
        enable_carbon_cost=False, minimum_throughput_enabled=False,
        allow_shortage=False, cost_period=CostPeriod.MONTH, mip_gap=0.0,
        verbose=False,
    )
    base.update(overrides)
    return OptimizationConfig(**base)


def build_three_dc_network(
    hub_rate: float = 2.0,
    mid_rate: float = 4.0,
    red_rate: float = 12.0,
    config: Optional[OptimizationConfig] = None,
) -> CanonicalNetwork:
    """
    PLANT → {DC_HUB, DC_MID, DC_RED} → MKT (demand 100), full redundancy.

        inbound  = 1.0/unit on every lane
        DC_HUB → MKT = hub_rate (cheapest, so it serves at baseline)
        DC_MID → MKT = mid_rate
        DC_RED → MKT = red_rate
        fixed cost   = 1,200/yr = 100/month per DC

    Only the serving DC's fixed cost is incurred (the others close, and closure
    cost is 0 here), so with hub_rate < mid_rate < red_rate:

        baseline        = 100 + 100×(1 + hub_rate)
        DC_HUB disrupted= 100 + 100×(1 + mid_rate)   → next cheapest takes over
        DC_MID disrupted= baseline                    (unused at baseline → ΔC = 0)
        DC_RED disrupted= baseline                    (unused at baseline → ΔC = 0)
    """
    facilities = [
        FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING, capacity_units_per_period=9999,
                       is_mandatory=True, is_closable=False, fixed_cost_per_year=0.0),
        FacilityRecord(id="DC_HUB", name="Hub", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
                       fixed_cost_per_year=1200.0),
        FacilityRecord(id="DC_MID", name="Mid", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
                       fixed_cost_per_year=1200.0),
        FacilityRecord(id="DC_RED", name="Red", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
                       fixed_cost_per_year=1200.0),
        FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
    ]
    lanes = []
    for dc, rate in (("DC_HUB", hub_rate), ("DC_MID", mid_rate), ("DC_RED", red_rate)):
        lanes.append(LaneRecord(origin_id="PLANT", destination_id=dc,
                                mode=TransportMode.ROAD, rate_per_unit=1.0,
                                distance_km=100.0, lead_time_days=1.0))
        lanes.append(LaneRecord(origin_id=dc, destination_id="MKT",
                                mode=TransportMode.ROAD, rate_per_unit=rate,
                                distance_km=50.0, lead_time_days=1.0))
    net = CanonicalNetwork(
        network_id="THREE_DC",
        facilities=facilities,
        products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=100.0)],
        demands=[DemandRecord(market_id="MKT", product_id="P1",
                              quantity=100.0, std_dev=0.0)],
        lanes=lanes,
        config=config or _cfg(),
    )
    return net.model_copy(update={"data_version": net.compute_data_version()})


def build_single_source_network(config: Optional[OptimizationConfig] = None) -> CanonicalNetwork:
    """MKT reachable ONLY through DC_ONLY — its loss makes the network infeasible."""
    facilities = [
        FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                       status=FacilityStatus.EXISTING, capacity_units_per_period=9999,
                       is_mandatory=True, is_closable=False),
        FacilityRecord(id="DC_ONLY", name="Only DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
                       fixed_cost_per_year=1200.0),
        FacilityRecord(id="DC_SPARE", name="Spare DC", role=NodeRole.DC,
                       status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
                       fixed_cost_per_year=1200.0),
        FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
        FacilityRecord(id="MKT_SPARE", name="Spare Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
    ]
    lanes = [
        LaneRecord(origin_id="PLANT", destination_id="DC_ONLY", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        LaneRecord(origin_id="PLANT", destination_id="DC_SPARE", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_ONLY", destination_id="MKT", mode=TransportMode.ROAD,
                   rate_per_unit=2.0, distance_km=50.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_SPARE", destination_id="MKT_SPARE",
                   mode=TransportMode.ROAD, rate_per_unit=2.0,
                   distance_km=50.0, lead_time_days=1.0),
        # Second source for MKT_SPARE only. This makes DC_SPARE's loss
        # ABSORBABLE while DC_ONLY's stays fatal — so one node in the batch is
        # infeasible and the other still produces a valid REI.
        LaneRecord(origin_id="DC_ONLY", destination_id="MKT_SPARE",
                   mode=TransportMode.ROAD, rate_per_unit=6.0,
                   distance_km=400.0, lead_time_days=1.0),
    ]
    net = CanonicalNetwork(
        network_id="SINGLE_SOURCE",
        facilities=facilities,
        products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=100.0)],
        demands=[
            DemandRecord(market_id="MKT", product_id="P1", quantity=100.0, std_dev=0.0),
            DemandRecord(market_id="MKT_SPARE", product_id="P1",
                         quantity=50.0, std_dev=0.0),
        ],
        lanes=lanes,
        config=config or _cfg(allow_shortage=False),
    )
    return net.model_copy(update={"data_version": net.compute_data_version()})


#: Assess only the DCs the baseline actually opens (the production default).
DC_ONLY = DisruptionConfig(eligible_roles=[NodeRole.DC])

#: Assess EVERY DC, including ones the baseline leaves closed. Used where a test
#: needs a specific node in the batch regardless of whether the optimum uses it.
DC_ALL = DisruptionConfig(eligible_roles=[NodeRole.DC],
                          only_baseline_open_facilities=False)


# ===========================================================================
# A — Baseline
# ===========================================================================

class TestBaseline:

    def test_baseline_solves_and_captures_cost(self):
        net = build_three_dc_network()
        baseline = compute_baseline(net, net.config, DC_ONLY)

        assert baseline.result.is_solved
        assert baseline.result.solver.status == SolverStatus.OPTIMAL
        # 100 fixed + 100 × (1 inbound + 2 outbound) = 400
        assert baseline.cost == pytest.approx(400.0, abs=1e-3)

    def test_baseline_carries_snapshot_and_model_identity(self):
        net = build_three_dc_network()
        baseline = compute_baseline(net, net.config, DC_ONLY, snapshot_id="snap_x")

        assert baseline.snapshot_id == "snap_x"
        assert baseline.material_fingerprint.startswith("fp")
        assert baseline.model_version == net.config.model_version

    def test_infeasible_baseline_raises_rather_than_returning_zero(self):
        net = build_single_source_network()
        lanes = [ln for ln in net.lanes if ln.destination_id != "MKT"]
        broken = net.model_copy(update={"lanes": lanes})
        with pytest.raises(BaselineSolveError):
            compute_baseline(broken, broken.config, DC_ONLY)


# ===========================================================================
# B — Single facility disruption (hand-verified)
# ===========================================================================

class TestSingleFacilityDisruption:

    def test_incremental_cost_and_rei_are_hand_verifiable(self):
        net = build_three_dc_network(hub_rate=2.0, mid_rate=4.0, red_rate=12.0)
        reg = assess_network_resilience(net, net.config, DC_ONLY)

        hub = reg.get("DC_HUB")
        assert hub is not None

        # baseline        = 100 + 100×(1+2) = 400
        # DC_HUB removed  = 100 + 100×(1+4) = 600
        # ΔC = 200 ; ΔC%  = 50%
        assert hub.baseline_business_cost == pytest.approx(400.0, abs=1e-3)
        assert hub.disrupted_business_cost == pytest.approx(600.0, abs=1e-3)
        assert hub.performance_impact == pytest.approx(200.0, abs=1e-3)
        assert hub.economic_impact == pytest.approx(200.0, abs=1e-3)
        assert hub.cost_impact_pct == pytest.approx(50.0, abs=1e-3)
        # Largest positive impact in the batch.
        assert hub.rei == pytest.approx(1.0, abs=TOL)
        assert hub.rank == 1

    def test_disrupted_facility_carries_no_flow(self):
        net = build_three_dc_network()
        reg = assess_network_resilience(net, net.config, DC_ONLY)
        hub = reg.get("DC_HUB")
        # The re-optimised network genuinely rerouted rather than reporting a
        # phantom cost against unchanged flows.
        assert hub.rerouted_volume is not None and hub.rerouted_volume > 0
        assert hub.unserved_demand == pytest.approx(0.0, abs=1e-6)

    def test_row_carries_full_provenance(self):
        net = build_three_dc_network()
        reg = assess_network_resilience(net, net.config, DC_ONLY,
                                        batch_id="b1", snapshot_id="snap_a")
        for row in reg.results:
            assert row.batch_id == "b1"
            assert row.network_snapshot_id == "snap_a"
            assert row.model_version == net.config.model_version
            assert row.calculation_timestamp
            assert row.disruption_type == "FACILITY_FAILURE"
            assert row.scenario_id and row.facility_id in row.scenario_id


# ===========================================================================
# C — Multiple facilities, baseline reuse
# ===========================================================================

class TestMultipleFacilities:

    def test_every_eligible_facility_processed_exactly_once(self):
        net = build_three_dc_network()
        reg = assess_network_resilience(net, net.config, DC_ALL)

        ids = [r.facility_id for r in reg.results]
        assert sorted(ids) == ["DC_HUB", "DC_MID", "DC_RED"]
        assert len(ids) == len(set(ids)), "no facility may be assessed twice"

    def test_baseline_solved_once_and_reused(self):
        """1 + N solves, never 2N."""
        net = build_three_dc_network()
        calls: List[str] = []

        def counting_solve(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            calls.append(scenario_id or "?")
            return solve(network, config=config, scenario_id=scenario_id)

        reg = assess_network_resilience(net, net.config, DC_ALL, solve_fn=counting_solve)

        baseline_calls = [c for c in calls if c == "REI_BASELINE"]
        assert len(baseline_calls) == 1, f"baseline solved {len(baseline_calls)} times"
        assert len(calls) == 1 + reg.n_facilities_assessed
        assert reg.n_milp_solves == len(calls)

    def test_all_rows_share_one_baseline_cost(self):
        net = build_three_dc_network()
        reg = assess_network_resilience(net, net.config, DC_ALL)
        assert len({r.baseline_business_cost for r in reg.results}) == 1

    def test_eligibility_excludes_markets_and_closed_facilities(self):
        net = build_three_dc_network()
        facs = [
            f.model_copy(update={"status": FacilityStatus.CLOSED})
            if f.id == "DC_RED" else f for f in net.facilities
        ]
        net = net.model_copy(update={"facilities": facs})

        baseline = compute_baseline(net, net.config, DisruptionConfig())
        eligible = {f.id for f in discover_eligible_facilities(
            net, DisruptionConfig(only_baseline_open_facilities=False), baseline.result)}

        assert "MKT" not in eligible, "market nodes are demand, not capacity"
        assert "DC_RED" not in eligible, "already-closed facilities are not eligible"


# ===========================================================================
# D — Zero impact
# ===========================================================================

class TestZeroImpact:

    def test_all_zero_impact_gives_all_zero_rei_without_dividing(self):
        reis, max_impact, status = normalize_rei([0.0, 0.0, 0.0])
        assert status == REIStatus.NO_RELATIVE_COST_EXPOSURE
        assert reis == [0.0, 0.0, 0.0]
        assert max_impact == pytest.approx(0.0, abs=TOL)

    def test_unused_facility_has_zero_impact_end_to_end(self):
        """A DC the baseline does not use costs nothing to lose."""
        net = build_three_dc_network()
        reg = assess_network_resilience(
            net, net.config,
            DisruptionConfig(eligible_roles=[NodeRole.DC],
                             only_baseline_open_facilities=False),
        )
        for fid in ("DC_MID", "DC_RED"):
            row = reg.get(fid)
            assert row.performance_impact == pytest.approx(0.0, abs=1e-3)
            assert row.economic_impact == pytest.approx(0.0, abs=1e-3)
            assert row.rei == pytest.approx(0.0, abs=TOL)


# ===========================================================================
# E — Negative incremental cost
# ===========================================================================

class TestNegativeIncrementalCost:

    def test_negative_impact_floors_to_zero_exposure_but_pi_survives(self):
        assert economic_impact_of(-5000.0) == pytest.approx(0.0)
        assert economic_impact_of(0.0) == pytest.approx(0.0)
        assert economic_impact_of(250.0) == pytest.approx(250.0)

        reis, max_impact, status = normalize_rei([100.0, -40.0, 50.0])
        assert status == REIStatus.COMPUTED
        assert reis == [pytest.approx(1.0), pytest.approx(0.0), pytest.approx(0.5)]
        assert max_impact == pytest.approx(100.0)

    def test_no_artificial_positive_rei_is_manufactured(self):
        """A cost-reducing disruption must not appear as exposure."""
        net = build_case16_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig())
        negatives = [r for r in reg.results
                     if r.performance_impact is not None and r.performance_impact < 0]
        assert negatives, "Case-16 is expected to contain a cost-reducing disruption"
        for row in negatives:
            assert row.rei == pytest.approx(0.0, abs=TOL)
            assert row.economic_impact == pytest.approx(0.0, abs=TOL)
            # ...but the raw signed value is retained for investigation.
            assert row.performance_impact < 0


# ===========================================================================
# F — Infeasible disruption
# ===========================================================================

class TestInfeasibleDisruption:

    def test_infeasible_node_reports_null_rei_and_batch_continues(self):
        net = build_single_source_network()
        reg = assess_network_resilience(net, net.config, DC_ONLY)

        only = reg.get("DC_ONLY")
        spare = reg.get("DC_SPARE")
        assert only is not None and spare is not None

        # The unabsorbable node.
        assert only.solver_status == SolverStatus.INFEASIBLE
        assert only.is_feasible is False
        assert only.rei is None, "no REI may be invented for an infeasible disruption"
        assert only.performance_impact is None
        assert only.disrupted_business_cost is None
        assert only.calculation_status == CalculationStatus.INFEASIBLE

        # The batch still produced a usable result for the other node.
        assert spare.calculation_status == CalculationStatus.OK
        assert spare.performance_impact is not None

        assert reg.batch_status == REIBatchStatus.COMPLETED_WITH_ERRORS
        assert reg.n_successful >= 1
        assert reg.n_infeasible >= 1

    def test_infeasible_node_is_not_assigned_rei_one(self):
        net = build_single_source_network()
        reg = assess_network_resilience(net, net.config, DC_ONLY)
        assert reg.get("DC_ONLY").rei is None


# ===========================================================================
# G — Solver failure isolation
# ===========================================================================

class TestFailureIsolation:

    def test_one_failing_scenario_does_not_destroy_the_batch(self):
        net = build_three_dc_network()

        def flaky_solve(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            if scenario_id == "REI_DISRUPT_DC_MID":
                raise RuntimeError("simulated solver crash")
            return solve(network, config=config, scenario_id=scenario_id)

        reg = assess_network_resilience(net, net.config, DC_ALL, solve_fn=flaky_solve)

        failed = reg.get("DC_MID")
        assert failed.calculation_status == CalculationStatus.ERROR
        assert failed.solver_status == SolverStatus.ERROR
        assert failed.rei is None
        assert "simulated solver crash" in (failed.failure_reason or "")

        # Every other node still produced a valid result.
        for fid in ("DC_HUB", "DC_RED"):
            row = reg.get(fid)
            assert row.calculation_status == CalculationStatus.OK
            assert row.rei is not None

        assert reg.batch_status == REIBatchStatus.COMPLETED_WITH_ERRORS
        assert reg.n_failed == 1
        assert reg.n_successful == 2

    def test_total_failure_reports_failed_batch(self):
        net = build_three_dc_network()

        def always_fail(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            if scenario_id == "REI_BASELINE":
                return solve(network, config=config, scenario_id=scenario_id)
            raise RuntimeError("all disruptions fail")

        reg = assess_network_resilience(net, net.config, DC_ALL, solve_fn=always_fail)
        assert reg.batch_status == REIBatchStatus.FAILED
        assert reg.n_successful == 0

    def test_no_eligible_facilities_raises(self):
        net = build_three_dc_network()
        with pytest.raises(NoEligibleFacilitiesError):
            assess_network_resilience(
                net, net.config,
                DisruptionConfig(exclude_facility_ids=["PLANT", "DC_HUB",
                                                       "DC_MID", "DC_RED"]),
            )


# ===========================================================================
# H — Baseline immutability
# ===========================================================================

class TestBaselineImmutability:

    def test_batch_does_not_mutate_the_network(self):
        net = build_three_dc_network()
        before = net.model_dump_json()
        assess_network_resilience(net, net.config, DC_ONLY)
        assert net.model_dump_json() == before, "REI batch mutated the baseline network"

    def test_material_fingerprint_unchanged_by_a_batch(self):
        net = build_three_dc_network()
        before = compute_material_fingerprint(net)
        assess_network_resilience(net, net.config, DC_ONLY)
        assert compute_material_fingerprint(net) == before

    def test_facility_flags_survive_disruption(self):
        net = build_three_dc_network()
        assess_network_resilience(net, net.config, DC_ONLY)
        for fac in net.facilities:
            assert fac.is_forced_closed is False
            assert fac.is_disruption_target is False
        assert net.get_facility("DC_HUB").capacity_units_per_period == 1000

    def test_case16_baseline_immutable(self):
        net = build_case16_network()
        before = net.model_dump_json()
        assess_network_resilience(net, net.config, DisruptionConfig())
        assert net.model_dump_json() == before


# ===========================================================================
# I — Snapshot consistency
# ===========================================================================

class TestSnapshotConsistency:

    def test_fingerprint_is_deterministic(self):
        a = build_three_dc_network()
        b = build_three_dc_network()
        assert compute_material_fingerprint(a) == compute_material_fingerprint(b)

    def test_rei_cannot_be_reused_across_different_snapshots(self):
        service = REIService()
        net_a = build_three_dc_network()
        service.get_or_compute(net_a, net_a.config, DC_ONLY)

        # A materially different network must not hit the cache.
        net_b = build_three_dc_network()
        facs = [f.model_copy(update={"capacity_units_per_period": 555.0})
                if f.id == "DC_HUB" else f for f in net_b.facilities]
        net_b = net_b.model_copy(update={"facilities": facs})

        assert not service.is_valid_for(net_b, net_b.config, DC_ONLY)
        reg_b = service.get_or_compute(net_b, net_b.config, DC_ONLY)
        assert reg_b.served_from_cache is False

    def test_registry_records_its_snapshot(self):
        net = build_three_dc_network()
        reg = assess_network_resilience(net, net.config, DC_ONLY, snapshot_id="snap_42")
        assert reg.network_snapshot_id == "snap_42"
        assert reg.material_fingerprint == compute_material_fingerprint(net)
        assert all(r.network_snapshot_id == "snap_42" for r in reg.results)


# ===========================================================================
# J — Idempotency
# ===========================================================================

class TestIdempotency:

    def test_identical_request_is_served_from_cache(self):
        service = REIService()
        net = build_three_dc_network()

        first = service.get_or_compute(net, net.config, DC_ONLY)
        assert first.served_from_cache is False
        assert first.n_milp_solves == 1 + first.n_facilities_assessed

        second = service.get_or_compute(net, net.config, DC_ONLY)
        assert second.served_from_cache is True
        assert second.batch_id == first.batch_id, "same calculation, same batch"

        assert service.stats.batches_computed == 1
        assert service.stats.batches_served_from_cache == 1

    def test_cached_batch_executes_no_solves(self):
        service = REIService()
        net = build_three_dc_network()
        service.get_or_compute(net, net.config, DC_ONLY)

        calls: List[str] = []

        def counting_solve(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            calls.append(scenario_id or "?")
            return solve(network, config=config, scenario_id=scenario_id)

        service.solve_fn = counting_solve
        service.get_or_compute(net, net.config, DC_ONLY)
        assert calls == [], "a cache hit must not invoke the solver"

    def test_force_recompute_bypasses_cache(self):
        service = REIService()
        net = build_three_dc_network()
        first = service.get_or_compute(net, net.config, DC_ONLY)
        forced = service.get_or_compute(net, net.config, DC_ONLY, force_recompute=True)

        assert forced.served_from_cache is False
        assert forced.batch_id != first.batch_id

    def test_different_assumptions_do_not_share_a_cache_entry(self):
        """REI is only comparable within one set of assumptions."""
        service = REIService()
        net = build_three_dc_network()
        service.get_or_compute(net, net.config, DisruptionConfig(allow_shortage=False))
        second = service.get_or_compute(net, net.config,
                                        DisruptionConfig(allow_shortage=True))
        assert second.served_from_cache is False

    def test_cached_batch_cannot_be_mutated_through_the_caller(self):
        service = REIService()
        net = build_three_dc_network()
        service.get_or_compute(net, net.config, DC_ONLY)

        served = service.get_or_compute(net, net.config, DC_ONLY)
        served.results[0].rei = 0.123

        again = service.get_or_compute(net, net.config, DC_ONLY)
        assert again.results[0].rei != 0.123, "stored batch was mutated by a consumer"


# ===========================================================================
# K — Material-change invalidation
# ===========================================================================

class TestMaterialChangeInvalidation:

    @staticmethod
    def _mutate_facility(net: CanonicalNetwork, fid: str, **updates) -> CanonicalNetwork:
        facs = [f.model_copy(update=updates) if f.id == fid else f
                for f in net.facilities]
        return net.model_copy(update={"facilities": facs})

    def test_demand_change_invalidates(self):
        net = build_three_dc_network()
        changed = net.model_copy(update={
            "demands": [d.model_copy(update={"quantity": d.quantity * 2})
                        for d in net.demands]})
        assert not networks_are_materially_equal(net, changed)

    @pytest.mark.parametrize("updates", [
        {"capacity_units_per_period": 42.0},
        {"fixed_cost_per_year": 99_999.0},
        {"handling_cost_per_unit": 5.0},
        {"closure_cost": 1234.0},
        {"is_forced_closed": True},
        {"status": FacilityStatus.CLOSED},
        {"min_throughput_per_period": 10.0},
    ])
    def test_material_facility_changes_invalidate(self, updates):
        net = build_three_dc_network()
        changed = self._mutate_facility(net, "DC_HUB", **updates)
        assert not networks_are_materially_equal(net, changed), (
            f"{updates} must invalidate REI"
        )

    def test_topology_change_invalidates(self):
        net = build_three_dc_network()
        fewer = net.model_copy(update={
            "lanes": [ln for ln in net.lanes
                      if not (ln.origin_id == "DC_RED" and ln.destination_id == "MKT")]})
        assert not networks_are_materially_equal(net, fewer)

    def test_transport_cost_change_invalidates(self):
        net = build_three_dc_network()
        dearer = net.model_copy(update={
            "lanes": [ln.model_copy(update={"rate_per_unit": ln.rate_per_unit + 1})
                      for ln in net.lanes]})
        assert not networks_are_materially_equal(net, dearer)

    def test_sla_change_invalidates(self):
        net = build_three_dc_network()
        with_sla = net.model_copy(update={
            "demands": [d.model_copy(update={"sla_days": 1.0}) for d in net.demands]})
        assert not networks_are_materially_equal(net, with_sla)

    def test_material_config_change_invalidates(self):
        net = build_three_dc_network()
        assert compute_material_fingerprint(net, _cfg()) != compute_material_fingerprint(
            net, _cfg(enable_inventory=True))

    @pytest.mark.parametrize("updates", [
        {"name": "Renamed Hub"},
        {"region": "North"},
        {"country": "UK"},
        {"tags": ["primary", "audited"]},
    ])
    def test_descriptive_metadata_does_not_invalidate(self, updates):
        """Renaming a warehouse must not force 1 + N solves."""
        net = build_three_dc_network()
        renamed = self._mutate_facility(net, "DC_HUB", **updates)
        assert networks_are_materially_equal(net, renamed), (
            f"{updates} must NOT invalidate REI"
        )

    def test_solver_tuning_does_not_invalidate(self):
        """Raising a time limit does not change the optimum."""
        net = build_three_dc_network()
        a = compute_material_fingerprint(net, _cfg(time_limit_seconds=60, verbose=False))
        b = compute_material_fingerprint(net, _cfg(time_limit_seconds=600, verbose=True))
        assert a == b

    def test_network_description_does_not_invalidate(self):
        net = build_three_dc_network()
        described = net.model_copy(update={"description": "a lovely network"})
        assert networks_are_materially_equal(net, described)

    def test_renamed_network_serves_from_cache(self):
        """End-to-end: a cosmetic edit reuses the stored batch."""
        service = REIService()
        net = build_three_dc_network()
        service.get_or_compute(net, net.config, DC_ONLY)

        renamed = self._mutate_facility(net, "DC_HUB", name="Hub (renamed)")
        served = service.get_or_compute(renamed, renamed.config, DC_ONLY)
        assert served.served_from_cache is True

    def test_capacity_change_forces_recompute(self):
        service = REIService()
        net = build_three_dc_network()
        service.get_or_compute(net, net.config, DC_ONLY)

        changed = self._mutate_facility(net, "DC_HUB", capacity_units_per_period=500.0)
        recomputed = service.get_or_compute(changed, changed.config, DC_ONLY)
        assert recomputed.served_from_cache is False

    def test_explicit_invalidation(self):
        service = REIService()
        net = build_three_dc_network()
        service.get_or_compute(net, net.config, DC_ONLY)
        assert service.is_valid_for(net, net.config, DC_ONLY)

        assert service.invalidate_for(net, "operator forced refresh") >= 1
        assert not service.is_valid_for(net, net.config, DC_ONLY)
        assert service.get_or_compute(net, net.config, DC_ONLY).served_from_cache is False


# ===========================================================================
# L — Normalisation bounds
# ===========================================================================

class TestNormalisation:

    def test_bounds_and_maximum(self):
        reis, max_impact, status = normalize_rei([100.0, 50.0, 25.0])
        assert status == REIStatus.COMPUTED
        assert reis == [pytest.approx(1.0), pytest.approx(0.5), pytest.approx(0.25)]
        assert max_impact == pytest.approx(100.0)

    def test_every_rei_is_within_unit_interval(self):
        """Required for future RF = P + REI − P·REI."""
        for net in (build_three_dc_network(), build_case16_network()):
            reg = assess_network_resilience(net, net.config, DisruptionConfig())
            for row in reg.results:
                if row.rei is not None:
                    assert 0.0 <= row.rei <= 1.0, (
                        f"{row.facility_id} REI {row.rei} outside [0, 1]"
                    )

    def test_highest_positive_exposure_receives_one(self):
        net = build_three_dc_network()
        reg = assess_network_resilience(net, net.config, DC_ONLY)
        if reg.rei_status == REIStatus.COMPUTED:
            top = max((r for r in reg.results if r.economic_impact is not None),
                      key=lambda r: r.economic_impact)
            assert top.rei == pytest.approx(1.0, abs=TOL)

    def test_unassessed_nodes_have_null_rei_not_zero(self):
        reis, _, _ = normalize_rei([100.0, None])
        assert reis[1] is None, "unassessed must be null, not a zero exposure claim"


# ===========================================================================
# Store & key behaviour
# ===========================================================================

class TestRegistryStore:

    @staticmethod
    def _key(fp="fp1_a", mv="1.2.0", dt="FACILITY_FAILURE", sig="s1") -> REICacheKey:
        return REICacheKey(material_fingerprint=fp, model_version=mv,
                           disruption_type=dt, disruption_signature=sig)

    def test_put_get_roundtrip(self):
        store = REIRegistryStore()
        net = build_three_dc_network()
        reg = assess_network_resilience(net, net.config, DC_ONLY)
        key = self._key()

        assert store.get(key) is None
        store.put(key, reg)
        assert store.get(key).batch_id == reg.batch_id
        assert store.stats()["hits"] == 1

    def test_invalidated_entry_is_not_served(self):
        store = REIRegistryStore()
        net = build_three_dc_network()
        key = self._key()
        store.put(key, assess_network_resilience(net, net.config, DC_ONLY))

        assert store.invalidate(key, "demand changed") is True
        assert store.get(key) is None
        assert store.stats()["stale_entries"] == 1

    def test_model_version_is_part_of_identity(self):
        assert self._key(mv="1.2.0") != self._key(mv="2.0.0")

    def test_lru_eviction_is_bounded(self):
        store = REIRegistryStore(max_entries=2)
        net = build_three_dc_network()
        reg = assess_network_resilience(net, net.config, DC_ONLY)
        for i in range(4):
            store.put(self._key(fp=f"fp1_{i}"), reg)
        assert len(store) == 2

    def test_disruption_signature_distinguishes_assumptions(self):
        a = disruption_signature(DisruptionConfig(allow_shortage=True))
        b = disruption_signature(DisruptionConfig(allow_shortage=False))
        assert a != b
        assert disruption_signature(DisruptionConfig()) == disruption_signature(
            DisruptionConfig())


# ===========================================================================
# Parallel execution equivalence
# ===========================================================================

class TestParallelExecution:

    def test_parallel_batch_matches_sequential_exactly(self):
        net = build_case16_network()
        seq = assess_network_resilience(net, net.config, DisruptionConfig(), max_workers=1)
        par = assess_network_resilience(net, net.config, DisruptionConfig(), max_workers=4)

        assert [r.facility_id for r in seq.results] == [r.facility_id for r in par.results]
        for a, b in zip(seq.results, par.results):
            assert a.rank == b.rank
            assert a.calculation_status == b.calculation_status
            if a.performance_impact is None:
                assert b.performance_impact is None
            else:
                assert a.performance_impact == pytest.approx(b.performance_impact, abs=1e-4)
                assert a.rei == pytest.approx(b.rei, abs=1e-9)

    def test_parallel_batch_does_not_mutate_baseline(self):
        net = build_case16_network()
        before = net.model_dump_json()
        assess_network_resilience(net, net.config, DisruptionConfig(), max_workers=4)
        assert net.model_dump_json() == before


# ===========================================================================
# Orchestrator integration
# ===========================================================================

class TestOrchestratorIntegration:

    def test_orchestrator_consumes_the_service_and_reuses_batches(self):
        """
        The orchestrator asks for REI; it does not implement it, and it
        inherits caching from the service.
        """
        import asyncio
        from netgravity.orchestrator.engines.deterministic import REIClient

        service = REIService()
        client = REIClient(service=service)
        net = build_three_dc_network()

        first = asyncio.run(client.assess(net, config=net.config, disruption_config=DC_ONLY))
        second = asyncio.run(client.assess(net, config=net.config, disruption_config=DC_ONLY))

        assert first["served_from_cache"] is False
        assert second["served_from_cache"] is True
        assert second["batch_id"] == first["batch_id"]
        assert first["batch_status"] == "COMPLETED"
        assert service.stats.batches_computed == 1

    def test_client_exposes_batch_provenance(self):
        import asyncio
        from netgravity.orchestrator.engines.deterministic import REIClient

        net = build_three_dc_network()
        out = asyncio.run(REIClient().assess(net, config=net.config,
                                             disruption_config=DC_ONLY))
        for field in ("batch_id", "batch_status", "material_fingerprint",
                      "model_version", "n_milp_solves", "n_successful", "n_failed"):
            assert field in out
