"""
NetGravity V1.4 — Targeted Hardening Test Suite
================================================

Covers the five V1.4 changes:

  1  Closure economics integrated into objective, reconciliation and REI basis
  2  Contractual constraints (active / expired / prohibited / penalised)
  3  V1 service methodology made explicit (transit-time SLA only)
  4  Five formalised optimization modes over ONE MILP
  5  Frozen deterministic result contracts

Plus scenario isolation, cost reconciliation, REI integrity, determinism,
backward compatibility, and a full end-to-end pipeline integration test.

Fixtures reuse `tests/fixtures/case16_synthetic.py` wherever possible; small
hand-checkable networks are built locally where exact arithmetic matters.
"""

from __future__ import annotations

import pytest

from netgravity.costs.business_cost import compute_business_network_cost
from netgravity.costs.reconciliation import reconcile_costs
from netgravity.metrics.contracts import (
    build_network_state_result,
    build_scenario_result,
)
from netgravity.optimization.milp import solve
from netgravity.optimization.modes import (
    MODE_POLICIES,
    get_mode_policy,
    prepare_network_for_mode,
)
from netgravity.resilience.rei import (
    assess_facility_resilience,
    assess_network_resilience,
    compute_baseline,
)
from netgravity.scenarios.engine import ScenarioEngine
from netgravity.schemas.contracts import NetworkStateResult, ScenarioResult
from netgravity.schemas.network import (
    CanonicalNetwork,
    ContractStatus,
    CostPeriod,
    DemandRecord,
    FacilityRecord,
    FacilityStatus,
    LaneRecord,
    NodeRole,
    ObjectiveMode,
    OptimizationConfig,
    OptimizationMode,
    ProductRecord,
    ServiceMetric,
    SLAMode,
    TransportMode,
    V1_SUPPORTED_SERVICE_METRICS,
)
from netgravity.schemas.resilience import DisruptionConfig, ResilienceCostBasis
from netgravity.schemas.results import REIStatus, RiskClassification, SolverStatus
from netgravity.schemas.scenario import FacilityChange, Scenario
from netgravity.tests.fixtures.case16_synthetic import build_case16_network
from netgravity.validation.checks import validate_network


# ---------------------------------------------------------------------------
# Local hand-checkable fixtures
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> OptimizationConfig:
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


def build_closure_network(
    dc_a_closure_cost: float = 0.0,
    dc_a_fixed_per_year: float = 120_000.0,   # 10,000 / month
    dc_b_closure_cost: float = 7_777.0,
    cheap_lane_off_baseline: bool = False,
    config: OptimizationConfig | None = None,
) -> CanonicalNetwork:
    """
    Two EXISTING DCs plus one unselected CANDIDATE, one market.

        demand(MKT) = 100
        PLANT → DC_A = 1.0/unit ; PLANT → DC_B = 1.0/unit ; PLANT → DC_NEW = 1.0/unit
        DC_A → MKT   = 1.0/unit  (cheap)
        DC_B → MKT   = 2.0/unit
        DC_NEW → MKT = 9.0/unit  (never attractive)

        DC_A fixed = dc_a_fixed_per_year / 12   (default 10,000/month)
        DC_B fixed = 1,200 / 12 = 100/month
        DC_NEW: CANDIDATE, fixed 1,200/yr, opening_cost 500

    With DC_A's fixed cost at 10,000/month, closing it and serving from DC_B
    saves far more than it costs — unless a large closure cost is charged.

        keep DC_A open : 10,000 + 100 + 100×(1+1) = 10,300
        close DC_A     :             100 + 100×(1+2) =    400  (+ closure cost)

    So DC_A closes unless closure_cost > 9,900.
    """
    facilities = [
        FacilityRecord(
            id="PLANT", name="Plant", role=NodeRole.PLANT,
            status=FacilityStatus.EXISTING, capacity_units_per_period=5000,
            is_mandatory=True, is_closable=False, fixed_cost_per_year=0.0,
        ),
        FacilityRecord(
            id="DC_A", name="DC A", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
            fixed_cost_per_year=dc_a_fixed_per_year, closure_cost=dc_a_closure_cost,
        ),
        FacilityRecord(
            id="DC_B", name="DC B", role=NodeRole.DC,
            status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
            fixed_cost_per_year=1_200.0, closure_cost=dc_b_closure_cost,
        ),
        FacilityRecord(
            id="DC_NEW", name="Candidate DC", role=NodeRole.DC,
            status=FacilityStatus.CANDIDATE, capacity_units_per_period=1000,
            fixed_cost_per_year=1_200.0, opening_cost=500.0,
            # A candidate carrying a closure cost: must NEVER be charged when
            # simply not selected.
            closure_cost=9_999.0,
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
        LaneRecord(origin_id="PLANT", destination_id="DC_NEW", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
        # The CHEAP outbound lane. `cheap_lane_off_baseline` marks it as not
        # part of the observed network, which is what separates
        # ACTUAL_AS_IS_EVALUATION (baseline lanes only) from
        # CURRENT_FOOTPRINT_OPTIMIZATION (all lanes available).
        LaneRecord(origin_id="DC_A", destination_id="MKT", mode=TransportMode.ROAD,
                   rate_per_unit=1.0, distance_km=50.0, lead_time_days=1.0,
                   is_active_baseline=not cheap_lane_off_baseline),
        LaneRecord(origin_id="DC_B", destination_id="MKT", mode=TransportMode.ROAD,
                   rate_per_unit=2.0, distance_km=80.0, lead_time_days=1.0),
        LaneRecord(origin_id="DC_NEW", destination_id="MKT", mode=TransportMode.ROAD,
                   rate_per_unit=9.0, distance_km=400.0, lead_time_days=1.0),
    ]
    return CanonicalNetwork(
        network_id="CLOSURE_NET",
        facilities=facilities,
        products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=100.0)],
        demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=100.0, std_dev=0.0)],
        lanes=lanes,
        config=config or _cfg(),
    )


def _open_ids(res) -> set:
    return {fd.facility_id for fd in res.facility_decisions if fd.is_open}


# ===========================================================================
# CHANGE 1 — CLOSURE ECONOMICS
# ===========================================================================

