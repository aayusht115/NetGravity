"""
NetGravity — Uploaded Dataset Store
===================================
The record of what was uploaded to a project, how it was read, and what was
committed from it. Keyed by `project_id`, written through to the database.

Why this exists
---------------
Two separate failures shared one cause: the upload preview lived in a
process-global dict.

  * **The audit trail was unreachable.** After completing an analysis, opening
    Upload Data showed "Uploaded Files (0)" and "No files uploaded yet" — for a
    solved project. There was no read-only view of the active file, the mapping
    decisions, the quality findings or the ingestion time. A user could not
    verify the data behind a decision, diagnose a mapping error, or safely
    replace a dataset without creating a new project.

  * **The application could not scale past one worker.** `upload-and-parse`
    wrote the parsed structure into memory; `commit` read it back. Under any
    multi-process deployment — gunicorn with two workers, two containers behind
    a load balancer — the two calls land on different processes and the commit
    fails with "there is no parsed upload for this project to commit" on an
    upload that plainly succeeded. It also grew without bound: nothing ever
    evicted a preview.

So the record is durable, and it distinguishes two lifecycle stages that were
previously one blob:

  * a PREVIEW — parsed, not yet analysable, replaced by the next upload;
  * a COMMITTED dataset — bound to a snapshot, and the thing a reader is
    auditing when they ask "what produced this number?".

This module does not parse, map, assemble or solve. It remembers.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DatasetStore:
    """Per-project upload records, write-through to durable storage."""

    #: (project_id, document) -> None
    _persist = None
    #: () -> {project_id: document}
    _restore = None

    def __init__(self) -> None:
        self._by_project: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    # -- durability ----------------------------------------------------
    def bind_persistence(self, persist, restore) -> None:
        self._persist = persist
        self._restore = restore

    def load(self) -> int:
        if self._restore is None:
            return 0
        restored = 0
        for project_id, document in (self._restore() or {}).items():
            if not isinstance(document, dict):
                continue
            with self._lock:
                self._by_project[project_id] = document
            restored += 1
        logger.info("dataset_store.loaded projects=%d", restored)
        return restored

    def _write_through(self, project_id: str) -> None:
        if self._persist is None:
            return
        with self._lock:
            document = self._by_project.get(project_id)
        if document is not None:
            self._persist(project_id, document)

    # -- preview -------------------------------------------------------
    def put_preview(self, project_id: str, preview: Dict[str, Any]) -> None:
        """
        Record a parsed upload, replacing any earlier uncommitted one.

        The committed history is preserved across a new preview: a user
        re-uploading is not a user erasing what the current analysis ran on.
        """
        with self._lock:
            record = self._by_project.setdefault(project_id, {})
            record["project_id"] = project_id
            record["preview"] = preview
            record["preview_at"] = time.time()
        self._write_through(project_id)

    def preview(self, project_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._by_project.get(project_id) or {}
            return record.get("preview")

    # -- commit --------------------------------------------------------
    def record_commit(
        self, project_id: str, *,
        snapshot_id: str,
        network_summary: Dict[str, Any],
        assumptions: List[str],
        issues: List[str],
    ) -> Dict[str, Any]:
        """
        Promote the current preview to the project's committed dataset.

        Keeps the mapping decisions, quality findings and integrity problems
        that were shown at review time — not a re-derivation of them. What a
        user confirmed is what an audit has to be able to read back.

        The parsed `structure` is dropped from the committed record: it is
        several megabytes of the client's own rows, and the network assembled
        from it is already held by the snapshot manager. Keeping it twice buys
        nothing and doubles the storage of every project.
        """
        with self._lock:
            record = self._by_project.setdefault(project_id, {})
            preview = dict(record.get("preview") or {})
            preview.pop("structure", None)

            committed = {
                "snapshot_id": snapshot_id,
                "committed_at": time.time(),
                "files": preview.get("files", []),
                "mapping": preview.get("mapping", {}),
                "mapStats": preview.get("mapStats", {}),
                "dataQuality": preview.get("dataQuality", {}),
                "integrity": preview.get("integrity", []),
                "currency": preview.get("currency"),
                "geography": preview.get("geography", {}),
                "network_summary": network_summary,
                "assumptions": list(assumptions),
                "issues": list(issues),
                # What the upload did NOT contain, as measured at review time.
                # Carried across the commit because the preview is dropped
                # below and this is exactly when it starts mattering: the
                # analysis has now run on this data, and the gaps are what
                # would have made it better.
                "completeness": preview.get("completeness", {}),
            }
            history = list(record.get("history") or [])
            history.append(committed)
            # Bounded: an audit needs the current dataset and what preceded it,
            # not every upload a long-running project ever made.
            record["history"] = history[-10:]
            record["committed"] = committed
            # The preview has been acted on; it is no longer pending.
            record.pop("preview", None)
        self._write_through(project_id)
        logger.info("dataset.committed project_id=%s snapshot_id=%s",
                    project_id, snapshot_id)
        return committed

    def committed(self, project_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return (self._by_project.get(project_id) or {}).get("committed")

    def history(self, project_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list((self._by_project.get(project_id) or {}).get("history") or [])

    def record(self, project_id: str) -> Dict[str, Any]:
        """Everything known about this project's data, for the audit view."""
        with self._lock:
            record = dict(self._by_project.get(project_id) or {})
        preview = record.get("preview")
        if preview:
            # The structure is the client's rows; the audit view needs the
            # decisions, not the payload.
            preview = {k: v for k, v in preview.items() if k != "structure"}
        return {
            "project_id": project_id,
            "preview": preview,
            "preview_at": record.get("preview_at"),
            "committed": record.get("committed"),
            "history": list(record.get("history") or []),
        }

    def forget(self, project_id: str) -> None:
        with self._lock:
            self._by_project.pop(project_id, None)
        self._write_through(project_id)


#: Process-wide singleton, bound to durable storage at application start.
dataset_store = DatasetStore()
