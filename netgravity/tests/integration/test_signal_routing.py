"""
Phase 6 correction — the external-signal routing boundary.

    External sources
          ↓
    Extraction / Parsing Agent      structures signals, decides nothing
          ↓
    ORCHESTRATOR                    decides which may inform a forecast
          ↓
    Forecasting Agent               decides HOW they apply
          ↓
    Forecast → ORCHESTRATOR → MILP

Nine claims are proved here, matching the correction point for point:

  1. Extraction produces structured external signals.
  2. The Orchestrator receives them.
  3. The Orchestrator passes the relevant ones to Forecasting.
  4. Forecasting uses only signals supplied through its request.
  5. Forecasting does not fetch signals itself.
  6. Extraction does not invoke Forecasting.
  7. The Orchestrator controls forecasting execution.
  8. RF probability is never fabricated from confidence or severity.
  9. Forecast → Orchestrator → MILP is the authoritative optimisation path.

Several are asserted STRUCTURALLY, against compiled source with docstrings
stripped. A behavioural test shows the code does not currently fetch a signal;
an import test shows it cannot.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest

from netgravity.forecasting import (
    DemandPoint,
    DemandTimeSeries,
    ForecastRequest,
    ForecastStatus,
    ForecastingService,
)
from netgravity.forecasting.signals.enrichment import SignalEnricher
from netgravity.ingestion.schemas.signal import (
    GuardrailVerdict,
    MarketIntelligenceSignal,
    ScenarioUse,
    SignalBucket,
    SignalConfidence,
    SignalDirection,
)
from netgravity.orchestrator import build_orchestrator
from netgravity.orchestrator.routing.signal_router import (
    ExternalSignalRouter,
    RoutingOutcome,
)
from netgravity.orchestrator.schemas.requests import (
    EventSeverity,
    ExternalSignal,
    Intent,
    OrchestratorRequest,
)
from netgravity.tests.integration.conftest import build_delhi_network

_MARKETS = ("MKT_NORTH", "MKT_WEST", "MKT_EAST")
_NETWORK_ENTITIES = {
    "PLANT_N", "DC_DELHI", "DC_MUMBAI", "DC_KOLKATA", *_MARKETS,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _signal(
    signal_id: str = "sig_1",
    *,
    bucket: SignalBucket = SignalBucket.CUSTOMER,
    direction: SignalDirection = SignalDirection.UP,
    confidence: SignalConfidence = SignalConfidence.HIGH,
    scenario_use: ScenarioUse = ScenarioUse.FORECAST_ENRICHMENT,
    entities: Sequence[str] = ("MKT_NORTH",),
    passed: bool = True,
) -> MarketIntelligenceSignal:
    """A structured signal of the shape the Extraction Agent produces."""
    return MarketIntelligenceSignal(
        signal_id=signal_id, title=f"signal {signal_id}",
        published_date="2026-01-01", bucket=bucket, direction=direction,
        confidence=confidence, scenario_use=scenario_use,
        affected_entities=list(entities),
        verdict=GuardrailVerdict(passed=passed, bucket=bucket),
    )


def _history(_snapshot=None):
    return [
        DemandTimeSeries(
            market_id=market, product_id="P1",
            history=[DemandPoint(period=t + 1, quantity=100.0 + t * 2)
                     for t in range(14)],
        )
        for market in _MARKETS
    ], []


def _orch(signal_provider=None):
    return build_orchestrator(
        network=build_delhi_network(), enable_llm=False,
        history_provider=_history, signal_provider=signal_provider,
    )


def _run_forecast(orch, signals: Optional[List[Any]] = None):
    return orch.run_sync(OrchestratorRequest(
        input="What will demand look like next quarter?",
        explicit_intent=Intent.FORECAST,
        market_signals=list(signals or []),
    ))


def _code_only(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _package(name: str) -> List[Path]:
    return sorted((Path(__file__).resolve().parents[2] / name).rglob("*.py"))


# ===========================================================================
# 1. Extraction produces structured external signals
# ===========================================================================

class TestExtractionProducesSignals:

    def test_the_extraction_chain_produces_a_structured_routable_signal(self):
        """
        The real extraction path: parse a raw record, then run the relevance
        guardrail that attaches a verdict and a permitted use. The output is a
        typed signal with entities, bucket, direction and a verdict —
        structure, not a decision about forecasting.
        """
        from netgravity.ingestion.adapters.signals import _parse_signal
        from netgravity.ingestion.guardrails import relevance

        raw = {
            "signal_id": "news_001",
            "title": "Customer expands distribution across the north",
            "source_title": "Trade Press", "published_date": "2026-01-05",
            "bucket": "CUSTOMER", "direction": "UP", "magnitude": "+8% volume",
            "affected_entities": ["MKT_NORTH"], "geography": "North India",
            "confidence": "HIGH", "rationale": "expansion announced",
        }
        signal, issues = _parse_signal(raw, index=1, file="signals.json")
        assert signal is not None, issues
        assert signal.bucket is SignalBucket.CUSTOMER
        assert signal.direction is SignalDirection.UP
        assert "MKT_NORTH" in signal.affected_entities

        # Extraction's guardrail attaches a verdict and a classification.
        [graded] = relevance.apply([signal], known_entity_ids=_NETWORK_ENTITIES)
        assert graded.verdict is not None
        assert isinstance(graded.scenario_use, ScenarioUse)

        # And the orchestrator — not extraction — decides what happens next.
        decision = ExternalSignalRouter().route_for_forecast(
            [graded], known_entity_ids=_NETWORK_ENTITIES,
        )
        assert len(decision.records) == 1

    def test_an_extracted_signal_carries_no_probability(self):
        """
        The structural guarantee that keeps extraction out of the RF pathway.
        """
        fields = set(MarketIntelligenceSignal.model_fields)
        for banned in ("probab", "likelihood", "p_event"):
            assert not any(banned in f.lower() for f in fields), banned

    def test_extraction_classifies_but_does_not_authorise(self):
        """
        `scenario_use` is extraction's CLASSIFICATION of what kind of signal
        this is. It is evidence the orchestrator reads, not an instruction it
        obeys — the orchestrator can and does refuse a signal that carries
        FORECAST_ENRICHMENT for reasons extraction could not know, such as the
        pinned network not containing the entity.
        """
        off_network = _signal("off", entities=("MKT_ATLANTIS",))
        assert off_network.scenario_use is ScenarioUse.FORECAST_ENRICHMENT

        decision = ExternalSignalRouter().route_for_forecast(
            [off_network], known_entity_ids=_NETWORK_ENTITIES,
        )
        assert decision.accepted == []
        assert decision.records[0].outcome is RoutingOutcome.OUT_OF_SCOPE


# ===========================================================================
# 2 & 3. The Orchestrator receives signals and routes the relevant ones
# ===========================================================================

class TestOrchestratorRouting:

    def test_signals_supplied_on_the_request_reach_the_context(self):
        orch = _orch()
        response = _run_forecast(orch, [_signal("s1"), _signal("s2")])
        context = orch.state_store.get(response.execution_id)

        assert [s.signal_id for s in context.market_signals] == ["s1", "s2"]

    def test_a_signal_provider_supplies_extraction_output(self):
        """The other route in: a service standing in for the extraction feed."""
        orch = _orch(signal_provider=lambda snapshot: ([_signal("from_provider")], []))
        response = _run_forecast(orch)
        context = orch.state_store.get(response.execution_id)

        assert context.signal_routing.accepted_ids == ["from_provider"]

    def test_only_routed_signals_reach_the_forecasting_agent(self):
        orch = _orch()
        response = _run_forecast(orch, [
            _signal("eligible"),
            _signal("logged", scenario_use=ScenarioUse.LOGGED_ONLY),
            _signal("blocked", passed=False),
            _signal("low", confidence=SignalConfidence.LOW),
            _signal("offnet", entities=("MKT_ATLANTIS",)),
        ])
        context = orch.state_store.get(response.execution_id)

        assert context.signal_routing.accepted_ids == ["eligible"]
        # And only that one appears in the forecast's own provenance.
        assert context.forecast_result.provenance.signal_ids == ["eligible"]

    def test_every_routing_outcome_is_distinct_and_recorded(self):
        decision = ExternalSignalRouter().route_for_forecast(
            [
                _signal("ok"),
                _signal("logged", scenario_use=ScenarioUse.LOGGED_ONLY),
                _signal("blocked", passed=False),
                _signal("low", confidence=SignalConfidence.LOW),
                _signal("offnet", entities=("MKT_ATLANTIS",)),
                _signal("unscoped", entities=()),
            ],
            known_entity_ids=_NETWORK_ENTITIES,
        )
        outcomes = {r.signal_id: r.outcome for r in decision.records}

        assert outcomes["ok"] is RoutingOutcome.ROUTED_TO_FORECASTING
        assert outcomes["logged"] is RoutingOutcome.NOT_FORECAST_USE
        assert outcomes["blocked"] is RoutingOutcome.GUARDRAIL_NOT_PASSED
        assert outcomes["low"] is RoutingOutcome.LOW_CONFIDENCE
        assert outcomes["offnet"] is RoutingOutcome.OUT_OF_SCOPE
        assert outcomes["unscoped"] is RoutingOutcome.OUT_OF_SCOPE
        assert all(r.reason for r in decision.records)

    def test_a_refused_signal_is_surfaced_not_dropped(self):
        orch = _orch()
        response = _run_forecast(orch, [_signal("logged", scenario_use=ScenarioUse.LOGGED_ONLY)])

        assert any("not routed to forecasting" in w for w in response.warnings)
        assert any("logged" in w for w in response.warnings)

    def test_the_routing_decision_is_audited(self):
        from netgravity.orchestrator.audit import events

        orch = _orch()
        response = _run_forecast(orch, [_signal("ok"), _signal("blocked", passed=False)])
        trace = orch.get_trace(response.execution_id)
        recorded = [e for e in trace.to_dict()["events"]
                    if e["type"] == events.SIGNALS_ROUTED]

        assert len(recorded) == 1
        detail = recorded[0]["detail"]
        assert detail["accepted"] == 1
        assert detail["considered"] == 2
        assert {d["signal_id"] for d in detail["decisions"]} == {"ok", "blocked"}

    def test_the_router_lives_in_the_orchestrator_not_in_forecasting(self):
        """
        Where the decision is made is the architecture. A router inside the
        forecasting package would mean the agent adjudicating its own inputs.
        """
        import netgravity.orchestrator.routing.signal_router as router_module

        assert router_module.__name__.startswith("netgravity.orchestrator.")
        for path in _package("forecasting"):
            code = _code_only(path)
            assert "ExternalSignalRouter" not in code, path.name


# ===========================================================================
# 4 & 5. Forecasting uses only what it is given, and fetches nothing
# ===========================================================================

class TestForecastingConsumesOnlySuppliedSignals:

    def test_an_unsupplied_signal_cannot_influence_a_forecast(self):
        """
        The agent has no route to a signal it was not handed. Two identical
        requests differing only in `signals` are the whole proof.
        """
        service = ForecastingService()
        series = [DemandTimeSeries(
            market_id="MKT_NORTH", product_id="P1",
            history=[DemandPoint(period=t + 1, quantity=100.0 + t * 2) for t in range(14)],
        )]

        without = service.forecast(ForecastRequest(
            series=series, horizon=1, snapshot_id="snap_x",
        )).series[0]
        with_signal = service.forecast(ForecastRequest(
            series=series, horizon=1, snapshot_id="snap_x",
            signals=[_signal()], enable_signal_enrichment=True,
        )).series[0]

        assert without.points[0].baseline_mean is None
        assert with_signal.points[0].mean > without.points[0].mean

    def test_enrichment_is_off_unless_the_orchestrator_enables_it(self):
        service = ForecastingService()
        series = [DemandTimeSeries(
            market_id="MKT_NORTH", product_id="P1",
            history=[DemandPoint(period=t + 1, quantity=100.0 + t) for t in range(14)],
        )]
        result = service.forecast(ForecastRequest(
            series=series, horizon=1, snapshot_id="snap_x", signals=[_signal()],
        ))
        assert result.series[0].signal_adjustments == []

    def test_forecasting_cannot_reach_the_signal_source(self):
        """
        No import of the ingestion package, no adapter, no file read, no
        network client. The agent literally cannot fetch a signal.
        """
        for path in _package("forecasting"):
            code = _code_only(path)
            for banned in ("netgravity.ingestion", "requests", "urllib", "httpx",
                           "anthropic", "openai"):
                assert f"import {banned}" not in code, f"{path.name} imports {banned}"
                assert f"from {banned}" not in code, f"{path.name} imports {banned}"

    def test_forecasting_reads_no_signal_file_or_directory(self):
        for path in _package("forecasting"):
            if path.name == "history.py":
                # The one module permitted to read files, and only staging
                # history the ingestion pipeline already wrote.
                continue
            code = _code_only(path)
            for banned in ("open(", "Path(", "rglob", "glob(", "json.load"):
                assert banned not in code, f"{path.name} performs I/O via {banned}"

    def test_the_history_reader_touches_no_signals(self):
        from netgravity.forecasting import history

        code = _code_only(Path(history.__file__))
        assert "signal" not in code.lower()

    def test_forecasting_makes_no_model_call(self):
        """
        The source repository asked an LLM for the demand multiplier. Nothing
        in the integrated package can call a model.
        """
        for path in _package("forecasting"):
            code = _code_only(path)
            for banned in ("LLMGateway", "messages.create", "chat.completions",
                           "api_key", "API_KEY"):
                assert banned not in code, f"{path.name} references {banned}"


# ===========================================================================
# 6. Extraction does not invoke Forecasting
# ===========================================================================

class TestExtractionDoesNotInvokeForecasting:

    def test_the_ingestion_package_never_imports_forecasting(self):
        for path in _package("ingestion"):
            code = _code_only(path)
            assert "netgravity.forecasting" not in code, path.name
            assert "ForecastingService" not in code, path.name

    def test_the_extraction_agent_never_imports_forecasting(self):
        agent = (Path(__file__).resolve().parents[2]
                 / "orchestrator" / "agents" / "extraction_agent.py")
        code = _code_only(agent)
        assert "netgravity.forecasting" not in code
        assert "ForecastingService" not in code
        assert "ForecastRequest" not in code

    def test_the_extraction_result_cannot_carry_a_forecast(self):
        from netgravity.orchestrator.schemas.extraction import ExtractionResult

        fields = set(ExtractionResult.model_fields)
        for banned in ("forecast", "prediction", "projected"):
            assert not any(banned in f.lower() for f in fields), banned

    def test_extraction_output_reaches_forecasting_only_through_the_orchestrator(self):
        """
        Exactly one non-test place constructs a `ForecastRequest`: the
        orchestrator's capability handler.
        """
        root = Path(__file__).resolve().parents[2]
        builders = set()
        for path in root.rglob("*.py"):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel.startswith(("tests/", "forecasting/")):
                continue
            if "ForecastRequest(" in path.read_text(encoding="utf-8"):
                builders.add(rel)
        assert builders == {"orchestrator/registry.py"}, builders


# ===========================================================================
# 7. The Orchestrator controls forecasting execution
# ===========================================================================

class TestOrchestratorControlsExecution:

    def test_no_forecast_runs_unless_the_workflow_asks_for_one(self):
        from netgravity.orchestrator.core.planner import CAP_FORECAST, WORKFLOW_TEMPLATES
        from netgravity.orchestrator.schemas.requests import IntentResolution

        for intent, template in WORKFLOW_TEMPLATES.items():
            steps = template.build(IntentResolution(intent=intent))
            has_forecast = CAP_FORECAST in {s.capability for s in steps}
            assert has_forecast == (intent is Intent.FORECAST), intent

    def test_supplying_signals_to_a_non_forecast_workflow_forecasts_nothing(self):
        """
        Signals on the request are an offer to the control plane, not a
        trigger. A resilience query carrying signals still runs no forecast.
        """
        orch = _orch()
        response = orch.run_sync(OrchestratorRequest(
            input="Which facilities are most exposed?",
            explicit_intent=Intent.RESILIENCE_QUERY,
            market_signals=[_signal("ignored")],
        ))
        context = orch.state_store.get(response.execution_id)

        assert context.forecast_result is None
        assert context.signal_routing is None

    def test_the_forecast_capability_is_reachable_only_from_a_plan(self):
        orch = _orch()
        capability = orch.registry.get("forecast.demand")
        assert capability.dependencies == ("network.load_snapshot",)


# ===========================================================================
# 8. RF probability is never fabricated
# ===========================================================================

class TestRFProbabilityIsNeverFabricated:

    def test_a_risk_signal_is_refused_as_a_forecasting_input(self):
        risk = ExternalSignal(
            event_type="FLOOD", location="Delhi", severity=EventSeverity.SEVERE,
            event_probability=0.7, affected_entity_ids=["MKT_NORTH"],
        )
        decision = ExternalSignalRouter().route_for_forecast(
            [risk], known_entity_ids=_NETWORK_ENTITIES,
        )
        assert decision.accepted == []
        assert decision.records[0].outcome is RoutingOutcome.REFUSED_RISK_SIGNAL
        assert "RF pathway" in decision.records[0].reason

    def test_the_enricher_refuses_one_too_even_if_routing_is_bypassed(self):
        """Defence in depth for a directly-constructed ForecastRequest."""
        risk = ExternalSignal(
            event_type="FLOOD", location="Delhi", severity=EventSeverity.SEVERE,
            event_probability=0.7, affected_entity_ids=["MKT_NORTH"],
        )
        allowed, reason = SignalEnricher().applicable(risk, "MKT_NORTH")
        assert allowed is False
        assert "RISK signal" in reason

    def test_confidence_does_not_change_the_adjustment(self):
        """HIGH → 0.8 is the same error as SEVERE → P = 0.7."""
        service = ForecastingService()
        series = [DemandTimeSeries(
            market_id="MKT_NORTH", product_id="P1",
            history=[DemandPoint(period=t + 1, quantity=100.0 + t * 2) for t in range(14)],
        )]
        results = {}
        for grade in (SignalConfidence.HIGH, SignalConfidence.MEDIUM):
            results[grade] = service.forecast(ForecastRequest(
                series=series, horizon=1, snapshot_id="snap_x",
                signals=[_signal(confidence=grade)], enable_signal_enrichment=True,
            )).series[0]

        high, medium = results[SignalConfidence.HIGH], results[SignalConfidence.MEDIUM]
        assert high.points[0].mean == pytest.approx(medium.points[0].mean)
        assert (high.signal_adjustments[0].mean_multiplier
                == medium.signal_adjustments[0].mean_multiplier)

    def test_the_router_derives_no_number_from_a_signal(self):
        """
        The router is a gate. Nothing in it reads confidence, severity,
        magnitude or direction and produces a quantity.
        """
        from netgravity.orchestrator.routing import signal_router

        code = _code_only(Path(signal_router.__file__))
        for banned in ("float(", "* 0.", "0.8", "0.7", "multiplier"):
            assert banned not in code, f"signal_router computes a value via {banned}"

    def test_a_market_signal_cannot_become_an_rf_input(self):
        """
        A forecast-enriching signal, however confident, produces no event
        probability anywhere in the run.
        """
        orch = _orch()
        response = _run_forecast(orch, [_signal(confidence=SignalConfidence.HIGH)])
        context = orch.state_store.get(response.execution_id)

        assert context.external_signal is None
        assert context.risk_results is None
        assert response.risk is None

    def test_the_two_signal_kinds_occupy_separate_fields(self):
        from netgravity.orchestrator.core.execution_context import ExecutionContext

        context = ExecutionContext()
        assert hasattr(context, "external_signal")     # RF pathway
        assert hasattr(context, "market_signals")      # forecasting pathway
        assert context.external_signal is None
        assert context.market_signals == []


# ===========================================================================
# 9. Forecast → Orchestrator → MILP
# ===========================================================================

class TestForecastToMILPPath:

    def test_the_optimisation_path_runs_through_the_orchestrator(self):
        from netgravity.optimization.milp import solve

        orch = _orch()
        response = _run_forecast(orch, [_signal()])
        forecast = orch.state_store.get(response.execution_id).forecast_result

        record, application = orch.build_forecast_scenario(forecast)
        assert record is not None and application.ok

        result = solve(application.network, application.network.config, record.scenario_id)
        assert result.solver.status.value == "OPTIMAL"

    def test_a_signal_adjusted_forecast_reaches_the_milp_input(self):
        """
        End to end: extraction structures it, the orchestrator routes it,
        forecasting applies it, and the adjusted quantity is what the MILP
        would optimise against — traceable back to the signal by id.
        """
        orch = _orch()
        response = _run_forecast(orch, [_signal("expansion")])
        context = orch.state_store.get(response.execution_id)
        forecast = context.forecast_result

        _, application = orch.build_forecast_scenario(forecast)
        north = next(d for d in application.network.demands
                     if d.market_id == "MKT_NORTH")
        series = forecast.for_key("MKT_NORTH", "P1")

        assert series.signal_adjustments[0].signal_id == "expansion"
        assert north.quantity == pytest.approx(series.point(1).p50, abs=1e-3)
        assert series.point(1).baseline_mean < series.point(1).mean
        assert "expansion" in forecast.provenance.signal_ids

    def test_forecasting_never_calls_the_solver_itself(self):
        for path in _package("forecasting"):
            code = _code_only(path)
            assert "milp" not in code.lower(), path.name
            assert "solve(" not in code, path.name

    def test_the_bridge_is_the_only_forecast_to_network_path(self):
        root = Path(__file__).resolve().parents[2]
        converters = set()
        for path in root.rglob("*.py"):
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel.startswith("tests/"):
                continue
            if "apply_forecast_to_network" in path.read_text(encoding="utf-8"):
                converters.add(rel)
        assert converters == {
            "orchestrator/engines/forecast_bridge.py",
            "orchestrator/core/orchestrator.py",
        }, converters
