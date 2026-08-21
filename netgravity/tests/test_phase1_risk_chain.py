"""
NetGravity — Phase 1: deterministic risk chain (MILP → REI → P → RF).

Validates the full chain end-to-end plus every documented failure path.

    Network Snapshot → Baseline MILP → REI Batch → REI Registry
                                                        ↓
                             External Event Probability ↓
                                                   ↘    ↓
                                                  RF Calculator
                                                        ↓
                                                  Risk Assessment

Sections map to the Phase 1 acceptance list (A–P) plus §18 invariants.

Everything runs offline. No LLM participates in any calculation here, which the
suite asserts directly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List, Optional

import pytest

from netgravity.orchestrator.risk.risk_assessment import (
    assess_event_risk,
    lookup_rei,
    map_event_to_nodes,
)
from netgravity.orchestrator.risk.risk_factor import RF_FORMULA, compute_risk_factor
from netgravity.orchestrator.schemas.requests import EventSeverity, ExternalSignal
from netgravity.orchestrator.schemas.risk import (
    RFNotComputableReason,
    RFStatus,
)
from netgravity.resilience.persistence import (
    JsonFilePersistenceBackend,
    NullPersistenceBackend,
)
from netgravity.resilience.registry_store import REIRegistryStore
from netgravity.resilience.rei import assess_network_resilience
from netgravity.resilience.service import REIService
from netgravity.schemas.network import NodeRole
from netgravity.schemas.resilience import DisruptionConfig
from netgravity.schemas.results import (
    CalculationStatus,
    FacilityResilienceRegistry,
    REIBatchStatus,
    SolverStatus,
)
from netgravity.tests.test_rei_v1 import (
    DC_ALL,
    DC_ONLY,
    build_single_source_network,
    build_three_dc_network,
)

TOL = 1e-9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal(
    *,
    probability: Optional[float] = 0.7,
    nodes: Optional[List[str]] = None,
    location: str = "",
    severity: EventSeverity = EventSeverity.SEVERE,
) -> ExternalSignal:
    return ExternalSignal(
        event_type="FLOOD",
        location=location,
        severity=severity,
        event_probability=probability,
        probability_basis="stated by source" if probability is not None else None,
        confidence=0.9,
        source="met_office",
        affected_entity_ids=list(nodes or []),
    )


def _registry(network=None, snapshot_id: str = "snap_v17", config=DC_ALL):
    net = network or build_three_dc_network()
    return assess_network_resilience(net, net.config, config, snapshot_id=snapshot_id)


# ===========================================================================
# A — MILP baseline
# ===========================================================================

class TestBaseline:

    def test_baseline_solves_and_is_deterministic(self):
        net = build_three_dc_network()
        a = _registry(net)
        b = _registry(net)
        # baseline = 100 fixed + 100 × (1 inbound + 2 outbound) = 400
        assert a.baseline_business_cost == pytest.approx(400.0, abs=1e-3)
        assert a.baseline_business_cost == pytest.approx(b.baseline_business_cost, abs=1e-9)
        assert a.baseline_solver_status == SolverStatus.OPTIMAL

    def test_baseline_reused_across_the_batch(self):
        net = build_three_dc_network()
        calls: List[str] = []

        def counting(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            calls.append(scenario_id or "?")
            return solve(network, config=config, scenario_id=scenario_id)

        reg = assess_network_resilience(net, net.config, DC_ALL, solve_fn=counting)
        assert len([c for c in calls if c == "REI_BASELINE"]) == 1
        assert len(calls) == 1 + reg.n_facilities_assessed

    def test_baseline_inputs_unchanged_after_rei(self):
        net = build_three_dc_network()
        before = net.model_dump_json()
        _registry(net)
        assert net.model_dump_json() == before


# ===========================================================================
# B / C — REI single and multi node (hand-calculable)
# ===========================================================================

class TestREIValues:

    def test_single_node_hand_calculation(self):
        """C0 = 400, DC_HUB failure → C1 = 600 ⇒ PI = 200, EI = 200, REI = 1."""
        reg = _registry()
        hub = reg.get("DC_HUB")
        assert hub.baseline_business_cost == pytest.approx(400.0, abs=1e-3)
        assert hub.disrupted_business_cost == pytest.approx(600.0, abs=1e-3)
        assert hub.performance_impact == pytest.approx(200.0, abs=1e-3)
        assert hub.economic_impact == pytest.approx(200.0, abs=1e-3)
        assert hub.cost_impact_pct == pytest.approx(50.0, abs=1e-3)
        assert hub.rei == pytest.approx(1.0, abs=TOL)

    def test_multi_node_normalisation(self):
        """DC_HUB PI=200 → REI 1; DC_MID and DC_RED PI=0 → REI 0."""
        reg = _registry()
        assert reg.get("DC_HUB").rei == pytest.approx(1.0, abs=TOL)
        assert reg.get("DC_MID").rei == pytest.approx(0.0, abs=TOL)
        assert reg.get("DC_RED").rei == pytest.approx(0.0, abs=TOL)
        for row in reg.results:
            if row.rei is not None:
                assert 0.0 <= row.rei <= 1.0


# ===========================================================================
# D / E — negative PI and all-zero
# ===========================================================================

class TestNegativeAndZero:

    def test_negative_pi_visible_but_zero_exposure(self):
        """
        Case-16 contains a disruption that REDUCES cost. PI stays signed and
        negative; EI and REI are zero.
        """
        from netgravity.tests.fixtures.case16_synthetic import build_case16_network
        net = build_case16_network()
        reg = assess_network_resilience(net, net.config, DisruptionConfig())

        negatives = [r for r in reg.results
                     if r.performance_impact is not None and r.performance_impact < 0]
        assert negatives, "expected a cost-reducing disruption in Case-16"
        for row in negatives:
            assert row.performance_impact < 0, "signed PI must remain visible"
            assert row.economic_impact == pytest.approx(0.0, abs=TOL)
            assert row.rei == pytest.approx(0.0, abs=TOL)

    def test_all_zero_impact_gives_zero_rei_without_dividing(self):
        """Every DC unused at baseline ⇒ EI_max = 0 ⇒ all REI = 0."""
        net = build_three_dc_network()
        # Make the hub unattractive so nothing is uniquely valuable: with equal
        # rates, losing any one DC costs nothing.
        equal = build_three_dc_network(hub_rate=2.0, mid_rate=2.0, red_rate=2.0)
        reg = assess_network_resilience(equal, equal.config, DC_ALL)
        assert all(r.rei == pytest.approx(0.0, abs=TOL)
                   for r in reg.results if r.rei is not None)
        assert reg.max_performance_impact == pytest.approx(0.0, abs=1e-3)


# ===========================================================================
# F / G / H — infeasible, time limit, solver exception
# ===========================================================================

class TestSolverStatusHandling:

    def test_infeasible_produces_no_rei_and_batch_continues(self):
        net = build_single_source_network()
        reg = assess_network_resilience(net, net.config, DC_ALL, snapshot_id="snap_v17")

        only = reg.get("DC_ONLY")
        assert only.solver_status == SolverStatus.INFEASIBLE
        assert only.rei is None and only.performance_impact is None
        assert only.economic_impact is None
        assert only.calculation_status == CalculationStatus.INFEASIBLE

        spare = reg.get("DC_SPARE")
        assert spare.calculation_status == CalculationStatus.OK
        assert reg.batch_status == REIBatchStatus.COMPLETED_WITH_ERRORS

    def test_time_limit_never_produces_a_valid_rei(self):
        """An unverified incumbent must not become exposure."""
        net = build_three_dc_network()

        def time_limited(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            result = solve(network, config=config, scenario_id=scenario_id)
            if scenario_id == "REI_DISRUPT_DC_HUB":
                result.solver.status = SolverStatus.TIME_LIMIT
            return result

        reg = assess_network_resilience(net, net.config, DC_ALL, solve_fn=time_limited)
        hub = reg.get("DC_HUB")
        assert hub.calculation_status == CalculationStatus.TIME_LIMIT
        assert hub.rei is None, "a time-limited incumbent is not a valid REI"
        assert hub.performance_impact is None
        assert "time limit" in (hub.failure_reason or "").lower()
        # Other nodes are unaffected.
        assert reg.get("DC_MID").calculation_status == CalculationStatus.OK

    def test_solver_exception_is_isolated_and_never_cached_as_valid(self):
        service = REIService()
        net = build_three_dc_network()

        def exploding(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            if scenario_id == "REI_DISRUPT_DC_MID":
                raise RuntimeError("simulated solver crash")
            return solve(network, config=config, scenario_id=scenario_id)

        service.solve_fn = exploding
        reg = service.get_or_compute(net, net.config, DC_ALL)

        failed = reg.get("DC_MID")
        assert failed.calculation_status == CalculationStatus.ERROR
        assert failed.rei is None
        assert reg.get("DC_HUB").rei is not None, "other nodes still valid"
        assert reg.batch_status == REIBatchStatus.COMPLETED_WITH_ERRORS

    def test_totally_failed_batch_is_not_cached(self):
        service = REIService()
        net = build_three_dc_network()

        def all_fail(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            if scenario_id == "REI_BASELINE":
                return solve(network, config=config, scenario_id=scenario_id)
            raise RuntimeError("everything fails")

        service.solve_fn = all_fail
        reg = service.get_or_compute(net, net.config, DC_ALL)
        assert reg.batch_status == REIBatchStatus.FAILED
        assert not service.is_valid_for(net, net.config, DC_ALL), (
            "a failed batch must not be served back on the next request"
        )


# ===========================================================================
# I / J — cache and invalidation (with explicit solve counting)
# ===========================================================================

class TestCacheAndInvalidation:

    def test_second_identical_run_executes_zero_solves(self):
        service = REIService()
        net = build_three_dc_network()
        calls: List[str] = []

        def counting(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            calls.append(scenario_id or "?")
            return solve(network, config=config, scenario_id=scenario_id)

        service.solve_fn = counting
        first = service.get_or_compute(net, net.config, DC_ALL)
        first_calls = len(calls)
        assert first_calls == 1 + first.n_facilities_assessed

        calls.clear()
        second = service.get_or_compute(net, net.config, DC_ALL)
        assert calls == [], "a cache hit must execute zero MILP solves"
        assert second.served_from_cache is True
        assert second.batch_id == first.batch_id

    @pytest.mark.parametrize("mutate,label", [
        (lambda n: n.model_copy(update={
            "demands": [d.model_copy(update={"quantity": d.quantity * 2})
                        for d in n.demands]}), "demand"),
        (lambda n: n.model_copy(update={
            "facilities": [f.model_copy(update={"capacity_units_per_period": 111.0})
                           if f.id == "DC_HUB" else f for f in n.facilities]}), "capacity"),
        (lambda n: n.model_copy(update={
            "lanes": [ln.model_copy(update={"rate_per_unit": ln.rate_per_unit + 3})
                      for ln in n.lanes]}), "transport cost"),
    ])
    def test_material_change_invalidates_and_recomputes(self, mutate, label):
        service = REIService()
        net = build_three_dc_network()
        service.get_or_compute(net, net.config, DC_ALL)

        changed = mutate(net)
        assert not service.is_valid_for(changed, changed.config, DC_ALL), (
            f"{label} change must invalidate REI"
        )
        recomputed = service.get_or_compute(changed, changed.config, DC_ALL)
        assert recomputed.served_from_cache is False

    def test_cosmetic_change_keeps_rei_reusable(self):
        service = REIService()
        net = build_three_dc_network()
        service.get_or_compute(net, net.config, DC_ALL)

        renamed = net.model_copy(update={
            "facilities": [f.model_copy(update={"name": "Renamed", "tags": ["x"]})
                           if f.id == "DC_HUB" else f for f in net.facilities]})
        served = service.get_or_compute(renamed, renamed.config, DC_ALL)
        assert served.served_from_cache is True, (
            "a cosmetic edit must not force 1 + N solves"
        )


class TestPersistence:

    def test_null_backend_is_not_durable(self):
        store = REIRegistryStore()
        assert store.is_durable is False
        assert store.stats()["durable"] is False

    def test_batch_survives_a_simulated_restart(self):
        """The point of persistence: no recomputation after a process restart."""
        with tempfile.TemporaryDirectory() as tmp:
            backend = JsonFilePersistenceBackend(tmp)
            net = build_three_dc_network()

            service_a = REIService(REIRegistryStore(backend=backend))
            assert service_a.store.is_durable is True
            first = service_a.get_or_compute(net, net.config, DC_ALL)
            assert first.served_from_cache is False

            # A brand-new store and service — as after a restart.
            calls: List[str] = []

            def counting(network, config, scenario_id):
                from netgravity.optimization.milp import solve
                calls.append(scenario_id or "?")
                return solve(network, config=config, scenario_id=scenario_id)

            service_b = REIService(
                REIRegistryStore(backend=JsonFilePersistenceBackend(tmp)),
                solve_fn=counting,
            )
            restored = service_b.get_or_compute(net, net.config, DC_ALL)

            assert calls == [], "a restart must not recompute a persisted batch"
            assert restored.served_from_cache is True
            assert restored.batch_id == first.batch_id
            assert service_b.store.stats()["backend_restores"] == 1

    def test_invalidation_removes_the_durable_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = JsonFilePersistenceBackend(tmp)
            net = build_three_dc_network()
            service = REIService(REIRegistryStore(backend=backend))
            service.get_or_compute(net, net.config, DC_ALL)
            assert len(backend.keys()) == 1

            service.invalidate_for(net, "operator refresh")
            assert backend.keys() == [], (
                "a stale batch must not be resurrected by a restart"
            )

    def test_corrupt_record_is_ignored_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = JsonFilePersistenceBackend(tmp)
            net = build_three_dc_network()
            service = REIService(REIRegistryStore(backend=backend))
            service.get_or_compute(net, net.config, DC_ALL)

            for path in Path(tmp).glob("*.json"):
                path.write_text("{ this is not valid json", encoding="utf-8")

            fresh = REIService(REIRegistryStore(backend=JsonFilePersistenceBackend(tmp)))
            assert fresh.get_or_compute(net, net.config, DC_ALL).served_from_cache is False


# ===========================================================================
# K — RF unit values
# ===========================================================================

class TestRFValues:

    @pytest.mark.parametrize("p,rei,expected", [
        (0.7, 0.8, 0.94),
        (0.0, 0.8, 0.80),
        (1.0, 0.8, 1.00),
        (0.7, 0.0, 0.70),
        (0.0, 0.0, 0.00),
        (1.0, 1.0, 1.00),
        (0.5, 0.5, 0.75),
    ])
    def test_exact_values(self, p, rei, expected):
        result = compute_risk_factor(p, rei)
        assert result.status == RFStatus.COMPUTED
        assert result.risk_factor == pytest.approx(expected, abs=TOL)
        assert result.formula == RF_FORMULA

    def test_zero_probability_is_a_value_not_missing(self):
        """P = 0 is an explicit statement; it must compute, not refuse."""
        result = compute_risk_factor(0.0, 0.8)
        assert result.status == RFStatus.COMPUTED
        assert result.risk_factor == pytest.approx(0.8)

        missing = compute_risk_factor(None, 0.8)
        assert missing.status == RFStatus.NOT_COMPUTABLE
        assert missing.not_computable_reason == RFNotComputableReason.NO_EVENT_PROBABILITY


# ===========================================================================
# L — RF missing evidence
# ===========================================================================

class TestRFMissingEvidence:

    def test_missing_probability(self):
        reg = _registry()
        out = assess_event_risk(_signal(probability=None, nodes=["DC_HUB"]), reg,
                                expected_snapshot_id="snap_v17")
        assert out.results == []
        assert out.not_computable[0].not_computable_reason == (
            RFNotComputableReason.NO_EVENT_PROBABILITY)
        assert out.not_computable[0].risk_factor is None

    def test_missing_rei_for_the_mapped_node(self):
        """The node was assessed but produced no REI (infeasible)."""
        net = build_single_source_network()
        reg = assess_network_resilience(net, net.config, DC_ALL, snapshot_id="snap_v17")
        out = assess_event_risk(_signal(nodes=["DC_ONLY"]), reg,
                                expected_snapshot_id="snap_v17")
        assert out.results == []
        assert out.not_computable[0].not_computable_reason == RFNotComputableReason.NO_REI

    def test_stale_rei(self):
        reg = _registry(snapshot_id="snap_v17")
        out = assess_event_risk(_signal(nodes=["DC_HUB"]), reg,
                                expected_snapshot_id="snap_v18")
        assert out.results == []
        assert out.not_computable[0].not_computable_reason == RFNotComputableReason.STALE_REI
        assert "snap_v17" in out.not_computable[0].notes[-1]
        assert "snap_v18" in out.not_computable[0].notes[-1]

    def test_severity_without_probability_never_substitutes(self):
        """The core correction: SEVERE is not a probability."""
        reg = _registry()
        signal = _signal(probability=None, nodes=["DC_HUB"],
                         severity=EventSeverity.CRITICAL)
        out = assess_event_risk(signal, reg, expected_snapshot_id="snap_v17")
        assert out.max_risk_factor is None
        assert out.not_computable[0].not_computable_reason == (
            RFNotComputableReason.NO_EVENT_PROBABILITY)
        # Confidence is high but is likewise not a probability.
        assert signal.confidence == pytest.approx(0.9)
        assert signal.event_probability is None


# ===========================================================================
# M — node mapping
# ===========================================================================

class TestNodeMapping:

    def test_explicit_mapping_resolves(self):
        reg = _registry()
        mapping = map_event_to_nodes(_signal(nodes=["DC_HUB"]), reg)
        assert mapping.resolved == ["DC_HUB"]
        assert mapping.method == "explicit"

    def test_correct_rei_is_retrieved_for_the_mapped_node(self):
        reg = _registry()
        out = assess_event_risk(_signal(probability=0.7, nodes=["DC_HUB"]), reg,
                                expected_snapshot_id="snap_v17")
        row = out.results[0]
        assert row.facility_id == "DC_HUB"
        assert row.rei == pytest.approx(1.0, abs=TOL)
        # RF = 0.7 + 1.0 - 0.7 = 1.0
        assert row.risk_factor == pytest.approx(1.0, abs=TOL)

    def test_unknown_node_is_not_computable(self):
        reg = _registry()
        out = assess_event_risk(_signal(nodes=["DC_ATLANTIS"]), reg,
                                expected_snapshot_id="snap_v17")
        assert out.results == []
        assert out.not_computable[0].not_computable_reason == (
            RFNotComputableReason.NODE_MAPPING_UNAVAILABLE)

    def test_unknown_location_is_not_computable(self):
        reg = _registry()
        out = assess_event_risk(_signal(nodes=[], location="Atlantis"), reg,
                                expected_snapshot_id="snap_v17")
        assert out.not_computable[0].not_computable_reason == (
            RFNotComputableReason.NODE_MAPPING_UNAVAILABLE)

    def test_event_never_broadcasts_across_the_whole_network(self):
        """A flood in one place says nothing about another place's probability."""
        reg = _registry()
        out = assess_event_risk(_signal(nodes=["DC_HUB"]), reg,
                                expected_snapshot_id="snap_v17")
        assert {r.facility_id for r in out.results} == {"DC_HUB"}

    def test_lookup_reports_reason_for_unusable_rei(self):
        net = build_single_source_network()
        reg = assess_network_resilience(net, net.config, DC_ALL, snapshot_id="s")
        result = lookup_rei("DC_ONLY", reg, expected_snapshot_id="s")
        assert result.is_usable is False
        assert result.unavailable_reason == RFNotComputableReason.NO_REI
        assert "INFEASIBLE" in result.detail


