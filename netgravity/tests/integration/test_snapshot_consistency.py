"""
Phase 2 — §8, §23: snapshot consistency through the orchestrator.

Two distinct mechanisms, often confused:

    EXECUTION FRESHNESS   the run is pinned to snapshot X; if the observed
                          network has moved to Y, the run goes STALE and stops.
                          Enforced by `SnapshotManager.assert_fresh`.

    REI/RF CONSISTENCY    the REI batch was computed against snapshot X; if the
                          execution is on Y, RF refuses with STALE_REI.
                          Enforced by `lookup_rei`.

The first stops a run before it starts. The second catches the subtler case
where the run is legitimate but the exposure evidence it found is not.

The requirement §23 adds beyond Phase 1: once REI is recalculated for the
current snapshot, RF must become computable again. A check that can only ever
say no is not a check, it is an outage.
"""

from __future__ import annotations

from typing import Any

import pytest

from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.core.execution_state import ExecutionState
from netgravity.orchestrator.engines.deterministic import REIClient
from netgravity.orchestrator.risk.risk_assessment import lookup_rei
from netgravity.orchestrator.schemas.requests import Intent, OrchestratorRequest
from netgravity.orchestrator.schemas.risk import RFNotComputableReason

from .conftest import build_delhi_network, flood_signal

TOL = 1e-9


class PinnedSnapshotREIClient(REIClient):
    """
    Produces a genuine REI batch stamped with a snapshot id we choose.

    Models the real-world case: a batch computed at V17 that is still sitting in
    the registry when the network has moved to V18. `pinned` is mutable so one
    test can go stale then recover.
    """

    def __init__(self, pinned: str) -> None:
        super().__init__()
        self.pinned = pinned

    async def assess_registry(self, network, *, snapshot_id=None, **kwargs: Any):
        return await super().assess_registry(
            network, snapshot_id=self.pinned, **kwargs
        )


def _risk_run(orch, **kwargs):
    return orch.run_sync(OrchestratorRequest(
        input="Flood warning for Delhi NCR.",
        explicit_intent=Intent.EXTERNAL_EVENT,
        external_signal=flood_signal(),
        disable_llm=True, **kwargs,
    ))


# ===========================================================================
# §8 / §23 — mismatch is detected
# ===========================================================================

class TestStaleREIDetection:

    def test_v18_execution_with_v17_rei_refuses(self, orch):
        orch.services["rei"] = PinnedSnapshotREIClient("snap_V17")
        response = _risk_run(orch)

        rows = response.risk["not_computable"]
        assert len(rows) == 1
        assert rows[0]["not_computable_reason"] == RFNotComputableReason.STALE_REI.value
        assert rows[0]["risk_factor"] is None
        assert response.risk["results"] == []

    def test_the_refusal_names_both_snapshots(self, orch):
        orch.services["rei"] = PinnedSnapshotREIClient("snap_V17")
        response = _risk_run(orch)

        notes = " ".join(response.risk["not_computable"][0]["notes"])
        assert "snap_V17" in notes
        assert (orch.snapshots.current_id or "") in notes
        assert "Recalculate REI" in notes

    def test_validation_is_not_weakened_by_a_numerically_perfect_batch(self, orch):
        """
        The stale batch here is arithmetically identical to a fresh one — same
        network, same solver, same REI values. Only the snapshot label differs,
        and that alone is enough to refuse.
        """
        orch.services["rei"] = PinnedSnapshotREIClient("snap_V17")
        response = _risk_run(orch)

        assert response.results["resilience"]["rei_by_facility"]["DC_DELHI"] == \
            pytest.approx(0.8, abs=TOL)
        assert response.risk["max_risk_factor"] is None

    def test_a_matching_snapshot_computes_normally(self, orch):
        """The control: identical wiring, correct snapshot id, RF computes."""
        orch.services["rei"] = PinnedSnapshotREIClient(orch.snapshots.current_id)
        response = _risk_run(orch)

        assert response.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert response.risk["not_computable"] == []


# ===========================================================================
# §23 — and recovers once REI is recalculated
# ===========================================================================

