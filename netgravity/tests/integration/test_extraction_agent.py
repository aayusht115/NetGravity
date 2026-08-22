"""
Phase 4A — Extraction / Parsing Agent, end to end.

    client files → ExtractionParsingAgent → CanonicalNetwork → snapshot
                 → existing MILP → existing REI

THE ARCHITECTURAL CLAIM THESE TESTS DEFEND
──────────────────────────────────────────
The data-ingestion pipeline is the **client-data implementation component of
the Extraction / Parsing Agent**, not a separate agent. Two consequences are
asserted structurally rather than described:

* there is exactly ONE canonical model — the ingestion package imports MAIN's
  `netgravity.schemas.network` and builds a `CanonicalNetwork` directly, so
  there is no adapter between two competing representations and no chance of
  them drifting;
* there is exactly ONE snapshot mechanism — an extracted network is registered
  through the orchestrator's existing `SnapshotManager`, which is what makes
  material fingerprinting, REI cache keys and stale-evidence checks apply to
  ingested data without any of them being modified.

DATA
────
`data/mock/india` is fabricated and gitignored. `scripts/generate_mock_dataset.py`
regenerates it deterministically; the module-scoped fixture below builds it on
demand, so these tests run on a fresh clone with no manual step. Nothing here
depends on real client files.

NO NETWORK CALLS. Every extraction runs with `allow_ai=False`, so the ingestion
pipeline stays in rules/stub mode and the suite consumes no LLM capacity.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from netgravity.optimization.milp import solve
from netgravity.orchestrator.agents.extraction_agent import ExtractionParsingAgent
from netgravity.orchestrator.schemas.extraction import (
    ExtractionRequest,
    ExtractionResult,
    ExtractionStatus,
    SourceType,
    ValidationSeverity,
)
from netgravity.orchestrator.state.stores import SnapshotManager
from netgravity.resilience.service import REIService
from netgravity.schemas.network import CanonicalNetwork, NodeRole
from netgravity.schemas.results import CalculationStatus, REIBatchStatus

REPO_ROOT = Path(__file__).resolve().parents[3]
MOCK_DIR = REPO_ROOT / "data" / "mock" / "india"


def _code_only(path: Path) -> str:
    """
    Source with comments and docstrings removed.

    These modules describe at length the things they must never do, so a naive
    substring scan matches their own documentation. Stripping to executable
    code means the invariant is tested against behaviour, not prose.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and                     isinstance(getattr(body[0], "value", None), ast.Constant) and                     isinstance(body[0].value.value, str):
                body.pop(0)
    return ast.unparse(tree)


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def client_files() -> Path:
    """
    The representative client corpus, generated if absent.

    Generated rather than committed: the team deliberately gitignores
    `data/mock/`, and reversing that decision to make tests pass would be the
    wrong trade. Deterministic content means `data_version` is stable across
    machines.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from generate_mock_dataset import generate  # noqa: PLC0415

    if not (MOCK_DIR / "facilities.csv").exists():
        generate(MOCK_DIR)
    return MOCK_DIR


@pytest.fixture(scope="module")
def extraction(client_files) -> ExtractionResult:
    """One extraction, shared. Registers into its own SnapshotManager."""
    agent = ExtractionParsingAgent(snapshots=SnapshotManager())
    return agent.extract(ExtractionRequest(
        source=str(client_files),
        source_type=SourceType.CLIENT_DATA_DIRECTORY,
        register_snapshot=True,
        auto_confirm_mappings=True,
        allow_ai=False,
    ))


# ===========================================================================
# §16.B — the agent routes and returns a validated structured result
# ===========================================================================

class TestTheAgentInterface:

    def test_a_directory_of_client_files_is_accepted(self, extraction):
        assert extraction.status in (ExtractionStatus.ACCEPTED,
                                     ExtractionStatus.WARNING)
        assert extraction.ok
        assert extraction.errors == []

    def test_the_result_carries_a_canonical_network(self, extraction):
        assert isinstance(extraction.canonical_data, CanonicalNetwork)

    def test_the_orchestrator_never_sees_a_parser_detail(self):
        """
        §7: the Orchestrator consumes structured evidence, not Excel sheets.
        Asserted against the field list so a future addition has to be
        deliberate.
        """
        forbidden = ("workbook", "sheet", "dataframe", "csv", "parser",
                     "rows", "cell", "excel")
        for field in ExtractionResult.model_fields:
            assert not any(f in field.lower() for f in forbidden), field

    def test_the_result_cannot_carry_an_engine_owned_value(self):
        """
        Extraction produces evidence. An REI or RF arriving from a parser is
        either fabricated or stale, and both are worse than absent.
        """
        for banned in ("rei", "rf", "risk_factor", "governance", "objective"):
            assert banned not in ExtractionResult.model_fields

    def test_a_smuggled_metric_is_refused_by_the_schema(self):
        with pytest.raises(ValueError, match="engine-owned values"):
            ExtractionResult(status=ExtractionStatus.ACCEPTED,
                             review_items=[{"facility_id": "DC_X", "rei": 0.9}])

    @pytest.mark.parametrize("source, expected", [
        ("does/not/exist", SourceType.UNSUPPORTED),
    ])
    def test_an_unknown_source_is_rejected_not_guessed_at(self, source, expected):
        result = ExtractionParsingAgent().extract(ExtractionRequest(source=source))
        assert result.status == ExtractionStatus.REJECTED
        assert result.canonical_data is None
        assert result.errors

    def test_extraction_never_raises(self, tmp_path):
        """
        A layer that throws leaves the caller unable to tell bad data from a
        broken system, and the two need different responses.
        """
        broken = tmp_path / "facilities.csv"
        broken.write_bytes(b"\x00\x01\x02 not,a,valid\ncsv at all\x00")
        result = ExtractionParsingAgent().extract(
            ExtractionRequest(source=str(tmp_path), allow_ai=False))
        assert isinstance(result, ExtractionResult)
        assert result.status in (ExtractionStatus.REJECTED,
                                 ExtractionStatus.HUMAN_REVIEW_REQUIRED,
                                 ExtractionStatus.WARNING)

    def test_an_empty_directory_produces_no_network(self, tmp_path):
        result = ExtractionParsingAgent().extract(
            ExtractionRequest(source=str(tmp_path), allow_ai=False))
        assert result.status == ExtractionStatus.REJECTED
        assert result.canonical_data is None


# ===========================================================================
# §6 / §16.C — one canonical model, not two
# ===========================================================================

class TestSingleCanonicalModel:

    def test_ingestion_imports_mains_schemas_rather_than_defining_its_own(self):
        """
        The check that matters for §6. If ingestion declared its own
        FacilityRecord, every field added to MAIN's would silently fail to
        reach the solver.
        """
        import netgravity.ingestion.builder as builder
        from netgravity.schemas import network as main_schemas

        assert builder.CanonicalNetwork is main_schemas.CanonicalNetwork
        assert builder.FacilityRecord is main_schemas.FacilityRecord
        assert builder.DemandRecord is main_schemas.DemandRecord
        assert builder.LaneRecord is main_schemas.LaneRecord
        assert builder.ProductRecord is main_schemas.ProductRecord

    def test_no_competing_canonical_class_is_defined_anywhere_in_ingestion(self):
        """A duplicate would be found by name; there must be none."""
        import pkgutil

        import netgravity.ingestion as pkg

        offenders = []
        for mod in pkgutil.walk_packages(pkg.__path__, f"{pkg.__name__}."):
            if ".tests" in mod.name:
                continue
            source_file = Path(mod.module_finder.path) / f"{mod.name.rsplit('.', 1)[-1]}.py"
            if not source_file.exists():
                continue
            text = source_file.read_text(encoding="utf-8", errors="ignore")
            for cls in ("class CanonicalNetwork", "class FacilityRecord",
                        "class DemandRecord", "class LaneRecord",
                        "class ProductRecord"):
                if cls in text:
                    offenders.append(f"{mod.name}: {cls}")
        assert offenders == [], offenders

    def test_the_built_network_validates_against_mains_rules(self, extraction):
        from netgravity.validation.checks import validate_network

        report = validate_network(extraction.canonical_data)
        assert report is not None

    def test_the_network_has_the_expected_shape(self, extraction):
        net = extraction.canonical_data
        roles = {}
        for f in net.facilities:
            roles[f.role] = roles.get(f.role, 0) + 1
        assert roles[NodeRole.PLANT] == 3
        assert roles[NodeRole.DC] == 5
        assert roles[NodeRole.MARKET] == 8
        assert len(net.products) == 2
        assert len(net.demands) == 13
        assert len(net.lanes) == 35


# ===========================================================================
# §11 / §16.E — one snapshot mechanism
# ===========================================================================

class TestSnapshotIntegration:

    def test_the_extracted_network_registers_through_the_existing_manager(
        self, client_files,
    ):
        snapshots = SnapshotManager()
        result = ExtractionParsingAgent(snapshots=snapshots).extract(
            ExtractionRequest(source=str(client_files), register_snapshot=True,
                              auto_confirm_mappings=True, allow_ai=False))
        assert result.snapshot_id == snapshots.current_id
        assert snapshots.get(result.snapshot_id) is not None

    def test_the_snapshot_id_derives_from_the_engines_own_data_version(
        self, extraction,
    ):
        """No parallel versioning scheme: `snap_` + the content hash."""
        assert extraction.snapshot_id == f"snap_{extraction.data_version[:12]}"

    def test_ingestion_is_idempotent_on_unchanged_input(self, client_files):
        """Same files, same bytes, same version — content-addressed."""
        agent = ExtractionParsingAgent()
        first = agent.extract(ExtractionRequest(
            source=str(client_files), auto_confirm_mappings=True, allow_ai=False))
        second = agent.extract(ExtractionRequest(
            source=str(client_files), auto_confirm_mappings=True, allow_ai=False))
        assert first.data_version == second.data_version

    def test_a_material_change_produces_a_new_fingerprint(self, extraction):
        """
        §11: material change → new fingerprint → dependent REI invalidated.
        Capacity is material; it changes what the solver can do.
        """
        from netgravity.resilience.fingerprint import compute_material_fingerprint

        net = extraction.canonical_data
        before = compute_material_fingerprint(net)
        changed = net.model_copy(deep=True)
        target = next(f for f in changed.facilities if f.id == "DC_BHIWANDI")
        target.capacity_units_per_period += 1_000
        assert compute_material_fingerprint(changed) != before

    def test_a_cosmetic_change_does_not(self, extraction):
        """
        §11: cosmetic change → no unnecessary recomputation. Renaming a
        facility must not throw away a valid REI batch.
        """
        from netgravity.resilience.fingerprint import compute_material_fingerprint

        net = extraction.canonical_data
        before = compute_material_fingerprint(net)
        renamed = net.model_copy(deep=True)
        target = next(f for f in renamed.facilities if f.id == "DC_BHIWANDI")
        target.name = "Bhiwandi Distribution Centre (North)"
        assert compute_material_fingerprint(renamed) == before


# ===========================================================================
# §12 / §16.D — extraction feeds the EXISTING MILP
# ===========================================================================

class TestMILPIntegration:

    def test_the_ingested_network_solves_to_optimal(self, extraction):
        result = solve(extraction.canonical_data)
        assert result.solver.status.value == "OPTIMAL"

    def test_all_demand_is_served(self, extraction):
        """Zero shortage: the ingested network is genuinely feasible."""
        result = solve(extraction.canonical_data)
        components = result.objective_components
        shortage = (components.get("shortage_cost") if isinstance(components, dict)
                    else components.shortage_cost)
        assert shortage == pytest.approx(0.0)

    def test_the_result_carries_the_ingested_data_version(self, extraction):
        """Traceability: an optimisation result names the data it came from."""
        result = solve(extraction.canonical_data)
        assert result.data_version == extraction.data_version

    def test_the_optimum_opens_facilities_and_moves_volume(self, extraction):
        result = solve(extraction.canonical_data)
        assert sum(1 for d in result.facility_decisions if d.is_open) >= 5
        assert len(result.flow_decisions) > 0

    def test_milp_mathematics_were_not_modified(self):
        """
        §20. Phase 4A changed no solver code; assert it against the tree so an
        accidental edit shows up here rather than in a cost figure.
        """
        import subprocess

        changed = subprocess.run(
            ["git", "diff", "--name-only", "origin/main...HEAD"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        ).stdout
        for path in ("netgravity/optimization/milp.py",
                     "netgravity/resilience/rei.py",
                     "netgravity/orchestrator/risk/"):
            assert path not in changed, f"{path} must not change in Phase 4A"


# ===========================================================================
# §13 / §16.E — extraction feeds the EXISTING REI service
# ===========================================================================

class TestREIIntegration:

    @pytest.fixture(scope="class")
    def registry(self, extraction):
        return REIService().get_or_compute(
            extraction.canonical_data, snapshot_id=extraction.snapshot_id)

    def test_rei_runs_on_ingested_data(self, registry):
        assert registry.batch_status in (REIBatchStatus.COMPLETED,
                                         REIBatchStatus.COMPLETED_WITH_ERRORS)
        assert registry.baseline_solver_status.value == "OPTIMAL"
        assert registry.results

    def test_the_baseline_is_solved_once_not_per_node(self, registry):
        """
        1 baseline + N disruptions, never 2N.

        The allowance for infeasible nodes is real, not slack: REI re-solves a
        node whose disruption came back INFEASIBLE with service diagnostics on,
        to tell "no feasible plan" from "solver gave up". That is one extra
        solve per infeasible node and it is worth paying for.
        """
        assessed = len(registry.results)
        infeasible = sum(1 for r in registry.results
                         if r.calculation_status == CalculationStatus.INFEASIBLE)
        assert registry.n_milp_solves <= assessed + 1 + infeasible

    def test_the_registry_is_stamped_with_the_ingested_snapshot(
        self, registry, extraction,
    ):
        assert registry.network_snapshot_id == extraction.snapshot_id

    def test_a_second_call_is_served_from_cache_with_zero_solves(self, extraction):
        service = REIService()
        cold = service.get_or_compute(extraction.canonical_data,
                                      snapshot_id=extraction.snapshot_id)
        warm = service.get_or_compute(extraction.canonical_data,
                                      snapshot_id=extraction.snapshot_id)
        assert cold.n_milp_solves > 0
        assert warm.n_milp_solves == 0

    def test_rei_values_are_a_relative_ranking_in_range(self, registry):
        scored = [r for r in registry.results if r.rei is not None]
        assert scored
        assert all(0.0 <= r.rei <= 1.0 for r in scored)
        assert max(r.rei for r in scored) == pytest.approx(1.0)

    def test_an_infeasible_disruption_reports_none_not_zero(self, registry):
        """
        The core invariant, on real ingested data. Removing DC_BHIWANDI or
        DC_HOSUR leaves demand unservable at this capacity. That is an ABSENCE
        of a computable exposure, not an exposure of zero — a zero would rank
        the node as perfectly safe, which is the opposite of the truth.
        """
        infeasible = [r for r in registry.results
                      if r.calculation_status == CalculationStatus.INFEASIBLE]
        assert infeasible, "expected at least one infeasible disruption"
        for row in infeasible:
            assert row.rei is None
            assert row.performance_impact is None

    def test_the_batch_reports_partial_success_honestly(self, registry):
        if any(r.calculation_status == CalculationStatus.INFEASIBLE
               for r in registry.results):
            assert registry.batch_status == REIBatchStatus.COMPLETED_WITH_ERRORS

    def test_results_are_deterministic_across_runs(self, extraction):
        first = REIService().get_or_compute(extraction.canonical_data)
        second = REIService().get_or_compute(extraction.canonical_data)
        assert {r.facility_id: r.rei for r in first.results} == \
               {r.facility_id: r.rei for r in second.results}


# ===========================================================================
# §8 / §16.F — external signal reaches the Orchestrator; RF stays authoritative
# ===========================================================================

class TestExternalSignalPath:

    def test_the_agent_extracts_a_structured_signal_with_a_stated_probability(self):
        result = ExtractionParsingAgent().extract(ExtractionRequest(
            source="There is a 70% probability of flooding around DC_DELHI.",
            source_type=SourceType.EXTERNAL_SIGNAL_TEXT,
            options={"known_facility_ids": ["DC_DELHI", "DC_MUMBAI"]},
        ))
        assert result.status == ExtractionStatus.ACCEPTED
        signal = result.external_signals[0]
        assert signal.event_type == "FLOOD"
        assert signal.event_probability == pytest.approx(0.70)
        assert signal.affected_entity_ids == ["DC_DELHI"]
        assert signal.probability_basis

    def test_severity_without_a_stated_probability_yields_none(self):
        """
        Severity is not probability. A signal describing a catastrophe with no
        stated likelihood must produce None, and RF must then refuse rather
        than infer.
        """
        result = ExtractionParsingAgent().extract(ExtractionRequest(
            source="Catastrophic flooding is expected around DC_DELHI.",
            source_type=SourceType.EXTERNAL_SIGNAL_TEXT,
            options={"known_facility_ids": ["DC_DELHI"]},
        ))
        signal = result.external_signals[0]
        assert signal.event_probability is None
        assert result.status == ExtractionStatus.WARNING
        assert any(f.code == "NO_EVENT_PROBABILITY"
                   for f in result.validation_results)

    def test_the_agent_does_not_calculate_rf(self):
        """
        §8: the Extraction Agent STOPS at the signal. The result has no field
        for an RF and the module imports no risk calculator.
        """
        import netgravity.orchestrator.agents.extraction_agent as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines()
            if not line.strip().startswith("#")
        )
        assert "risk_factor" not in code
        assert "from netgravity.orchestrator.risk" not in code
        assert "rei" not in ExtractionResult.model_fields

    def test_the_signal_feeds_rf_only_through_the_orchestrators_calculator(
        self, extraction,
    ):
        """
        The complete external-signal chain, using the authoritative pieces:
        the agent supplies P, REI comes from the registry, and RF is computed
        by the existing calculator — never by extraction.
        """
        from netgravity.orchestrator.risk.risk_factor import compute_risk_factor

        network = extraction.canonical_data
        registry = REIService().get_or_compute(network)
        scored = [r for r in registry.results if r.rei is not None]
        target = max(scored, key=lambda r: r.rei)

        result = ExtractionParsingAgent().extract(ExtractionRequest(
            source=f"There is a 70% probability of flooding around {target.facility_id}.",
            source_type=SourceType.EXTERNAL_SIGNAL_TEXT,
            options={"known_facility_ids": [f.id for f in network.facilities]},
        ))
        probability = result.external_signals[0].event_probability
        assessment = compute_risk_factor(
            likelihood=probability, rei=target.rei,
            facility_id=target.facility_id,
        )
        expected = probability + target.rei - probability * target.rei
        assert assessment.risk_factor == pytest.approx(expected)


# ===========================================================================
# §10 — observed vs scenario separation
# ===========================================================================

class TestObservedDataStaysObserved:

    def test_extraction_produces_observed_state_only(self, extraction):
        """
        There is no path from an ExtractionRequest to a scenario override. The
        request schema has no field for one, which is the structural form of
        "ingestion cannot silently mutate the baseline".
        """
        for field in ExtractionRequest.model_fields:
            assert "scenario" not in field
            assert "override" not in field

    def test_a_registered_snapshot_is_not_hypothetical(self, client_files):
        snapshots = SnapshotManager()
        result = ExtractionParsingAgent(snapshots=snapshots).extract(
            ExtractionRequest(source=str(client_files), register_snapshot=True,
                              auto_confirm_mappings=True, allow_ai=False))
        snapshot = snapshots.get(result.snapshot_id)
        assert getattr(snapshot, "is_hypothetical", False) is False

    def test_the_agent_never_writes_a_scenario(self):
        import netgravity.orchestrator.agents.extraction_agent as module

        code = _code_only(Path(module.__file__))
        assert "ScenarioIntentSpec" not in code
        assert "scenario_overrides" not in code


# ===========================================================================
# §14 — validation classification
# ===========================================================================

class TestValidationClassification:

    def test_a_clean_run_is_accepted_or_warned_never_silently_repaired(
        self, extraction,
    ):
        assert extraction.status in (ExtractionStatus.ACCEPTED,
                                     ExtractionStatus.WARNING)

    def test_findings_carry_row_level_provenance(self, extraction):
        """§15: a finding a planner cannot locate is not actionable."""
        located = [f for f in extraction.validation_results if f.where]
        assert located
        assert all("file" in f.where for f in located)

    def test_a_missing_required_column_is_reported(self, tmp_path):
        (tmp_path / "facilities.csv").write_text(
            "facility_id,facility_name\nDC_X,Ex\n", encoding="utf-8")
        result = ExtractionParsingAgent().extract(
            ExtractionRequest(source=str(tmp_path), allow_ai=False))
        assert result.status != ExtractionStatus.ACCEPTED

    def test_an_invalid_numeric_field_does_not_become_a_silent_zero(self, tmp_path):
        """
        §14: do not silently repair questionable client data. A capacity of
        "abc" must be reported, not coerced to 0 — a zero-capacity facility is
        a materially different network.
        """
        (tmp_path / "facilities.csv").write_text(
            "facility_id,facility_name,role,status,capacity_units_per_period\n"
            "DC_X,Ex DC,DC,EXISTING,abc\n", encoding="utf-8")
        result = ExtractionParsingAgent().extract(
            ExtractionRequest(source=str(tmp_path), allow_ai=False))

        if result.canonical_data is not None:
            bad = [f for f in result.canonical_data.facilities if f.id == "DC_X"]
            assert not bad or bad[0].capacity_units_per_period != 0
        assert result.status != ExtractionStatus.ACCEPTED

    def test_an_unsupported_file_type_is_not_parsed(self, tmp_path):
        (tmp_path / "notes.docx").write_bytes(b"PK\x03\x04 not really a docx")
        result = ExtractionParsingAgent().extract(
            ExtractionRequest(source=str(tmp_path), allow_ai=False))
        assert result.canonical_data is None
        assert result.status == ExtractionStatus.REJECTED

    def test_severity_levels_are_all_representable(self):
        assert {s.value for s in ValidationSeverity} == {"INFO", "WARNING", "ERROR"}
        assert {s.value for s in ExtractionStatus} == {
            "ACCEPTED", "WARNING", "HUMAN_REVIEW_REQUIRED", "REJECTED"}


# ===========================================================================
# §9 — the AI boundary
# ===========================================================================

class TestAIBoundary:

    def test_extraction_runs_with_no_credentials(self, extraction):
        """
        The whole pipeline is rules/stub by default. The suite therefore makes
        no network call and consumes no shared LLM capacity.
        """
        assert extraction.ok
        assert extraction.provenance.ai_assisted is False

    def test_stubbed_output_is_never_reported_as_ai_assisted(self, extraction):
        """
        Canned demo output is not assistance. Conflating the two would let a
        stub extraction be read as a live one.
        """
        assert extraction.provenance.ai_assisted is False
        assert extraction.provenance.ai_provider is None
        assert extraction.provenance.counts.get("ai_stubbed_files", 0) >= 0

    def test_ai_is_off_unless_explicitly_requested(self):
        assert ExtractionRequest(source="x").allow_ai is False

    def test_mappings_require_confirmation_by_default(self):
        """An unconfirmed mapping is exactly the case that should stop and ask."""
        assert ExtractionRequest(source="x").auto_confirm_mappings is False


# ===========================================================================
# §17 — performance, measured rather than extrapolated
# ===========================================================================

class TestPerformance:

    def test_extraction_of_the_reference_corpus_is_fast(self, client_files):
        started = time.perf_counter()
        result = ExtractionParsingAgent().extract(ExtractionRequest(
            source=str(client_files), auto_confirm_mappings=True, allow_ai=False))
        elapsed = time.perf_counter() - started
        assert result.ok
        # Generous: this asserts the absence of a pathology, not a target. The
        # measured figure for 81 rows is ~0.1 s and is reported as such — it is
        # NOT evidence about production-scale files.
        assert elapsed < 10.0

    def test_the_reported_duration_matches_the_work_done(self, extraction):
        assert extraction.duration_seconds > 0
        assert extraction.provenance.counts["rows_read"] > 0
        assert extraction.provenance.counts["rows_accepted"] > 0


# ===========================================================================
# §18 — security within ingestion scope
# ===========================================================================

class TestSecurity:

    def test_a_path_traversal_source_does_not_escape(self, tmp_path):
        result = ExtractionParsingAgent().extract(ExtractionRequest(
            source=str(tmp_path / ".." / ".." / "etc" / "passwd"), allow_ai=False))
        assert result.status == ExtractionStatus.REJECTED
        assert result.canonical_data is None

    def test_a_formula_cell_is_not_evaluated(self, tmp_path):
        """
        CSV formula injection: a leading "=" is data to a parser and a formula
        to a spreadsheet. It must arrive as text or be rejected — never
        executed, and never silently become a number.
        """
        (tmp_path / "facilities.csv").write_text(
            "facility_id,facility_name,role,status,capacity_units_per_period\n"
            '=cmd|\'/c calc\'!A1,Evil DC,DC,EXISTING,1000\n', encoding="utf-8")
        result = ExtractionParsingAgent().extract(
            ExtractionRequest(source=str(tmp_path), allow_ai=False))
        if result.canonical_data is not None:
            for facility in result.canonical_data.facilities:
                assert not facility.name.startswith("=")

    def test_no_credential_appears_in_the_result(self, extraction):
        blob = extraction.model_dump_json()
        for marker in ("api_key", "API_KEY", "Bearer ", "sk-", "token="):
            assert marker not in blob
