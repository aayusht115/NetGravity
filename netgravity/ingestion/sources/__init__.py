"""
NetGravity — Data Sources
==========================
One interface, several origins. See base.py for why.

    FileSource / discover()   CSV + Excel (every sheet), implemented
    ErpSource / WmsSource     live systems, deliberate stub
"""

from netgravity.ingestion.sources.base import DataSource, RecordOrigin, RecordSet
from netgravity.ingestion.sources.erp import ErpSource, WmsSource
from netgravity.ingestion.sources.files import (
    TABULAR_SUFFIXES,
    FileSource,
    discover,
)

__all__ = [
    "DataSource", "RecordOrigin", "RecordSet",
    "FileSource", "discover", "TABULAR_SUFFIXES",
    "ErpSource", "WmsSource",
]
