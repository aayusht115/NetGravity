"""Shared fixtures for the ingestion test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.storage.local import LocalStorage

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "india_sample"
REPO_DATA = Path(__file__).resolve().parents[3] / "data" / "mock" / "india"


@pytest.fixture
def sample_dir() -> Path:
    """Tiny, fast fixture network — 2 facilities, 2 markets, 1 product."""
    return FIXTURE_DIR


@pytest.fixture
def india_dir() -> Path:
    """The full India mock dataset, skipped if it is not present."""
    if not REPO_DATA.exists():
        pytest.skip("full India dataset not present")
    return REPO_DATA


@pytest.fixture
def tmp_config(tmp_path) -> IngestionConfig:
    """Config pointing at a throwaway data root, with no LLM key (stub mode)."""
    cfg = IngestionConfig(data_root=tmp_path, storage_backend="local", llm_api_key=None)
    for zone in ("raw", "standardized", "curated"):
        (tmp_path / zone).mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def tmp_storage(tmp_config) -> LocalStorage:
    return LocalStorage(tmp_config.data_root)
