"""
NetGravity — Storage Abstraction
=================================
The ONLY place in the ingestion package that knows where bytes physically live.

No other module may open a file path directly. Everything goes through this
interface, which means moving from a laptop to Azure Blob Storage is a
one-class swap driven by an environment variable, not a code change.

Keys are POSIX-style relative paths within a zone, e.g.:
    zone="raw",     key="distributors/north/2026-08-19/shipments.xlsx"
    zone="curated", key="a4f9c2e1b8d3.json"
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List


class StorageBackend(ABC):
    """Minimal blob-like interface. Implemented by LocalStorage and AzureBlobStorage."""

    @abstractmethod
    def save(self, zone: str, key: str, data: bytes) -> str:
        """Write bytes. Returns a locator string (path or blob URL)."""

    @abstractmethod
    def get(self, zone: str, key: str) -> bytes:
        """Read bytes. Raises FileNotFoundError if absent."""

    @abstractmethod
    def exists(self, zone: str, key: str) -> bool:
        ...

    @abstractmethod
    def list(self, zone: str, prefix: str = "") -> List[str]:
        """List keys within a zone, optionally filtered by prefix."""

    @abstractmethod
    def locator(self, zone: str, key: str) -> str:
        """Human-readable location, for reports and provenance fields."""

    # --- convenience wrappers (identical for every backend) ---

    def save_text(self, zone: str, key: str, text: str, encoding: str = "utf-8") -> str:
        return self.save(zone, key, text.encode(encoding))

    def get_text(self, zone: str, key: str, encoding: str = "utf-8") -> str:
        return self.get(zone, key).decode(encoding)
