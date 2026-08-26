"""
Phase 8.7 — Live Agentic Flow Validation Script
Runs 8 targeted live test cases using the real LLM Gateway API (max 8 calls).
Generates execution traces, authority verification, metrics, and report.md.
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Ensure project root is in sys.path
sys.path.insert(0, 'd:/Case Comp/Kearney/netgravity')

# Load environment from .env if present
env_path = 'd:/Case Comp/Kearney/netgravity/.env'
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip().strip('"\'')

from netgravity.schemas.network import CanonicalNetwork, FacilityRecord, NodeRole
from netgravity.forecasting import DemandPoint, DemandTimeSeries
from netgravity.ingestion.schemas.signal import (
    GuardrailVerdict, MarketIntelligenceSignal, ScenarioUse,
    SignalBucket, SignalConfidence, SignalDirection,
)
from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMGatewayConfig
from netgravity.orchestrator.planner.llm_planner import LiveLLMPlanner, MAX_LIVE_PLANNER_CALLS
from netgravity.orchestrator.schemas.adaptive import AdaptiveExecutionConfig, AdaptiveAction
from netgravity.orchestrator.schemas.requests import Intent, OrchestratorRequest, ExternalSignal
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.tests.integration.conftest import build_delhi_network
from netgravity.orchestrator.core.circuit_breaker import CircuitBreaker


def sanitize(text: Any) -> Any:
    """Ensure no API tokens or authorization headers are included in output."""
    if isinstance(text, str):
        token = os.environ.get("TEXT_API_TOKEN", "")
        if token and len(token) > 4:
            text = text.replace(token, "[REDACTED_TOKEN]")
        return text
    if isinstance(text, dict):
        return {k: sanitize(v) for k, v in text.items() if k.lower() not in ("authorization", "bearer", "text_api_token")}
    if isinstance(text, list):
        return [sanitize(i) for i in text]
    return text


def make_delhi_history(growth_step: float = 15.0):
    return [
        DemandTimeSeries(
            market_id=mkt,
            product_id="P1",
            history=[DemandPoint(period=t + 1, quantity=100.0 + t * growth_step) for t in range(12)],
        )
        for mkt in ("MKT_NORTH", "MKT_WEST", "MKT_EAST")
    ], []


def main():
    print("==================================================")
    print("PHASE 8.7 — LIVE AGENTIC FLOW VALIDATION PRE-FLIGHT")
    print("==================================================")

    gw_cfg = LLMGatewayConfig.from_env()
    gw_cfg.max_requests_per_execution = 15
    gw = LLMGateway(gw_cfg)
    print(f"Configured Model: {gw.config.model_name}")
    print(f"Gateway Endpoint: {gw.config.base_url}")
    print(f"Gateway Available: {gw.available}")
    
    if not gw.available:
        print("[ERROR] LLM Gateway is not available. Stopping Pre-Flight.")
        sys.exit(1)

    usage_data = {}
    try:
        r = requests.get(
            f"{gw.config.base_url}/v1/usage",
            headers={"Authorization": f"Bearer {gw.config.token}"},
            timeout=10,
        )
        if r.status_code == 200:
            usage_data = r.json()
    except Exception as e:
        print(f"[WARNING] Usage query exception: {e}")

    requests_today = usage_data.get("requests_today", 0)
    max_requests = usage_data.get("max_requests_per_day", 100)
    remaining_reqs = max_requests - requests_today
    remaining_usd = usage_data.get("remaining_usd", "N/A")
    budget_usd = usage_data.get("budget_usd", "N/A")
    spent_usd = usage_data.get("spent_usd", "N/A")

    print(f"Daily Request Limit: {max_requests}")
    print(f"Requests Today: {requests_today}")
    print(f"Remaining Requests: {remaining_reqs}")
    print(f"Budget (USD): ${spent_usd} spent / ${budget_usd} total (${remaining_usd} remaining)")
    print(f"Local Planner Call Limit (Phase 8.7): 8 max")
    print("Planned Live Calls for Validation: <= 8")

    if remaining_reqs < 8:
        print(f"[ERROR] Quota exhausted: only {remaining_reqs} requests remaining today. STOPPING.")
        sys.exit(1)

    print("==================================================\n")

    out_dir = "d:/Case Comp/Kearney/netgravity/validation/agentic_phase_8_7"
    os.makedirs(f"{out_dir}/traces", exist_ok=True)
    os.makedirs(f"{out_dir}/metrics", exist_ok=True)

    delhi_net = build_delhi_network()
    adaptive_config = AdaptiveExecutionConfig(
        enable_materiality_branching=True,
        material_forecast_threshold=0.15,
        max_replans=3,
    )

    base_orch = build_orchestrator(network=delhi_net, enable_llm=False)
    live_planner = LiveLLMPlanner(gateway=gw, registry=base_orch.registry, max_calls=8)

    test_results = []

    # ----------------------------------------------------
    # CASE 1 — SIMPLE LIVE PLAN
    # ----------------------------------------------------
    print("--- Running Case 1: Simple Live Plan ---")
    orch_1 = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
    )
    req_1 = OrchestratorRequest(input="What is the current state of the network?")
    t0 = time.time()
    resp_1 = orch_1.run_sync(req_1)
    dur_1 = time.time() - t0

    ctx_1 = orch_1.state_store.get(resp_1.execution_id)
    trace_1 = orch_1.get_trace(resp_1.execution_id)
    
    test_results.append({
        "case": 1,
        "name": "Simple Live Plan",
        "input": req_1.input,
        "duration_s": round(dur_1, 2),
        "status": resp_1.status,
        "initial_plan": [s.capability for s in ctx_1.initial_plan.steps] if ctx_1 and ctx_1.initial_plan else [],
        "executed_steps": [s["capability"] for s in resp_1.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_1.decision_history] if ctx_1 else [],
        "replan_count": ctx_1.replan_count if ctx_1 else 0,
        "summary": resp_1.summary,
    })

    if trace_1:
        with open(f"{out_dir}/traces/case_1_trace.json", "w") as f:
            json.dump(sanitize(trace_1.to_dict()), f, indent=2)

    # ----------------------------------------------------
    # CASE 2 — FORECAST → ADAPTIVE DECISION
    # ----------------------------------------------------
    print("--- Running Case 2: Forecast -> Adaptive Decision ---")
    orch_2 = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
        history_provider=lambda snap: make_delhi_history(growth_step=15.0),
    )
    req_2 = OrchestratorRequest(
        input="Forecast demand for Delhi for the next 6 months and assess whether the projected demand requires additional network analysis.",
        explicit_intent=Intent.FORECAST,
    )
    t0 = time.time()
    resp_2 = orch_2.run_sync(req_2)
    dur_2 = time.time() - t0

    ctx_2 = orch_2.state_store.get(resp_2.execution_id)
    trace_2 = orch_2.get_trace(resp_2.execution_id)

    test_results.append({
        "case": 2,
        "name": "Forecast -> Adaptive Decision",
        "input": req_2.input,
        "duration_s": round(dur_2, 2),
        "status": resp_2.status,
        "initial_plan": [s.capability for s in ctx_2.initial_plan.steps] if ctx_2 and ctx_2.initial_plan else [],
        "executed_steps": [s["capability"] for s in resp_2.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_2.decision_history] if ctx_2 else [],
        "replan_count": ctx_2.replan_count if ctx_2 else 0,
        "summary": resp_2.summary,
    })

    if trace_2:
        with open(f"{out_dir}/traces/case_2_trace.json", "w") as f:
            json.dump(sanitize(trace_2.to_dict()), f, indent=2)

    # ----------------------------------------------------
    # CASE 3 — EXTERNAL SIGNAL → FORECAST → ADAPTATION
    # ----------------------------------------------------
    print("--- Running Case 3: External Signal -> Forecast -> Adaptation ---")
    sig_3 = MarketIntelligenceSignal(
        signal_id="sig_delhi_expansion",
        title="Delhi Major Customer Expansion",
        published_date="2026-02-01",
        bucket=SignalBucket.CUSTOMER,
        direction=SignalDirection.UP,
        confidence=SignalConfidence.HIGH,
        scenario_use=ScenarioUse.FORECAST_ENRICHMENT,
        affected_entities=["MKT_NORTH"],
        verdict=GuardrailVerdict(passed=True, bucket=SignalBucket.CUSTOMER),
    )
    orch_3 = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
        history_provider=lambda snap: make_delhi_history(growth_step=15.0),
    )
    req_3 = OrchestratorRequest(
        input="A major customer is expanding in Delhi. Assess how this could affect demand and determine whether the network requires further analysis.",
        market_signals=[sig_3],
    )
    t0 = time.time()
    resp_3 = orch_3.run_sync(req_3)
    dur_3 = time.time() - t0

    ctx_3 = orch_3.state_store.get(resp_3.execution_id)
    trace_3 = orch_3.get_trace(resp_3.execution_id)

    test_results.append({
        "case": 3,
        "name": "External Signal -> Forecast -> Adaptation",
        "input": req_3.input,
        "duration_s": round(dur_3, 2),
        "status": resp_3.status,
        "signal_routing": ctx_3.signal_routing.model_dump(mode="json") if ctx_3 and ctx_3.signal_routing else None,
        "executed_steps": [s["capability"] for s in resp_3.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_3.decision_history] if ctx_3 else [],
        "replan_count": ctx_3.replan_count if ctx_3 else 0,
        "summary": resp_3.summary,
    })

    if trace_3:
        with open(f"{out_dir}/traces/case_3_trace.json", "w") as f:
            json.dump(sanitize(trace_3.to_dict()), f, indent=2)

    # ----------------------------------------------------
    # CASE 4 — IRRELEVANT SIGNAL
    # ----------------------------------------------------
    print("--- Running Case 4: Irrelevant Signal ---")
    sig_4 = MarketIntelligenceSignal(
        signal_id="sig_mumbai_promo",
        title="Mumbai Retail Promotion",
        published_date="2026-02-01",
        bucket=SignalBucket.COMPETITOR,
        direction=SignalDirection.UP,
        confidence=SignalConfidence.LOW,
        scenario_use=ScenarioUse.LOGGED_ONLY,
        affected_entities=["DC_MUMBAI"],
        verdict=GuardrailVerdict(passed=False, bucket=SignalBucket.COMPETITOR, reasons=["Log only"]),
    )
    orch_4 = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
    )
    req_4 = OrchestratorRequest(
        input="Forecast demand for Delhi markets.",
        explicit_intent=Intent.FORECAST,
        market_signals=[sig_4],
    )
    t0 = time.time()
    resp_4 = orch_4.run_sync(req_4)
    dur_4 = time.time() - t0

    ctx_4 = orch_4.state_store.get(resp_4.execution_id)
    trace_4 = orch_4.get_trace(resp_4.execution_id)

    test_results.append({
        "case": 4,
        "name": "Irrelevant Signal",
        "input": req_4.input,
        "duration_s": round(dur_4, 2),
        "status": resp_4.status,
        "signal_routing": ctx_4.signal_routing.model_dump(mode="json") if ctx_4 and ctx_4.signal_routing else None,
        "executed_steps": [s["capability"] for s in resp_4.steps],
        "replan_count": ctx_4.replan_count if ctx_4 else 0,
        "summary": resp_4.summary,
    })

    if trace_4:
        with open(f"{out_dir}/traces/case_4_trace.json", "w") as f:
            json.dump(sanitize(trace_4.to_dict()), f, indent=2)

    # ----------------------------------------------------
    # CASE 5 — RISK SIGNAL ATTACK
    # ----------------------------------------------------
    print("--- Running Case 5: Risk Signal Attack ---")
    risk_sig_5 = ExternalSignal(
        event_type="FLOOD",
        location="DC_DELHI",
        event_probability=0.85,
        probability_basis="weather warning",
        source="met_dept",
        confidence=0.9,
        affected_entity_ids=["DC_DELHI"],
    )
    orch_5 = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
    )
    req_5 = OrchestratorRequest(
        input="Severe flood predicted around Delhi. Update demand forecast accordingly.",
        external_signal=risk_sig_5,
    )
    t0 = time.time()
    resp_5 = orch_5.run_sync(req_5)
    dur_5 = time.time() - t0

    ctx_5 = orch_5.state_store.get(resp_5.execution_id)
    trace_5 = orch_5.get_trace(resp_5.execution_id)

    test_results.append({
        "case": 5,
        "name": "Risk Signal Attack",
        "input": req_5.input,
        "duration_s": round(dur_5, 2),
        "status": resp_5.status,
        "signal_routing": ctx_5.signal_routing.model_dump(mode="json") if ctx_5 and ctx_5.signal_routing else None,
        "executed_steps": [s["capability"] for s in resp_5.steps],
        "risk_refused": any(rec.action == "REFUSED_RISK_SIGNAL" for rec in (ctx_5.signal_routing.records if ctx_5 and ctx_5.signal_routing else [])),
        "summary": resp_5.summary,
    })

    if trace_5:
        with open(f"{out_dir}/traces/case_5_trace.json", "w") as f:
            json.dump(sanitize(trace_5.to_dict()), f, indent=2)

    # ----------------------------------------------------
    # CASE 6 — COMPLEX MULTI-CAPABILITY REQUEST
    # ----------------------------------------------------
    print("--- Running Case 6: Complex Multi-Capability Request ---")
    orch_6 = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
        history_provider=lambda snap: make_delhi_history(growth_step=15.0),
    )
    req_6 = OrchestratorRequest(
        input="A major customer is expanding in Delhi. Forecast the impact, assess network resilience, determine the operational impact, and recommend what we should do."
    )
    t0 = time.time()
    resp_6 = orch_6.run_sync(req_6)
    dur_6 = time.time() - t0

    ctx_6 = orch_6.state_store.get(resp_6.execution_id)
    trace_6 = orch_6.get_trace(resp_6.execution_id)

    test_results.append({
        "case": 6,
        "name": "Complex Multi-Capability Request",
        "input": req_6.input,
        "duration_s": round(dur_6, 2),
        "status": resp_6.status,
        "executed_steps": [s["capability"] for s in resp_6.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_6.decision_history] if ctx_6 else [],
        "replan_count": ctx_6.replan_count if ctx_6 else 0,
        "summary": resp_6.summary,
    })

    if trace_6:
        with open(f"{out_dir}/traces/case_6_trace.json", "w") as f:
            json.dump(sanitize(trace_6.to_dict()), f, indent=2)

    # ----------------------------------------------------
    # CASE 7 — FAILURE / RECOVERY
    # ----------------------------------------------------
    print("--- Running Case 7: Failure / Recovery ---")
    from netgravity.orchestrator.exceptions import EngineFailureError

    class TemporaryFailingREI:
        def __init__(self, target):
            self.target = target
            self.calls = 0

        def assess(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise EngineFailureError("Transient REI service failure")
            return self.target.assess(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.target, name)

    orch_7 = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
    )
    if "rei" in orch_7.services:
        orch_7.services["rei"] = TemporaryFailingREI(orch_7.services["rei"])

    req_7 = OrchestratorRequest(
        input="Review resilience and exposure for Delhi facilities.",
        explicit_intent=Intent.RESILIENCE_QUERY,
    )
    t0 = time.time()
    resp_7 = orch_7.run_sync(req_7)
    dur_7 = time.time() - t0

    ctx_7 = orch_7.state_store.get(resp_7.execution_id)
    trace_7 = orch_7.get_trace(resp_7.execution_id)

    test_results.append({
        "case": 7,
        "name": "Failure / Recovery",
        "input": req_7.input,
        "duration_s": round(dur_7, 2),
        "status": resp_7.status,
        "executed_steps": [s["capability"] for s in resp_7.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_7.decision_history] if ctx_7 else [],
        "errors": list(ctx_7.errors) if ctx_7 else [],
        "summary": resp_7.summary,
    })

    if trace_7:
        with open(f"{out_dir}/traces/case_7_trace.json", "w") as f:
            json.dump(sanitize(trace_7.to_dict()), f, indent=2)

    # ----------------------------------------------------
    # CASE 8 — REPLAN / CLOSED-LOOP PROOF
    # ----------------------------------------------------
    print("--- Running Case 8: Replan / Closed-Loop Proof ---")
    orch_8 = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
        history_provider=lambda snap: make_delhi_history(growth_step=20.0),
    )
    req_8 = OrchestratorRequest(
        input="Forecast Delhi demand and determine the network impact.",
        explicit_intent=Intent.FORECAST,
    )
    t0 = time.time()
    resp_8 = orch_8.run_sync(req_8)
    dur_8 = time.time() - t0

    ctx_8 = orch_8.state_store.get(resp_8.execution_id)
    trace_8 = orch_8.get_trace(resp_8.execution_id)

    test_results.append({
        "case": 8,
        "name": "Replan / Closed-Loop Proof",
        "input": req_8.input,
        "duration_s": round(dur_8, 2),
        "status": resp_8.status,
        "initial_plan": [s.capability for s in ctx_8.initial_plan.steps] if ctx_8 and ctx_8.initial_plan else [],
        "executed_steps": [s["capability"] for s in resp_8.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_8.decision_history] if ctx_8 else [],
        "replan_count": ctx_8.replan_count if ctx_8 else 0,
        "replan_records": [r.model_dump(mode="json") for r in ctx_8.replan_history] if ctx_8 else [],
        "summary": resp_8.summary,
    })

    if trace_8:
        with open(f"{out_dir}/traces/case_8_trace.json", "w") as f:
            json.dump(sanitize(trace_8.to_dict()), f, indent=2)

    # Save metrics and raw traces
    with open(f"{out_dir}/metrics/summary_results.json", "w") as f:
        json.dump(sanitize(test_results), f, indent=2)

    with open(f"{out_dir}/metrics/planner_stats.json", "w") as f:
        json.dump({
            "calls_attempted": live_planner.calls_attempted,
            "calls_successful": live_planner.calls_successful,
            "calls_failed": live_planner.calls_failed,
            "max_calls": live_planner.max_calls,
        }, f, indent=2)

    print("\n==================================================")
    print("PHASE 8.7 LIVE AGENTIC VALIDATION COMPLETE")
    print(f"Total Planner Calls Attempted: {live_planner.calls_attempted} (Max: {live_planner.max_calls})")
    print(f"Calls Successful: {live_planner.calls_successful}")
    print(f"Calls Failed: {live_planner.calls_failed}")
    print("==================================================")


if __name__ == "__main__":
    main()
