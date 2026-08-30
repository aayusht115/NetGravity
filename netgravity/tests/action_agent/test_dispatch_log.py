"""
Tests for netgravity.action_agent.dispatch_log.

Claim under test: `already_dispatched(trigger_type, reference_id)` is the
sole dedup mechanism for triggers 3/4 (recommendation/investigate) — it
must distinguish trigger_type and reference_id independently, so a
recommendation and an investigate email for the SAME reference_id (which
cannot normally happen, but the check must not conflate them) are tracked
separately, and two different reference_ids of the same trigger_type never
collide.
"""

from __future__ import annotations

from netgravity.action_agent.dispatch_log import DispatchLogStore, DispatchRecord


def test_not_dispatched_before_any_record(aa_storage):
    log = DispatchLogStore(aa_storage)
    assert log.already_dispatched("recommendation", "appr_123") is False


def test_dispatched_after_recording(aa_storage):
    log = DispatchLogStore(aa_storage)
    log.record(DispatchRecord(trigger_type="recommendation", reference_id="appr_123",
                              recipients=["a@b.com"], subject="s", result="stubbed"))

    assert log.already_dispatched("recommendation", "appr_123") is True


def test_trigger_type_and_reference_id_are_independent_keys(aa_storage):
    log = DispatchLogStore(aa_storage)
    log.record(DispatchRecord(trigger_type="recommendation", reference_id="exec_1",
                              recipients=["a@b.com"], subject="s", result="stubbed"))

    # Different trigger_type, same reference_id -> not the same dispatch.
    assert log.already_dispatched("investigate", "exec_1") is False
    # Different reference_id, same trigger_type -> not the same dispatch.
    assert log.already_dispatched("recommendation", "exec_2") is False


def test_list_all_returns_every_recorded_dispatch(aa_storage):
    log = DispatchLogStore(aa_storage)
    log.record(DispatchRecord(trigger_type="required_data", reference_id="ing_1",
                              recipients=["a@b.com"], subject="s1", result="stubbed"))
    log.record(DispatchRecord(trigger_type="optional_data", reference_id="ing_1",
                              recipients=["a@b.com"], subject="s2", result="stubbed"))

    records = log.list_all()
    assert len(records) == 2
    assert {r.trigger_type for r in records} == {"required_data", "optional_data"}
