"""
NetGravity — REI registry persistence backends.

Separates the two things `REIRegistryStore` was previously conflating:

    PERSISTENT SOURCE OF TRUTH   survives restart; the record of what was computed
    PERFORMANCE CACHE            in-memory LRU; makes repeat lookups instant

`REIRegistryStore` remains the cache. A backend plugged into it becomes the
source of truth: entries are written through on `put`, and read back on a cache
miss, so a restarted process does not re-run 1 + N MILP solves for work it has
already done.

Two backends ship:

    NullPersistenceBackend   no durability (default; preserves prior behaviour)
    JsonFilePersistenceBackend   one JSON file per batch under a directory

JSON-on-disk rather than a database because the current deployment is a single
Flask process with no database dependency, and adding one would be
infrastructure the measurements do not yet justify. The `PersistenceBackend`
protocol is the seam: a SQL or blob-store backend implements three methods and
nothing else changes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Protocol, runtime_checkable

from netgravity.schemas.results import FacilityResilienceRegistry

logger = logging.getLogger(__name__)

#: Bumped when the on-disk record layout changes, so records written by an
#: older layout are ignored rather than mis-parsed.
PERSISTENCE_FORMAT_VERSION = 1


@runtime_checkable
class PersistenceBackend(Protocol):
    """Durable storage for computed REI batches."""

    def save(self, key: str, registry: FacilityResilienceRegistry) -> None:
        ...

    def load(self, key: str) -> Optional[FacilityResilienceRegistry]:
        ...

    def delete(self, key: str) -> bool:
        ...

    def keys(self) -> List[str]:
        ...


class NullPersistenceBackend:
    """
    No durability. The default.

    Explicit rather than implicit: a caller can tell from the backend type that
    results will not survive a restart, instead of discovering it later.
    """

    def save(self, key: str, registry: FacilityResilienceRegistry) -> None:
        return None

    def load(self, key: str) -> Optional[FacilityResilienceRegistry]:
        return None

    def delete(self, key: str) -> bool:
        return False

    def keys(self) -> List[str]:
        return []

    @property
    def is_durable(self) -> bool:
        return False


def _safe_filename(key: str) -> str:
    """
    Map a cache key to a filesystem-safe name.

    Keys contain '|' and '@', which are awkward or illegal on some filesystems.
    A hash keeps the mapping total and collision-resistant while the key itself
    is stored inside the record for verification.
    """
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json"


class JsonFilePersistenceBackend:
    """
    One JSON document per REI batch, under a directory.

    Writes are atomic (temp file + `os.replace`) so a crash mid-write cannot
    leave a truncated record that would later be read as a valid batch.

    Suitable for a single-process deployment. It is NOT a substitute for a
    database under concurrent writers from multiple processes — see the module
    docstring and the Phase 1 limitations.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------

    def save(self, key: str, registry: FacilityResilienceRegistry) -> None:
        """Write a batch atomically. Never raises — persistence is best-effort."""
        path = self.directory / _safe_filename(key)
        record = {
            "format_version": PERSISTENCE_FORMAT_VERSION,
            "cache_key": key,
            "registry": registry.model_dump(mode="json"),
        }
        try:
            with self._lock:
                fd, tmp = tempfile.mkstemp(dir=str(self.directory), suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(record, handle, default=str)
                    os.replace(tmp, path)     # atomic on POSIX and Windows
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            logger.info(
                "rei.persistence.saved batch_id=%s key=%s path=%s",
                registry.batch_id, key, path.name,
            )
        except Exception as exc:  # noqa: BLE001 - durability must not break a run
            logger.error("rei.persistence.save_failed key=%s error=%s", key, exc)

    def load(self, key: str) -> Optional[FacilityResilienceRegistry]:
        """
        Read a batch back, or None.

        A record written under a different format version, or one whose stored
        key disagrees with the requested key, is IGNORED rather than trusted —
        serving a mismatched batch is worse than recomputing.
        """
        path = self.directory / _safe_filename(key)
        if not path.exists():
            return None
        try:
            with self._lock:
                with path.open("r", encoding="utf-8") as handle:
                    record = json.load(handle)

            if record.get("format_version") != PERSISTENCE_FORMAT_VERSION:
                logger.warning(
                    "rei.persistence.format_mismatch key=%s stored=%s expected=%s",
                    key, record.get("format_version"), PERSISTENCE_FORMAT_VERSION,
                )
                return None
            if record.get("cache_key") != key:
                logger.warning("rei.persistence.key_mismatch path=%s", path.name)
                return None

            registry = FacilityResilienceRegistry.model_validate(record["registry"])
            logger.info(
                "rei.persistence.loaded batch_id=%s key=%s", registry.batch_id, key,
            )
            return registry
        except Exception as exc:  # noqa: BLE001 - a corrupt record must not crash a run
            logger.error("rei.persistence.load_failed key=%s error=%s", key, exc)
            return None

    def delete(self, key: str) -> bool:
        path = self.directory / _safe_filename(key)
        try:
            with self._lock:
                if path.exists():
                    path.unlink()
                    return True
        except Exception as exc:  # noqa: BLE001
            logger.error("rei.persistence.delete_failed key=%s error=%s", key, exc)
        return False

    def keys(self) -> List[str]:
        """Cache keys of every stored batch."""
        found: List[str] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    record = json.load(handle)
                if record.get("format_version") == PERSISTENCE_FORMAT_VERSION:
                    found.append(record.get("cache_key", ""))
            except Exception:  # noqa: BLE001
                continue
        return [k for k in found if k]

    @property
    def is_durable(self) -> bool:
        return True

    def stats(self) -> Dict[str, object]:
        return {
            "backend": "json_file",
            "directory": str(self.directory),
            "records": len(list(self.directory.glob("*.json"))),
            "durable": True,
        }