# ===========================================================================
# N — end-to-end success
# ===========================================================================

class TestEndToEndSuccess:

    def test_full_chain_with_known_inputs(self):
        """
        snapshot → MILP → REI batch → registry → event(P=0.7) → node map →
        REI lookup → RF, with every number hand-checkable.

            C0 = 400, DC_HUB failure C1 = 600 ⇒ PI 200 ⇒ REI 1.0
            RF = 0.7 + 1.0 − 0.7×1.0 = 1.0
        """
        net = build_three_dc_network()
        service = REIService()
        registry = service.get_or_compute(net, net.config, DC_ALL,
                                          snapshot_id="snap_v17")

        assert registry.batch_status == REIBatchStatus.COMPLETED
        assert registry.get("DC_HUB").rei == pytest.approx(1.0, abs=TOL)

        out = assess_event_risk(
            _signal(probability=0.7, nodes=["DC_HUB"]),
            registry, expected_snapshot_id="snap_v17",
        )
        assert len(out.results) == 1
        rf = out.results[0]
        assert rf.status == RFStatus.COMPUTED
        assert rf.likelihood == pytest.approx(0.7)
        assert rf.rei == pytest.approx(1.0)
        assert rf.risk_factor == pytest.approx(1.0, abs=TOL)
        assert rf.formula == RF_FORMULA
        # Full provenance survives the chain.
        assert "external_signal:met_office" in rf.provenance["likelihood"]
        assert registry.batch_id in rf.provenance["rei"]

    def test_documented_delhi_example(self):
        """P=0.7, REI=0.8 ⇒ RF = 0.94, exactly as specified."""
        result = compute_risk_factor(0.7, 0.8, facility_id="DC_DELHI")
        assert result.risk_factor == pytest.approx(0.94, abs=TOL)

    def test_mid_exposure_node_end_to_end(self):
        """A node with REI 0 still computes: RF collapses to P."""
        registry = _registry()
        out = assess_event_risk(_signal(probability=0.6, nodes=["DC_MID"]),
                                registry, expected_snapshot_id="snap_v17")
        rf = out.results[0]
        assert rf.rei == pytest.approx(0.0)
        assert rf.risk_factor == pytest.approx(0.6, abs=TOL)


