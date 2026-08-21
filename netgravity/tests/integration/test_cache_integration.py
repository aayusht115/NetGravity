"""
Phase 2 — §22: the orchestrator must not bypass the REI cache.

Every count here is of REAL solver invocations, taken by wrapping
`optimization.milp.solve`. Asserting on what the cache reports about itself
would be circular.

The property under test is not "caching is fast" but "the control plane routes
through the service that owns caching". A path that reached
`assess_network_resilience` directly would still be correct, still be
deterministic, and would silently re-run 1 + N solves on every request.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.audit import events
from netgravity.orchestrator.engines.deterministic import REIClient
from netgravity.orchestrator.schemas.requests import Intent, OrchestratorRequest
from netgravity.resilience.service import REIService

from .conftest import build_delhi_network, flood_signal

TOL = 1e-9


class SolveCounter:
    """Counts real MILP invocations, recording what each was for."""

    def __init__(self) -> None:
        self.scenario_ids: List[str] = []

    def __call__(self, network: Any, config: Any = None, scenario_id: Any = None):
        from netgravity.optimization.milp import solve
        self.scenario_ids.append(scenario_id or "(none)")
        return solve(network, config=config, scenario_id=scenario_id)

    @property
    def count(self) -> int:
        return len(self.scenario_ids)

    def reset(self) -> None:
        self.scenario_ids.clear()


def _orchestrator(network=None):
    counter = SolveCounter()
    orch = build_orchestrator(network=network or build_delhi_network(), enable_llm=False)
    orch.services["rei"] = REIClient(service=REIService(solve_fn=counter))
    return orch, counter


def _risk_run(orch, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input="Flood warning for Delhi NCR.",
        explicit_intent=Intent.EXTERNAL_EVENT,
        external_signal=flood_signal(),
        disable_llm=True, **kwargs,
    ))


# ===========================================================================
# §22 — miss then hit
# ===========================================================================

class TestCacheThroughOrchestrator:

    def test_first_assessment_misses_and_computes(self):
        orch, counter = _orchestrator()
        response = _risk_run(orch, request_id="first")

        registry = response.results["resilience"]
        assert registry["served_from_cache"] is False
        # 1 baseline + 4 eligible nodes + 1 service diagnostic for the
        # infeasible plant = 6.
        assert counter.count == 6
        assert counter.scenario_ids[0] == "REI_BASELINE"
        assert registry["n_milp_solves"] == 6

    def test_second_identical_assessment_executes_no_solves(self):
        orch, counter = _orchestrator()
        _risk_run(orch, request_id="first")
        counter.reset()

        response = _risk_run(orch, request_id="second")

        assert counter.count == 0, (
            f"the orchestrator bypassed the cache: {counter.scenario_ids}"
        )
        assert response.results["resilience"]["served_from_cache"] is True
        assert response.results["resilience"]["n_milp_solves"] == 0

    def test_the_cached_run_produces_identical_risk_numbers(self):
        orch, counter = _orchestrator()
        first = _risk_run(orch, request_id="first")
        counter.reset()
        second = _risk_run(orch, request_id="second")

        assert counter.count == 0
        assert first.risk["results"][0]["rei"] == second.risk["results"][0]["rei"]
        assert first.risk["results"][0]["risk_factor"] == pytest.approx(
            second.risk["results"][0]["risk_factor"], abs=TOL
        )
        assert second.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)

    def test_the_batch_identity_is_preserved_across_the_hit(self):
        orch, _ = _orchestrator()
        first = _risk_run(orch, request_id="first")
        second = _risk_run(orch, request_id="second")

        assert second.results["resilience"]["batch_id"] == \
            first.results["resilience"]["batch_id"]
        assert second.results["resilience"]["material_fingerprint"] == \
            first.results["resilience"]["material_fingerprint"]

    def test_idempotency_and_caching_are_different_mechanisms(self):
        """
        Reusing a request_id short-circuits at the execution store and never
        reaches REI at all. Reusing the NETWORK reaches REI and hits the cache.
        Both avoid work; only the second proves anything about the cache.
        """
        orch, counter = _orchestrator()
        _risk_run(orch, request_id="same")
        counter.reset()

        duplicate = _risk_run(orch, request_id="same")
        assert counter.count == 0
        assert any("Duplicate request_id" in w for w in duplicate.warnings)

        fresh = _risk_run(orch, request_id="different")
        assert counter.count == 0, "a genuinely new request still hit the cache"
        assert not any("Duplicate request_id" in w for w in fresh.warnings)


# ===========================================================================
# §22 — invalidation is driven by material change
# ===========================================================================

class TestCacheInvalidation:

    def test_a_material_change_forces_recomputation(self):
        orch, counter = _orchestrator()
        _risk_run(orch, request_id="first")
        counter.reset()

        # Capacity is material to the optimum, so the fingerprint moves.
        orch.register_network(build_delhi_network(delhi_capacity=120.0),
                              label="capacity change")
        response = _risk_run(orch, request_id="after-change")

        assert counter.count > 0, "a material change must not be served from cache"
        assert response.results["resilience"]["served_from_cache"] is False

    def test_a_cosmetic_change_does_not(self):
        """
        Renaming a facility changes `data_version` but not the optimum. The
        material fingerprint is what governs the cache, so this is still a hit.
        """
        orch, counter = _orchestrator()
        _risk_run(orch, request_id="first")
        counter.reset()

        renamed = build_delhi_network()
        facilities = [
            f.model_copy(update={"name": "Delhi NCR Distribution Centre (renamed)"})
            if f.id == "DC_DELHI" else f
            for f in renamed.facilities
        ]
        renamed = renamed.model_copy(update={"facilities": facilities})
        renamed = renamed.model_copy(update={"data_version": renamed.compute_data_version()})

        orch.register_network(renamed, label="renamed")
        response = _risk_run(orch, request_id="after-rename")

        assert counter.count == 0, "a rename must not cost 1 + N solves"
        assert response.results["resilience"]["served_from_cache"] is True

    def test_explicit_invalidation_is_honoured(self):
        orch, counter = _orchestrator()
        _risk_run(orch, request_id="first")
        counter.reset()

        removed = orch.services["rei"].service.invalidate_all("operator forced refresh")
        assert removed >= 1

        response = _risk_run(orch, request_id="after-invalidate")
        assert counter.count > 0
        assert response.results["resilience"]["served_from_cache"] is False


# ===========================================================================
# §22 / §26 — cache behaviour is observable
# ===========================================================================

class TestCacheObservability:

    def test_the_rei_lookup_event_reports_cache_state(self):
        orch, _ = _orchestrator()
        first = _risk_run(orch, request_id="first")
        second = _risk_run(orch, request_id="second")

        [cold] = orch.get_trace(first.execution_id).events_of(events.REI_LOOKUP)
        [warm] = orch.get_trace(second.execution_id).events_of(events.REI_LOOKUP)

        assert cold.detail["served_from_cache"] is False
        assert cold.detail["milp_solves"] == 6
        assert warm.detail["served_from_cache"] is True
        assert warm.detail["milp_solves"] == 0
        assert warm.detail["batch_id"] == cold.detail["batch_id"]

    def test_service_stats_reconcile_with_the_measured_solves(self):
        orch, counter = _orchestrator()
        _risk_run(orch, request_id="first")
        _risk_run(orch, request_id="second")
        _risk_run(orch, request_id="third")

        stats = orch.services["rei"].service.health()["service"]
        assert stats["batches_requested"] == 3
        assert stats["batches_computed"] == 1
        assert stats["batches_served_from_cache"] == 2
        assert stats["cache_hit_rate"] == pytest.approx(2 / 3, abs=1e-4)
        assert stats["milp_solves_executed"] == counter.count == 6
        assert stats["milp_solves_avoided"] == 12