class TestClosureCostEconomics:
    """
    Closure cost must be charged exactly when an EXISTING facility transitions
    open → closed, and never otherwise.
    """

    def test_existing_facility_remains_open_no_closure_cost(self):
        """Rule 1: a facility that stays open is never charged."""
        # Cheap fixed cost → DC_A stays open.
        net = build_closure_network(dc_a_closure_cost=5_000.0, dc_a_fixed_per_year=1_200.0)
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_A" in _open_ids(res)
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)

    def test_existing_facility_closed_charges_closure_cost_exactly_once(self):
        """Rule 2: an EXISTING facility closed by the optimization is charged once."""
        net = build_closure_network(dc_a_closure_cost=1_000.0)
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_A" not in _open_ids(res), "expensive DC_A should close"

        # Charged exactly once — the configured amount, not a multiple.
        assert res.objective_components["closure_cost"] == pytest.approx(1_000.0, abs=1e-6)

        # Hand calculation: DC_B fixed 100 + transport 100×(1+2) + closure 1,000
        assert res.solver.objective_value == pytest.approx(1_400.0, abs=1e-3)

    def test_closure_cost_changes_the_decision_when_large_enough(self):
        """The closure charge is genuinely in the objective, not just reported."""
        # Closing saves 9,900/month; a 20,000 closure charge must deter it.
        net = build_closure_network(dc_a_closure_cost=20_000.0)
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_A" in _open_ids(res), "large closure cost should keep DC_A open"
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)

    def test_already_closed_facility_not_charged(self):
        """Rule 3: a facility already CLOSED in the baseline is never charged."""
        net = build_closure_network(dc_a_closure_cost=1_000.0)
        facs = [
            f.model_copy(update={"status": FacilityStatus.CLOSED}) if f.id == "DC_A" else f
            for f in net.facilities
        ]
        net = net.model_copy(update={"facilities": facs})
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_A" not in _open_ids(res)
        # Baseline status is CLOSED, not EXISTING → no transition, no charge.
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)

    def test_unselected_candidate_not_charged(self):
        """Rule 4: an unselected greenfield candidate is never charged."""
        net = build_closure_network(dc_a_closure_cost=0.0, dc_a_fixed_per_year=1_200.0)
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_NEW" not in _open_ids(res), "candidate should not be selected"
        # DC_NEW carries closure_cost=9,999 but is a CANDIDATE.
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)

    def test_greenfield_mode_never_charges_closure(self):
        """Absence from a greenfield design is not a closure decision."""
        net = build_closure_network(
            dc_a_closure_cost=1_000.0,
            config=_cfg(optimization_mode=OptimizationMode.GREENFIELD_OPTIMIZATION),
        )
        res = solve(net, config=net.config)
        assert res.is_solved
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)

    def test_closure_cost_reconciles_independently(self):
        """Rule 5a: reconciliation reproduces the closure charge from decisions."""
        net = build_closure_network(dc_a_closure_cost=1_000.0)
        res = solve(net, config=net.config)
        rec = reconcile_costs(res, net, config=net.config)
        assert rec.independent_component_costs["closure_cost"] == pytest.approx(1_000.0, abs=1e-6)
        assert rec.is_reconciled, f"gap {rec.absolute_difference}"
        assert rec.absolute_difference == pytest.approx(0.0, abs=0.05)

    def test_closure_cost_included_in_business_cost(self):
        """Rule 5b: closure cost is a real business cost, inside the REI basis."""
        net = build_closure_network(dc_a_closure_cost=1_000.0)
        res = solve(net, config=net.config)
        bc = compute_business_network_cost(res, net, config=net.config)

        assert "closure_cost" in bc.components
        assert bc.components["closure_cost"] == pytest.approx(1_000.0, abs=1e-6)
        assert bc.total == pytest.approx(1_400.0, abs=1e-3)
        assert sum(bc.components.values()) == pytest.approx(bc.total, abs=1e-4)

    def test_closure_cost_can_be_excluded_from_business_cost(self):
        net = build_closure_network(dc_a_closure_cost=1_000.0)
        res = solve(net, config=net.config)
        basis = ResilienceCostBasis(include_closure_cost=False)
        bc = compute_business_network_cost(res, net, config=net.config, cost_basis=basis)
        assert "closure_cost" not in bc.components
        assert bc.total == pytest.approx(400.0, abs=1e-3)

    def test_cost_categories_remain_distinct(self):
        """Operating, opening, closure, shortage and carbon stay separate."""
        net = build_closure_network(dc_a_closure_cost=1_000.0)
        res = solve(net, config=net.config)
        comp = res.objective_components
        for key in ("facility_cost", "opening_cost", "closure_cost",
                    "transport_cost", "handling_cost", "inventory_cost",
                    "shortage_cost", "carbon_cost"):
            assert key in comp, f"missing distinct cost category: {key}"
        # Closure is not folded into facility (operating) cost.
        assert comp["facility_cost"] == pytest.approx(100.0, abs=1e-3)
        assert comp["closure_cost"] == pytest.approx(1_000.0, abs=1e-6)

    def test_scenario_close_charges_closure_cost_once(self):
        """
        A scenario CLOSE of an EXISTING facility is charged — the case that
        needs `baseline_status`, because the engine overwrites `status` with
        CLOSED.
        """
        net = build_case16_network()
        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="CLOSE_EAST", scenario_name="Close DC East",
            facility_changes=[FacilityChange(facility_id="DC_EAST", action="CLOSE")],
        )
        res = engine.run(net, scen)
        assert res.is_solved
        # DC_EAST closure_cost in the fixture is 40,000, charged exactly once.
        assert res.objective_components["closure_cost"] == pytest.approx(40_000.0, abs=1e-3)

    def test_disruption_target_exempt_from_closure_cost(self):
        """An involuntary outage is not a voluntary closure decision."""
        net = build_closure_network(
            dc_a_closure_cost=1_000.0, dc_a_fixed_per_year=1_200.0,
            dc_b_closure_cost=0.0,   # keep the baseline arithmetic clean
        )
        dcfg = DisruptionConfig(eligible_roles=[NodeRole.DC])
        res = assess_facility_resilience(net, net.config, "DC_A", dcfg)
        assert res.is_feasible
        # Hand calculation:
        #   baseline (DC_A serves)  : 100 fixed + 100×(1+1) = 300
        #   DC_A down (DC_B serves) : 100 fixed + 100×(1+2) = 400
        #   PI = 100 — rerouting only. DC_A's 1,000 closure cost is NOT charged
        #   because the outage is involuntary.
        assert res.performance_impact == pytest.approx(100.0, abs=1e-3)

    def test_backward_compatibility_switch(self):
        """enable_closure_cost=False restores pre-V1.4 behaviour exactly."""
        net = build_closure_network(
            dc_a_closure_cost=20_000.0, config=_cfg(enable_closure_cost=False),
        )
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_A" not in _open_ids(res), "without closure economics DC_A should close"
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)
        assert res.solver.objective_value == pytest.approx(400.0, abs=1e-3)


