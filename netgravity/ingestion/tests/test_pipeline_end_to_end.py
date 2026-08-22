"""
End-to-end tests.

The final one is the acceptance test for this whole module: real CSV files in,
a solved optimum out, with no UI and no API key involved.
"""

from __future__ import annotations

import pytest

from netgravity.ingestion import run_ingestion
from netgravity.ingestion.snapshot import load_snapshot
from netgravity.ingestion.storage import get_storage


def test_full_run_on_the_india_dataset(india_dir, tmp_config):
    result = run_ingestion(india_dir, config=tmp_config, label="test run")
    report = result.report

    assert report.ok, [i.render() for i in report.errors]
    assert report.network_assembled
    assert result.network is not None
    assert report.engine_validation_passed is True


def test_india_network_has_the_expected_shape(india_dir, tmp_config):
    result = run_ingestion(india_dir, config=tmp_config)
    counts = result.report.counts
    assert counts["facilities"] == 10        # 4 plants + 5 DCs + 1 candidate
    assert counts["markets"] == 10
    assert counts["products"] == 1
    assert counts["demands"] == 10
    assert counts["lanes"] > 30


def test_run_works_with_no_api_key(india_dir, tmp_config):
    """Stub mode must carry the entire pipeline, and say so honestly."""
    assert tmp_config.stub_mode is True
    result = run_ingestion(india_dir, config=tmp_config)

    ai_files = [f for f in result.report.files if f.ai_used]
    assert ai_files, "expected at least one AI-backed adapter to run"
    assert all(f.ai_stubbed for f in ai_files), \
        "with no key, every AI result must be marked stubbed"


def test_snapshot_is_written_and_reloadable(india_dir, tmp_config):
    result = run_ingestion(india_dir, config=tmp_config)
    assert result.report.snapshot_path

    storage = get_storage(tmp_config)
    restored = load_snapshot(result.report.data_version, storage)
    assert restored.data_version == result.network.data_version
    assert len(restored.facilities) == len(result.network.facilities)


def test_dry_run_writes_nothing(india_dir, tmp_config):
    result = run_ingestion(india_dir, config=tmp_config, save=False)
    assert result.report.network_assembled
    assert result.report.snapshot_path is None

    storage = get_storage(tmp_config)
    assert storage.list("curated") == []


def test_missing_source_directory_fails_cleanly(tmp_path, tmp_config):
    result = run_ingestion(tmp_path / "nope", config=tmp_config)
    assert result.network is None
    assert "error" in result.report.extras


def test_guardrail_runs_over_the_seeded_signals(india_dir, tmp_config):
    result = run_ingestion(india_dir, config=tmp_config)
    assert result.signals, "expected seeded signals to be ingested"

    passed = [s for s in result.signals if s.passed_guardrail]
    filtered = [s for s in result.signals if not s.passed_guardrail]
    assert passed and filtered, "the seed set should exercise both outcomes"
    assert all(s.verdict is not None for s in result.signals), \
        "every signal must carry an auditable verdict"


def test_competitor_signal_never_reaches_the_optimizer(india_dir, tmp_config):
    from netgravity.ingestion.schemas.signal import SignalBucket

    result = run_ingestion(india_dir, config=tmp_config)
    competitor = [s for s in result.signals if s.bucket == SignalBucket.COMPETITOR]
    assert competitor, "seed data should include a competitor signal"
    assert not any(s.passed_guardrail for s in competitor)


@pytest.mark.slow
def test_ingested_network_solves_to_optimal(india_dir, tmp_config):
    """
    THE ACCEPTANCE TEST.

    CSV files -> validated -> CanonicalNetwork -> MILP -> a real optimum.
    Before this module existed, the engine had only ever solved a hardcoded
    fixture. This proves the two halves are actually joined.
    """
    from netgravity.optimization.milp import solve

    result = run_ingestion(india_dir, config=tmp_config, save=False)
    assert result.network is not None

    solved = solve(result.network)
    assert solved.solver.status.value == "OPTIMAL"
    assert solved.kpis is not None
    assert solved.kpis.total_cost > 0
    # Every unit of demand must be served — the network has ample capacity
    assert solved.kpis.unmet_demand == 0
    assert solved.kpis.total_served == pytest.approx(solved.kpis.total_demand)
