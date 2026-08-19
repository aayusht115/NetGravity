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
from netgravity.ingestion.schemas.signal import ExternalSignal
from netgravity.ingestion.snapshot import save_snapshot
from netgravity.ingestion.storage import get_storage
from netgravity.schemas.network import CanonicalNetwork
from netgravity.validation.checks import validate_network


class IngestionResult:
    """Everything one run produced."""

    def __init__(self, report: IngestionReport,
                 network: Optional[CanonicalNetwork] = None,
                 contracts: Optional[List[ContractRule]] = None,
                 signals: Optional[List[ExternalSignal]] = None,
                 distributor_mappings: Optional[List[DistributorMapping]] = None):
        self.report = report
        self.network = network
        self.contracts = contracts or []
        self.signals = signals or []
        self.distributor_mappings = distributor_mappings or []

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
) -> IngestionResult:
    """Execute a full ingestion run against a source directory."""
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

    # --- 1. Structured path (no AI) --------------------------------------
    from netgravity.ingestion.adapters import structured

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
    if include_distributors:
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
    signals: List[ExternalSignal] = []
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

    # --- 4. Assemble ------------------------------------------------------
    if not src.facilities or not src.products:
        report.extras["error"] = (
            "not enough data to assemble a network "
            f"({len(src.facilities)} facilities, {len(src.products)} products)"
        )
        return IngestionResult(report, contracts=contracts, signals=signals,
                               distributor_mappings=distributor_mappings)

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
                               distributor_mappings=distributor_mappings)

    if build_issues and report.files:
        report.files[0].issues.extend(build_issues)

    report.network_assembled = True
    report.counts = summarise(network)
    report.data_version = network.data_version

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
                           distributor_mappings=distributor_mappings)
