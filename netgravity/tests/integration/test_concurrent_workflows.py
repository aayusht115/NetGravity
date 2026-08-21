"""
Phase 2 — §24: concurrent independent requests.

Three requests in flight at once, as §24 describes: a Delhi risk assessment, a
Mumbai risk assessment, and a Kolkata what-if. What must hold:

    no shared mutable state corruption
    baseline unchanged
    scenario overrides do not leak between requests
    REI registry stays consistent
    execution ids stay distinct
    evidence stays with the request that produced it

Concurrency is exercised two ways, because they fail differently:

    asyncio.gather   several `orchestrator.run()` coroutines on one loop. This
                     is how the API surface actually serves concurrent
                     requests, and solver work reaches the shared thread pool.
    ThreadPoolExecutor  several `run_sync` calls on separate OS threads, which
                     genuinely races the locks in the stores.

No distributed infrastructure is introduced; this uses the existing execution
context and state model.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)

from .conftest import build_delhi_network, flood_signal

TOL = 1e-9

PLANNER = Actor(actor_id="planner_1", role=ActorRole.PLANNER)


def _risk_request(node: str, probability: float, request_id: str) -> OrchestratorRequest:
    return OrchestratorRequest(
        request_id=request_id,
        input=f"Flood warning affecting {node}.",
        explicit_intent=Intent.EXTERNAL_EVENT,
        external_signal=flood_signal(probability=probability, nodes=[node],
                                     location=node),
        disable_llm=True,
    )


def _scenario_request(node: str, delta: float, request_id: str) -> OrchestratorRequest:
    return OrchestratorRequest(
        request_id=request_id,
        input=f"What if {node} capacity changes by {delta}?",
        explicit_intent=Intent.SCENARIO_ANALYSIS,
        explicit_scenarios=[ScenarioIntentSpec(
            action=ScenarioActionType.CHANGE_CAPACITY,
            facility_ids=[node], capacity_delta_units=delta,
            label=f"{node} {delta:+.0f}",
        )],
        actor=PLANNER, disable_llm=True,
    )


#: The §24 trio.
def _the_three_requests() -> List[OrchestratorRequest]:
    return [
        _risk_request("DC_DELHI", 0.7, "delhi-risk"),
        _risk_request("DC_MUMBAI", 0.5, "mumbai-risk"),
        _scenario_request("DC_KOLKATA", -1_000.0, "kolkata-whatif"),
    ]


async def _gather(orch, requests):
    return await asyncio.gather(*(orch.run(r) for r in requests))


def _run_concurrently(orch, requests):
    return asyncio.run(_gather(orch, requests))


def _run_threaded(orch, requests):
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return [f.result() for f in [pool.submit(orch.run_sync, r) for r in requests]]


# ===========================================================================
# §24 — the three-request scenario
# ===========================================================================

class TestConcurrentRequests:

    @pytest.mark.parametrize("runner", [_run_concurrently, _run_threaded],
                             ids=["asyncio", "threads"])
    def test_all_three_complete(self, delhi_network, runner):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        responses = runner(orch, _the_three_requests())

        assert len(responses) == 3
        for response in responses:
            assert response.status in (
                ExecutionState.COMPLETED.value,
                ExecutionState.REQUIRES_HUMAN.value,
                ExecutionState.REQUIRES_APPROVAL.value,
            ), f"{response.request_id} ended {response.status}: {response.errors}"

    @pytest.mark.parametrize("runner", [_run_concurrently, _run_threaded],
                             ids=["asyncio", "threads"])
    def test_execution_ids_are_distinct(self, delhi_network, runner):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        responses = runner(orch, _the_three_requests())

        ids = [r.execution_id for r in responses]
        assert len(set(ids)) == 3
        assert len(set(r.request_id for r in responses)) == 3

    @pytest.mark.parametrize("runner", [_run_concurrently, _run_threaded],
                             ids=["asyncio", "threads"])
    def test_evidence_stays_with_its_own_request(self, delhi_network, runner):
        """
        The corruption this guards against: Delhi's RF appearing on Mumbai's
        response. Each figure is checked against its own hand calculation.

            DC_DELHI  : P=0.7, REI=0.8 ⇒ RF = 0.94
            DC_MUMBAI : P=0.5, REI=1.0 ⇒ RF = 1.00
        """
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        by_request = {r.request_id: r for r in runner(orch, _the_three_requests())}

        delhi = by_request["delhi-risk"].risk["results"]
        assert [r["facility_id"] for r in delhi] == ["DC_DELHI"]
        assert delhi[0]["likelihood"] == pytest.approx(0.7, abs=TOL)
        assert delhi[0]["risk_factor"] == pytest.approx(0.94, abs=TOL)

        mumbai = by_request["mumbai-risk"].risk["results"]
        assert [r["facility_id"] for r in mumbai] == ["DC_MUMBAI"]
        assert mumbai[0]["likelihood"] == pytest.approx(0.5, abs=TOL)
        assert mumbai[0]["risk_factor"] == pytest.approx(1.0, abs=TOL)

        # The what-if carries a scenario and no risk block at all.
        kolkata = by_request["kolkata-whatif"]
        assert kolkata.risk is None
        assert kolkata.scenario_id is not None

    @pytest.mark.parametrize("runner", [_run_concurrently, _run_threaded],
                             ids=["asyncio", "threads"])
    def test_the_baseline_survives_all_three(self, delhi_network, runner):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        before = orch.snapshots.current().network.model_dump_json()
        before_ids = orch.snapshots.list_ids()

        runner(orch, _the_three_requests())

        assert orch.snapshots.current().network.model_dump_json() == before
        assert orch.snapshots.list_ids() == before_ids

    @pytest.mark.parametrize("runner", [_run_concurrently, _run_threaded],
                             ids=["asyncio", "threads"])
    def test_the_rei_registry_stays_consistent(self, delhi_network, runner):
        """
        All three requests are on one snapshot, so every REI batch must agree.
        A racing cache would show divergent values or a corrupted entry.
        """
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        responses = runner(orch, _the_three_requests())

        # All three: the scenario workflow assesses resilience too.
        registries = [r.results["resilience"] for r in responses
                      if "resilience" in r.results]
        assert len(registries) == 3

        fingerprints = {r["material_fingerprint"] for r in registries}
        assert len(fingerprints) == 1, "one network must yield one fingerprint"
        for registry in registries:
            assert registry["rei_by_facility"]["DC_DELHI"] == pytest.approx(0.8, abs=TOL)
            assert registry["rei_by_facility"]["DC_MUMBAI"] == pytest.approx(1.0, abs=TOL)
            assert registry["rei_by_facility"]["DC_KOLKATA"] == pytest.approx(0.4, abs=TOL)
            assert registry["baseline_business_cost"] == pytest.approx(1200.0, abs=1e-6)
            assert registry["network_snapshot_id"] == orch.snapshots.current_id

        store = orch.services["rei"].service.store
        assert len(store) == 1, "one fingerprint means one cache entry"

    @pytest.mark.parametrize("runner", [_run_concurrently, _run_threaded],
                             ids=["asyncio", "threads"])
    def test_concurrent_cold_misses_agree_even_when_they_duplicate_work(
        self, delhi_network, runner,
    ):
        """
        There is no in-flight deduplication in the REI cache: three simultaneous
        cold requests can each compute their own batch, and the last to finish
        wins the cache entry. That costs redundant solves — a real limitation,
        recorded here rather than hidden — but it is not a correctness problem.
        The batches are computed from the same immutable snapshot under the same
        assumptions, so they agree exactly, and the store converges on one entry.
        """
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        responses = runner(orch, _the_three_requests())
        registries = [r.results["resilience"] for r in responses
                      if "resilience" in r.results]

        values = {tuple(sorted(r["rei_by_facility"].items())) for r in registries}
        assert len(values) == 1, "concurrent batches must not diverge"

        stats = orch.services["rei"].service.health()["service"]
        assert stats["batches_requested"] == 3
        assert stats["batches_computed"] >= 1
        assert len(orch.services["rei"].service.store) == 1


# ===========================================================================
# §24 — scenario isolation under concurrency
# ===========================================================================

class TestConcurrentScenarioIsolation:

    @pytest.mark.parametrize("runner", [_run_concurrently, _run_threaded],
                             ids=["asyncio", "threads"])
    def test_overrides_do_not_leak_between_concurrent_scenarios(self, delhi_network,
                                                                runner):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        requests = [
            _scenario_request("DC_DELHI", -1_000.0, "s-delhi"),
            _scenario_request("DC_MUMBAI", -2_000.0, "s-mumbai"),
            _scenario_request("DC_KOLKATA", -3_000.0, "s-kolkata"),
        ]
        by_request = {r.request_id: r for r in runner(orch, requests)}

        expected = {
            "s-delhi": ("DC_DELHI", -1_000.0, 4_000.0),
            "s-mumbai": ("DC_MUMBAI", -2_000.0, 3_000.0),
            "s-kolkata": ("DC_KOLKATA", -3_000.0, 2_000.0),
        }
        for request_id, (node, delta, expected_capacity) in expected.items():
            response = by_request[request_id]
            assert response.results["network"]["scenario_overrides"] == [
                f"CHANGE_CAPACITY {node} {delta:+,.0f} units/period"
            ]

            record = orch.scenarios.get(response.scenario_id)
            capacities = {f.id: f.capacity_units_per_period
                          for f in record.network.facilities if f.id.startswith("DC_")}
            assert capacities[node] == pytest.approx(expected_capacity)
            # Every OTHER DC in this scenario is untouched at its baseline value.
            for other, value in capacities.items():
                if other != node:
                    assert value == pytest.approx(5_000.0), (
                        f"{request_id} leaked a change into {other}"
                    )

    @pytest.mark.parametrize("runner", [_run_concurrently, _run_threaded],
                             ids=["asyncio", "threads"])
    def test_each_scenario_gets_its_own_record(self, delhi_network, runner):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        requests = [
            _scenario_request("DC_DELHI", -1_000.0, "s1"),
            _scenario_request("DC_MUMBAI", -2_000.0, "s2"),
            _scenario_request("DC_KOLKATA", -3_000.0, "s3"),
        ]
        responses = runner(orch, requests)

        scenario_ids = [r.scenario_id for r in responses]
        assert len(set(scenario_ids)) == 3
        assert sorted(orch.scenarios.list_ids()) == sorted(scenario_ids)
        for record in (orch.scenarios.get(i) for i in scenario_ids):
            assert record.is_hypothetical is True
            assert record.parent_snapshot_id == orch.snapshots.current_id

    def test_concurrent_scenarios_and_risk_do_not_interfere(self, delhi_network):
        """Mixed workload: the §24 trio plus more, all at once."""
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        requests = [
            *_the_three_requests(),
            _risk_request("DC_KOLKATA", 0.2, "kolkata-risk"),
            _scenario_request("DC_DELHI", -500.0, "delhi-whatif"),
        ]
        by_request = {r.request_id: r for r in _run_concurrently(orch, requests)}

        # RF = 0.2 + 0.4 − 0.08 = 0.52
        assert by_request["kolkata-risk"].risk["results"][0]["risk_factor"] == \
            pytest.approx(0.52, abs=TOL)
        assert by_request["delhi-risk"].risk["results"][0]["risk_factor"] == \
            pytest.approx(0.94, abs=TOL)
        assert by_request["delhi-whatif"].results["network"]["scenario_overrides"] == [
            "CHANGE_CAPACITY DC_DELHI -500 units/period"
        ]
        assert orch.snapshots.list_ids() == [orch.snapshots.current_id]


# ===========================================================================
# §24 — the shared state itself
# ===========================================================================

class TestSharedStateIntegrity:

    def test_the_execution_store_holds_every_run_exactly_once(self, delhi_network):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        responses = _run_threaded(orch, _the_three_requests())

        stored = orch.state_store.list_execution_ids()
        for response in responses:
            assert response.execution_id in stored
        assert len(stored) == len(set(stored)) == 3

    def test_every_run_has_its_own_sealed_audit_trace(self, delhi_network):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        responses = _run_threaded(orch, _the_three_requests())

        for response in responses:
            trace = orch.get_trace(response.execution_id)
            assert trace is not None
            assert trace.execution_id == response.execution_id
            assert trace.request_id == response.request_id
            assert trace.completed_at is not None
            # No event from a sibling run leaked into this trace.
            for event in trace.events:
                assert event.detail["execution_id"] == response.execution_id

    def test_a_racing_duplicate_request_executes_exactly_once(self, delhi_network):
        """
        Four identical request_ids submitted simultaneously. The guarantee that
        matters is that the WORK happens once: the context is registered before
        the lifecycle begins, so later arrivals find it and deduplicate.
        """
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        duplicates = [_risk_request("DC_DELHI", 0.7, "same-id") for _ in range(4)]
        responses = _run_threaded(orch, duplicates)

        assert len({r.request_id for r in responses}) == 1
        assert len({r.execution_id for r in responses}) == 1, (
            "the same request must not spawn independent executions"
        )
        assert len(orch.state_store.list_execution_ids()) == 1

        deduplicated = [r for r in responses
                        if any("Duplicate request_id" in w for w in r.warnings)]
        assert len(deduplicated) == 3

    def test_a_racing_duplicate_returns_a_point_in_time_view(self, delhi_network):
        """
        A known limitation, asserted so it is documented rather than discovered.

        Deduplication returns the original execution's state AS IT STANDS. A
        duplicate that arrives while the original is still running therefore sees
        a partial response — `risk` may be absent — rather than blocking until
        the original finishes. That is incomplete, not wrong: no figure is
        fabricated and no second execution runs. Closing it needs an in-flight
        registry that waits on the original, which Phase 2 does not add.

        Sequential retry — the realistic client behaviour after a timeout — is
        fully correct, as the next test shows.
        """
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        duplicates = [_risk_request("DC_DELHI", 0.7, "same-id") for _ in range(4)]
        responses = _run_threaded(orch, duplicates)

        # Whichever responses DO carry a risk block agree exactly.
        with_risk = [r for r in responses if r.risk is not None and r.risk["results"]]
        assert with_risk, "at least the original completed"
        for response in with_risk:
            assert response.risk["results"][0]["risk_factor"] == pytest.approx(
                0.94, abs=TOL
            )
        # And a partial view never invents one.
        for response in responses:
            if response.risk is not None:
                for row in response.risk["results"]:
                    assert row["risk_factor"] is not None

    def test_sequential_retry_of_the_same_request_is_fully_idempotent(self,
                                                                      delhi_network):
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        first = orch.run_sync(_risk_request("DC_DELHI", 0.7, "retry"))
        second = orch.run_sync(_risk_request("DC_DELHI", 0.7, "retry"))

        assert second.execution_id == first.execution_id
        assert any("Duplicate request_id" in w for w in second.warnings)
        assert second.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert len(orch.state_store.list_execution_ids()) == 1

    def test_repeated_concurrent_batches_are_deterministic(self, delhi_network):
        """
        The same workload run twice concurrently must give identical numbers. A
        race that corrupted shared state would surface as drift here.
        """
        results = []
        for attempt in range(2):
            orch = build_orchestrator(network=delhi_network, enable_llm=False)
            responses = _run_threaded(orch, [
                _risk_request("DC_DELHI", 0.7, f"d-{attempt}"),
                _risk_request("DC_MUMBAI", 0.5, f"m-{attempt}"),
                _risk_request("DC_KOLKATA", 0.2, f"k-{attempt}"),
            ])
            results.append({
                r.risk["results"][0]["facility_id"]: r.risk["results"][0]["risk_factor"]
                for r in responses
            })

        assert results[0] == results[1]
        assert results[0] == {
            "DC_DELHI": pytest.approx(0.94, abs=TOL),
            "DC_MUMBAI": pytest.approx(1.0, abs=TOL),
            "DC_KOLKATA": pytest.approx(0.52, abs=TOL),
        }
