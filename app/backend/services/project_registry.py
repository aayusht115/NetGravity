"""
NetGravity — Project Registry & Network Snapshot Binding
========================================================
Closes the single decisive architectural gap found in the Phase 10.0 forensic
audit (P0-1 / P0-2).

Before this module the application layer built ONE process-global orchestrator
at import time from `build_case16_network()` — a fixture the code itself labels
"FABRICATED demonstration data" — and every project pointed at it. Uploaded
customer data, though fully parsed, validated and assembled into a real
`CanonicalNetwork` by `netgravity/ingestion/`, was never handed to the engine.

Nothing new is invented here. The orchestrator already exposes exactly the two
primitives required, and both are already tested:

  * `Orchestrator.register_network(network) -> snapshot_id`
        content-addressed, deep-copied, thread-safe (`state/stores.py:82`)
  * `OrchestratorRequest(network_snapshot_id=...)`
        an explicitly pinned snapshot ALWAYS wins over the current one
        (`execution_context.py:642-646`)

This module is the missing edge between them: it owns `project -> snapshot_id`,
enforces ownership, and refuses to answer analytical questions for a project
that has no network bound rather than borrowing another project's.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.backend.services.errors import (
    ForbiddenError,
    NoNetworkBoundError,
    NotFoundError,
    ValidationError,
)

logger = logging.getLogger(__name__)


@dataclass
class ProjectRecord:
    """
    A workspace owned by exactly one user and bound to at most one snapshot.

    `snapshot_id` is None until data has been ingested and finalized. That None
    is load-bearing: it is what makes "this project has no network yet" a
    representable state instead of a silent fallback to synthetic data.
    """

    project_id: str
    name: str
    owner_id: str
    #: Where this network is. Empty until the data says.
    #:
    #: This defaulted to "India" — on the dataclass, in `create()`, in the
    #: loader and in the API — so a US workbook produced a project labelled
    #: "India Network" on every screen that names the geography, priced in
    #: rupees, drawn on an India basemap. The default was not a placeholder
    #: a user could correct: the creation form offered no other option.
    #:
    #: `region_source` records how the value was arrived at — "user" when
    #: chosen at creation, "inferred" when derived from the uploaded
    #: coordinates, "" when still unknown — so a screen can show a derived
    #: label as derived rather than as a stated fact.
    region: str = ""
    region_source: str = ""
    client: str = ""
    description: str = ""
    status: str = "Draft"
    snapshot_id: Optional[str] = None
    snapshot_label: str = ""
    is_demo: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Ingestion runs that produced this project's network, newest last.
    ingestion_run_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.project_id,
            "name": self.name,
            "owner_id": self.owner_id,
            "region": self.region,
            "region_source": self.region_source,
            "client": self.client,
            "description": self.description,
            "status": self.status,
            "snapshot_id": self.snapshot_id,
            "snapshot_label": self.snapshot_label,
            "is_demo": self.is_demo,
            # Explicit so the UI can render an honest empty state rather than
            # assuming a network exists.
            "has_network": self.snapshot_id is not None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ingestion_run_ids": list(self.ingestion_run_ids),
        }

    def stored(self) -> Dict[str, Any]:
        """
        The record as the DATABASE holds it, field for field.

        Deliberately not `to_dict()`. That is the API projection: it renames
        `project_id` to `id` for the client and adds a derived `has_network`.
        Persisting it and reading it back looked right and silently restored
        nothing, because the loader looks for `project_id` and every row had
        `id`. Two shapes, two named methods, so neither can stand in for the
        other by accident.
        """
        return {
            "project_id": self.project_id,
            "name": self.name,
            "owner_id": self.owner_id,
            "region": self.region,
            "region_source": self.region_source,
            "client": self.client,
            "description": self.description,
            "status": self.status,
            "snapshot_id": self.snapshot_id,
            "snapshot_label": self.snapshot_label,
            "is_demo": self.is_demo,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ingestion_run_ids": list(self.ingestion_run_ids),
        }


class ProjectRegistry:
    """
    Project ownership and network-snapshot binding.

    Every read path takes the requesting `user_id` and raises `ForbiddenError`
    on a cross-owner access. That is deliberately a 403 and not a 404: the
    isolation boundary should be observable in logs and assertable in tests.
    """

    def __init__(self, orchestrator: Optional[Any] = None) -> None:
        self._orchestrator = orchestrator
        self._projects: Dict[str, ProjectRecord] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Durability
    # ------------------------------------------------------------------
    def load(self) -> None:
        """
        Rebuild the project list from the database.

        Projects lived only in the dictionary above, so restarting the server
        deleted every workspace a user had created along with the binding to
        their uploaded network. Idempotent; the demo workspace is re-seeded
        separately and is not persisted.
        """
        with self._lock:
            if self._loaded:
                return
            self._loaded = True

        from app.backend.services import persistence

        restored = 0
        for doc in persistence.load_projects():
            if not doc.get("project_id"):
                continue
            record = ProjectRecord(
                project_id=doc["project_id"],
                name=doc.get("name", "Untitled Project"),
                owner_id=doc.get("owner_id", ""),
                region=doc.get("region") or "",
                region_source=doc.get("region_source") or "",
                client=doc.get("client", ""),
                description=doc.get("description", ""),
                status=doc.get("status", "Draft"),
                snapshot_id=doc.get("snapshot_id"),
                snapshot_label=doc.get("snapshot_label", ""),
                is_demo=bool(doc.get("is_demo")),
                created_at=float(doc.get("created_at") or time.time()),
                updated_at=float(doc.get("updated_at") or time.time()),
                ingestion_run_ids=list(doc.get("ingestion_run_ids") or []),
            )
            with self._lock:
                self._projects[record.project_id] = record
            restored += 1
        logger.info("project.registry.loaded projects=%d", restored)

    def _persist(self, record: "ProjectRecord") -> None:
        """Write one project through to disk. A failed write never fails a request."""
        if record.is_demo:
            # Re-seeded from the bundled network on every start, so persisting
            # it would leave a stale copy pointing at a snapshot id that the
            # next start does not reproduce.
            return
        from app.backend.services import persistence
        persistence.guarded(persistence.save_project)(
            record.project_id, record.owner_id, record.stored(), record.updated_at,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        owner_id: str,
        region: str = "",
        client: str = "",
        description: str = "",
    ) -> ProjectRecord:
        name = (name or "").strip()
        if not name:
            raise ValidationError("Project name is required.")
        if not owner_id:
            raise ValidationError("An owner is required to create a project.")

        record = ProjectRecord(
            project_id=f"pr-{uuid.uuid4().hex[:10]}",
            name=name,
            owner_id=owner_id,
            region=(region or "").strip(),
            region_source=("user" if (region or "").strip() else ""),
            client=(client or "").strip(),
            description=(description or "").strip(),
            # A new project genuinely has no analysable network until data is
            # ingested, and says so.
            status="Awaiting data",
        )
        with self._lock:
            self._projects[record.project_id] = record
        self._persist(record)
        logger.info("project.created project_id=%s owner=%s", record.project_id, owner_id)
        return record

    def get(self, project_id: str, *, user_id: str) -> ProjectRecord:
        with self._lock:
            record = self._projects.get(project_id)
        if record is None:
            raise NotFoundError(f"Project '{project_id}' not found.")
        self._assert_access(record, user_id)
        return record

    def list_all(self) -> List[ProjectRecord]:
        """
        Every project, regardless of owner. For start-up reporting only.

        Deliberately NOT exposed through the API: every request path takes a
        `user_id` and enforces ownership, and adding an unscoped read to that
        surface is how a cross-tenant leak starts.
        """
        with self._lock:
            return list(self._projects.values())

    def list_for(self, user_id: str) -> List[ProjectRecord]:
        """Only the caller's own projects, plus shared demo workspaces."""
        with self._lock:
            records = [
                r for r in self._projects.values()
                if r.owner_id == user_id or r.is_demo
            ]
        return sorted(records, key=lambda r: r.updated_at, reverse=True)

    def update(self, project_id: str, *, user_id: str, **fields: Any) -> ProjectRecord:
        record = self.get(project_id, user_id=user_id)
        if record.is_demo and record.owner_id != user_id:
            raise ForbiddenError("Shared demo workspaces are read-only.")
        mutable = {"name", "region", "client", "description", "status"}
        with self._lock:
            for key, value in fields.items():
                if key in mutable and value is not None:
                    setattr(record, key, str(value))
            record.updated_at = time.time()
        self._persist(record)
        return record

    # ------------------------------------------------------------------
    # Snapshot binding — the edge that was missing
    # ------------------------------------------------------------------
    def bind_network(
        self,
        project_id: str,
        network: Any,
        *,
        user_id: str,
        label: str = "",
        ingestion_run_id: str = "",
    ) -> str:
        """
        Register an ingested `CanonicalNetwork` with the orchestrator and bind
        the resulting snapshot to this project.

        Registered with `make_current=False` on purpose: one project's upload
        must not silently redirect another project's analysis by moving the
        orchestrator's global "current" pointer. Requests always pin their
        snapshot explicitly, so nothing depends on that pointer.
        """
        record = self.get(project_id, user_id=user_id)
        if self._orchestrator is None:
            raise NoNetworkBoundError(
                "The analysis engine is not available, so no network can be bound.",
                context={"project_id": project_id},
            )

        snapshot = self._orchestrator.snapshots.register(
            network,
            label=label or f"{record.name} ({project_id})",
            make_current=False,
        )

        # Where the uploaded network actually is, from its own coordinates.
        #
        # Only fills a blank: a region the user chose at creation is their
        # statement about their own business and is never overwritten by an
        # inference. This is what stops a US workbook being labelled India —
        # not by defaulting differently, but by reading the data.
        inferred_region = ""
        geography = getattr(network, "geography", None) or {}
        if isinstance(geography, dict):
            inferred_region = str(geography.get("region") or "").strip()

        with self._lock:
            record.snapshot_id = snapshot.snapshot_id
            record.snapshot_label = label or record.name
            record.status = "Analysis ready"
            record.updated_at = time.time()
            if inferred_region and not record.region:
                record.region = inferred_region
                record.region_source = "inferred"
            if ingestion_run_id:
                record.ingestion_run_ids.append(ingestion_run_id)

        # The binding is the single most important thing to keep: without it a
        # restart leaves the network in the database and the project unable to
        # find it.
        self._persist(record)
        logger.info(
            "project.network.bound project_id=%s snapshot_id=%s run_id=%s",
            project_id, snapshot.snapshot_id, ingestion_run_id or "-",
        )
        return snapshot.snapshot_id

    def snapshot_for(self, project_id: str, *, user_id: str) -> str:
        """
        The snapshot a project's analysis must run against.

        Raises `NoNetworkBoundError` when the project has no network. This is
        the honest answer, and it is why no caller in this application can
        accidentally analyse synthetic data on a customer's behalf.
        """
        record = self.get(project_id, user_id=user_id)
        if not record.snapshot_id:
            raise NoNetworkBoundError(
                "This project has no network bound yet. Upload and confirm a "
                "dataset before requesting analysis.",
                context={"project_id": project_id, "project_name": record.name},
            )
        return record.snapshot_id

    # ------------------------------------------------------------------
    # Demo seeding
    # ------------------------------------------------------------------
    def seed_demo_project(self, network: Any, *, label: str = "Case-16 synthetic") -> Optional[ProjectRecord]:
        """
        Register the bundled synthetic network as an explicitly-labelled demo
        workspace, visible to every user and owned by none.

        Kept because it is the only network available in an offline environment,
        but it is now (a) named as synthetic in the record itself, (b) flagged
        `is_demo`, and (c) no longer the implicit default that real projects
        silently inherit.
        """
        if self._orchestrator is None or network is None:
            return None

        snapshot = self._orchestrator.snapshots.register(network, label=label, make_current=True)
        record = ProjectRecord(
            project_id="pr-demo-case16",
            name="Case-16 Demo Network (synthetic)",
            owner_id="__system__",
            # The bundled fixture genuinely is an India network, so this one is
            # a statement of fact about known data, not a default.
            region="India",
            region_source="fixture",
            client="Demonstration",
            description=(
                "Bundled synthetic demonstration network. The figures produced "
                "for this workspace are computed by the real engines, but the "
                "underlying network is fabricated sample data, not observed."
            ),
            status="Analysis ready",
            snapshot_id=snapshot.snapshot_id,
            snapshot_label=label,
            is_demo=True,
        )
        with self._lock:
            self._projects[record.project_id] = record
        logger.info("project.demo.seeded snapshot_id=%s", snapshot.snapshot_id)
        return record

    # ------------------------------------------------------------------
    def _assert_access(self, record: ProjectRecord, user_id: str) -> None:
        if record.is_demo:
            return
        if record.owner_id != user_id:
            logger.warning(
                "project.access.denied project_id=%s owner=%s requester=%s",
                record.project_id, record.owner_id, user_id,
            )
            raise ForbiddenError(
                "This project belongs to another user.",
                context={"project_id": record.project_id},
            )


# Single application-wide instance; bound to the orchestrator at app start.
project_registry = ProjectRegistry()
