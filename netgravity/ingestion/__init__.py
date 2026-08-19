"""
NetGravity — Data Ingestion Pipeline
=====================================
Turns real-world source files into a validated CanonicalNetwork the
optimisation engine can solve.

    from netgravity.ingestion import run_ingestion
    result = run_ingestion("data/mock/india")
    network = result.network        # a CanonicalNetwork, ready for the MILP

FOUR SOURCE PATHS
-----------------
    structured    ERP / WMS / TMS exports        deterministic, no AI
    distributor   messy per-distributor Excel    AI column mapping
    contracts     PDFs / rate cards              AI clause extraction
    signals       external news & macro          AI structuring + guardrail

DESIGN RULES
------------
1. The engine is never modified. netgravity/schemas and netgravity/validation
   are imported, never edited. Deleting this package leaves the engine intact.
2. All file access goes through storage/ — local disk today, Azure Blob later,
   switched by one environment variable.
3. Every AI call goes through ai/client.py. With no API key set it runs in
   stub mode, so the pipeline and its tests work without credentials.
4. Contract surcharges never overwrite contracted rates; they are a separate
   adjustment layer so headline and effective cost stay visible side by side.
"""

from netgravity.ingestion.builder import build_network
from netgravity.ingestion.config import IngestionConfig, load_config
from netgravity.ingestion.pipeline import IngestionResult, run_ingestion
from netgravity.ingestion.snapshot import load_snapshot, save_snapshot

__all__ = [
    "run_ingestion", "IngestionResult",
    "build_network",
    "load_config", "IngestionConfig",
    "save_snapshot", "load_snapshot",
]

__version__ = "0.1.0"
