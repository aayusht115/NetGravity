"""
NetGravity — Ingestion Configuration
=====================================
Single source of truth for every path, mode and credential the ingestion
pipeline needs. Nothing in the ingestion package may construct a filesystem
path or read an environment variable directly — it all comes from here.

WHY THIS MATTERS FOR AZURE
--------------------------
When this pipeline moves to Azure, no ingestion code changes. Only the
environment variables below change:

    NETGRAVITY_STORAGE_BACKEND   local  ->  azure_blob
    NETGRAVITY_DATA_ROOT         ./data ->  (unused; container names apply)
    NETGRAVITY_LLM_API_KEY       .env   ->  Azure Key Vault secret reference

ENVIRONMENT VARIABLES
---------------------
    NETGRAVITY_DATA_ROOT          Root folder for data zones (default: <repo>/data)
    NETGRAVITY_STORAGE_BACKEND    "local" | "azure_blob"   (default: local)
    NETGRAVITY_AZURE_CONN_STR     Azure Blob connection string (azure_blob only)
    NETGRAVITY_LLM_API_KEY        LLM provider key. If ABSENT, the AI client
                                  runs in STUB MODE and returns canned
                                  responses so the pipeline still runs.
    NETGRAVITY_LLM_MODEL          Model identifier (default below)
    NETGRAVITY_LLM_PROVIDER       "anthropic" (default). Provider is isolated
                                  in ai/client.py — swapping is a one-file change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Repository root = three levels up from this file
# netgravity/ingestion/config.py -> netgravity/ingestion -> netgravity -> <repo root>
REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Data zones
# ---------------------------------------------------------------------------
# Borrowed from data-lake practice: raw is never edited, standardized is the
# post-mapping intermediate, curated holds immutable versioned snapshots.
# This is a naming convention today and Blob containers on Azure later.

ZONE_RAW = "raw"
ZONE_STANDARDIZED = "standardized"
ZONE_CURATED = "curated"

STUB_MODE_BANNER = (
    "AI STUB MODE — no LLM API key found; returning canned responses. "
    "Set NETGRAVITY_LLM_API_KEY to enable live extraction."
)


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


@dataclass
class IngestionConfig:
    """Resolved configuration for one ingestion run."""

    # --- Paths ---
    data_root: Path = field(default_factory=lambda: Path(_env("NETGRAVITY_DATA_ROOT", str(REPO_ROOT / "data"))))

    # --- Storage backend ---
    storage_backend: str = field(default_factory=lambda: _env("NETGRAVITY_STORAGE_BACKEND", "local"))
    azure_connection_string: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_AZURE_CONN_STR"))

    # --- LLM ---
    # PLACEHOLDER: leave unset until the team confirms provider + key.
    # Absent key => stub mode => pipeline still runs end to end.
    llm_api_key: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_LLM_API_KEY"))
    llm_provider: str = field(default_factory=lambda: _env("NETGRAVITY_LLM_PROVIDER", "anthropic"))
    llm_model: str = field(default_factory=lambda: _env("NETGRAVITY_LLM_MODEL", "claude-sonnet-4-5"))

    # --- Behaviour ---
    # Rows failing a WARNING-level check are kept but flagged; ERROR-level rows
    # are always dropped from the assembled network.
    strict: bool = False

    @property
    def stub_mode(self) -> bool:
        """True when no LLM key is configured — AI adapters return canned data."""
        return not bool(self.llm_api_key)

    def zone_path(self, zone: str) -> Path:
        return self.data_root / zone

    @property
    def raw_path(self) -> Path:
        return self.zone_path(ZONE_RAW)

    @property
    def standardized_path(self) -> Path:
        return self.zone_path(ZONE_STANDARDIZED)

    @property
    def curated_path(self) -> Path:
        return self.zone_path(ZONE_CURATED)

    def describe(self) -> str:
        lines = [
            f"  data root       : {self.data_root}",
            f"  storage backend : {self.storage_backend}",
            f"  LLM provider    : {self.llm_provider} ({self.llm_model})",
            f"  LLM mode        : {'STUB (no key set)' if self.stub_mode else 'LIVE'}",
            f"  strict mode     : {self.strict}",
        ]
        return "\n".join(lines)


def load_config(strict: bool = False) -> IngestionConfig:
    """Build an IngestionConfig from the current environment."""
    cfg = IngestionConfig()
    cfg.strict = strict
    return cfg
