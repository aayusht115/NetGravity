"""
NetGravity — Versioned Network Snapshots
=========================================
Writes an assembled CanonicalNetwork to the curated zone, keyed by the
engine's own deterministic content hash (compute_data_version()).

WHY NO DATABASE (YET)
---------------------
CanonicalNetwork already carries compute_data_version() — a SHA-256 of its
own inputs — and a data_version field. That gives immutable, reproducible,
content-addressed versioning with zero extra infrastructure. The same inputs
always produce the same version id; different inputs never collide.

A relational store earns its place once we need cross-run HISTORY (scenario
comparison, KPI trends, approval audit trails). That is Layer 5/7 work, not
ingestion. When it arrives, these snapshots become rows without changing how
ingestion writes them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from netgravity.ingestion.config import ZONE_CURATED
from netgravity.ingestion.storage.base import StorageBackend
from netgravity.schemas.network import CanonicalNetwork

MANIFEST_KEY = "_manifest.json"


def snapshot_key(data_version: str) -> str:
    return f"{data_version}.json"


def save_snapshot(network: CanonicalNetwork, storage: StorageBackend,
                  *, label: str = "", source: str = "") -> str:
    """
    Persist the network. Returns the locator (path or blob URL).

    Snapshots are content-addressed and therefore idempotent: re-running
    ingestion on unchanged inputs overwrites the identical file rather than
    accumulating duplicates.
    """
    version = network.data_version or network.compute_data_version()
    network.data_version = version

    payload = network.model_dump(mode="json")
    body = json.dumps(payload, indent=2, sort_keys=True, default=str)
    locator = storage.save_text(ZONE_CURATED, snapshot_key(version), body)

    _append_manifest(storage, version, label=label, source=source,
                     description=network.description)
    return locator


def load_snapshot(data_version: str, storage: StorageBackend) -> CanonicalNetwork:
    """Rehydrate a previously saved network by its version id."""
    body = storage.get_text(ZONE_CURATED, snapshot_key(data_version))
    return CanonicalNetwork.model_validate(json.loads(body))


def latest_version(storage: StorageBackend) -> Optional[str]:
    """Most recently recorded version id, or None if nothing saved yet."""
    entries = read_manifest(storage)
    if not entries:
        return None
    return entries[-1].get("data_version")


def read_manifest(storage: StorageBackend) -> List[dict]:
    try:
        return json.loads(storage.get_text(ZONE_CURATED, MANIFEST_KEY))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _append_manifest(storage: StorageBackend, version: str, *,
                     label: str, source: str, description: str) -> None:
    """
    Maintain a small append-only index of snapshots.

    Kept deliberately simple — it is a convenience for humans and the CLI,
    not a source of truth. The snapshots themselves are authoritative.
    """
    entries = read_manifest(storage)
    entries = [e for e in entries if e.get("data_version") != version]
    entries.append({
        "data_version": version,
        "label": label,
        "source": source,
        "description": description,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    storage.save_text(ZONE_CURATED, MANIFEST_KEY,
                      json.dumps(entries, indent=2, sort_keys=True))
