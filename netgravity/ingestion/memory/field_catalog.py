"""Client-scoped catalogue for fields that are not canonical MILP inputs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from netgravity.ingestion.field_aliases import normalise_name
from netgravity.ingestion.schemas.field_mapping import FieldDisposition
from netgravity.ingestion.storage.base import StorageBackend

CATALOG_ZONE = "standardized"
CATALOG_PREFIX = "field_catalog"


def _safe(value: str) -> str:
    return normalise_name(value) or "unknown"


@dataclass
class CatalogEntry:
    client_id: str
    content_type: str
    source_column: str
    disposition: FieldDisposition
    definition: str = ""
    unit: Optional[str] = None
    period: Optional[str] = None
    confirmed_by: str = "human"
    confirmed_at: str = ""
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "client_id": self.client_id,
            "content_type": self.content_type,
            "source_column": self.source_column,
            "disposition": self.disposition.value,
            "definition": self.definition,
            "unit": self.unit,
            "period": self.period,
            "confirmed_by": self.confirmed_by,
            "confirmed_at": self.confirmed_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CatalogEntry":
        try:
            disposition = FieldDisposition(str(raw.get("disposition")))
        except ValueError:
            disposition = FieldDisposition.UNRESOLVED
        return cls(
            client_id=str(raw.get("client_id") or "default"),
            content_type=str(raw.get("content_type") or "UNKNOWN"),
            source_column=str(raw.get("source_column") or ""),
            disposition=disposition,
            definition=str(raw.get("definition") or ""),
            unit=raw.get("unit") or None,
            period=raw.get("period") or None,
            confirmed_by=str(raw.get("confirmed_by") or "human"),
            confirmed_at=str(raw.get("confirmed_at") or ""),
            note=str(raw.get("note") or ""),
        )


class FieldCatalog:
    """Structured source of truth for supplementary and ignored fields."""

    def __init__(self, storage: StorageBackend, client_id: str = "default"):
        self.storage = storage
        self.client_id = client_id or "default"

    def _key(self, content_type: str, source_column: str) -> str:
        return (f"{CATALOG_PREFIX}/{_safe(self.client_id)}/"
                f"{_safe(content_type)}/{_safe(source_column)}.json")

    def resolve(self, content_type: str, source_column: str) -> Optional[CatalogEntry]:
        try:
            raw = self.storage.get_text(
                CATALOG_ZONE, self._key(content_type, source_column))
            return CatalogEntry.from_dict(json.loads(raw))
        except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def record(self, *, content_type: str, source_column: str,
               disposition: FieldDisposition, definition: str = "",
               unit: Optional[str] = None, period: Optional[str] = None,
               confirmed_by: str = "human", note: str = "") -> CatalogEntry:
        entry = CatalogEntry(
            client_id=self.client_id,
            content_type=content_type,
            source_column=source_column,
            disposition=disposition,
            definition=definition,
            unit=unit,
            period=period,
            confirmed_by=confirmed_by,
            confirmed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            note=note,
        )
        self.storage.save_text(
            CATALOG_ZONE, self._key(content_type, source_column),
            json.dumps(entry.as_dict(), indent=2, sort_keys=True),
        )
        return entry