# ===========================================================================
# CHANGE 2 — CONTRACTUAL CONSTRAINTS
# ===========================================================================

class TestContractualConstraints:
    """State 1: prohibited. State 2: allowed with penalty. State 3: expired/none."""

    def _contracted(self, status, allows_early, closure_cost=1_000.0, **cfg):
        net = build_closure_network(dc_a_closure_cost=closure_cost, config=_cfg(**cfg))
        facs = [
            f.model_copy(update={
                "contract_status": status,
                "contract_allows_early_closure": allows_early,
            }) if f.id == "DC_A" else f
            for f in net.facilities
        ]
        return net.model_copy(update={"facilities": facs})

    def test_state1_active_contract_closure_prohibited_forces_open(self):
        """Active contract + closure prohibited → facility must remain open."""
        net = self._contracted(ContractStatus.ACTIVE, allows_early=False)
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_A" in _open_ids(res), "contract must pin DC_A open despite 10,000/mo cost"
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)
        # Hand check: 10,000 fixed + 100 transport-in + 100 out = 10,300
        assert res.solver.objective_value == pytest.approx(10_300.0, abs=1e-3)

    def test_state2_active_contract_closure_allowed_with_penalty(self):
        """Active contract + early closure allowed → may close, penalty charged."""
        net = self._contracted(ContractStatus.ACTIVE, allows_early=True, closure_cost=1_000.0)
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_A" not in _open_ids(res), "closure permitted and economic"
        assert res.objective_components["closure_cost"] == pytest.approx(1_000.0, abs=1e-6)

    def test_state3_expired_contract_allows_normal_optimization(self):
        """Expired contract behaves exactly like no contract."""
        expired = self._contracted(ContractStatus.EXPIRED, allows_early=False)
        none_   = self._contracted(ContractStatus.NONE, allows_early=True)

        r_exp = solve(expired, config=expired.config)
        r_non = solve(none_, config=none_.config)

        assert "DC_A" not in _open_ids(r_exp), "expired contract must not pin the facility open"
        assert _open_ids(r_exp) == _open_ids(r_non)
        assert r_exp.solver.objective_value == pytest.approx(r_non.solver.objective_value, abs=1e-3)

    def test_no_contract_is_the_default(self):
        fac = FacilityRecord(id="X", name="X", role=NodeRole.DC)
        assert fac.contract_status == ContractStatus.NONE
        assert fac.contract_allows_early_closure is True
        assert fac.contract_prohibits_closure is False

    def test_contract_prohibition_survives_only_via_explicit_relaxation(self):
        """Scenario overrides must be explicit — relaxing the contract works."""
        net = self._contracted(ContractStatus.ACTIVE, allows_early=False)
        relaxed_facs = [
            f.model_copy(update={"contract_status": ContractStatus.EXPIRED})
            if f.id == "DC_A" else f
            for f in net.facilities
        ]
        relaxed = net.model_copy(update={"facilities": relaxed_facs})
        res = solve(relaxed, config=relaxed.config)
        assert "DC_A" not in _open_ids(res)

    def test_contract_vs_forced_close_conflict_is_reported(self):
        """V-015 names the conflict rather than leaving a bare INFEASIBLE."""
        net = self._contracted(ContractStatus.ACTIVE, allows_early=False)
        facs = [
            f.model_copy(update={"is_forced_closed": True}) if f.id == "DC_A" else f
            for f in net.facilities
        ]
        conflicted = net.model_copy(update={"facilities": facs})

        report = validate_network(conflicted)
        codes = {i.code for i in report.errors}
        assert "V-015" in codes, "contract/forced-close conflict must be reported"
        msg = next(i.description for i in report.errors if i.code == "V-015")
        assert "contract" in msg.lower() and "DC_A" in msg

    def test_enforce_contracts_switch_disables_the_constraint(self):
        net = self._contracted(ContractStatus.ACTIVE, allows_early=False, enforce_contracts=False)
        res = solve(net, config=net.config)
        assert "DC_A" not in _open_ids(res), "constraint disabled → normal optimization"

    def test_disruption_target_exempt_from_contract_pin(self):
        """
        A contracted facility must still be disruptable — otherwise every
        resilience run on a contracted facility returns infeasible for the
        wrong reason.
        """
        net = self._contracted(ContractStatus.ACTIVE, allows_early=False, closure_cost=0.0)
        dcfg = DisruptionConfig(eligible_roles=[NodeRole.DC])
        res = assess_facility_resilience(net, net.config, "DC_A", dcfg)
        assert res.is_feasible, "contract pin must not block a physical disruption"
        assert res.performance_impact is not None

    def test_contract_fields_do_not_mutate_baseline(self):
        """Scenario isolation: hypothetical contract state never leaks back."""
        net = self._contracted(ContractStatus.ACTIVE, allows_early=False)
        before = net.model_dump_json()
        solve(net, config=net.config)
        assert net.model_dump_json() == before


# ===========================================================================
# CHANGE 3 — V1 SERVICE METHODOLOGY
# ===========================================================================