# ===========================================================================
# O — end-to-end failure paths
# ===========================================================================

class TestEndToEndFailures:

    def test_infeasible_rei_yields_structured_not_computable(self):
        net = build_single_source_network()
        registry = assess_network_resilience(net, net.config, DC_ALL,
                                             snapshot_id="snap_v17")
        out = assess_event_risk(_signal(nodes=["DC_ONLY"]), registry,
                                expected_snapshot_id="snap_v17")
        assert out.max_risk_factor is None
        assert out.not_computable[0].risk_factor is None
        assert out.not_computable[0].not_computable_reason == RFNotComputableReason.NO_REI

    def test_time_limited_rei_yields_not_computable(self):
        net = build_three_dc_network()

        def time_limited(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            result = solve(network, config=config, scenario_id=scenario_id)
            if scenario_id == "REI_DISRUPT_DC_HUB":
                result.solver.status = SolverStatus.TIME_LIMIT
            return result

        registry = assess_network_resilience(net, net.config, DC_ALL,
                                             solve_fn=time_limited,
                                             snapshot_id="snap_v17")
        out = assess_event_risk(_signal(nodes=["DC_HUB"]), registry,
                                expected_snapshot_id="snap_v17")
        assert out.results == []
        assert out.not_computable[0].not_computable_reason == RFNotComputableReason.NO_REI

    def test_solver_exception_yields_not_computable(self):
        net = build_three_dc_network()

        def exploding(network, config, scenario_id):
            from netgravity.optimization.milp import solve
            if scenario_id == "REI_DISRUPT_DC_HUB":
                raise RuntimeError("boom")
            return solve(network, config=config, scenario_id=scenario_id)

        registry = assess_network_resilience(net, net.config, DC_ALL,
                                             solve_fn=exploding, snapshot_id="s")
        out = assess_event_risk(_signal(nodes=["DC_HUB"]), registry,
                                expected_snapshot_id="s")
        assert out.results == []
        assert out.not_computable[0].not_computable_reason == RFNotComputableReason.NO_REI

    def test_nothing_is_ever_fabricated_on_any_failure_path(self):
        """Across every failure mode: no zero, no invented P, no invented RF."""
        reg = _registry()
        cases = [
            _signal(probability=None, nodes=["DC_HUB"]),        # no P
            _signal(probability=0.7, nodes=["DC_GHOST"]),       # bad mapping
            _signal(probability=0.7, nodes=[], location="?"),   # no mapping
        ]
        for signal in cases:
            out = assess_event_risk(signal, reg, expected_snapshot_id="snap_v17")
            assert out.max_risk_factor is None
            assert out.results == []
            for row in out.not_computable:
                assert row.risk_factor is None, "RF must never be fabricated"
                assert row.status == RFStatus.NOT_COMPUTABLE
                assert row.not_computable_reason is not None
                assert row.notes, "every refusal must carry a reason"

    def test_failure_paths_do_not_raise(self):
        reg = _registry()
        # None of these should throw; all should return structured refusals.
        for signal in (_signal(probability=None), _signal(nodes=["nope"]),
                       _signal(nodes=[], location="")):
            out = assess_event_risk(signal, reg, expected_snapshot_id="snap_v17")
            assert out is not None


# ===========================================================================
# §18 — invariants
# ===========================================================================

class TestInvariants:

    @pytest.mark.parametrize("p", [0.0, 0.1, 0.33, 0.5, 0.77, 1.0])
    @pytest.mark.parametrize("rei", [0.0, 0.2, 0.5, 0.9, 1.0])
    def test_rf_bounds_and_dominance(self, p, rei):
        """RF ∈ [0,1], and RF ≥ P and RF ≥ REI for all valid inputs."""
        rf = compute_risk_factor(p, rei).risk_factor
        assert 0.0 <= rf <= 1.0
        assert rf >= p - TOL, f"RF {rf} < P {p}"
        assert rf >= rei - TOL, f"RF {rf} < REI {rei}"

    def test_rf_is_deterministic(self):
        a = compute_risk_factor(0.37, 0.61).risk_factor
        b = compute_risk_factor(0.37, 0.61).risk_factor
        assert a == b

    def test_rei_is_deterministic_for_identical_inputs(self):
        net = build_three_dc_network()
        a = assess_network_resilience(net, net.config, DC_ALL)
        b = assess_network_resilience(net, net.config, DC_ALL)
        assert [r.facility_id for r in a.results] == [r.facility_id for r in b.results]
        for x, y in zip(a.results, b.results):
            if x.rei is None:
                assert y.rei is None
            else:
                assert x.rei == pytest.approx(y.rei, abs=TOL)

    def test_every_valid_rei_is_in_unit_interval(self):
        from netgravity.tests.fixtures.case16_synthetic import build_case16_network
        for net in (build_three_dc_network(), build_case16_network()):
            reg = assess_network_resilience(net, net.config, DisruptionConfig())
            for row in reg.results:
                if row.rei is not None:
                    assert 0.0 <= row.rei <= 1.0

    def test_probability_range_is_enforced(self):
        from netgravity.orchestrator.exceptions import ValidationFailureError
        for bad in (1.2, -0.1):
            with pytest.raises(ValidationFailureError):
                compute_risk_factor(bad, 0.5)
            with pytest.raises(ValueError):
                ExternalSignal(event_type="X", event_probability=bad)


# ===========================================================================
# Orchestrator integration (no maths in the control plane)
# ===========================================================================

class TestOrchestratorIntegration:

    def test_end_to_end_through_the_orchestrator(self):
        from netgravity.orchestrator import build_orchestrator
        from netgravity.orchestrator.schemas.requests import OrchestratorRequest

        from netgravity.tests.fixtures.case16_synthetic import build_case16_network
        orch = build_orchestrator(network=build_case16_network(), enable_llm=False)

        # DC_EAST has REI 0 in Case-16 (cost-reducing disruption), so RF = P.
        resp = orch.run_sync(OrchestratorRequest(
            input="flooding near DC_EAST",
            external_signal=_signal(probability=0.7, nodes=["DC_EAST"]),
        ))
        assert resp.risk is not None
        assert resp.risk["results"], "RF must be computed when P and REI are available"
        rf = resp.risk["results"][0]
        assert rf["facility_id"] == "DC_EAST"
        assert rf["status"] == "COMPUTED"
        assert rf["likelihood"] == pytest.approx(0.7)
        assert 0.0 <= rf["risk_factor"] <= 1.0

    def test_orchestrator_reports_not_computable_without_probability(self):
        from netgravity.orchestrator import build_orchestrator
        from netgravity.orchestrator.schemas.requests import OrchestratorRequest
        from netgravity.tests.fixtures.case16_synthetic import build_case16_network

        orch = build_orchestrator(network=build_case16_network(), enable_llm=False)
        resp = orch.run_sync(OrchestratorRequest(
            input="severe flooding near DC_EAST",
            external_signal=_signal(probability=None, nodes=["DC_EAST"]),
        ))
        assert resp.risk["results"] == []
        assert resp.risk["not_computable"]
        assert resp.risk["not_computable"][0]["not_computable_reason"] == (
            "NO_EVENT_PROBABILITY")

    def test_orchestrator_contains_no_risk_mathematics(self):
        """The control plane coordinates; it does not compute."""
        import inspect
        from netgravity.orchestrator.core import orchestrator as orch_mod
        src = inspect.getsource(orch_mod)
        # The RF formula must not appear anywhere in the orchestrator core.
        assert "P + REI" not in src
        assert "* rei" not in src.lower().replace(" ", " ")

    def test_no_llm_participates_in_the_deterministic_chain(self):
        import inspect
        from netgravity.orchestrator.risk import risk_assessment, risk_factor
        from netgravity.resilience import rei as rei_mod
        for module in (risk_factor, risk_assessment, rei_mod):
            src = inspect.getsource(module).lower()
            assert "gateway.generate" not in src
            assert "llmgateway" not in src
