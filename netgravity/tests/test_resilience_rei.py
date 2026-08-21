"""
NetGravity — Facility Resilience Assessment & Risk Exposure Index (REI) Tests
=============================================================================

Covers the mandatory acceptance tests for the REI capability:

  1  Business cost calculation against hand-computed values
  2  Shortage penalty exclusion (mandatory)
  3  Facility failure with an alternative facility
  4  High-exposure facility  → REI = 1.00, rank 1
  5  Low-exposure / redundant facility
  6  REI normalisation (100 / 50 / 25 → 1.00 / 0.50 / 0.25)
  7  Zero PI → REI 0, no divide-by-zero
  8  Infeasible disruption
  9  Negative PI retained, not clamped
 10  Fair comparison — ranking determined solely by PI
 +   Full integration against the canonical Case-16 network (MILP not mocked)
 +   Determinism
 +   Performance profile (1 + N solves)
 +   Solver-objective vs business-cost separation, incl. the demand-priority
     shortage reconciliation fix

All networks here are small and hand-checkable. No facility identity is
hard-coded inside the engine — the fixtures below are test data, and discovery
always runs off `network.facilities`.
"""

from __future__ import annotations

import math

import pytest

from netgravity.costs.business_cost import (
    BusinessCostError,
    compute_business_network_cost,
)
from netgravity.costs.reconciliation import reconcile_costs
from netgravity.optimization.milp import solve
from netgravity.resilience.rei import (
    BaselineSolveError,
    economic_impact_of,
    FacilityNotFoundError,
    InvalidDisruptionTargetError,
    NoEligibleFacilitiesError,
    apply_facility_disruption,
    assess_facility_resilience,
    assess_network_resilience,
    compute_baseline,
    discover_eligible_facilities,
    normalize_rei,
)
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
from netgravity.schemas.resilience import (
    DisruptionConfig,
    ResilienceCostBasis,
    RiskClassificationRules,
)
from netgravity.schemas.results import REIStatus, RiskClassification, SolverStatus
from netgravity.tests.fixtures.case16_synthetic import build_case16_network

TOL = 1e-6


# ---------------------------------------------------------------------------
# Fixtures — small, deterministic, hand-checkable networks
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> OptimizationConfig:
    """Config with inventory and SLA off so hand arithmetic stays exact."""
    base = dict(
        solver_name="HiGHS",
        enable_inventory=False,
        enforce_sla=False,
        enable_carbon_cost=False,
        minimum_throughput_enabled=False,
        allow_shortage=False,
        cost_period=CostPeriod.MONTH,
        mip_gap=0.0,
        verbose=False,
    )
    base.update(overrides)
    return OptimizationConfig(**base)


def build_dual_dc_network(
    dc_b_market_rate: float = 6.0,
    dc_a_capacity: float = 1000.0,
    dc_b_capacity: float = 1000.0,
    config: OptimizationConfig | None = None,
) -> CanonicalNetwork:
    """
    Plant → {DC_A, DC_B} → MKT, both DCs able to serve the whole market.

        demand(MKT)          = 500
        Plant → DC_A         = 1.0/unit ; Plant → DC_B = 1.0/unit
        DC_A  → MKT          = 2.0/unit
        DC_B  → MKT          = dc_b_market_rate (default 6.0/unit)
        fixed cost per month = 1200/12 = 100 for each DC
        handling             = 0

    Baseline (DC_A wins):  100 + 500×(1.0 + 2.0) = 1,600
    DC_A disrupted:        100 + 500×(1.0 + 6.0) = 3,600  → PI = +2,000
    """
    facilities = [
        FacilityRecord(
            id="PLANT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING, capacity_units_per_period=5000,
            is_mandatory=True, is_closable=False, fixed_cost_per_year=0.0,
        ),
        FacilityRecord(
            id="DC_A", name="DC A", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, capacity_units_per_period=dc_a_capacity,
            fixed_cost_per_year=1200.0, handling_cost_per_unit=0.0,
        ),
        FacilityRecord(
            id="DC_B", name="DC B", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, capacity_units_per_period=dc_b_capacity,
            fixed_cost_per_year=1200.0, handling_cost_per_unit=0.0,
        ),
        FacilityRecord(
            id="MKT", name="Market", role=NodeRole.MARKET,
            status=FacilityStatus.EXISTING, is_closable=False,
        ),
    ]
    lanes = [
        LaneRecord(origin_id="PLANT", destination_id="DC_A", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        LaneRecord(origin_id="PLANT", destination_id="DC_B", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_A", destination_id="MKT", mode=TransportMode.ROAD,
                   rate_per_unit=2.0, distance_km=50.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_B", destination_id="MKT", mode=TransportMode.ROAD,
                   rate_per_unit=dc_b_market_rate, distance_km=300.0, lead_time_days=1.0),
    ]
    return CanonicalNetwork(
        network_id="DUAL_DC",
        facilities=facilities,
        products=[ProductRecord(id="P1", name="Product 1", weight_kg=1.0, unit_value=100.0)],
        demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=500.0, std_dev=0.0)],
        lanes=lanes,
        config=config or _cfg(),
    )