class TestServiceMethodologyV1:

    def _sla_network(self, sla_days, slow_lane_lt=9.0, config=None):
        facilities = [
            FacilityRecord(id="PLANT", name="Plant", role=NodeRole.PLANT,
                           status=FacilityStatus.EXISTING, capacity_units_per_period=5000,
                           is_mandatory=True, is_closable=False),
            FacilityRecord(id="DC_FAST", name="Fast DC", role=NodeRole.DC,
                           status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
                           fixed_cost_per_year=1_200.0),
            FacilityRecord(id="DC_SLOW", name="Slow DC", role=NodeRole.DC,
                           status=FacilityStatus.EXISTING, capacity_units_per_period=1000,
                           fixed_cost_per_year=1_200.0),
            FacilityRecord(id="MKT", name="Market", role=NodeRole.MARKET,
                           status=FacilityStatus.EXISTING, is_closable=False),
        ]
        lanes = [
            LaneRecord(origin_id="PLANT", destination_id="DC_FAST", mode=TransportMode.ROAD,
                       rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
            LaneRecord(origin_id="PLANT", destination_id="DC_SLOW", mode=TransportMode.ROAD,
                       rate_per_unit=1.0, distance_km=100.0, lead_time_days=1.0),
            LaneRecord(origin_id="DC_FAST", destination_id="MKT", mode=TransportMode.ROAD,
                       rate_per_unit=5.0, distance_km=50.0, lead_time_days=1.0),
            # Cheaper but slow — chosen on cost alone, excluded on SLA.
            LaneRecord(origin_id="DC_SLOW", destination_id="MKT", mode=TransportMode.ROAD,
                       rate_per_unit=1.0, distance_km=900.0, lead_time_days=slow_lane_lt),
        ]
        return CanonicalNetwork(
            network_id="SLA_NET",
            facilities=facilities,
            products=[ProductRecord(id="P1", name="P1", weight_kg=1.0, unit_value=100.0)],
            demands=[DemandRecord(market_id="MKT", product_id="P1", quantity=100.0,
                                  std_dev=0.0, sla_days=sla_days)],
            lanes=lanes,
            config=config or _cfg(enforce_sla=True),
        )

    def test_sla_feasible_lane_remains_available(self):
        # SLA 10 days admits the slow lane (9 days) — cheapest wins.
        net = self._sla_network(sla_days=10.0)
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_SLOW" in _open_ids(res), "SLA-feasible cheap lane should be used"
        assert res.service_report.n_lanes_sla_excluded == 0

    def test_sla_infeasible_lane_is_excluded(self):
        # SLA 2 days excludes the 9-day lane; the dearer fast lane must be used.
        net = self._sla_network(sla_days=2.0)
        res = solve(net, config=net.config)
        assert res.is_solved
        assert "DC_FAST" in _open_ids(res)
        assert "DC_SLOW" not in _open_ids(res), "SLA-infeasible lane must be unusable"

        sr = res.service_report
        assert sr.n_lanes_sla_excluded == 1
        assert sr.sla_enforced is True
        assert sr.methodology == "TRANSIT_TIME_SLA_FEASIBILITY"

    def test_sla_enforcement_can_be_disabled(self):
        net = self._sla_network(sla_days=2.0, config=_cfg(enforce_sla=False))
        res = solve(net, config=net.config)
        assert "DC_SLOW" in _open_ids(res), "SLA off → cheapest lane usable"
        assert res.service_report.sla_enforced is False

    def test_end_to_end_sla_mode_behaves_consistently(self):
        """END_TO_END accumulates inbound + DC + outbound lead time."""
        # Outbound 1 day passes LAST_MILE at SLA 2, but end-to-end is
        # 1 (inbound) + 1 (DC replenishment) + 1 (outbound) = 3 > 2.
        net = self._sla_network(sla_days=2.0, slow_lane_lt=1.0)
        facs = [
            f.model_copy(update={"replenishment_lead_time_days": 1.0})
            if f.role == NodeRole.DC else f
            for f in net.facilities
        ]
        net = net.model_copy(update={"facilities": facs})

        last_mile = solve(net, config=net.config.model_copy(update={"sla_mode": SLAMode.LAST_MILE}))
        assert last_mile.is_solved
        assert last_mile.service_report.sla_mode == "LAST_MILE"
        assert last_mile.service_report.n_lanes_sla_excluded == 0

        e2e = solve(net, config=net.config.model_copy(update={"sla_mode": SLAMode.END_TO_END}))
        assert e2e.service_report.sla_mode == "END_TO_END"
        assert e2e.service_report.n_lanes_sla_excluded > 0, (
            "END_TO_END must account for inbound + DC lead time"
        )

    def test_scenario_sla_change_is_handled(self):
        """Tightening the SLA through a scenario changes lane eligibility."""
        loose = self._sla_network(sla_days=10.0)
        tight = self._sla_network(sla_days=2.0)
        r_loose = solve(loose, config=loose.config)
        r_tight = solve(tight, config=tight.config)
        assert r_loose.service_report.n_lanes_sla_excluded == 0
        assert r_tight.service_report.n_lanes_sla_excluded == 1
        assert r_tight.solver.objective_value > r_loose.solver.objective_value

    def test_result_does_not_claim_unsupported_service_optimization(self):
        """Default config uses only implemented capabilities."""
        net = self._sla_network(sla_days=10.0)
        res = solve(net, config=net.config)
        sr = res.service_report
        assert sr.service_metric == "TRANSIT_TIME"
        assert sr.service_metric_supported is True
        assert sr.unsupported_features == []
        assert sr.claims_only_supported_capabilities is True

    @pytest.mark.parametrize("metric", [ServiceMetric.CSL, ServiceMetric.FILL_RATE, ServiceMetric.PENALTY])
    def test_unimplemented_service_metrics_are_flagged_not_silently_honoured(self, metric):
        """Declared-but-inert metrics must be reported, never pretended active."""
        assert metric.value not in V1_SUPPORTED_SERVICE_METRICS
        net = self._sla_network(sla_days=10.0, config=_cfg(enforce_sla=True, service_metric=metric))
        res = solve(net, config=net.config)

        sr = res.service_report
        assert sr.service_metric == metric.value
        assert sr.service_metric_supported is False
        assert sr.unsupported_features, "unsupported metric must be named"
        assert any(metric.value in f and "NOT implemented" in f for f in sr.unsupported_features)
        assert sr.claims_only_supported_capabilities is False
        # Also surfaced on solver metadata.
        assert any(metric.value in w for w in res.solver.warnings)

    def test_cost_service_objective_mode_is_flagged_as_unimplemented(self):
        net = self._sla_network(
            sla_days=10.0,
            config=_cfg(enforce_sla=True, objective_mode=ObjectiveMode.COST_SERVICE),
        )
        res = solve(net, config=net.config)
        assert any("COST_SERVICE" in f for f in res.service_report.unsupported_features)

    def test_service_report_exposes_required_fields(self):
        net = self._sla_network(sla_days=2.0)
        res = solve(net, config=net.config)
        sr = res.service_report
        # SLA target + transit time + feasibility + served/unserved demand.
        assert sr.total_demand == pytest.approx(100.0, abs=1e-6)
        assert sr.served_demand == pytest.approx(100.0, abs=1e-6)
        assert sr.unserved_demand == pytest.approx(0.0, abs=1e-6)
        assert sr.n_lanes_evaluated >= 1
        assert 0.0 <= sr.pct_demand_in_sla <= 100.0

    def test_violations_populated_only_with_diagnostics(self):
        net = self._sla_network(sla_days=2.0)
        quiet = solve(net, config=net.config)
        assert quiet.service_report.violations == []

        loud = solve(net, config=net.config.model_copy(update={"verbose": True}))
        assert len(loud.service_report.violations) == 1
        v = loud.service_report.violations[0]
        assert v.origin_id == "DC_SLOW" and v.destination_id == "MKT"
        assert v.sla_days == pytest.approx(2.0, abs=1e-6)
        assert v.lead_time_days == pytest.approx(9.0, abs=1e-6)
        assert v.excess_days == pytest.approx(7.0, abs=1e-6)
        assert v.excluded_pre_solve is True


# ===========================================================================
# CHANGE 4 — OPTIMIZATION MODES
# ===========================================================================

class TestOptimizationModes:

    def test_all_five_modes_have_policies(self):
        assert len(MODE_POLICIES) == 5
        for mode in OptimizationMode:
            policy = get_mode_policy(mode)
            assert policy.mode == mode
            assert policy.description

    def test_default_mode_is_backward_compatible_no_op(self):
        """The default mode must transform nothing."""
        cfg = OptimizationConfig()
        assert cfg.optimization_mode == OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION
        net = build_case16_network()
        prepared = prepare_network_for_mode(net, OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION)
        assert prepared is net, "default mode must be a strict no-op"

    def test_mode_recorded_in_result(self):
        for mode in OptimizationMode:
            net = build_closure_network(config=_cfg(optimization_mode=mode))
            res = solve(net, config=net.config)
            assert res.optimization_mode == mode.value
            assert res.is_hypothetical == get_mode_policy(mode).is_hypothetical

    def test_actual_as_is_is_the_only_non_hypothetical_mode(self):
        """Observed and optimized states can never be conflated."""
        for mode in OptimizationMode:
            policy = get_mode_policy(mode)
            expected = (mode != OptimizationMode.ACTUAL_AS_IS_EVALUATION)
            assert policy.is_hypothetical is expected

    @staticmethod
    def _mode_net(mode):
        """
        Footprint-lock fixture: the CHEAP DC_A → MKT lane is off-baseline.

            observed lane set only  → must ship via DC_B at 3/unit
            all lanes available     → may ship via DC_A at 2/unit
        """
        return build_closure_network(
            dc_a_fixed_per_year=1_200.0,
            cheap_lane_off_baseline=True,
            config=_cfg(optimization_mode=mode),
        )

    def test_mode1_actual_as_is_pins_footprint_and_baseline_lanes(self):
        net = self._mode_net(OptimizationMode.ACTUAL_AS_IS_EVALUATION)
        res = solve(net, config=net.config)
        assert res.is_solved

        # Both EXISTING DCs pinned open; candidate excluded.
        assert {"DC_A", "DC_B"} <= _open_ids(res)
        assert "DC_NEW" not in _open_ids(res)
        # Observed anchor, not a hypothetical.
        assert res.is_hypothetical is False

        # The off-baseline cheap lane is unavailable, so flow must take DC_B.
        dc_a_flow = sum(f.flow_units for f in res.flow_decisions
                        if f.origin_id == "DC_A" and f.destination_id == "MKT")
        dc_b_flow = sum(f.flow_units for f in res.flow_decisions
                        if f.origin_id == "DC_B" and f.destination_id == "MKT")
        assert dc_a_flow == pytest.approx(0.0, abs=1e-6), "off-baseline lane must be unavailable"
        assert dc_b_flow == pytest.approx(100.0, abs=1e-6)
        # 100 + 100 fixed + 100×(1+2) = 500
        assert res.solver.objective_value == pytest.approx(500.0, abs=1e-3)

    def test_mode2_current_footprint_locks_decisions_but_frees_routing(self):
        net = self._mode_net(OptimizationMode.CURRENT_FOOTPRINT_OPTIMIZATION)
        res = solve(net, config=net.config)
        assert res.is_solved

        # Footprint still locked open, candidate still excluded.
        assert {"DC_A", "DC_B"} <= _open_ids(res)
        assert "DC_NEW" not in _open_ids(res)

        # But every lane is now available, so routing shifts to the cheap one.
        dc_a_flow = sum(f.flow_units for f in res.flow_decisions
                        if f.origin_id == "DC_A" and f.destination_id == "MKT")
        assert dc_a_flow > 0, "current-footprint mode must free up routing"
        # 100 + 100 fixed + 100×(1+1) = 400
        assert res.solver.objective_value == pytest.approx(400.0, abs=1e-3)
        # No closure decision is possible, so no closure cost.
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)

    def test_mode1_and_mode2_differ_on_lane_availability(self):
        """The two footprint-locked modes are genuinely distinct."""
        as_is = solve(self._mode_net(OptimizationMode.ACTUAL_AS_IS_EVALUATION))
        footprint = solve(self._mode_net(OptimizationMode.CURRENT_FOOTPRINT_OPTIMIZATION))

        assert as_is.solver.objective_value > footprint.solver.objective_value, (
            "freeing routing must beat the observed lane set here"
        )
        # Same footprint either way — only routing differs.
        assert _open_ids(as_is) == _open_ids(footprint)
        assert as_is.is_hypothetical is False
        assert footprint.is_hypothetical is True

    def test_mode3_greenfield_releases_footprint(self):
        net = build_closure_network(
            dc_a_closure_cost=50_000.0, dc_a_fixed_per_year=120_000.0,
            config=_cfg(optimization_mode=OptimizationMode.GREENFIELD_OPTIMIZATION),
        )
        res = solve(net, config=net.config)
        assert res.is_solved
        # Expensive DC_A dropped, and no closure penalty deters it.
        assert "DC_A" not in _open_ids(res)
        assert res.objective_components["closure_cost"] == pytest.approx(0.0, abs=1e-6)

    def test_mode3_greenfield_can_select_candidates(self):
        net = build_closure_network(
            config=_cfg(optimization_mode=OptimizationMode.GREENFIELD_OPTIMIZATION),
        )
        prepared = prepare_network_for_mode(net, OptimizationMode.GREENFIELD_OPTIMIZATION)
        cand = prepared.get_facility("DC_NEW")
        assert cand.capacity_units_per_period > 0, "candidates must stay available in greenfield"

    def test_mode4_brownfield_applies_closure_and_contracts(self):
        policy = get_mode_policy(OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION)
        assert policy.apply_closure_cost is True
        assert policy.enforce_contracts is True
        assert policy.locks_facility_decisions is False

    def test_mode5_disruption_policy(self):
        policy = get_mode_policy(OptimizationMode.DISRUPTION_RESILIENCE_OPTIMIZATION)
        assert policy.apply_closure_cost is True
        assert policy.enforce_contracts is True
        assert policy.is_hypothetical is True

    def test_resilience_runs_use_disruption_mode(self):
        net = build_case16_network()
        baseline = compute_baseline(net, net.config, DisruptionConfig())
        assert baseline.effective_config.optimization_mode == (
            OptimizationMode.DISRUPTION_RESILIENCE_OPTIMIZATION
        )
        assert baseline.result.optimization_mode == "DISRUPTION_RESILIENCE_OPTIMIZATION"

    def test_footprint_locking_mode_is_overridden_for_resilience(self):
        """A footprint-locking mode would make every disruption infeasible."""
        net = build_case16_network()
        cfg = net.config.model_copy(update={
            "optimization_mode": OptimizationMode.CURRENT_FOOTPRINT_OPTIMIZATION
        })
        res = assess_facility_resilience(net, cfg, "DC_EAST", DisruptionConfig())
        assert res.is_feasible, "resilience must override a footprint-locking mode"

    def test_invalid_mode_raises_with_valid_options(self):
        with pytest.raises(ValueError, match="Unknown optimization mode"):
            get_mode_policy("NOT_A_MODE")
        with pytest.raises(ValueError):
            OptimizationConfig(optimization_mode="NONSENSE")

    def test_mode_preparation_never_mutates_input(self):
        for mode in OptimizationMode:
            net = build_case16_network()
            before = net.model_dump_json()
            prepare_network_for_mode(net, mode)
            assert net.model_dump_json() == before, f"{mode.value} mutated the input network"

    def test_one_milp_serves_every_mode(self):
        """No mode duplicates the formulation — all route through milp.solve."""
        import inspect
        from netgravity.optimization import modes as modes_mod
        src = inspect.getsource(modes_mod)
        assert "pulp" not in src, "mode module must not formulate optimization"
        assert "LpProblem" not in src