class TestSnapshotRecovery:

    def test_recalculating_rei_for_the_current_snapshot_restores_rf(self, orch):
        """
        §23's recovery requirement. The recalculation must be genuine, so the
        cache is cleared alongside re-pinning — otherwise the second run is
        served the same stale batch and nothing has been recalculated at all.
        """
        client = PinnedSnapshotREIClient("snap_V17")
        orch.services["rei"] = client

        stale = _risk_run(orch, request_id="stale")
        assert stale.risk["not_computable"][0]["not_computable_reason"] == \
            RFNotComputableReason.STALE_REI.value

        client.pinned = orch.snapshots.current_id
        client.service.invalidate_all("REI recalculated for the current snapshot")

        recovered = _risk_run(orch, request_id="recovered")

        assert recovered.risk["not_computable"] == []
        assert recovered.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert recovered.risk["max_risk_factor"] == pytest.approx(0.94, abs=TOL)
        assert recovered.results["resilience"]["served_from_cache"] is False

    def test_a_network_update_then_reassessment_works_end_to_end(self, delhi_network):
        """
        The realistic sequence: assess, the network changes, reassess. The second
        run is on a new snapshot with a new REI batch, and RF is computable
        throughout because both moved together.
        """
        orch = build_orchestrator(network=delhi_network, enable_llm=False)
        first = _risk_run(orch, request_id="v1")
        assert first.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)
        v1_snapshot = first.network_snapshot_id

        orch.register_network(build_delhi_network(delhi_capacity=60.0), label="v2")
        second = _risk_run(orch, request_id="v2")

        assert second.network_snapshot_id != v1_snapshot
        assert second.risk["results"][0]["risk_factor"] is not None
        assert second.results["resilience"]["network_snapshot_id"] == \
            second.network_snapshot_id


# ===========================================================================
# Regression — a cosmetic edit must not permanently disable RF
# ===========================================================================

class TestCosmeticChangeDoesNotBreakRF:
    """
    A defect found while building this suite, and the reason
    `computed_for_snapshot_id` exists.

    A snapshot id derives from `data_version`, which hashes descriptive fields.
    The REI cache keys on the MATERIAL fingerprint, which does not. So renaming a
    facility minted a new snapshot id, hit the cache, and returned a batch
    stamped with the OLD id — which the staleness check then correctly refused.
    Net effect: a label edit permanently broke risk assessment.

    The two mechanisms now agree. The check was not relaxed; the batch is
    re-stamped at the point where the fingerprint proves the two snapshots are
    equivalent, with the original retained for audit.
    """

    @staticmethod
    def _renamed(**kwargs):
        net = build_delhi_network(**kwargs)
        facilities = [
            f.model_copy(update={"name": f"{f.name} (renamed)"})
            if f.id == "DC_DELHI" else f
            for f in net.facilities
        ]
        net = net.model_copy(update={"facilities": facilities})
        return net.model_copy(update={"data_version": net.compute_data_version()})

    def test_a_rename_keeps_rf_computable(self, orch):
        first = _risk_run(orch, request_id="before")
        assert first.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)

        orch.register_network(self._renamed(), label="renamed")
        after = _risk_run(orch, request_id="after")

        assert after.network_snapshot_id != first.network_snapshot_id
        assert after.results["resilience"]["served_from_cache"] is True
        assert after.risk["not_computable"] == []
        assert after.risk["results"][0]["risk_factor"] == pytest.approx(0.94, abs=TOL)

    def test_the_substitution_is_recorded_not_silent(self, orch):
        _risk_run(orch, request_id="before")
        original_snapshot = orch.snapshots.current_id
        orch.register_network(self._renamed(), label="renamed")
        after = _risk_run(orch, request_id="after")

        registry = after.results["resilience"]
        assert registry["network_snapshot_id"] == after.network_snapshot_id
        assert registry["computed_for_snapshot_id"] == original_snapshot
        assert any("computed against snapshot" in w for w in registry["warnings"])

    def test_a_material_change_is_still_refused_by_the_same_machinery(self, orch):
        """
        The guard on the fix. Capacity IS material, so it changes the
        fingerprint, misses the cache and computes a batch for the new snapshot.
        Nothing stale is ever reused.
        """
        _risk_run(orch, request_id="before")
        orch.register_network(build_delhi_network(delhi_capacity=60.0), label="material")
        after = _risk_run(orch, request_id="after")

        registry = after.results["resilience"]
        assert registry["served_from_cache"] is False
        assert registry["computed_for_snapshot_id"] is None
        assert registry["network_snapshot_id"] == after.network_snapshot_id

    def test_a_batch_from_a_genuinely_different_network_is_still_stale(self, orch):
        """
        Re-stamping happens only on a fingerprint-keyed cache hit. A batch
        arriving by any other route is still checked and still refused — proven
        by the pinned client, which bypasses the service entirely.
        """
        orch.services["rei"] = PinnedSnapshotREIClient("snap_SOMETHING_ELSE")
        response = _risk_run(orch)
        assert response.risk["not_computable"][0]["not_computable_reason"] == \
            RFNotComputableReason.STALE_REI.value