def _exposure_facilities() -> list[FacilityRecord]:
    return [
        FacilityRecord(
            id="PLANT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING, capacity_units_per_period=10000,
            is_mandatory=True, is_closable=False, fixed_cost_per_year=0.0,
        ),
        FacilityRecord(
            id="DC_HUB", name="Hub DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, capacity_units_per_period=5000,
            fixed_cost_per_year=1200.0, handling_cost_per_unit=0.0,
        ),
        FacilityRecord(
            id="DC_RED", name="Redundant DC", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, capacity_units_per_period=5000,
            fixed_cost_per_year=1200.0, handling_cost_per_unit=0.0,
        ),
        FacilityRecord(id="MKT_BIG", name="Big Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
        FacilityRecord(id="MKT_SM", name="Small Market", role=NodeRole.MARKET,
                       status=FacilityStatus.EXISTING, is_closable=False),
    ]


def _exposure_demands() -> list[DemandRecord]:
    return [
        DemandRecord(market_id="MKT_BIG", product_id="P1", quantity=1000.0, std_dev=0.0),
        DemandRecord(market_id="MKT_SM", product_id="P1", quantity=100.0, std_dev=0.0),
    ]


def build_exposure_network(config: OptimizationConfig | None = None) -> CanonicalNetwork:
    """
    High-exposure vs redundant DC, with FULL SERVICE preserved either way.

    Every market has a fallback source, so no disruption strands demand and the
    cost comparison stays like-for-like. Exposure differs only in how expensive
    the fallback is.

        PLANT → DC_HUB = 1.0   PLANT → DC_RED = 1.0
        DC_HUB → MKT_BIG = 2.0   DC_RED → MKT_BIG = 12.0   (dear fallback)
        DC_RED → MKT_SM  = 2.0   DC_HUB → MKT_SM  =  5.0   (dear fallback)
        fixed cost = 1200/12 = 100 per DC per month

    Hand calculation:
        baseline (both open) = 200 + 1000×3 + 100×3            = 3,500
        DC_HUB lost (RED only) = 100 + 1000×13 + 100×3         = 13,400 → PI = +9,900
        DC_RED lost (HUB only) = 100 + 1000×3  + 100×6         =  3,700 → PI =   +200

        max PI = 9,900  →  REI(DC_HUB) = 1.00, REI(DC_RED) = 200/9900 ≈ 0.0202

    (Baseline really is both-open: hub-only costs 3,700 and red-only 13,400.)

    Note: assessments on this network restrict eligible_roles to DC. The PLANT is
    a single point of supply whose loss strands everything, which is a different
    phenomenon (see build_single_source_network).
    """
    lanes = [
        LaneRecord(origin_id="PLANT", destination_id="DC_HUB", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        LaneRecord(origin_id="PLANT", destination_id="DC_RED", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_HUB", destination_id="MKT_BIG", mode=TransportMode.ROAD,
                   rate_per_unit=2.0, distance_km=50.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_RED", destination_id="MKT_BIG", mode=TransportMode.ROAD,
                   rate_per_unit=12.0, distance_km=600.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_RED", destination_id="MKT_SM", mode=TransportMode.ROAD,
                   rate_per_unit=2.0, distance_km=50.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_HUB", destination_id="MKT_SM", mode=TransportMode.ROAD,
                   rate_per_unit=5.0, distance_km=300.0, lead_time_days=1.0),
    ]
    return CanonicalNetwork(
        network_id="EXPOSURE_NET",
        facilities=_exposure_facilities(),
        products=[ProductRecord(id="P1", name="Product 1", weight_kg=1.0, unit_value=100.0)],
        demands=_exposure_demands(),
        lanes=lanes,
        config=config or _cfg(),
    )


def build_single_source_network(config: OptimizationConfig | None = None) -> CanonicalNetwork:
    """
    Same nodes, but MKT_BIG is reachable ONLY from DC_HUB — no fallback at all.

    Losing DC_HUB therefore strands 1,000 of 1,100 units:
      - with allow_shortage=True  → feasible with a huge shortage penalty
        (the case that proves the penalty never reaches business cost);
      - with allow_shortage=False → INFEASIBLE
        (the case that proves infeasibility is reported, not costed).
    """
    lanes = [
        LaneRecord(origin_id="PLANT", destination_id="DC_HUB", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        LaneRecord(origin_id="PLANT", destination_id="DC_RED", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        # MKT_BIG has a single source — no redundancy.
        LaneRecord(origin_id="DC_HUB", destination_id="MKT_BIG", mode=TransportMode.ROAD,
                   rate_per_unit=2.0, distance_km=50.0, lead_time_days=1.0),
        # MKT_SM has two sources.
        LaneRecord(origin_id="DC_RED", destination_id="MKT_SM", mode=TransportMode.ROAD,
                   rate_per_unit=2.0, distance_km=50.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_HUB", destination_id="MKT_SM", mode=TransportMode.ROAD,
                   rate_per_unit=3.0, distance_km=80.0, lead_time_days=1.0),
    ]
    return CanonicalNetwork(
        network_id="SINGLE_SOURCE_NET",
        facilities=_exposure_facilities(),
        products=[ProductRecord(id="P1", name="Product 1", weight_kg=1.0, unit_value=100.0)],
        demands=_exposure_demands(),
        lanes=lanes,
        config=config or _cfg(),
    )


def _dc_only(**overrides) -> DisruptionConfig:
    """Disruption config restricted to DC nodes (excludes the single plant)."""
    params = dict(eligible_roles=[NodeRole.DC])
    params.update(overrides)
    return DisruptionConfig(**params)


# ---------------------------------------------------------------------------
# TEST 1 — Business cost calculation
# ---------------------------------------------------------------------------

class TestBusinessCostCalculation:
    """Business cost components verified against hand-computed values."""

    def test_business_cost_matches_hand_calculation(self):
        net = build_dual_dc_network()
        res = solve(net, config=net.config)
        assert res.is_solved

        bc = compute_business_network_cost(res, net, config=net.config)

        # Hand calculation — DC_A only:
        #   facility  = 1200/12                      =   100
        #   transport = 500×1.0 (in) + 500×2.0 (out) = 1,500
        #   handling  = 0 ; inventory disabled ; carbon unpriced
        assert bc.components["facility_cost"] == pytest.approx(100.0, abs=1e-4)
        assert bc.components["transport_cost"] == pytest.approx(1500.0, abs=1e-4)
        assert bc.components["handling_cost"] == pytest.approx(0.0, abs=1e-4)
        assert bc.components["inventory_cost"] == pytest.approx(0.0, abs=1e-4)
        assert bc.total == pytest.approx(1600.0, abs=1e-4)

        # Components must sum exactly to the reported total.
        assert sum(bc.components.values()) == pytest.approx(bc.total, abs=1e-4)

    def test_handling_and_opening_cost_included(self):
        """Handling and candidate opening cost enter business cost."""
        net = build_dual_dc_network()
        facs = []
        for f in net.facilities:
            if f.id == "DC_A":
                facs.append(f.model_copy(update={
                    "handling_cost_per_unit": 0.5,
                    "status": FacilityStatus.CANDIDATE,
                    "opening_cost": 250.0,
                }))
            else:
                facs.append(f)
        net = net.model_copy(update={"facilities": facs})

        res = solve(net, config=net.config)
        assert res.is_solved
        bc = compute_business_network_cost(res, net, config=net.config)

        # DC_A still cheapest: 100 fixed + 250 opening + 1500 transport + 250 handling
        assert bc.components["opening_cost"] == pytest.approx(250.0, abs=1e-4)
        assert bc.components["handling_cost"] == pytest.approx(250.0, abs=1e-4)  # 500 × 0.5
        assert bc.total == pytest.approx(2100.0, abs=1e-4)

    def test_carbon_included_only_when_genuinely_priced(self):
        """Carbon enters business cost only when a real carbon price applies."""
        # Unpriced: carbon excluded even though cost_basis asks for it.
        net = build_dual_dc_network()
        res = solve(net, config=net.config)
        bc = compute_business_network_cost(res, net, config=net.config)
        assert "carbon_cost" not in bc.components
        assert any("carbon_cost excluded" in n for n in bc.notes)

        # Priced: carbon included and equal to carbon_kg × price.
        priced_cfg = _cfg(enable_carbon_cost=True, carbon_price=0.10)
        net_p = build_dual_dc_network(config=priced_cfg)
        res_p = solve(net_p, config=priced_cfg)
        assert res_p.is_solved
        bc_p = compute_business_network_cost(res_p, net_p, config=priced_cfg)

        total_carbon = sum(fl.carbon_kg for fl in res_p.flow_decisions)
        assert bc_p.components["carbon_cost"] == pytest.approx(total_carbon * 0.10, abs=1e-3)
        assert bc_p.total == pytest.approx(sum(bc_p.components.values()), abs=1e-4)

    def test_unsolved_result_raises_rather_than_fabricating_cost(self):
        net = build_dual_dc_network()
        res = solve(net, config=net.config)
        broken = res.model_copy(deep=True)
        broken.solver.status = SolverStatus.INFEASIBLE
        with pytest.raises(BusinessCostError):
            compute_business_network_cost(broken, net, config=net.config)


# ---------------------------------------------------------------------------
# TEST 2 — Shortage penalty exclusion (MANDATORY)
# ---------------------------------------------------------------------------

class TestShortagePenaltyExclusion:
    """
    The artificial shortage penalty must inflate the solver objective but must
    NOT contaminate business network cost or Performance Impact.
    """

    def _capacity_starved_network(self) -> CanonicalNetwork:
        """DC_A capacity 200 < demand 500, DC_B unreachable → 300 units short."""
        net = build_dual_dc_network(dc_a_capacity=200.0)
        lanes = [ln for ln in net.lanes if ln.destination_id != "DC_B" and ln.origin_id != "DC_B"]
        facs = [f for f in net.facilities if f.id != "DC_B"]
        return CanonicalNetwork(
            network_id="STARVED",
            facilities=facs, products=net.products, demands=net.demands, lanes=lanes,
            config=_cfg(allow_shortage=True, shortage_penalty=1e6),
        )

    def test_solver_objective_inflated_but_business_cost_is_not(self):
        net = self._capacity_starved_network()
        res = solve(net, config=net.config)
        assert res.is_solved

        bc = compute_business_network_cost(res, net, config=net.config)

        # 200 units served, 300 short. Hand calculation of REAL cost:
        #   facility  = 100
        #   transport = 200×1.0 + 200×2.0 = 600
        assert bc.unserved_demand == pytest.approx(300.0, abs=1e-4)
        assert bc.total == pytest.approx(700.0, abs=1e-4)

        # The penalty is enormous and lives ONLY in the solver objective.
        assert bc.shortage_penalty_cost == pytest.approx(300.0 * 1e6, rel=1e-9)
        assert bc.solver_objective > 1e8
        assert bc.solver_objective - bc.total == pytest.approx(bc.shortage_penalty_cost, rel=1e-9)

        # Explicitly excluded, and reported as such.
        assert "shortage_cost" not in bc.components
        assert bc.excluded_components["shortage_cost"] == pytest.approx(300.0 * 1e6, rel=1e-9)
        assert any("shortage penalty" in n and "excluded" in n for n in bc.notes)

    def test_performance_impact_uses_business_cost_not_solver_objective(self):
        """
        MANDATORY: PI must be computed from business network cost.

        Disrupting DC_HUB here strands MKT_BIG entirely. If PI used the solver
        objective it would be in the billions; using business cost it stays in
        the thousands.
        """
        net = build_single_source_network()
        dcfg = _dc_only(allow_shortage=True)
        baseline = compute_baseline(net, net.config, dcfg)

        res = assess_facility_resilience(net, net.config, "DC_HUB", dcfg, baseline=baseline)

        assert res.is_feasible
        assert res.unserved_demand > 0.0, "expected DC_HUB loss to strand MKT_BIG demand"

        # The solver objective is dominated by the penalty...
        assert res.disrupted_solver_objective > 1e8
        assert res.excluded_shortage_penalty > 1e8

        # ...but PI is derived from business cost and stays economically sane.
        assert abs(res.performance_impact) < 1e6, (
            f"PI {res.performance_impact} looks contaminated by the shortage penalty"
        )
        expected_pi = res.disrupted_business_cost - res.baseline_business_cost
        assert res.performance_impact == pytest.approx(expected_pi, abs=1e-4)

        # And the penalty is nowhere inside business cost.
        assert res.disrupted_business_cost < res.disrupted_solver_objective

    def test_shortage_may_be_included_only_by_explicit_configuration(self):
        net = self._capacity_starved_network()
        res = solve(net, config=net.config)
        basis = ResilienceCostBasis(include_shortage_penalty=True)
        bc = compute_business_network_cost(res, net, config=net.config, cost_basis=basis)

        assert "shortage_cost" in bc.components
        assert bc.total == pytest.approx(700.0 + 300.0 * 1e6, rel=1e-9)
        assert any("explicit configuration" in n for n in bc.notes)


# ---------------------------------------------------------------------------
# TEST 2b — Solver objective vs reconciliation: demand-priority shortage
# ---------------------------------------------------------------------------

class TestShortageReconciliationParity:
    """
    The MILP penalises shortage with a demand-priority multiplier
    (1 + (priority − 1) × 0.5). Reconciliation and the reported objective
    components must mirror that term exactly, or the audit gap is spurious.
    """

    def _priority_shortage_network(self) -> CanonicalNetwork:
        facilities = [
            FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                           status=FacilityStatus.EXISTING, capacity_units_per_period=5000,
                           is_mandatory=True, is_closable=False),
            FacilityRecord(id="DC", name="DC", role=NodeRole.DC,
                           status=FacilityStatus.EXISTING, capacity_units_per_period=100.0,
                           fixed_cost_per_year=1200.0),
            FacilityRecord(id="MKT_P3", name="Priority 3 Market", role=NodeRole.MARKET,
                           status=FacilityStatus.EXISTING, is_closable=False),
        ]
        lanes = [
            LaneRecord(origin_id="PLANT", destination_id="DC", mode=TransportMode.ROAD,
                       rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
            LaneRecord(origin_id="DC", destination_id="MKT_P3", mode=TransportMode.ROAD,
                       rate_per_unit=2.0, distance_km=50.0, lead_time_days=1.0),
        ]
        return CanonicalNetwork(
            network_id="PRIORITY_SHORTAGE",
            facilities=facilities,
            products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=100.0)],
            # priority 3 → multiplier 1 + (3-1)×0.5 = 2.0
            demands=[DemandRecord(market_id="MKT_P3", product_id="P1",
                                  quantity=300.0, std_dev=0.0, priority=3)],
            lanes=lanes,
            config=_cfg(allow_shortage=True, shortage_penalty=1e6),
        )

    def test_priority_multiplier_reconciles(self):
        net = self._priority_shortage_network()
        res = solve(net, config=net.config)
        assert res.is_solved

        # 100 served, 200 short at priority 3 → multiplier 2.0
        expected_shortage = 200.0 * 1e6 * 2.0
        assert res.objective_components["shortage_cost"] == pytest.approx(expected_shortage, rel=1e-9)

        rec = reconcile_costs(res, net, config=net.config)
        assert rec.independent_component_costs["shortage_cost"] == pytest.approx(
            expected_shortage, rel=1e-9
        )
        assert rec.is_reconciled, (
            f"reconciliation gap {rec.absolute_difference} — objective and independent "
            f"shortage terms are out of step"
        )
        assert rec.absolute_difference == pytest.approx(0.0, abs=0.05)

    def test_business_cost_unaffected_by_priority_multiplier(self):
        """Whatever the multiplier, none of it reaches business cost."""
        net = self._priority_shortage_network()
        res = solve(net, config=net.config)
        bc = compute_business_network_cost(res, net, config=net.config)
        # facility 100 + transport 100×1.0 + 100×2.0 = 400
        assert bc.total == pytest.approx(400.0, abs=1e-4)


# ---------------------------------------------------------------------------
# TEST 3 — Facility failure with an alternative facility
# ---------------------------------------------------------------------------

class TestFacilityFailureWithAlternative:

    def test_flow_reroutes_to_alternative_dc(self):
        net = build_dual_dc_network(dc_b_market_rate=6.0)
        dcfg = DisruptionConfig()
        res = assess_facility_resilience(net, net.config, "DC_A", dcfg)

        assert res.is_feasible
        assert res.solver_status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
        assert res.unserved_demand == pytest.approx(0.0, abs=1e-6), "alternative DC should absorb all demand"
        assert res.rerouted_volume > 0.0, "flow must have moved to DC_B"

        # Hand calculation:
        #   baseline  = 100 + 500×(1.0 + 2.0) = 1,600
        #   DC_A lost = 100 + 500×(1.0 + 6.0) = 3,600
        #   PI = 2,000 ; CI = 2000/1600 = 125%
        assert res.baseline_business_cost == pytest.approx(1600.0, abs=1e-4)
        assert res.disrupted_business_cost == pytest.approx(3600.0, abs=1e-4)
        assert res.performance_impact == pytest.approx(2000.0, abs=1e-4)
        assert res.cost_impact_pct == pytest.approx(125.0, abs=1e-3)
        assert res.performance_impact > 0.0

    def test_disruption_actually_closes_the_facility(self):
        net = build_dual_dc_network()
        disrupted = apply_facility_disruption(net, "DC_A")
        fac = disrupted.get_facility("DC_A")
        assert fac.capacity_units_per_period == 0.0
        assert fac.is_forced_closed is True
        assert fac.is_mandatory is False
        # Original network is untouched.
        assert net.get_facility("DC_A").capacity_units_per_period == 1000.0
        assert net.get_facility("DC_A").is_forced_closed is False

        res = solve(disrupted, config=net.config)
        assert res.is_solved
        dc_a_flow = sum(fl.flow_units for fl in res.flow_decisions if fl.origin_id == "DC_A")
        assert dc_a_flow == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# TEST 4 & 5 — High-exposure and low-exposure facilities
# ---------------------------------------------------------------------------

class TestExposureRanking:

    def test_high_exposure_facility_gets_rei_one_and_rank_one(self):
        net = build_exposure_network()
        reg = assess_network_resilience(net, net.config, _dc_only())

        hub = reg.get("DC_HUB")
        assert hub is not None
        assert hub.performance_impact is not None
        # Hand calculation: 13,400 − 3,500 = 9,900, all demand still served.
        assert hub.baseline_business_cost == pytest.approx(3500.0, abs=1e-3)
        assert hub.disrupted_business_cost == pytest.approx(13400.0, abs=1e-3)
        assert hub.performance_impact == pytest.approx(9900.0, abs=1e-3)
        assert hub.unserved_demand == pytest.approx(0.0, abs=1e-6)

        assert hub.performance_impact == pytest.approx(reg.max_performance_impact, abs=1e-4)
        assert hub.rei == pytest.approx(1.0, abs=1e-9)
        assert hub.rank == 1
        assert reg.results[0].facility_id == "DC_HUB"
        assert reg.rei_status == REIStatus.COMPUTED

    def test_redundant_facility_has_low_relative_exposure(self):
        net = build_exposure_network()
        reg = assess_network_resilience(net, net.config, _dc_only())

        hub = reg.get("DC_HUB")
        red = reg.get("DC_RED")
        assert red is not None and red.performance_impact is not None

        # Hand calculation: 3,700 − 3,500 = 200, full service retained.
        assert red.performance_impact == pytest.approx(200.0, abs=1e-3)
        assert red.unserved_demand == pytest.approx(0.0, abs=1e-6)

        assert red.performance_impact < hub.performance_impact
        assert red.rei == pytest.approx(200.0 / 9900.0, abs=1e-6)
        assert red.rei < hub.rei
        assert red.rank > hub.rank

    def test_registry_reports_cost_basis_and_exclusions(self):
        net = build_exposure_network()
        reg = assess_network_resilience(net, net.config, _dc_only())
        assert "transport_cost" in reg.cost_basis_components
        assert "facility_cost" in reg.cost_basis_components
        assert "shortage_cost" not in reg.cost_basis_components
        assert "shortage_cost" in reg.excluded_components


# ---------------------------------------------------------------------------
# TEST 6 & 7 — REI normalisation and zero PI (pure arithmetic, no MILP)
# ---------------------------------------------------------------------------

class TestREINormalisation:

    def test_rei_normalisation_against_max(self):
        reis, max_pi, status = normalize_rei([100.0, 50.0, 25.0])
        assert max_pi == pytest.approx(100.0, abs=TOL)
        assert status == REIStatus.COMPUTED
        assert reis[0] == pytest.approx(1.00, abs=TOL)
        assert reis[1] == pytest.approx(0.50, abs=TOL)
        assert reis[2] == pytest.approx(0.25, abs=TOL)

    def test_zero_pi_yields_zero_rei_without_division(self):
        reis, max_pi, status = normalize_rei([0.0, 0.0, 0.0])
        assert max_pi == pytest.approx(0.0, abs=TOL)
        assert status == REIStatus.NO_RELATIVE_COST_EXPOSURE
        assert reis == [0.0, 0.0, 0.0]
        assert all(math.isfinite(r) for r in reis)

    def test_all_negative_pi_yields_zero_rei(self):
        """
        No facility has positive exposure, so every REI is 0.

        The returned maximum is the maximum ECONOMIC IMPACT (max(0, PI)), which
        is 0 here — not the maximum raw PI. Normalising against a negative
        maximum would invert the ranking.
        """
        reis, max_impact, status = normalize_rei([-10.0, -20.0])
        assert status == REIStatus.NO_RELATIVE_COST_EXPOSURE
        assert reis == [0.0, 0.0]
        assert max_impact == pytest.approx(0.0, abs=TOL)

    def test_none_pi_maps_to_none_rei(self):
        reis, max_pi, status = normalize_rei([100.0, None, 50.0])
        assert status == REIStatus.COMPUTED
        assert reis[0] == pytest.approx(1.0, abs=TOL)
        assert reis[1] is None
        assert reis[2] == pytest.approx(0.5, abs=TOL)

    def test_all_none_pi_is_not_computed(self):
        reis, max_pi, status = normalize_rei([None, None])
        assert status == REIStatus.NOT_COMPUTED
        assert max_pi is None
        assert reis == [None, None]

    def test_empty_input_does_not_raise(self):
        reis, max_pi, status = normalize_rei([])
        assert reis == []
        assert max_pi is None
        assert status == REIStatus.NOT_COMPUTED

    def test_zero_pi_end_to_end_registry(self):
        """
        A facility that the baseline does not use produces PI = 0. With
        only_baseline_open_facilities disabled, an unused candidate is assessed
        and the registry must handle a zero-PI row safely.
        """
        net = build_dual_dc_network()
        # DC_B is idle at baseline; assess it alone.
        dcfg = DisruptionConfig(
            only_baseline_open_facilities=False,
            exclude_facility_ids=["PLANT", "DC_A"],
        )
        reg = assess_network_resilience(net, net.config, dcfg)
        assert reg.n_facilities_assessed == 1
        row = reg.get("DC_B")
        assert row.performance_impact == pytest.approx(0.0, abs=1e-4)
        assert row.rei == pytest.approx(0.0, abs=TOL)
        assert reg.rei_status == REIStatus.NO_RELATIVE_COST_EXPOSURE
        assert any("No relative cost exposure" in w for w in reg.warnings)


# ---------------------------------------------------------------------------
# TEST 8 — Infeasible disruption
# ---------------------------------------------------------------------------

class TestInfeasibleDisruption:

    def test_infeasible_disruption_returns_none_pi_and_critical(self):
        """
        With shortage disabled, losing the only DC serving MKT_BIG makes the
        network infeasible. No fabricated cost may be produced.
        """
        net = build_single_source_network()
        dcfg = _dc_only(allow_shortage=False)
        res = assess_facility_resilience(net, net.config, "DC_HUB", dcfg)

        assert res.solver_status == SolverStatus.INFEASIBLE
        assert res.is_feasible is False
        assert res.performance_impact is None
        assert res.cost_impact_pct is None
        assert res.rei is None
        assert res.disrupted_business_cost is None
        assert res.risk_classification == RiskClassification.CRITICAL
        assert res.rei_status == REIStatus.NOT_COMPUTED
        assert any("cannot absorb" in d for d in res.diagnostics)

    def test_default_basis_is_like_for_like(self):
        """
        The default cost basis must be like-for-like: shortage disabled, so every
        compared solution serves 100% of demand and no facility can score a
        negative PI merely by serving less volume.
        """
        assert DisruptionConfig().allow_shortage is False
        assert DisruptionConfig().service_diagnostic_on_infeasible is True

    def test_service_diagnostic_quantifies_infeasible_disruption(self):
        """
        A CRITICAL facility must still report HOW MUCH demand it strands, without
        any of that reaching the cost-based ranking.
        """
        net = build_single_source_network()
        res = assess_facility_resilience(net, net.config, "DC_HUB", _dc_only())

        # Cost side stays undefined — the diagnostic never feeds it.
        assert res.is_feasible is False
        assert res.solver_status == SolverStatus.INFEASIBLE
        assert res.performance_impact is None
        assert res.cost_impact_pct is None
        assert res.rei is None
        assert res.disrupted_business_cost is None
        assert res.risk_classification == RiskClassification.CRITICAL

        # Service side IS quantified.
        assert res.service_diagnostic_applied is True
        # DC_HUB is MKT_BIG's only source: 1000 of 1100 units stranded.
        assert res.unserved_demand == pytest.approx(1000.0, abs=1e-3)
        assert res.unserved_demand_rate == pytest.approx(1000.0 / 1100.0, abs=1e-4)
        assert res.service_loss is not None and res.service_loss > 0.0
        assert res.disrupted_served == pytest.approx(100.0, abs=1e-3)
        assert res.rerouted_volume is not None
        assert res.carbon_delta is not None
        assert any("Service diagnostic" in d for d in res.diagnostics)

    def test_service_diagnostic_can_be_disabled(self):
        net = build_single_source_network()
        dcfg = _dc_only(service_diagnostic_on_infeasible=False)
        res = assess_facility_resilience(net, net.config, "DC_HUB", dcfg)

        assert res.is_feasible is False
        assert res.service_diagnostic_applied is False
        assert res.unserved_demand is None
        assert res.risk_classification == RiskClassification.CRITICAL

    def test_registry_survives_infeasible_rows(self):
        net = build_single_source_network()
        dcfg = _dc_only(allow_shortage=False)
        reg = assess_network_resilience(net, net.config, dcfg)

        assert reg.n_infeasible >= 1
        infeasible = reg.infeasible_facilities()
        assert any(r.facility_id == "DC_HUB" for r in infeasible)
        # Unrankable rows sort last and carry no rank.
        for r in reg.results:
            if r.performance_impact is None:
                assert r.rank is None
                assert r.rei is None
        # Feasible rows still ranked normally.
        ranked = [r for r in reg.results if r.rank is not None]
        assert ranked == sorted(ranked, key=lambda r: r.rank)


# ---------------------------------------------------------------------------
# TEST 9 — Negative PI retained, never clamped
# ---------------------------------------------------------------------------

class TestNegativePerformanceImpact:

    def test_negative_pi_is_retained_and_flagged(self):
        """
        Disrupting PLANT_NORTH in Case-16 strands demand, so the disrupted
        network serves less volume and costs less. The negative PI must survive
        intact and carry an explanatory diagnostic.
        """
        net = build_case16_network()
        dcfg = DisruptionConfig(allow_shortage=True)
        baseline = compute_baseline(net, net.config, dcfg)
        res = assess_facility_resilience(net, net.config, "PLANT_NORTH", dcfg, baseline=baseline)

        assert res.performance_impact is not None
        assert res.performance_impact < 0.0, "expected a negative PI for this disruption"
        # Not clamped to zero.
        assert res.performance_impact != 0.0
        assert res.disrupted_business_cost < res.baseline_business_cost
        assert res.cost_impact_pct < 0.0

        assert any("NEGATIVE PERFORMANCE IMPACT" in d for d in res.diagnostics)
        # The unserved-demand explanation must be surfaced, not hidden.
        assert res.unserved_demand > 0.0
        assert any("like-for-like" in d for d in res.diagnostics), (
            "a negative PI caused by unserved demand must be explained as a "
            "comparability caveat"
        )

    def test_negative_pi_yields_zero_exposure_but_raw_pi_survives(self):
        """
        A negative PI means NO economic exposure, so REI is 0 — not negative.

        REI must stay within [0, 1] because it feeds RF = P + REI - P*REI, which
        is only defined on the unit interval. The anomaly is not hidden: the raw
        signed PI is retained on the result and flagged separately (see
        `test_negative_pi_is_retained_and_flagged`); only the quantity REI
        normalises over is floored.
        """
        reis, max_impact, status = normalize_rei([100.0, -40.0])
        assert status == REIStatus.COMPUTED
        assert reis[0] == pytest.approx(1.0, abs=TOL)
        assert reis[1] == pytest.approx(0.0, abs=TOL)
        assert all(0.0 <= r <= 1.0 for r in reis if r is not None)
        # The economic impact used for normalisation is the floored value.
        assert economic_impact_of(-40.0) == pytest.approx(0.0)
        # ...while the raw PI is untouched.
        assert max_impact == pytest.approx(100.0, abs=TOL)


# ---------------------------------------------------------------------------
# TEST 10 — Fair comparison
# ---------------------------------------------------------------------------

class TestFairComparison:

    def test_ranking_determined_solely_by_performance_impact(self):
        net = build_exposure_network()
        reg = assess_network_resilience(net, net.config, _dc_only())

        ranked = [r for r in reg.results if r.performance_impact is not None]
        pis = [r.performance_impact for r in ranked]
        assert pis == sorted(pis, reverse=True), "rows must be ordered by descending PI"

        for r in ranked:
            assert r.rei == pytest.approx(r.performance_impact / reg.max_performance_impact, abs=1e-6)

    def test_every_row_shares_identical_disruption_assumptions(self):
        net = build_exposure_network()
        dcfg = _dc_only()
        reg = assess_network_resilience(net, net.config, dcfg)

        assert len({r.disruption_type for r in reg.results}) == 1
        assert len({r.disruption_period for r in reg.results}) == 1
        assert len({r.baseline_business_cost for r in reg.results}) == 1
        assert reg.disruption_type == dcfg.disruption_type.value
        assert reg.disruption_period == dcfg.disruption_period.value
        # Baseline solved once and shared by every row.
        assert all(r.baseline_business_cost == reg.baseline_business_cost for r in reg.results)

    def test_time_to_recovery_is_rejected_not_faked(self):
        """The MILP is single-period: TTR must be refused, never approximated."""
        with pytest.raises(ValueError, match="single-period"):
            DisruptionConfig(time_to_recovery_days=14.0)

    def test_disruption_config_rejects_unknown_fields(self):
        with pytest.raises(ValueError):
            DisruptionConfig(unknown_field=True)


# ---------------------------------------------------------------------------
# Facility discovery — never hard-coded
# ---------------------------------------------------------------------------

class TestFacilityDiscovery:

    def test_discovery_reads_from_the_network(self):
        net = build_exposure_network()
        baseline = compute_baseline(net, net.config, DisruptionConfig())
        facs = discover_eligible_facilities(net, DisruptionConfig(), baseline.result)
        ids = {f.id for f in facs}
        assert "MKT_BIG" not in ids and "MKT_SM" not in ids, "markets are not disruption targets"
        assert ids <= {f.id for f in net.facilities}
        assert len(ids) > 0

    def test_role_filter_and_exclusions_respected(self):
        net = build_exposure_network()
        baseline = compute_baseline(net, net.config, DisruptionConfig())
        dcfg = DisruptionConfig(eligible_roles=[NodeRole.DC], exclude_facility_ids=["DC_RED"])
        ids = {f.id for f in discover_eligible_facilities(net, dcfg, baseline.result)}
        assert ids == {"DC_HUB"}

    def test_closed_facilities_are_skipped(self):
        net = build_dual_dc_network()
        facs = [
            f.model_copy(update={"status": FacilityStatus.CLOSED}) if f.id == "DC_B" else f
            for f in net.facilities
        ]
        net = net.model_copy(update={"facilities": facs})
        dcfg = DisruptionConfig(only_baseline_open_facilities=False)
        ids = {f.id for f in discover_eligible_facilities(net, dcfg, None)}
        assert "DC_B" not in ids

    def test_no_eligible_facilities_raises(self):
        net = build_dual_dc_network()
        dcfg = DisruptionConfig(exclude_facility_ids=["PLANT", "DC_A", "DC_B"])
        with pytest.raises(NoEligibleFacilitiesError):
            assess_network_resilience(net, net.config, dcfg)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:

    def test_unknown_facility_id_raises(self):
        net = build_dual_dc_network()
        with pytest.raises(FacilityNotFoundError, match="NOT_A_FACILITY"):
            assess_facility_resilience(net, net.config, "NOT_A_FACILITY", DisruptionConfig())

    def test_empty_facility_id_raises(self):
        net = build_dual_dc_network()
        with pytest.raises(FacilityNotFoundError):
            assess_facility_resilience(net, net.config, "", DisruptionConfig())

    def test_market_node_cannot_be_disrupted(self):
        net = build_dual_dc_network()
        with pytest.raises(InvalidDisruptionTargetError, match="MARKET"):
            assess_facility_resilience(net, net.config, "MKT", DisruptionConfig())

    def test_infeasible_baseline_raises(self):
        """No baseline → no comparison. Must fail loudly, not return zeros."""
        net = build_single_source_network()
        # Sever the only route to MKT_BIG and forbid shortage.
        lanes = [ln for ln in net.lanes if ln.destination_id != "MKT_BIG"]
        broken = net.model_copy(update={"lanes": lanes, "config": _cfg(allow_shortage=False)})
        with pytest.raises(BaselineSolveError):
            assess_network_resilience(broken, broken.config, DisruptionConfig(allow_shortage=False))

    def test_invalid_risk_rules_rejected(self):
        with pytest.raises(ValueError):
            RiskClassificationRules(unserved_demand_rate_high=1.5)
        with pytest.raises(ValueError):
            RiskClassificationRules(
                unserved_demand_rate_critical=0.1, unserved_demand_rate_high=0.5
            )


# ---------------------------------------------------------------------------
# Risk classification — deterministic rules only
# ---------------------------------------------------------------------------

class TestRiskClassification:

    def test_no_arbitrary_rei_bands_by_default(self):
        """Feasible rows are NOT_CLASSIFIED unless a rule is configured."""
        net = build_exposure_network()
        reg = assess_network_resilience(net, net.config, _dc_only())
        feasible = [r for r in reg.results if r.is_feasible]
        assert feasible, "expected at least one feasible row"
        for r in feasible:
            # A top-ranked REI = 1.0 row must NOT be auto-labelled critical.
            assert r.risk_classification == RiskClassification.NOT_CLASSIFIED

    def test_configured_unserved_demand_threshold_applies(self):
        net = build_single_source_network()
        dcfg = _dc_only(
            allow_shortage=True,
            risk_rules=RiskClassificationRules(unserved_demand_rate_critical=0.5),
        )
        res = assess_facility_resilience(net, net.config, "DC_HUB", dcfg)
        # DC_HUB loss strands MKT_BIG (1000 of 1100 units ≈ 91%).
        assert res.unserved_demand_rate > 0.5
        assert res.risk_classification == RiskClassification.CRITICAL

    def test_configured_cost_impact_threshold_applies(self):
        net = build_dual_dc_network(dc_b_market_rate=6.0)
        dcfg = DisruptionConfig(risk_rules=RiskClassificationRules(cost_impact_pct_high=50.0))
        res = assess_facility_resilience(net, net.config, "DC_A", dcfg)
        assert res.cost_impact_pct > 50.0
        assert res.risk_classification == RiskClassification.HIGH


# ---------------------------------------------------------------------------
# FULL INTEGRATION — canonical Case-16 network, MILP not mocked
# ---------------------------------------------------------------------------

class TestFullIntegration:
    """
    Input network → config → baseline MILP → baseline business cost →
    facility discovery → per-facility MILP → business cost → PI → REI →
    ranking → registry.

    The real MILP runs throughout. Nothing is mocked.
    """

    def test_case16_end_to_end_registry(self):
        net = build_case16_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig())

        # Registry identity and provenance
        assert reg.network_id == "CASE16_SYNTHETIC"
        assert reg.data_version == net.data_version
        assert reg.baseline_solver_status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)
        assert reg.generated_at is not None

        # Baseline solved and costed
        assert reg.baseline_business_cost is not None and reg.baseline_business_cost > 0
        assert reg.baseline_served > 0

        # Facilities discovered dynamically from the network
        assert reg.n_facilities_assessed > 0
        network_ids = {f.id for f in net.facilities}
        for r in reg.results:
            assert r.facility_id in network_ids
            assert r.facility_name
            assert r.facility_role

        # Every row carries the shared assumptions and a completed assessment
        for r in reg.results:
            assert r.disruption_type == "FACILITY_FAILURE"
            assert r.disruption_period == "MODELLED_PLANNING_PERIOD"
            assert r.baseline_business_cost == reg.baseline_business_cost
            assert isinstance(r.is_feasible, bool)
            if r.is_feasible:
                assert r.disrupted_business_cost is not None
                assert r.performance_impact is not None
                assert r.rei is not None
                assert r.rank is not None
                assert r.rerouted_volume is not None
                assert r.carbon_delta is not None

        # REI normalisation held
        if reg.rei_status == REIStatus.COMPUTED:
            top = reg.results[0]
            assert top.rei == pytest.approx(1.0, abs=1e-9)
            assert top.performance_impact == pytest.approx(reg.max_performance_impact, abs=1e-4)

        # Ranking is contiguous over rankable rows
        ranks = [r.rank for r in reg.results if r.rank is not None]
        assert ranks == list(range(1, len(ranks) + 1))

        # Telemetry present
        assert reg.baseline_solve_seconds is not None
        assert reg.total_assessment_seconds is not None

    def test_case16_business_cost_below_solver_objective_under_shortage(self):
        """Separation of the two concepts holds on the real network."""
        net = build_case16_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig(allow_shortage=True))
        shorted = [r for r in reg.results if r.is_feasible and (r.unserved_demand or 0) > 0]
        assert shorted, "expected at least one disruption to strand demand in Case-16"
        for r in shorted:
            assert r.excluded_shortage_penalty > 0
            assert r.disrupted_business_cost < r.disrupted_solver_objective

    def test_registry_helpers(self):
        net = build_case16_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig())
        assert reg.get("NOPE") is None
        top2 = reg.top_n(2)
        assert len(top2) == min(2, len(reg.results))
        assert top2 == reg.results[: len(top2)]


