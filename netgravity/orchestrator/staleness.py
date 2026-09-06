"""
NetGravity — Card Staleness
=============================
A "card" here is whatever the Action Agent already points at: an
ApprovalRequest (recommendation, pinned to a baseline_snapshot_id /
scenario_id / scenario_version at creation — see
orchestrator/schemas/actions.py) or a bare execution_id (investigate — no
ApprovalRequest exists for a HUMAN_ONLY classification).

`SnapshotManager.assert_fresh()` (orchestrator/state/stores.py) already
IS the staleness check for observed snapshots — an execution pins a
snapshot_id at intake, and assert_fresh raises the moment the observed
network moves past it. This module reuses that logic in a read-only,
non-raising form so a deep-link landing page can show a warning banner
instead of a 500.

WHAT THIS DELIBERATELY DOES NOT DO
-----------------------------------
It never re-runs anything. Recomputing a recommendation against a newer
snapshot is a full governance/reasoning re-run — the orchestrator's job, not
the Action Agent's, which never originates a recommendation or runs its own
scenario. `refresh_if_stale()` therefore returns a diagnostic and a
human-readable warning, not a recomputed result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class StalenessResult:
    is_stale: bool
    pinned_snapshot_id: Optional[str]
    current_snapshot_id: Optional[str]
    message: str = ""


def _pinned_snapshot_id(card: Any) -> Optional[str]:
    """
    `card` is either an ApprovalRequest (baseline_snapshot_id) or an
    ExecutionContext (also baseline_snapshot_id) — same attribute name on
    both, so no branching needed.
    """
    return getattr(card, "baseline_snapshot_id", None)


def check_staleness(card: Any, snapshots: Any) -> StalenessResult:
    """
    `snapshots` is the orchestrator's SnapshotManager
    (orchestrator.snapshots). Read-only: never raises, never mutates.
    """
    pinned = _pinned_snapshot_id(card)
    current = snapshots.current_id

    if pinned is None:
        return StalenessResult(is_stale=False, pinned_snapshot_id=None,
                               current_snapshot_id=current,
                               message="no snapshot was pinned to this card")

    is_stale = current is not None and pinned != current
    message = (
        f"the observed network has changed since this was computed "
        f"(pinned to {pinned}, current is {current}) — the numbers below "
        f"may no longer be current"
        if is_stale else ""
    )
    return StalenessResult(is_stale=is_stale, pinned_snapshot_id=pinned,
                           current_snapshot_id=current, message=message)


def is_stale(card: Any, snapshots: Any) -> bool:
    return check_staleness(card, snapshots).is_stale


def refresh_if_stale(card: Any, snapshots: Any) -> StalenessResult:
    """
    Deliberately does not recompute anything (see module docstring) — this
    is the read-time diagnostic a deep-link landing page checks before
    showing Approve/Edit/Reject, not a re-run trigger.
    """
    return check_staleness(card, snapshots)
