"""
NetGravity — Ingestion Pipeline Orchestrator
=============================================
Wires the adapters, validation, builder, snapshot and (optionally) contracts
and signals into a single run.

FLOW
----
    source files
        -> adapters        (parse + row-level validation)
        -> builder         (assemble CanonicalNetwork)
        -> engine checks   (netgravity.validation.checks — reused, not rewritten)
        -> snapshot        (content-addressed, immutable)
        -> IngestionReport (what the CLI prints)

Contracts and signals are optional side-channels: they enrich the run but are
never required for it to succeed, so a missing contracts/ folder or an absent
LLM key degrades gracefully instead of failing the pipeline.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from netgravity.ingestion.builder import build_network, summarise
from netgravity.ingestion.config import IngestionConfig, load_config
from netgravity.ingestion.schemas.contract import ContractRule
from netgravity.ingestion.schemas.ingest_result import IngestionReport, Severity
from netgravity.ingestion.schemas.mapping import DistributorMapping
from netgravity.ingestion.schemas.signal import MarketIntelligenceSignal
from netgravity.ingestion.snapshot import save_snapshot
from netgravity.ingestion.storage import get_storage
from netgravity.schemas.network import CanonicalNetwork
from netgravity.validation.checks import validate_network


class IngestionResult:
    """Everything one run produced."""

    def __init__(self, report: IngestionReport,
                 network: Optional[CanonicalNetwork] = None,
                 contracts: Optional[List[ContractRule]] = None,
                 signals: Optional[List[MarketIntelligenceSignal]] = None,
                 distributor_mappings: Optional[List[DistributorMapping]] = None,
                 tabular=None):
        self.report = report
        self.network = network
        self.contracts = contracts or []
        self.signals = signals or []
        self.distributor_mappings = distributor_mappings or []
        #: TabularResult from the unified path, or None. Carries the mappings
        #: and whatever is awaiting human confirmation — this is what a
        #: review screen or HTTP endpoint reads.
        self.tabular = tabular

    @property
    def review_request(self):
        """Everything awaiting a human, ready to serialise. Empty if none."""
        from netgravity.ingestion.review import ReviewRequest
        if self.tabular is None:
            return ReviewRequest(run_id=self.report.run_id)
        request = self.tabular.review_request
        request.run_id = self.report.run_id
        return request

    @property
    def ok(self) -> bool:
        return self.report.ok and self.network is not None


def run_ingestion(
    source: Path,
    *,
    config: Optional[IngestionConfig] = None,
    save: bool = True,
    include_contracts: bool = True,
    include_signals: bool = True,
    include_distributors: bool = True,
    label: str = "",
    unified: bool = False,
    auto_confirm: bool = False,
    catalog_scope: str = "default",
    content_type_overrides: Optional[dict] = None,
) -> IngestionResult:
    """
    Execute a full ingestion run against a source directory.

    `unified` selects the rebuilt tabular path (tabular.py): any CSV/Excel,
    any filename, every sheet, classified from its content and routed by what
    it turns out to be rather than which folder it sat in.

    It is OPT-IN rather than the default. The two paths have been verified to
    produce a byte-identical network on the sample data — same data_version
    hash — but the unified path holds optimiser-bound mappings until they are
    confirmed, which changes the shape of a first run. `auto_confirm` settles
    those without a human for unattended runs, recorded as machine-confirmed.
    """
    cfg = config or load_config()
    source = Path(source)
    storage = get_storage(cfg)

    report = IngestionReport(
        run_id=uuid.uuid4().hex[:12],
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source=str(source),
    )

    if not source.exists():
        report.extras["error"] = f"source directory not found: {source}"
        return IngestionResult(report)

    # --- 1. Tabular path ---------------------------------------------------
    from netgravity.ingestion.adapters import structured

    tabular_outcome = None
    if unified:
        from netgravity.ingestion import tabular

        tabular_outcome = tabular.ingest_tabular(
            source, cfg, storage, auto_confirm=auto_confirm,
            catalog_scope=catalog_scope,
            content_type_overrides=content_type_overrides)
        report.files.extend(tabular_outcome.results)

        parsed = tabular.parse_into_records(tabular_outcome)
        report.files.extend(parsed["results"])

        src = structured.StructuredSource()
        src.facilities = parsed["facilities"]
        src.products = parsed["products"]
        src.demands = parsed["demands"]
        src.lanes = parsed["lanes"]

        if save and tabular_outcome.staging_rows:
            tabular.save_staging(tabular_outcome, storage, source.name)

        pending = tabular_outcome.review_request
        if not pending.is_empty:
            report.extras["Awaiting review"] = pending.summary
        if tabular_outcome.held:
            report.extras["Held (unidentified)"] = ", ".join(
                m.origin_label for m in tabular_outcome.held)
    else:
        src = structured.ingest_directory(source)
        report.files.extend(src.results)

    # --- 2. Contracts (AI or stub) ---------------------------------------
    distributor_mappings: List[DistributorMapping] = []

    contracts: List[ContractRule] = []
    if include_contracts:
        contract_dir = source / "contracts"
        if contract_dir.exists():
            from netgravity.ingestion.adapters import contracts as contracts_adapter
            contracts, results = contracts_adapter.ingest_directory(
                contract_dir, cfg, storage=storage
            )
            report.files.extend(results)

    # --- 2b. Distributor files (AI or stub) ------------------------------
    # Standardised rows are written to the standardized zone rather than fed
    # straight into the network: distributor data is transactional shipment
    # history, not network structure. It becomes forecasting input (Layer 3),
    # so ingesting it must never silently alter the Digital Twin.
    # The unified path already read every tabular file, distributor folder
    # included, and classified them by content. Running the legacy
    # distributor adapter as well would ingest the same rows twice.
    if include_distributors and not unified:
        distributor_dir = source / "distributors"
        if distributor_dir.exists():
            from netgravity.ingestion.adapters import distributor as dist_adapter
            known_ids = {f.id for f in src.facilities}
            rows, mappings, results = dist_adapter.ingest_directory(
                distributor_dir, cfg, storage, known_ids
            )
            report.files.extend(results)
            distributor_mappings.extend(mappings)
            if rows and save:
                import json
                storage.save_text(
                    "standardized",
                    f"distributor_rows/{source.name}.json",
                    json.dumps(rows, indent=2, default=str),
                )
            needs_review = sum(len(m.needs_review) for m in mappings)
            if mappings:
                report.extras["Distributor mappings"] = (
                    f"{len(mappings)} file format(s) mapped, "
                    f"{needs_review} column(s) flagged for human confirmation"
                )

    # --- 3. External signals (AI or stub) + guardrail ---------------------
    signals: List[MarketIntelligenceSignal] = []
    if include_signals:
        signal_dir = source / "signals"
        if signal_dir.exists():
            from netgravity.ingestion.adapters import signals as signals_adapter
            known_ids = {f.id for f in src.facilities}
            signals, results = signals_adapter.ingest_directory(signal_dir, cfg, known_ids)
            report.files.extend(results)
            passed = sum(1 for s in signals if s.passed_guardrail)
            report.extras["External signals"] = (
                f"{passed} passed guardrail, {len(signals) - passed} filtered "
                f"(all retained for audit)"
            )

    # --- 3b. Data completeness (deterministic, no model call) -------------
    if tabular_outcome is not None:
        from netgravity.ingestion.completeness import check_completeness

        completeness = check_completeness(tabular_outcome, has_contracts=bool(contracts))
        report.missing_required = [m.as_dict() for m in completeness.missing_required]
        report.missing_optional = [m.as_dict() for m in completeness.missing_optional]

    # --- 4. Assemble ------------------------------------------------------
    if not src.facilities or not src.products:
        report.extras["error"] = (
            "not enough data to assemble a network "
            f"({len(src.facilities)} facilities, {len(src.products)} products)"
        )
        return IngestionResult(report, contracts=contracts, signals=signals,
                               distributor_mappings=distributor_mappings,
                               tabular=tabular_outcome)

    try:
        network, build_issues = build_network(
            facilities=src.facilities,
            products=src.products,
            demands=src.demands,
            lanes=src.lanes,
            network_id=f"netgravity_{source.name}",
            description=label or f"Ingested from {source}",
            contracts=contracts,
        )
    except Exception as exc:
        report.extras["error"] = f"network assembly failed: {exc}"
        return IngestionResult(report, contracts=contracts, signals=signals,
                               distributor_mappings=distributor_mappings,
                               tabular=tabular_outcome)

    if build_issues and report.files:
        report.files[0].issues.extend(build_issues)

    # --- 4b. Contractual site commitments --------------------------------
    #
    # `build_network(contracts=...)` applies the RATE side of a contract. The
    # commitment side — whether a site may be closed at all — is applied here,
    # onto the assembled network, because it changes `FacilityRecord` fields the
    # MILP constrains rather than a lane's cost.
    #
    # Until now nothing set those fields, so constraint C5c and validation check
    # V-015 were both structurally present and permanently inert: a plan could
    # recommend closing a site the client was contractually unable to close.
    from netgravity.ingestion.contracts_to_network import apply_contract_rules

    commitment_result = apply_contract_rules(network, contracts)
    if commitment_result.changed_anything:
        network = commitment_result.network
        report.extras["Contractual commitments"] = (
            f"{commitment_result.n_applied} site commitment(s) applied; "
            f"{len(commitment_result.pinned_open)} facility(ies) held open, "
            f"{len(commitment_result.priced_exit)} with a stated exit penalty"
        )
    if commitment_result.assumptions or commitment_result.warnings:
        report.extras["Contract notes"] = " | ".join(
            commitment_result.assumptions + commitment_result.warnings)

    report.network_assembled = True
    report.counts = summarise(network)
    report.data_version = network.data_version

    if report.missing_required:
        # Structurally assembled (so the review/draft screens have something
        # to show), but not usable for analysis: required fields are missing
        # from named entities. Reuses the exact gate finalize() already
        # checks (network_assembled) rather than adding a second one.
        report.network_assembled = False
        report.extras["missing_required_data"] = (
            f"{len(report.missing_required)} required field(s) missing across "
            f"named entities — see 'missing_required' for detail. This dataset "
            f"cannot be finalized until they are provided."
        )

    # --- 5. Engine's own pre-solve validation (reused, not duplicated) ----
    engine_report = validate_network(network)
    report.engine_validation_passed = engine_report.is_valid
    report.engine_validation_issues = [
        f"{i.severity} [{i.code}] {i.description}" for i in engine_report.issues
    ]

    # --- 6. Persist -------------------------------------------------------
    if save:
        locator = save_snapshot(network, storage, label=label, source=str(source))
        report.snapshot_path = locator

        if src.history:
            import json
            storage.save_text(
                "standardized",
                f"history/{source.name}.json",
                json.dumps(src.history, indent=2, default=str),
            )

    return IngestionResult(report, network=network, contracts=contracts,
                           signals=signals,
                           distributor_mappings=distributor_mappings,
                           tabular=tabular_outcome)