# ---------------------------------------------------------------------------
# DETERMINISM
# ---------------------------------------------------------------------------

class TestDeterminism:

    def test_repeated_assessment_is_identical(self):
        net = build_case16_network()
        dcfg = DisruptionConfig()

        reg1 = assess_network_resilience(net, net.config, dcfg)
        reg2 = assess_network_resilience(net, net.config, dcfg)

        assert reg1.baseline_business_cost == pytest.approx(reg2.baseline_business_cost, abs=1e-4)
        assert reg1.rei_status == reg2.rei_status
        assert reg1.n_facilities_assessed == reg2.n_facilities_assessed
        assert [r.facility_id for r in reg1.results] == [r.facility_id for r in reg2.results]

        for a, b in zip(reg1.results, reg2.results):
            assert a.facility_id == b.facility_id
            assert a.rank == b.rank
            assert a.solver_status == b.solver_status
            assert a.is_feasible == b.is_feasible
            if a.performance_impact is None:
                assert b.performance_impact is None
            else:
                assert a.performance_impact == pytest.approx(b.performance_impact, abs=1e-4)
                assert a.disrupted_business_cost == pytest.approx(b.disrupted_business_cost, abs=1e-4)
                assert a.rei == pytest.approx(b.rei, abs=1e-9)

    def test_disruption_does_not_mutate_input_network(self):
        net = build_case16_network()
        before = net.model_dump_json()
        assess_network_resilience(net, net.config, DisruptionConfig())
        assert net.model_dump_json() == before, "assessment mutated the input network"