# ===========================================================================
# §17.18 / §23 — execution-level freshness
# ===========================================================================

class TestExecutionFreshness:

    def test_a_pinned_stale_execution_stops_before_any_engine_runs(self, orch):
        stale_id = orch.snapshots.current_id
        orch.register_network(build_delhi_network(delhi_capacity=80.0), label="moved")

        response = orch.run_sync(OrchestratorRequest(
            input="Flood warning for Delhi NCR.",
            explicit_intent=Intent.EXTERNAL_EVENT,
            external_signal=flood_signal(),
            network_snapshot_id=stale_id, disable_llm=True,
        ))

        assert response.status == ExecutionState.STALE.value
        assert response.steps == [] or all(
            s["status"] in ("PENDING", "BLOCKED") for s in response.steps
        )
        assert response.risk is None

    def test_an_explicit_pin_is_never_silently_replaced(self, orch):
        """
        Substituting the current snapshot for a pinned one would defeat pinning
        entirely — the caller would believe it analysed V17 while the engines
        used V18, and staleness could never be detected.
        """
        pinned = orch.snapshots.current_id
        orch.register_network(build_delhi_network(delhi_capacity=80.0), label="moved")

        response = orch.run_sync(OrchestratorRequest(
            input="Flood warning.", explicit_intent=Intent.EXTERNAL_EVENT,
            external_signal=flood_signal(), network_snapshot_id=pinned,
            disable_llm=True,
        ))
        assert response.network_snapshot_id == pinned
        assert response.network_snapshot_id != orch.snapshots.current_id

    def test_every_result_in_one_run_shares_one_snapshot(self, orch):
        """Consistency across the whole response, not just at the RF boundary."""
        response = _risk_run(orch)
        snapshot = response.network_snapshot_id

        assert response.results["resilience"]["network_snapshot_id"] == snapshot
        rei_provenance = response.risk["results"][0]["provenance"]["rei"]
        assert snapshot in rei_provenance


# ===========================================================================
# The check itself, in isolation
# ===========================================================================

class TestLookupSnapshotRule:

    def test_lookup_refuses_a_mismatched_registry(self, orch):
        import asyncio
        registry = asyncio.run(REIClient().assess_registry(
            orch.snapshots.current().network, snapshot_id="snap_V17",
        ))

        stale = lookup_rei("DC_DELHI", registry, expected_snapshot_id="snap_V18")
        assert stale.unavailable_reason == RFNotComputableReason.STALE_REI
        assert stale.rei is None
        assert stale.is_usable is False

        fresh = lookup_rei("DC_DELHI", registry, expected_snapshot_id="snap_V17")
        assert fresh.is_usable is True
        assert fresh.rei == pytest.approx(0.8, abs=TOL)

    def test_no_expectation_means_no_snapshot_check(self, orch):
        """
        A caller that supplies no expectation is not asserting anything about
        versions, so there is nothing to violate. The orchestrator always
        supplies one — see `test_v18_execution_with_v17_rei_refuses`.
        """
        import asyncio
        registry = asyncio.run(REIClient().assess_registry(
            orch.snapshots.current().network, snapshot_id="snap_V17",
        ))
        assert lookup_rei("DC_DELHI", registry).is_usable is True
