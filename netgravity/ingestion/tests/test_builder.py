"""Builder, snapshot and storage tests — the seam with the engine."""

from __future__ import annotations

import pytest

from netgravity.ingestion.adapters import structured
from netgravity.ingestion.builder import NetworkBuildError, build_network, summarise
from netgravity.ingestion.snapshot import (
    latest_version,
    load_snapshot,
    read_manifest,
    save_snapshot,
)
from netgravity.validation.checks import validate_network


def _build(sample_dir):
    src = structured.ingest_directory(sample_dir)
    network, issues = build_network(
        facilities=src.facilities, products=src.products,
        demands=src.demands, lanes=src.lanes,
    )
    return network, issues


def test_builder_produces_a_valid_canonical_network(sample_dir):
    network, _ = _build(sample_dir)
    assert len(network.facilities) == 4
    assert len(network.lanes) == 3
    assert network.data_version, "builder must stamp a content hash"


def test_engine_validation_passes_on_the_fixture(sample_dir):
    """The engine's own pre-solve checks must accept what ingestion produces."""
    network, _ = _build(sample_dir)
    report = validate_network(network)
    assert report.is_valid, [i.description for i in report.errors]


def test_summarise_separates_facilities_from_markets(sample_dir):
    network, _ = _build(sample_dir)
    counts = summarise(network)
    assert counts["facilities"] == 2
    assert counts["markets"] == 2
    assert counts["lanes"] == 3


def test_builder_refuses_a_network_with_no_facilities():
    with pytest.raises(NetworkBuildError):
        build_network(facilities=[], products=[], demands=[], lanes=[])


def test_data_version_is_deterministic(sample_dir):
    """Identical inputs must always produce the same version id."""
    a, _ = _build(sample_dir)
    b, _ = _build(sample_dir)
    assert a.data_version == b.data_version


def test_data_version_changes_when_data_changes(sample_dir):
    network, _ = _build(sample_dir)
    before = network.data_version

    src = structured.ingest_directory(sample_dir)
    src.demands[0].quantity += 1          # one unit of difference
    changed, _ = build_network(
        facilities=src.facilities, products=src.products,
        demands=src.demands, lanes=src.lanes,
    )
    assert changed.data_version != before


# --- snapshots --------------------------------------------------------------

def test_snapshot_round_trip(sample_dir, tmp_storage):
    network, _ = _build(sample_dir)
    save_snapshot(network, tmp_storage, label="test")

    restored = load_snapshot(network.data_version, tmp_storage)
    assert restored.data_version == network.data_version
    assert len(restored.facilities) == len(network.facilities)
    assert len(restored.lanes) == len(network.lanes)


def test_snapshot_is_idempotent(sample_dir, tmp_storage):
    """Re-running on unchanged inputs must not accumulate duplicate versions."""
    network, _ = _build(sample_dir)
    save_snapshot(network, tmp_storage)
    save_snapshot(network, tmp_storage)

    versions = [e["data_version"] for e in read_manifest(tmp_storage)]
    assert versions.count(network.data_version) == 1


def test_latest_version_tracks_the_manifest(sample_dir, tmp_storage):
    assert latest_version(tmp_storage) is None
    network, _ = _build(sample_dir)
    save_snapshot(network, tmp_storage)
    assert latest_version(tmp_storage) == network.data_version


# --- storage ----------------------------------------------------------------

def test_storage_round_trip(tmp_storage):
    tmp_storage.save_text("raw", "vendor/a/file.txt", "hello")
    assert tmp_storage.get_text("raw", "vendor/a/file.txt") == "hello"
    assert tmp_storage.exists("raw", "vendor/a/file.txt")
    assert "vendor/a/file.txt" in tmp_storage.list("raw")


def test_storage_rejects_path_traversal(tmp_storage):
    """A malicious or malformed key must not escape its zone."""
    with pytest.raises(ValueError):
        tmp_storage.save("raw", "../../etc/passwd", b"nope")


def test_missing_object_raises_file_not_found(tmp_storage):
    with pytest.raises(FileNotFoundError):
        tmp_storage.get("curated", "does_not_exist.json")
