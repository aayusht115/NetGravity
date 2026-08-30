"""
Tests for netgravity.orchestrator.staleness.

Claim under test: a card (ApprovalRequest or bare execution context) pinned
to a baseline_snapshot_id that no longer matches the SnapshotManager's
current_id is reported stale, read-only — never raising, never recomputing
anything. This mirrors what SnapshotManager.assert_fresh() already enforces
for a live execution, just as a non-raising diagnostic for a deep-link
landing page instead.
"""

from __future__ import annotations

from types import SimpleNamespace

from netgravity.orchestrator.staleness import check_staleness, is_stale, refresh_if_stale


def _card(baseline_snapshot_id):
    return SimpleNamespace(baseline_snapshot_id=baseline_snapshot_id)


def _snapshots(current_id):
    return SimpleNamespace(current_id=current_id)


def test_matching_snapshot_is_not_stale():
    card = _card("snap_abc")
    result = check_staleness(card, _snapshots("snap_abc"))
    assert result.is_stale is False
    assert result.message == ""


def test_changed_snapshot_is_stale_with_a_message():
    card = _card("snap_abc")
    result = check_staleness(card, _snapshots("snap_def"))
    assert result.is_stale is True
    assert "snap_abc" in result.message
    assert "snap_def" in result.message


def test_no_pinned_snapshot_is_never_stale():
    card = _card(None)
    result = check_staleness(card, _snapshots("snap_def"))
    assert result.is_stale is False


def test_is_stale_matches_check_staleness():
    card = _card("snap_old")
    snapshots = _snapshots("snap_new")
    assert is_stale(card, snapshots) == check_staleness(card, snapshots).is_stale


def test_refresh_if_stale_never_raises_and_returns_same_diagnostic():
    card = _card("snap_old")
    snapshots = _snapshots("snap_new")
    result = refresh_if_stale(card, snapshots)
    assert result.is_stale is True
    assert result.pinned_snapshot_id == "snap_old"
    assert result.current_snapshot_id == "snap_new"
