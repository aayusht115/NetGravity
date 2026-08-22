"""
NetGravity — Azure Blob Storage Backend
========================================
STATUS: Deployment stub. Not exercised locally.

The zone/key layout is identical to LocalStorage, so keys written locally
resolve unchanged against Blob containers:

    zone "raw"          -> container "raw"          (immutable, versioning on)
    zone "standardized" -> container "standardized"
    zone "curated"      -> container "curated"

To activate on Azure:
    pip install azure-storage-blob
    NETGRAVITY_STORAGE_BACKEND=azure_blob
    NETGRAVITY_AZURE_CONN_STR=<from Key Vault>

The azure-storage-blob dependency is imported lazily so it is NOT required
for local development or CI.
"""

from __future__ import annotations

from typing import List

from netgravity.ingestion.storage.base import StorageBackend


class AzureBlobStorage(StorageBackend):
    def __init__(self, connection_string: str):
        if not connection_string:
            raise ValueError(
                "Azure Blob backend selected but NETGRAVITY_AZURE_CONN_STR is not set."
            )
        self.connection_string = connection_string
        self._service = None

    def _client(self):
        if self._service is None:
            try:
                from azure.storage.blob import BlobServiceClient  # lazy import
            except ImportError as exc:  # pragma: no cover - deployment-only path
                raise ImportError(
                    "azure-storage-blob is not installed. "
                    "Run `pip install azure-storage-blob` to use the azure_blob backend."
                ) from exc
            self._service = BlobServiceClient.from_connection_string(self.connection_string)
        return self._service

    def _blob(self, zone: str, key: str):
        return self._client().get_blob_client(container=zone, blob=key)

    def save(self, zone: str, key: str, data: bytes) -> str:  # pragma: no cover
        self._blob(zone, key).upload_blob(data, overwrite=True)
        return self.locator(zone, key)

    def get(self, zone: str, key: str) -> bytes:  # pragma: no cover
        return self._blob(zone, key).download_blob().readall()

    def exists(self, zone: str, key: str) -> bool:  # pragma: no cover
        return self._blob(zone, key).exists()

    def list(self, zone: str, prefix: str = "") -> List[str]:  # pragma: no cover
        container = self._client().get_container_client(zone)
        return [b.name for b in container.list_blobs(name_starts_with=prefix)]

    def locator(self, zone: str, key: str) -> str:  # pragma: no cover
        return f"azure://{zone}/{key}"
