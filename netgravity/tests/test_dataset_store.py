"""
The upload audit trail, and the store behind it.

Two failures shared one cause — a module-level dict in the ingestion blueprint:

  * a solved project reopened its uploader to "Uploaded Files (0)", so nobody
    could audit the data behind a decision; and
  * `commit` could not find what `upload-and-parse` produced whenever the two
    calls landed on different worker processes, which is every multi-worker
    deployment.
"""

from __future__ import annotations

import pytest

from app.backend.services.dataset_store import DatasetStore


@pytest.fixture()
def store():
    return DatasetStore()


PREVIEW = {
    "files": [{"name": "network.xlsx", "rows": 10782, "sheets": ["Facilities"]}],
    "mapping": {"network.xlsx": [{"source": "Distance_Miles", "mapped": "Distance (miles, converted to km)"}]},
    "mapStats": {"detected": 147, "auto": 67, "review": 0, "ignored": 80},
    "dataQuality": {"totalRecords": 10782, "validPct": 99.9},
    "integrity": [{"type": "Orphan reference", "detail": "L099", "missingIds": ["L099"]}],
    "currency": "USD",
    "geography": {"region": "United States"},
    "structure": {"lanes": [{"laneId": "L001"}] * 500},
}


class TestTheUploadRecordSurvivesTheProcessThatMadeIt:
    def test_a_preview_is_readable_back(self, store):
        store.put_preview("pr-1", PREVIEW)
        assert store.preview("pr-1")["mapStats"]["auto"] == 67

    def test_a_preview_is_scoped_to_its_project(self, store):
        store.put_preview("pr-1", PREVIEW)
        assert store.preview("pr-2") is None

    def test_it_writes_through_to_the_host(self, store):
        written = {}
        store.bind_persistence(lambda pid, doc: written.__setitem__(pid, doc),
                               lambda: {})
        store.put_preview("pr-1", PREVIEW)
        assert "pr-1" in written, (
            "an upload record that lives only in one process cannot be "
            "committed by another, and cannot survive a restart"
        )

    def test_it_restores_what_was_written(self, store):
        saved = {"pr-9": {"project_id": "pr-9", "committed": {"snapshot_id": "snap_x"}}}
        store.bind_persistence(lambda pid, doc: None, lambda: saved)
        assert store.load() == 1
        assert store.committed("pr-9")["snapshot_id"] == "snap_x"


class TestCommittingRecordsWhatTheUserConfirmed:
    def test_the_committed_record_keeps_the_review_findings(self, store):
        store.put_preview("pr-1", PREVIEW)
        store.record_commit("pr-1", snapshot_id="snap_1",
                            network_summary={"facilities": 12},
                            assumptions=["miles converted"], issues=["M002 short"])
        c = store.committed("pr-1")
        assert c["mapStats"]["auto"] == 67
        assert c["dataQuality"]["validPct"] == 99.9
        assert c["integrity"][0]["missingIds"] == ["L099"]
        assert c["currency"] == "USD"
        assert c["assumptions"] == ["miles converted"]
        assert c["issues"] == ["M002 short"]
        assert c["snapshot_id"] == "snap_1"

    def test_the_parsed_rows_are_not_stored_twice(self, store):
        """The network assembled from them is already held by the snapshot
        manager; keeping several megabytes of the client's own rows beside it
        doubles the storage of every project and buys nothing."""
        store.put_preview("pr-1", PREVIEW)
        store.record_commit("pr-1", snapshot_id="snap_1",
                            network_summary={}, assumptions=[], issues=[])
        assert "structure" not in store.committed("pr-1")

    def test_committing_clears_the_pending_preview(self, store):
        store.put_preview("pr-1", PREVIEW)
        store.record_commit("pr-1", snapshot_id="snap_1",
                            network_summary={}, assumptions=[], issues=[])
        assert store.preview("pr-1") is None
        assert store.committed("pr-1") is not None

    def test_a_new_upload_does_not_erase_the_running_dataset(self, store):
        """Re-uploading is not the same as discarding what the analysis on
        screen was computed from."""
        store.put_preview("pr-1", PREVIEW)
        store.record_commit("pr-1", snapshot_id="snap_1",
                            network_summary={}, assumptions=[], issues=[])
        store.put_preview("pr-1", {**PREVIEW, "mapStats": {"auto": 1}})
        assert store.committed("pr-1")["snapshot_id"] == "snap_1"
        assert store.preview("pr-1")["mapStats"]["auto"] == 1

    def test_history_is_bounded(self, store):
        for i in range(15):
            store.put_preview("pr-1", PREVIEW)
            store.record_commit("pr-1", snapshot_id=f"snap_{i}",
                                network_summary={}, assumptions=[], issues=[])
        history = store.history("pr-1")
        assert len(history) == 10, "a long-running project must not grow forever"
        assert history[-1]["snapshot_id"] == "snap_14"

    def test_the_audit_view_omits_the_raw_rows(self, store):
        store.put_preview("pr-1", PREVIEW)
        record = store.record("pr-1")
        assert "structure" not in record["preview"]
        assert record["preview"]["mapStats"]["auto"] == 67