# ---------------------------------------------------------------------------
# PERFORMANCE — 1 + N solves, baseline never re-solved
# ---------------------------------------------------------------------------

class TestPerformance:

    def test_baseline_solved_exactly_once(self):
        """All facilities feasible ⇒ exactly 1 + N MILP solves, no diagnostics."""
        net = build_exposure_network()
        calls = []

        def counting_solve(network, config, scenario_id):
            calls.append(scenario_id)
            return solve(network, config=config, scenario_id=scenario_id)

        reg = assess_network_resilience(
            net, net.config, _dc_only(), solve_fn=counting_solve,
        )

        assert sum(1 for c in calls if c == "REI_BASELINE") == 1, (
            f"baseline must be solved exactly once, saw {calls}"
        )
        assert reg.n_infeasible == 0
        assert reg.n_diagnostic_solves == 0
        assert len(calls) == 1 + reg.n_facilities_assessed
        assert len(calls) == reg.total_milp_solves

    def test_diagnostic_solves_are_bounded_by_infeasible_count(self):
        """
        Worst case is 1 + N + (number of infeasible facilities) — one extra solve
        only for the facilities that actually need it.
        """
        net = build_single_source_network()
        calls = []

        def counting_solve(network, config, scenario_id):
            calls.append(scenario_id)
            return solve(network, config=config, scenario_id=scenario_id)

        reg = assess_network_resilience(
            net, net.config, _dc_only(), solve_fn=counting_solve,
        )

        assert reg.n_infeasible >= 1, "expected DC_HUB loss to be unabsorbable"
        assert reg.n_diagnostic_solves <= reg.n_infeasible
        assert reg.total_milp_solves == 1 + reg.n_facilities_assessed + reg.n_diagnostic_solves
        assert len(calls) == reg.total_milp_solves

        diag_calls = [c for c in calls if c.startswith("REI_SERVICE_DIAG_")]
        assert len(diag_calls) == reg.n_diagnostic_solves
        # Feasible facilities never trigger a diagnostic solve.
        for r in reg.results:
            if r.is_feasible:
                assert r.service_diagnostic_applied is False

    def test_solve_fn_injection_supports_future_execution_strategies(self):
        """The public interface accepts an alternative executor (caching/parallel/remote)."""
        net = build_dual_dc_network()
        cache = {}

        def caching_solve(network, config, scenario_id):
            key = (scenario_id, network.compute_data_version())
            if key not in cache:
                cache[key] = solve(network, config=config, scenario_id=scenario_id)
            return cache[key]

        reg = assess_network_resilience(
            net, net.config, DisruptionConfig(), solve_fn=caching_solve,
        )
        assert reg.n_facilities_assessed > 0
        assert len(cache) == reg.total_milp_solves

    def test_registry_records_runtime_telemetry(self):
        net = build_dual_dc_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig())
        assert reg.baseline_solve_seconds >= 0.0
        assert reg.total_assessment_seconds >= reg.baseline_solve_seconds
        for r in reg.results:
            assert r.solve_seconds is not None and r.solve_seconds >= 0.0
