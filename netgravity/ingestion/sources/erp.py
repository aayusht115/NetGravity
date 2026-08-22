"""
NetGravity — ERP / WMS Source
==============================
STATUS: DELIBERATE STUB. Not implemented. Raises rather than pretending.

WHY IT EXISTS UNBUILT
---------------------
ERP and WMS systems are expected to be the primary production source, but
which system (SAP, Oracle, Blue Yonder, a bespoke WMS) is not yet decided,
and each needs different connection code. Guessing now would mean writing a
"generic ERP connector" that matches nothing real.

What IS settled is the contract: whatever the system turns out to be, it
must hand back RecordSets like every other source. Everything downstream —
classification, column mapping, human review, memory, routing — is already
written against RecordSet, so building this connector later is additive.
Nothing else has to change.

This file follows the same discipline as storage/azure_blob.py and the
Azure OpenAI branch in ai/client.py: an unbuilt path FAILS LOUDLY and names
the real gap, instead of silently degrading to something that looks like it
worked.

WHAT A REAL IMPLEMENTATION NEEDS
--------------------------------
    connection    endpoint/host + auth (OAuth client credentials, API key,
                  or DB connection string), read from config, never literals
    discovery     which tables/endpoints to pull (an explicit allow-list is
                  safer than "everything" — an ERP has thousands of tables)
    paging        record_sets() yields lazily; a real table will not fit in
                  memory, so pull page by page
    incremental   a watermark (last-modified / change-token) so repeat runs
                  transfer deltas, not the whole table
    field labels  most ERPs expose technical column codes (MARA-MATNR) AND a
                  human label. Pass BOTH into RecordSet.columns context where
                  possible: the label is what makes AI mapping accurate, the
                  code is what stays stable across runs.

FIRST-CONNECTION BEHAVIOUR
--------------------------
By design this is NOT special-cased here. A newly connected system is simply
a source_id the memory layer has never seen, so every field lands in review
exactly once, the analyst confirms, and it is remembered from then on. That
is the same path a new vendor's spreadsheet takes. One mechanism, not two.
"""

from __future__ import annotations

from typing import Iterator, Optional

from netgravity.ingestion.sources.base import DataSource, RecordSet


class ErpSource(DataSource):
    """Placeholder for a live ERP/WMS connection."""

    source_type = "erp"

    def __init__(self, system_id: str, connection: Optional[str] = None,
                 tables: Optional[list] = None):
        self.system_id = system_id
        self.connection = connection
        self.tables = tables or []

    @property
    def source_id(self) -> str:
        return self.system_id

    def record_sets(self) -> Iterator[RecordSet]:
        raise NotImplementedError(
            f"ERP/WMS ingestion is not implemented yet (system '{self.system_id}'). "
            f"The pipeline contract is settled — this connector must yield RecordSet "
            f"objects like any other source — but the connection layer (auth, table "
            f"discovery, paging, incremental watermark) is deliberately unbuilt until "
            f"the target system is chosen. See the module docstring for the full "
            f"checklist. Use a file export from the system in the meantime: it goes "
            f"through the identical classify -> map -> review -> remember path."
        )


class WmsSource(ErpSource):
    """
    Same contract, separate class so the two can diverge without a rewrite.

    A WMS typically exposes movement/transaction data (which classification
    routes to the staging zone as forecasting input) where an ERP more often
    exposes master data (which routes to the optimiser-facing network). That
    difference lands in classification, not here — but keeping the classes
    distinct means a WMS-specific quirk later does not have to be bolted onto
    the ERP path.
    """

    source_type = "wms"
