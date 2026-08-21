"""
NetGravity — REI Registry Store.

REI is a DERIVED ANALYTICAL DATASET, not something to recompute on every
request. A batch costs 1 + N MILP solves; recomputing it because a UI refreshed
would be wasteful and slow.

This store makes REI reusable while the network is materially unchanged, and
stale the moment it is not.

Cache key
─────────
A REI value is valid only for one exact combination:

    (material_fingerprint, model_version, disruption_type, disruption_signature)

`material_fingerprint` — hash of the inputs that can change the optimum
                         (see `resilience/fingerprint.py`). Renaming a facility
                         does not change it; changing its capacity does.
`model_version`        — the maths that produced the value.
`disruption_type`      — FACILITY_FAILURE today; the key is ready for more.
`disruption_signature` — the assumptions the batch ran under (shortage policy,
                         cost basis, eligibility filters). Two batches under
                         different assumptions are not interchangeable.

Storage layering
────────────────
    in-memory LRU        performance cache (this class)
    PersistenceBackend   durable source of truth (see `persistence.py`)

Entries are written through to the backend on `put` and read back on a cache
miss, so a restarted process does not re-run 1 + N MILP solves for work it has
already done. With the default `NullPersistenceBackend` the store is
memory-only, exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from netgravity.schemas.results import FacilityResilienceRegistry

if TYPE_CHECKING:  # pragma: no cover
    from netgravity.resilience.persistence import PersistenceBackend

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class REICacheKey:
    """
    Identity of one REI calculation.

    Frozen and hashable so it can key the store directly. Equality here is
    exactly the condition under which a stored result may be reused.
    """
    material_fingerprint: str
    model_version: str
    disruption_type: str
    disruption_signature: str

    def as_string(self) -> str:
        return (f"{self.material_fingerprint}|{self.model_version}"
                f"|{self.disruption_type}|{self.disruption_signature}")


def disruption_signature(disruption_config) -> str:
    """
    Stable hash of the assumptions a batch ran under.

    Two batches with different shortage policies, cost bases or eligibility
    filters are NOT interchangeable — REI is only comparable within one set of
    assumptions — so those settings are part of the cache identity.
    """
    payload = {
        "disruption_type": disruption_config.disruption_type.value,
        "disruption_period": disruption_config.disruption_period.value,
        "allow_shortage": disruption_config.allow_shortage,
        "service_diagnostic_on_infeasible":
            disruption_config.service_diagnostic_on_infeasible,
        "only_baseline_open_facilities":
            disruption_config.only_baseline_open_facilities,
        "eligible_roles": sorted(
            r.value for r in (disruption_config.eligible_roles or [])
        ),
        "exclude_facility_ids": sorted(disruption_config.exclude_facility_ids),
        "cost_basis": disruption_config.cost_basis.model_dump(mode="json"),
        "risk_rules": disruption_config.risk_rules.model_dump(mode="json"),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


@dataclass
class REIRegistryEntry:
    """A stored batch plus its bookkeeping."""
    key: REICacheKey
    registry: FacilityResilienceRegistry
    stored_at: str = field(default_factory=_utc_now)
    hit_count: int = 0
    invalidated: bool = False
    invalidation_reason: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        return not self.invalidated


class REIRegistryStore:
    """
    Stores computed REI batches and serves them while they remain valid.

    Deliberately simple. The value is in the KEY being correct — a wrong key
    either serves stale numbers (dangerous) or never hits (pointless).
    """

    def __init__(
        self,
        max_entries: int = 64,
        backend: Optional["PersistenceBackend"] = None,
    ) -> None:
        """
        Args:
            max_entries: LRU bound on the in-memory cache.
            backend:     Durable source of truth. Entries are written through on
                         `put` and read back on a cache miss, so a restarted
                         process does not recompute work it already did.
                         Defaults to no durability.
        """
        from netgravity.resilience.persistence import NullPersistenceBackend

        self._entries: "OrderedDict[REICacheKey, REIRegistryEntry]" = OrderedDict()
        self._max_entries = max_entries
        self._backend = backend or NullPersistenceBackend()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._backend_hits = 0

    @property
    def is_durable(self) -> bool:
        """True when a restart would preserve computed batches."""
        return getattr(self._backend, "is_durable", False)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, key: REICacheKey) -> Optional[FacilityResilienceRegistry]:
        """
        Return a valid stored batch, or None.

        Invalidated entries are never served — the point of invalidation is
        that the numbers no longer describe the current network.
        """
        with self._lock:
            entry = self._entries.get(key)

            if entry is None:
                # Cache miss — try the durable source of truth before declaring
                # the work undone. This is what saves 1 + N solves across a
                # restart.
                restored = self._backend.load(key.as_string())
                if restored is not None:
                    entry = REIRegistryEntry(key=key, registry=restored)
                    self._entries[key] = entry
                    self._entries.move_to_end(key)
                    self._backend_hits += 1
                    logger.info(
                        "rei.store.restored_from_backend key=%s batch_id=%s",
                        key.as_string(), restored.batch_id,
                    )

            if entry is None:
                self._misses += 1
                logger.info("rei.store.miss key=%s", key.as_string())
                return None
            if not entry.is_valid:
                self._misses += 1
                logger.info(
                    "rei.store.stale key=%s reason=%s",
                    key.as_string(), entry.invalidation_reason,
                )
                return None

            entry.hit_count += 1
            self._hits += 1
            self._entries.move_to_end(key)   # LRU
            logger.info(
                "rei.store.hit key=%s batch_id=%s hits=%d",
                key.as_string(), entry.registry.batch_id, entry.hit_count,
            )
            return entry.registry

    def has_valid(self, key: REICacheKey) -> bool:
        """
        Whether a reusable batch exists, consulting the durable backend too.

        Checking only the in-memory cache would report "no" immediately after a
        restart, for work that is in fact already done.
        """
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                return entry.is_valid
            return self._backend.load(key.as_string()) is not None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def put(self, key: REICacheKey, registry: FacilityResilienceRegistry) -> None:
        """Store a completed batch, evicting the least recently used if full."""
        with self._lock:
            self._entries[key] = REIRegistryEntry(key=key, registry=registry)
            self._entries.move_to_end(key)
            # Write through to the durable source of truth BEFORE eviction, so a
            # batch pushed out of the LRU is not lost.
            self._backend.save(key.as_string(), registry)
            while len(self._entries) > self._max_entries:
                evicted, _ = self._entries.popitem(last=False)
                logger.info("rei.store.evicted key=%s", evicted.as_string())
            logger.info(
                "rei.store.stored key=%s batch_id=%s nodes=%d",
                key.as_string(), registry.batch_id, registry.n_facilities_assessed,
            )

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------

    def invalidate(self, key: REICacheKey, reason: str) -> bool:
        """Mark one entry stale. Returns True if an entry was affected."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.invalidated:
                return False
            entry.invalidated = True
            entry.invalidation_reason = reason
            # Drop the durable record too: a restart must not resurrect a batch
            # that has been declared stale.
            self._backend.delete(key.as_string())
            logger.info("rei.store.invalidated key=%s reason=%s", key.as_string(), reason)
            return True

    def invalidate_fingerprint(self, material_fingerprint: str, reason: str) -> int:
        """Invalidate every batch computed against one material fingerprint."""
        with self._lock:
            affected = 0
            for key, entry in self._entries.items():
                if key.material_fingerprint == material_fingerprint and not entry.invalidated:
                    entry.invalidated = True
                    entry.invalidation_reason = reason
                    self._backend.delete(key.as_string())
                    affected += 1
            if affected:
                logger.info(
                    "rei.store.invalidated_fingerprint fingerprint=%s entries=%d reason=%s",
                    material_fingerprint, affected, reason,
                )
            return affected

    def invalidate_all(self, reason: str) -> int:
        with self._lock:
            affected = 0
            for key, entry in self._entries.items():
                if not entry.invalidated:
                    entry.invalidated = True
                    entry.invalidation_reason = reason
                    self._backend.delete(key.as_string())
                    affected += 1
            return affected

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def entries(self) -> List[REIRegistryEntry]:
        with self._lock:
            return list(self._entries.values())

    def valid_keys(self) -> List[str]:
        with self._lock:
            return [k.as_string() for k, e in self._entries.items() if e.is_valid]

    def stats(self) -> Dict[str, object]:
        with self._lock:
            valid = sum(1 for e in self._entries.values() if e.is_valid)
            return {
                "entries": len(self._entries),
                "valid_entries": valid,
                "stale_entries": len(self._entries) - valid,
                "hits": self._hits,
                "misses": self._misses,
                "backend_restores": self._backend_hits,
                "max_entries": self._max_entries,
                "durable": self.is_durable,
            }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._hits = 0
            self._misses = 0

    def __len__(self) -> int:
        return len(self._entries)