# ===========================================================================
# CHANGE 5 — FROZEN RESULT CONTRACTS
# ===========================================================================

class TestDeterministicResultContracts:

    def test_network_state_result_completeness(self):
        net = build_case16_network()
        res = solve(net, config=net.config)
        state = build_network_state_result(res, net, config=net.config)

        assert isinstance(state, NetworkStateResult)
        # Snapshot identity
        assert state.network_id == "CASE16_SYNTHETIC"
        assert state.data_version == net.data_version
        # Mode + observed/hypothetical
        assert state.optimization_mode == res.optimization_mode
        assert state.mode_description
        assert state.is_hypothetical is True
        # Feasibility
        assert state.solver_status == SolverStatus.OPTIMAL
        assert state.is_feasible is True
        # Distinct cost categories
        c = state.costs
        for attr in ("facility_cost", "opening_cost", "closure_cost", "transport_cost",
                     "handling_cost", "inventory_cost", "carbon_cost"):
            assert hasattr(c, attr)
        assert c.business_network_cost > 0
        assert c.solver_objective > 0
        assert c.reconciliation_is_closed is True
        # Demand
        assert state.demand.total_demand > 0
        assert state.demand.served_demand > 0
        # Service
        assert state.service is not None
        # Facilities / flows / utilisation
        assert state.open_facilities and state.facilities and state.flows
        assert state.avg_utilization_pct >= 0
        # Metadata
        assert state.metadata.run_id == res.run_id
        assert state.metadata.solver_name
        assert state.metadata.generated_at

    def test_shortage_penalty_reported_separately_from_business_cost(self):
        """Consumers must never have to reverse a penalty out of an objective."""
        net = build_case16_network()
        cfg = net.config.model_copy(update={"allow_shortage": True})
        # Every DC down — including the two CANDIDATE DCs, which would
        # otherwise absorb the demand and leave nothing unserved.
        facs = [
            f.model_copy(update={"capacity_units_per_period": 0.0, "is_mandatory": False,
                                 "is_closable": True, "is_forced_closed": True})
            if f.role == NodeRole.DC else f
            for f in net.facilities
        ]
        starved = net.model_copy(update={"facilities": facs, "config": cfg})
        res = solve(starved, config=cfg)
        state = build_network_state_result(res, starved, config=cfg)

        assert state.demand.unserved_demand > 0
        assert state.costs.shortage_penalty_cost > 0
        assert state.costs.business_network_cost < state.costs.solver_objective
        assert "shortage_cost" in state.costs.excluded_components

    def test_scenario_result_is_always_hypothetical(self):
        net = build_case16_network()
        base = build_network_state_result(solve(net, config=net.config), net, config=net.config)

        engine = ScenarioEngine()
        scen = Scenario(scenario_id="S1", scenario_name="Close East",
                        facility_changes=[FacilityChange(facility_id="DC_EAST", action="CLOSE")])
        sres = engine.run(net, scen)

        sc = build_scenario_result(
            sres, net, scenario_id="S1", scenario_name="Close East",
            baseline_state=base, scenario_overrides=["CLOSE DC_EAST"], config=net.config,
        )
        assert isinstance(sc, ScenarioResult)
        assert sc.is_hypothetical is True
        assert sc.state.is_hypothetical is True
        assert sc.scenario_overrides == ["CLOSE DC_EAST"]
        # Baseline identity carried, baseline itself untouched.
        assert sc.baseline_network_id == base.network_id
        assert sc.baseline_data_version == base.data_version
        assert sc.baseline_business_cost == base.costs.business_network_cost
        assert sc.business_cost_delta is not None

    def test_scenario_result_never_overwrites_observed_state(self):
        net = build_case16_network()
        before = net.model_dump_json()
        base_state = build_network_state_result(solve(net, config=net.config), net, config=net.config)
        base_cost = base_state.costs.business_network_cost

        engine = ScenarioEngine()
        scen = Scenario(scenario_id="S1", scenario_name="Close East",
                        facility_changes=[FacilityChange(facility_id="DC_EAST", action="CLOSE")])
        engine.run(net, scen)

        assert net.model_dump_json() == before, "scenario mutated the observed network"
        assert base_state.costs.business_network_cost == base_cost

    def test_contract_metadata_is_exposed_for_downstream(self):
        net = build_case16_network()
        res = solve(net, config=net.config)
        state = build_network_state_result(res, net, config=net.config)
        for fs in state.facilities:
            assert fs.contract_status in ("NONE", "ACTIVE", "EXPIRED")
            assert fs.baseline_status in ("EXISTING", "CANDIDATE", "CLOSED")

    def test_contract_contains_no_risk_factor(self):
        """RF belongs to the Orchestrator, not the deterministic core."""
        payload = NetworkStateResult.model_json_schema()
        text = str(payload).lower()
        for banned in ("risk_factor", "likelihood", "probability"):
            assert banned not in text, f"deterministic contract must not carry '{banned}'"

    def test_result_contracts_are_json_serialisable(self):
        net = build_case16_network()
        res = solve(net, config=net.config)
        state = build_network_state_result(res, net, config=net.config)
        blob = state.model_dump_json()
        assert len(blob) > 100
        assert NetworkStateResult.model_validate_json(blob).network_id == state.network_id

    def test_resilience_result_carries_snapshot_and_required_fields(self):
        net = build_case16_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig())
        assert reg.results
        for r in reg.results:
            assert r.network_id == net.network_id
            assert r.data_version == net.data_version
            assert r.disruption_type and r.disruption_period
            assert r.baseline_business_cost is not None
            assert r.solver_status is not None
            assert isinstance(r.is_feasible, bool)
            assert r.risk_classification in set(RiskClassification)
        assert reg.cost_basis_components
        assert "shortage_cost" in reg.excluded_components


