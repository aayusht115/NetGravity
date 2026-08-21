"""
NetGravity — REI Service.

The seam between "who wants REI" and "how REI is computed".

    Orchestrator / API
            ↓
      REIService          ← cache lookup, invalidation, idempotency, persistence
            ↓
   assess_network_resilience   ← scenario generation + REI mathematics
            ↓
         MILP engine
            ↓
     REIRegistryStore

Separation of concerns, deliberately:

  * `rei.py` knows the mathematics and nothing about caching.
  * `registry_store.py` knows storage and nothing about the mathematics.
  * This module decides WHETHER a calculation is needed and records the result.
  * The Orchestrator decides WHEN to ask, and consumes typed results.

That layering is what lets the batch move to a job queue or a remote worker
later without touching the REI domain logic.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from netgravity.resilience.fingerprint import compute_material_fingerprint
from netgravity.resilience.registry_store import (
    REICacheKey,
    REIRegistryStore,
    disruption_signature,
)
from netgravity.resilience.rei import (
    NoEligibleFacilitiesError,
    SolveFn,
    assess_network_resilience,
)
from netgravity.schemas.network import CanonicalNetwork, OptimizationConfig
from netgravity.schemas.resilience import DisruptionConfig
from netgravity.schemas.results import FacilityResilienceRegistry, REIBatchStatus

logger = logging.getLogger(__name__)


@dataclass
class REIServiceStats:
    """Cache effectiveness, for observability and the performance report."""
    batches_requested: int = 0
    batches_computed: int = 0
    batches_served_from_cache: int = 0
    milp_solves_executed: int = 0
    milp_solves_avoided: int = 0

    @property
    def cache_hit_rate(self) -> float:
        if self.batches_requested == 0:
            return 0.0
        return round(self.batches_served_from_cache / self.batches_requested, 4)


class REIService:
    """
    Computes, caches and invalidates facility REI for a network snapshot.

    Idempotent by construction: a request for a calculation that already exists
    and is still valid returns the stored batch instead of re-running 1 + N
    MILP solves.
    """

    def __init__(
        self,
        store: Optional[REIRegistryStore] = None,
        *,
        solve_fn: Optional[SolveFn] = None,
        max_workers: int = 1,
    ) -> None:
        # `is not None`, not `or`: REIRegistryStore defines __len__, so an empty
        # store is FALSY and `store or REIRegistryStore()` would silently discard
        # a caller's durable store and substitute a memory-only one.
        self.store = store if store is not None else REIRegistryStore()
        self.solve_fn = solve_fn
        self.max_workers = max_workers
        self.stats = REIServiceStats()

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    def cache_key(
        self,
        network: CanonicalNetwork,
        config: Optional[OptimizationConfig] = None,
        disruption_config: Optional[DisruptionConfig] = None,
    ) -> REICacheKey:
        """
        Identity of the calculation this request implies.

        Built from the MATERIAL fingerprint rather than the raw data version, so
        renaming a facility does not invalidate a batch while changing its
        capacity does.
        """
        cfg = config or network.config
        dcfg = disruption_config or DisruptionConfig()
        return REICacheKey(
            material_fingerprint=compute_material_fingerprint(network, cfg),
            model_version=cfg.model_version,
            disruption_type=dcfg.disruption_type.value,
            disruption_signature=disruption_signature(dcfg),
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def get_or_compute(
        self,
        network: CanonicalNetwork,
        config: Optional[OptimizationConfig] = None,
        disruption_config: Optional[DisruptionConfig] = None,
        *,
        snapshot_id: Optional[str] = None,
        force_recompute: bool = False,
    ) -> FacilityResilienceRegistry:
        """
        Return a valid REI batch, computing one only if necessary.

        Args:
            network:           Observed network. Never mutated.
            config:            Optimization config (defaults to network.config).
            disruption_config: Disruption assumptions shared by the whole batch.
            snapshot_id:       Immutable snapshot identity for traceability.
            force_recompute:   Bypass the cache. Use when the caller knows the
                               stored value is suspect; normal invalidation
                               should be driven by the fingerprint instead.

        Returns:
            FacilityResilienceRegistry, either freshly computed or served from
            the store. `served_from_cache` distinguishes the two, and a cached
            batch reports `n_milp_solves = 0` for the serving request.

        Raises:
            NoEligibleFacilitiesError: nothing to assess.
            BaselineSolveError:        the baseline is infeasible.
        """
        cfg = config or network.config
        dcfg = disruption_config or DisruptionConfig()
        key = self.cache_key(network, cfg, dcfg)

        self.stats.batches_requested += 1

        if not force_recompute:
            cached = self.store.get(key)
            if cached is not None:
                self.stats.batches_served_from_cache += 1
                self.stats.milp_solves_avoided += cached.n_milp_solves
                logger.info(
                    "rei.service.cache_hit batch_id=%s fingerprint=%s solves_avoided=%d",
                    cached.batch_id, key.material_fingerprint, cached.n_milp_solves,
                )
                # A copy, flagged, so a consumer can tell a served batch from a
                # fresh one and cannot mutate the stored original.
                served = cached.model_copy(deep=True)
                served.served_from_cache = True
                # `n_milp_solves` counts what THIS request cost, which for a
                # cache hit is nothing. Both the field's own contract and this
                # method's docstring promise 0 here; leaving the originating
                # batch's count in place made a cache hit look like it had just
                # re-run 1 + N solves. The historical count is still available
                # from the stored entry, and `stats.milp_solves_avoided` above
                # reads it before this copy is made.
                served.n_milp_solves = 0
                return self._restamp_snapshot(served, snapshot_id)

        started = time.perf_counter()
        registry = assess_network_resilience(
            network, cfg, dcfg,
            solve_fn=self.solve_fn,
            snapshot_id=snapshot_id,
            max_workers=self.max_workers,
        )
        elapsed = round(time.perf_counter() - started, 4)

        self.stats.batches_computed += 1
        self.stats.milp_solves_executed += registry.n_milp_solves

        # Only a usable batch is cached. Caching a total failure would serve the
        # failure back on the next request instead of retrying it.
        if registry.batch_status != REIBatchStatus.FAILED:
            self.store.put(key, registry)
        else:
            logger.warning(
                "rei.service.not_cached batch_id=%s status=%s",
                registry.batch_id, registry.batch_status.value,
            )

        logger.info(
            "rei.service.computed batch_id=%s status=%s nodes=%d solves=%d elapsed_s=%.4f",
            registry.batch_id, registry.batch_status.value,
            registry.n_facilities_assessed, registry.n_milp_solves, elapsed,
        )
        return registry

    @staticmethod
    def _restamp_snapshot(
        registry: FacilityResilienceRegistry,
        snapshot_id: Optional[str],
    ) -> FacilityResilienceRegistry:
        """
        Re-label a cache-served batch with the snapshot it is being served TO.

        Why this is sound, and why it is necessary
        ──────────────────────────────────────────
        The cache key contains the MATERIAL fingerprint, so a hit proves the
        stored batch and the requesting network imply the same optimum. But a
        snapshot id is derived from `data_version`, which hashes descriptive
        fields too. Renaming a facility therefore mints a new snapshot id while
        leaving the fingerprint — and hence the whole REI batch — untouched.

        Without this re-stamp the two mechanisms contradict each other: the cache
        correctly serves the batch, and the RF layer then correctly refuses it as
        STALE_REI because the labels differ. A cosmetic edit would permanently
        disable risk assessment, recoverable only by manual invalidation.

        This does NOT weaken snapshot validation. The check in `lookup_rei` stays
        exactly as strict; what changes is that the batch now carries a truthful
        label. Any MATERIAL difference alters the fingerprint, which misses the
        cache entirely and computes a fresh batch for the new snapshot — so a
        genuinely stale REI is still caught. The originating snapshot is retained
        in `computed_for_snapshot_id`, so the substitution is auditable.
        """
        original = registry.network_snapshot_id
        if snapshot_id is None or original == snapshot_id:
            return registry

        registry.network_snapshot_id = snapshot_id
        registry.computed_for_snapshot_id = original
        for row in registry.results:
            row.network_snapshot_id = snapshot_id
        registry.warnings.append(
            f"Served from cache: this batch was computed against snapshot "
            f"'{original}' and is reused for '{snapshot_id}'. The two networks "
            f"share material fingerprint '{registry.material_fingerprint}', so "
            f"they imply an identical optimum and identical REI."
        )
        logger.info(
            "rei.service.snapshot_restamped batch_id=%s from=%s to=%s fingerprint=%s",
            registry.batch_id, original, snapshot_id, registry.material_fingerprint,
        )
        return registry

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def is_valid_for(
        self,
        network: CanonicalNetwork,
        config: Optional[OptimizationConfig] = None,
        disruption_config: Optional[DisruptionConfig] = None,
    ) -> bool:
        """Whether a stored batch can serve this exact network and assumptions."""
        return self.store.has_valid(self.cache_key(network, config, disruption_config))

    def invalidate_for(
        self,
        network: CanonicalNetwork,
        reason: str,
        config: Optional[OptimizationConfig] = None,
    ) -> int:
        """
        Invalidate every batch computed against this network's material state.

        Normally unnecessary: a material change alters the fingerprint, so the
        old entry simply stops matching. This exists for explicit operator
        action ("recompute regardless").
        """
        fingerprint = compute_material_fingerprint(network, config or network.config)
        return self.store.invalidate_fingerprint(fingerprint, reason)

    def invalidate_all(self, reason: str) -> int:
        return self.store.invalidate_all(reason)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def health(self) -> dict:
        return {
            "store": self.store.stats(),
            "service": {
                "batches_requested": self.stats.batches_requested,
                "batches_computed": self.stats.batches_computed,
                "batches_served_from_cache": self.stats.batches_served_from_cache,
                "cache_hit_rate": self.stats.cache_hit_rate,
                "milp_solves_executed": self.stats.milp_solves_executed,
                "milp_solves_avoided": self.stats.milp_solves_avoided,
            },
            "max_workers": self.max_workers,
        }
