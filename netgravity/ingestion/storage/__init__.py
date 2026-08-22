"""Storage backends. Use get_storage(config) — never instantiate directly."""

from __future__ import annotations

from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.storage.base import StorageBackend
from netgravity.ingestion.storage.local import LocalStorage

__all__ = ["StorageBackend", "LocalStorage", "get_storage"]


def get_storage(config: IngestionConfig) -> StorageBackend:
    """
    Factory: returns the backend named by NETGRAVITY_STORAGE_BACKEND.
    This single function is the whole local -> Azure switch.
    """
    backend = (config.storage_backend or "local").lower()

    if backend == "local":
        return LocalStorage(config.data_root)

    if backend == "azure_blob":
        from netgravity.ingestion.storage.azure_blob import AzureBlobStorage
        return AzureBlobStorage(config.azure_connection_string or "")

    raise ValueError(
        f"Unknown storage backend '{backend}'. Expected 'local' or 'azure_blob'."
    )