# ===========================================================================
# REI / RESILIENCE INTEGRITY (unchanged formulation must be preserved)
# ===========================================================================

class TestREIIntegrityPreserved:

    def test_rei_still_uses_business_cost_not_solver_objective(self):
        net = build_case16_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig())
        for r in reg.results:
            if r.performance_impact is not None:
                assert r.performance_impact == pytest.approx(
                    r.disrupted_business_cost - r.baseline_business_cost, abs=1e-3
                )

    def test_rei_normalisation_preserved(self):
        """
        REI normalises over ECONOMIC IMPACT = max(0, PI), bounding it to [0, 1].
        """
        net = build_case16_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig())
        if reg.rei_status == REIStatus.COMPUTED:
            assert reg.results[0].rei == pytest.approx(1.0, abs=1e-9)
            for r in reg.results:
                if r.performance_impact is not None:
                    expected = max(0.0, r.performance_impact) / reg.max_performance_impact
                    assert r.rei == pytest.approx(expected, abs=1e-6)
                    assert 0.0 <= r.rei <= 1.0

    def test_infeasible_disruption_handling_preserved(self):
        net = build_case16_network()
        dcfg = DisruptionConfig(allow_shortage=False)
        reg = assess_network_resilience(net, net.config, dcfg)
        for r in reg.results:
            if not r.is_feasible:
                assert r.performance_impact is None
                assert r.rei is None
                assert r.risk_classification == RiskClassification.CRITICAL

    def test_resilience_determinism_preserved(self):
        net = build_case16_network()
        a = assess_network_resilience(net, net.config, DisruptionConfig())
        b = assess_network_resilience(net, net.config, DisruptionConfig())
        assert [r.facility_id for r in a.results] == [r.facility_id for r in b.results]
        for ra, rb in zip(a.results, b.results):
            assert ra.rank == rb.rank
            if ra.performance_impact is not None:
                assert ra.performance_impact == pytest.approx(rb.performance_impact, abs=1e-4)

    def test_baseline_isolation_preserved(self):
        net = build_case16_network()
        before = net.model_dump_json()
        assess_network_resilience(net, net.config, DisruptionConfig())
        assert net.model_dump_json() == before


# ===========================================================================
# DETERMINISM & BACKWARD COMPATIBILITY
# ===========================================================================

class TestDeterminismAndCompatibility:

    def test_repeated_solve_is_identical(self):
        net = build_case16_network()
        a = solve(net, config=net.config)
        b = solve(net, config=net.config)
        assert a.solver.objective_value == pytest.approx(b.solver.objective_value, abs=1e-6)
        assert _open_ids(a) == _open_ids(b)
        assert a.objective_components == b.objective_components

    def test_legacy_config_without_new_fields_still_works(self):
        """A pre-V1.4 config dict must still construct and solve."""
        legacy = OptimizationConfig(
            objective_mode="COST_MIN", solver_name="HiGHS", enable_inventory=True,
            enforce_sla=True, allow_shortage=False, mip_gap=0.001,
        )
        assert legacy.optimization_mode == OptimizationMode.BROWNFIELD_SCENARIO_OPTIMIZATION
        assert legacy.enable_closure_cost is True
        assert legacy.enforce_contracts is True

        net = build_case16_network(config=legacy)
        res = solve(net, config=legacy)
        assert res.is_solved

    def test_legacy_facility_without_new_fields_still_works(self):
        fac = FacilityRecord(id="F", name="F", role=NodeRole.DC)
        assert fac.baseline_status is None
        assert fac.effective_baseline_status == fac.status
        assert fac.contract_status == ContractStatus.NONE
        assert fac.is_disruption_target is False
        assert fac.closure_cost == 0.0

    def test_zero_closure_cost_network_unchanged_by_feature(self):
        """Networks without closure costs behave identically either way."""
        on = build_closure_network(dc_a_closure_cost=0.0, config=_cfg(enable_closure_cost=True))
        off = build_closure_network(dc_a_closure_cost=0.0, config=_cfg(enable_closure_cost=False))
        r_on = solve(on, config=on.config)
        r_off = solve(off, config=off.config)
        assert r_on.solver.objective_value == pytest.approx(r_off.solver.objective_value, abs=1e-6)
        assert _open_ids(r_on) == _open_ids(r_off)

    def test_reconciliation_closes_across_all_modes(self):
        for mode in OptimizationMode:
            net = build_closure_network(
                dc_a_closure_cost=1_000.0, config=_cfg(optimization_mode=mode),
            )
            res = solve(net, config=net.config)
            assert res.is_solved, f"{mode.value} failed to solve"
            rec = reconcile_costs(res, net, config=net.config)
            assert rec.is_reconciled, (
                f"{mode.value} reconciliation gap {rec.absolute_difference}"
            )


# ===========================================================================
# END-TO-END INTEGRATION
# ===========================================================================

class TestEndToEndDeterministicPipeline:
    """
    Observed network
      → Actual As-Is
      → Current Footprint Optimization
      → Brownfield Scenario
      → MILP
      → Business Cost
      → Resilience scenario
      → PI → REI → ResilienceResult

    The real MILP runs at every stage; nothing is mocked.
    """

    def test_full_pipeline_is_internally_consistent(self):
        observed = build_case16_network()
        observed_snapshot = observed.model_dump_json()

        # --- Stage 1: Actual As-Is ------------------------------------------
        as_is_cfg = observed.config.model_copy(update={
            "optimization_mode": OptimizationMode.ACTUAL_AS_IS_EVALUATION
        })
        as_is_res = solve(observed, config=as_is_cfg)
        assert as_is_res.is_solved
        as_is = build_network_state_result(as_is_res, observed, config=as_is_cfg)

        assert as_is.is_hypothetical is False, "as-is must be the observed anchor"
        assert as_is.optimization_mode == "ACTUAL_AS_IS_EVALUATION"
        assert as_is.costs.reconciliation_is_closed
        assert as_is.costs.business_network_cost > 0
        # Every EXISTING facility is open in the observed footprint.
        existing = {f.id for f in observed.facilities
                    if f.role not in (NodeRole.MARKET, NodeRole.CUSTOMER)
                    and f.status == FacilityStatus.EXISTING}
        assert existing <= set(as_is.open_facilities)

        # --- Stage 2: Current Footprint Optimization ------------------------
        cf_cfg = observed.config.model_copy(update={
            "optimization_mode": OptimizationMode.CURRENT_FOOTPRINT_OPTIMIZATION
        })
        cf_res = solve(observed, config=cf_cfg)
        assert cf_res.is_solved
        current_footprint = build_network_state_result(cf_res, observed, config=cf_cfg)

        assert current_footprint.is_hypothetical is True
        # Same footprint, and never worse than as-is (routing is freed).
        assert existing <= set(current_footprint.open_facilities)
        assert current_footprint.costs.business_network_cost <= as_is.costs.business_network_cost + 1e-6

        # --- Stage 3: Brownfield Scenario -----------------------------------
        engine = ScenarioEngine()
        scen = Scenario(
            scenario_id="CLOSE_CENTRAL", scenario_name="Close DC Central",
            facility_changes=[FacilityChange(facility_id="DC_CENTRAL", action="CLOSE")],
        )
        scen_res = engine.run(observed, scen)
        assert scen_res.is_solved

        scenario = build_scenario_result(
            scen_res, observed, scenario_id="CLOSE_CENTRAL",
            scenario_name="Close DC Central", baseline_state=as_is,
            scenario_overrides=["CLOSE DC_CENTRAL"], config=observed.config,
        )
        assert scenario.is_hypothetical is True
        assert "DC_CENTRAL" not in scenario.state.open_facilities
        # Closure economics applied: DC_CENTRAL closure_cost is 50,000.
        assert scenario.state.costs.closure_cost == pytest.approx(50_000.0, abs=1e-3)
        # Delta arithmetic is internally consistent.
        assert scenario.business_cost_delta == pytest.approx(
            scenario.state.costs.business_network_cost - as_is.costs.business_network_cost,
            abs=1e-3,
        )

        # --- Stage 4: Business cost consistency -----------------------------
        for state in (as_is, current_footprint, scenario.state):
            c = state.costs
            included = sum(getattr(c, name) for name in c.included_components)
            assert included == pytest.approx(c.business_network_cost, abs=1e-2), (
                f"{state.optimization_mode}: components must sum to business cost"
            )
            # Business cost never silently contains the shortage penalty.
            assert "shortage_cost" not in c.included_components

        # --- Stage 5: Resilience → PI → REI ---------------------------------
        registry = assess_network_resilience(observed, observed.config, DisruptionConfig())
        assert registry.n_facilities_assessed > 0
        assert registry.baseline_business_cost is not None

        for r in registry.results:
            assert r.network_id == observed.network_id
            assert r.data_version == observed.data_version
            assert r.baseline_business_cost == registry.baseline_business_cost
            if r.is_feasible:
                assert r.performance_impact == pytest.approx(
                    r.disrupted_business_cost - r.baseline_business_cost, abs=1e-3
                )
                assert r.rei is not None
            else:
                assert r.performance_impact is None and r.rei is None

        if registry.rei_status == REIStatus.COMPUTED:
            top = registry.results[0]
            assert top.rei == pytest.approx(1.0, abs=1e-9)
            assert top.rank == 1
            ranked = [r.performance_impact for r in registry.results
                      if r.performance_impact is not None]
            assert ranked == sorted(ranked, reverse=True)

        # --- Stage 6: observed state survived every hypothetical ------------
        assert observed.model_dump_json() == observed_snapshot, (
            "observed baseline was mutated somewhere in the pipeline"
        )
