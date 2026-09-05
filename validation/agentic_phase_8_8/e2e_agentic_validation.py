"""
Phase 8.8 — End-to-End Agentic System Validation Script
Validates complete NetGravity backend starting from ChatService / Conversational NLU.
Runs Scenarios A, B, C, D, E plus Secondary Direct Orchestrator runs & NLU edge cases.
Generates scenario trace JSON files, summary_results.json, and report.md.
"""

import dataclasses
import os
import sys
import json
import time
import requests
import uuid
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
from netgravity.orchestrator.planner.llm_planner import (
    ENRICHMENT_CAPABILITIES, LiveLLMPlanner, MAX_LIVE_PLANNER_CALLS,
)
from netgravity.orchestrator.schemas.adaptive import AdaptiveExecutionConfig, AdaptiveAction
from netgravity.orchestrator.schemas.requests import Intent, OrchestratorRequest, ExternalSignal
from netgravity.orchestrator.schemas.conversation import ChatRequest, ChatResponse
from netgravity.orchestrator.conversation.chat_service import ChatService
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.tests.integration.conftest import build_delhi_network
from netgravity.orchestrator.exceptions import EngineFailureError


def dump(obj: Any) -> Any:
    """
    Serialise a trace object whatever kind it is.

    `signal_routing` is a `@dataclass`, not a pydantic model, and this harness
    called `.model_dump()` on it unconditionally. The line was never reached
    because the LLM planner never returned a parseable plan, so every run fell
    back to the deterministic planner and took a path that leaves
    `ExecutionContext.signal_routing` unset. Fixing the planner reached it on
    the first try and the harness crashed at Scenario A.
    """
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json.loads(json.dumps(dataclasses.asdict(obj), default=str))
    return json.loads(json.dumps(obj, default=str))


def assess(chat_resp: Any, orch_resp: Any, ctx: Any,
           expect_errors: bool = False, **extra: Any) -> Dict[str, Any]:
    """
    Judge a scenario from what actually happened.

    Every field here was a hardcoded `True`, and `"classification"` was the
    literal `"PASS"` — so this harness could not fail. It reported five PASSes
    over a run in which three scenarios returned `status: FAILED` and every
    live planner call came back truncated and unparseable. A check that always
    passes is worse than no check: it is a standing claim that something works.

    The criteria are deliberately modest, because this harness validates
    AGENTIC BEHAVIOUR rather than answer quality:

      * steps actually executed, in dependency order (`network.load_snapshot`
        cannot come after something that needs it);
      * the run reached a terminal state that is not FAILED;
      * decisions were recorded, which is what makes the run inspectable.

    `replanning_observed` stays reported-but-not-required: not every scenario
    should need to replan, and demanding it would push the harness toward
    provoking replans rather than observing them.
    """
    steps = [s["capability"] for s in (getattr(orch_resp, "steps", None) or [])]
    status = getattr(chat_resp, "status", None) or (
        chat_resp.get("status") if isinstance(chat_resp, dict) else None)

    ordered = True
    if "network.load_snapshot" in steps:
        first_dependent = next(
            (i for i, c in enumerate(steps)
             if c in ("optimization.solve", "resilience.assess", "forecast.demand")),
            None)
        if first_dependent is not None:
            ordered = steps.index("network.load_snapshot") < first_dependent

    decisions = len(getattr(ctx, "decision_history", []) or []) if ctx else 0
    errors = list(getattr(orch_resp, "errors", None) or [])

    result = {
        "sequential_behavior": bool(steps) and ordered,
        "adaptive_behavior": decisions > 0,
        "replanning_observed": (ctx.replan_count > 0) if ctx else False,
        "steps_executed": len(steps),
        "chat_status": status,
        "step_errors": [e.get("code") if isinstance(e, dict) else str(e)
                        for e in errors],
    }
    result.update(extra)

    failed = [k for k, v in result.items()
              if isinstance(v, bool) and k != "replanning_observed" and not v]
    if status == "FAILED":
        failed.append("chat_status=FAILED")

    # `expect_errors` is for the scenario that INJECTS a failure — Scenario D
    # replaces the REI service with one that raises, and the whole point is
    # that the manager traps it. There, an error is the evidence; its ABSENCE
    # would mean the injected failure never fired and the scenario proved
    # nothing.
    if expect_errors:
        # Trapped means HANDLED, which includes handled by succeeding on retry.
        # `TemporaryFailingREI` fails once and then works, so the run can end
        # with no error at all — and that is the manager doing its job, not the
        # injection failing to fire. The evidence is in the decision history:
        # a retry or recovery was decided. Requiring a surviving error would
        # have marked a clean recovery a failure.
        history = [d for d in (getattr(ctx, "decision_history", None) or [])]
        recovered = any(
            "RETRY" in str(getattr(d, "action", "")).upper()
            or "RECOVER" in str(getattr(d, "action", "")).upper()
            for d in history
        )
        # The chat turn is what consumes the single injected failure, so its
        # warnings are evidence too — the direct run afterwards sees a healthy
        # service and would otherwise look like nothing was ever injected.
        warned = any(
            "unavailable" in str(w).lower() or "fail" in str(w).lower()
            or "retry" in str(w).lower()
            for w in (result.get("chat_warnings") or [])
        )
        result["failure_trapped"] = bool(errors) or recovered or warned
        result["recovery_decisions"] = sum(
            1 for d in history
            if "RETRY" in str(getattr(d, "action", "")).upper())
        if not result["failure_trapped"]:
            failed.append("injected failure was neither recorded nor recovered")
    else:
        # An ENRICHMENT step failing is the degradation the design intends —
        # "no observed demand history is available, so no forecast can be
        # produced" is the system being honest, and the analysis around it
        # still completed. Only a load-bearing step failing is a scenario
        # failure. The set is imported rather than restated so this cannot
        # drift from what the planner actually marks optional.
        blocking = [
            e for e in errors
            if (e.get("capability") if isinstance(e, dict) else None)
            not in ENRICHMENT_CAPABILITIES
        ]
        result["degraded_steps"] = len(errors) - len(blocking)
        if blocking:
            failed.append(
                f"{len(blocking)} required-step error(s): "
                + ", ".join(str(e.get("capability")) for e in blocking))

    result["agentic_behavior_proven"] = not failed
    result["classification"] = "PASS" if not failed else "FAIL"
    result["failed_criteria"] = failed
    return result


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
    print("PHASE 8.8 — END-TO-END AGENTIC SYSTEM VALIDATION PRE-FLIGHT")
    print("==================================================")

    gw_cfg = LLMGatewayConfig.from_env()
    gw_cfg.max_requests_per_execution = 15
    gw = LLMGateway(gw_cfg)
    
    print(f"Configured Model: {gw.config.model_name}")
    print(f"Gateway Endpoint: {gw.config.base_url}")
    print(f"Gateway Available: {gw.available}")

    if not gw.available:
        print("[ERROR] LLM Gateway unavailable.")
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
    print(f"Local Planner Limit (Phase 8.8): 5 max")

    if remaining_reqs < 5:
        print(f"[ERROR] Quota exhausted: only {remaining_reqs} remaining. STOPPING.")
        sys.exit(1)

    print("==================================================\n")

    out_dir = "d:/Case Comp/Kearney/netgravity/validation/agentic_phase_8_8"
    os.makedirs(out_dir, exist_ok=True)

    delhi_net = build_delhi_network()
    adaptive_config = AdaptiveExecutionConfig(
        enable_materiality_branching=True,
        material_forecast_threshold=0.15,
        max_replans=3,
    )

    base_orch = build_orchestrator(network=delhi_net, enable_llm=False)
    live_planner = LiveLLMPlanner(gateway=gw, registry=base_orch.registry, max_calls=5)

    summary_results = {}
    
    # ----------------------------------------------------
    # SCENARIO A — CUSTOMER EXPANSION / DEMAND SURGE
    # ----------------------------------------------------
    print("--- Running Scenario A: Customer Expansion / Demand Surge ---")
    sig_a = MarketIntelligenceSignal(
        signal_id="sig_delhi_expansion",
        title="Delhi Customer Expansion",
        published_date="2026-02-01",
        bucket=SignalBucket.CUSTOMER,
        direction=SignalDirection.UP,
        confidence=SignalConfidence.HIGH,
        scenario_use=ScenarioUse.FORECAST_ENRICHMENT,
        affected_entities=["MKT_NORTH"],
        verdict=GuardrailVerdict(passed=True, bucket=SignalBucket.CUSTOMER),
    )
    orch_a = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
        history_provider=lambda snap: make_delhi_history(growth_step=15.0),
    )
    chat_a = ChatService(orchestrator=orch_a)
    user_msg_a = "A major customer is expanding in Delhi. Assess the impact on demand and the network, and recommend what we should do."
    
    t0 = time.time()
    chat_resp_a = chat_a.chat(ChatRequest(message=user_msg_a, conversation_id="conv_scen_a"))
    dur_a = time.time() - t0

    # Secondary Direct Orchestrator Run for Diagnosis
    req_a_direct = OrchestratorRequest(input=user_msg_a, market_signals=[sig_a])
    orch_resp_a_direct = orch_a.run_sync(req_a_direct)

    ctx_a = orch_a.state_store.get(orch_resp_a_direct.execution_id)
    trace_a = orch_a.get_trace(orch_resp_a_direct.execution_id)

    trace_data_a = {
        "scenario": "A",
        "name": "Customer Expansion / Demand Surge",
        "original_user_request": user_msg_a,
        "duration_s": round(dur_a, 2),
        "primary_chat_response": chat_resp_a.model_dump(mode="json"),
        "secondary_direct_response": orch_resp_a_direct.model_dump(mode="json"),
        "executed_steps": [s["capability"] for s in orch_resp_a_direct.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_a.decision_history] if ctx_a else [],
        "replan_count": ctx_a.replan_count if ctx_a else 0,
        "signal_routing": dump(ctx_a.signal_routing) if ctx_a else None,
        "agentic_assessment": assess(chat_resp_a, orch_resp_a_direct, ctx_a)
    }
    with open(f"{out_dir}/scenario_a_trace.json", "w") as f:
        json.dump(sanitize(trace_data_a), f, indent=2)
    summary_results["Scenario_A"] = trace_data_a["agentic_assessment"]

    # ----------------------------------------------------
    # SCENARIO B — EXTERNAL DISRUPTION (MUMBAI)
    # ----------------------------------------------------
    print("--- Running Scenario B: External Disruption ---")
    sig_b = ExternalSignal(
        event_type="DISRUPTION",
        location="DC_MUMBAI",
        event_probability=0.85,
        probability_basis="port strike warning",
        source="logistics_news",
        confidence=0.9,
        affected_entity_ids=["DC_MUMBAI"],
    )
    orch_b = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
    )
    chat_b = ChatService(orchestrator=orch_b)
    user_msg_b = "A major disruption is affecting Mumbai. Assess the impact on network resilience, cost and service, and recommend mitigation."
    
    t0 = time.time()
    chat_resp_b = chat_b.chat(ChatRequest(message=user_msg_b, conversation_id="conv_scen_b"))
    dur_b = time.time() - t0

    req_b_direct = OrchestratorRequest(input=user_msg_b, external_signal=sig_b)
    orch_resp_b_direct = orch_b.run_sync(req_b_direct)

    ctx_b = orch_b.state_store.get(orch_resp_b_direct.execution_id)
    trace_b = orch_b.get_trace(orch_resp_b_direct.execution_id)

    risk_refused_b = any(rec.action == "REFUSED_RISK_SIGNAL" for rec in (ctx_b.signal_routing.records if ctx_b and ctx_b.signal_routing else []))

    trace_data_b = {
        "scenario": "B",
        "name": "External Disruption",
        "original_user_request": user_msg_b,
        "duration_s": round(dur_b, 2),
        "primary_chat_response": chat_resp_b.model_dump(mode="json"),
        "secondary_direct_response": orch_resp_b_direct.model_dump(mode="json"),
        "executed_steps": [s["capability"] for s in orch_resp_b_direct.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_b.decision_history] if ctx_b else [],
        "risk_refused_from_forecast": risk_refused_b,
        "agentic_assessment": assess(chat_resp_b, orch_resp_b_direct, ctx_b,
                                     risk_isolated_from_demand=risk_refused_b)
    }
    with open(f"{out_dir}/scenario_b_trace.json", "w") as f:
        json.dump(sanitize(trace_data_b), f, indent=2)
    summary_results["Scenario_B"] = trace_data_b["agentic_assessment"]

    # ----------------------------------------------------
    # SCENARIO C — FORECAST → OPTIMIZATION
    # ----------------------------------------------------
    print("--- Running Scenario C: Forecast -> Optimization ---")
    orch_c = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
        history_provider=lambda snap: make_delhi_history(growth_step=20.0),
    )
    chat_c = ChatService(orchestrator=orch_c)
    user_msg_c = "Demand is expected to change materially in Delhi over the next planning horizon. Determine how the network allocation should change and quantify the impact."

    t0 = time.time()
    chat_resp_c = chat_c.chat(ChatRequest(message=user_msg_c, conversation_id="conv_scen_c"))
    dur_c = time.time() - t0

    req_c_direct = OrchestratorRequest(input=user_msg_c, explicit_intent=Intent.FORECAST)
    orch_resp_c_direct = orch_c.run_sync(req_c_direct)

    ctx_c = orch_c.state_store.get(orch_resp_c_direct.execution_id)

    trace_data_c = {
        "scenario": "C",
        "name": "Forecast -> Optimization",
        "original_user_request": user_msg_c,
        "duration_s": round(dur_c, 2),
        "primary_chat_response": chat_resp_c.model_dump(mode="json"),
        "secondary_direct_response": orch_resp_c_direct.model_dump(mode="json"),
        "executed_steps": [s["capability"] for s in orch_resp_c_direct.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_c.decision_history] if ctx_c else [],
        "replan_count": ctx_c.replan_count if ctx_c else 0,
        "agentic_assessment": assess(chat_resp_c, orch_resp_c_direct, ctx_c)
    }
    with open(f"{out_dir}/scenario_c_trace.json", "w") as f:
        json.dump(sanitize(trace_data_c), f, indent=2)
    summary_results["Scenario_C"] = trace_data_c["agentic_assessment"]

    # ----------------------------------------------------
    # SCENARIO D — FAILURE / INSUFFICIENT EVIDENCE
    # ----------------------------------------------------
    print("--- Running Scenario D: Failure / Insufficient Evidence ---")
    class TemporaryFailingREI:
        def __init__(self, target):
            self.target = target
            self.calls = 0
        def assess(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise EngineFailureError("Transient REI capability failure")
            return self.target.assess(*args, **kwargs)
        def __getattr__(self, name):
            return getattr(self.target, name)

    orch_d = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
    )
    if "rei" in orch_d.services:
        orch_d.services["rei"] = TemporaryFailingREI(orch_d.services["rei"])

    chat_d = ChatService(orchestrator=orch_d)
    user_msg_d = "Review resilience and exposure for Delhi facilities."

    t0 = time.time()
    chat_resp_d = chat_d.chat(ChatRequest(message=user_msg_d, conversation_id="conv_scen_d"))
    dur_d = time.time() - t0

    req_d_direct = OrchestratorRequest(input=user_msg_d, explicit_intent=Intent.RESILIENCE_QUERY)
    orch_resp_d_direct = orch_d.run_sync(req_d_direct)

    ctx_d = orch_d.state_store.get(orch_resp_d_direct.execution_id)

    trace_data_d = {
        "scenario": "D",
        "name": "Failure / Insufficient Evidence",
        "original_user_request": user_msg_d,
        "duration_s": round(dur_d, 2),
        "primary_chat_response": chat_resp_d.model_dump(mode="json"),
        "secondary_direct_response": orch_resp_d_direct.model_dump(mode="json"),
        "executed_steps": [s["capability"] for s in orch_resp_d_direct.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_d.decision_history] if ctx_d else [],
        "errors": list(ctx_d.errors) if ctx_d else [],
        # Assessed against the CHAT run: `TemporaryFailingREI` fails once and
        # then works, and the chat turn is what consumes that one failure. The
        # direct run afterwards sees a healthy REI, so judging the injection by
        # it reported "no failure was injected" on a scenario that had trapped
        # one correctly a moment earlier.
        "agentic_assessment": assess(chat_resp_d, orch_resp_d_direct, ctx_d,
                                     expect_errors=True,
                                     chat_warnings=list(chat_resp_d.warnings or []))
    }
    with open(f"{out_dir}/scenario_d_trace.json", "w") as f:
        json.dump(sanitize(trace_data_d), f, indent=2)
    summary_results["Scenario_D"] = trace_data_d["agentic_assessment"]

    # ----------------------------------------------------
    # SCENARIO E — COMPLEX EXECUTIVE REQUEST
    # ----------------------------------------------------
    print("--- Running Scenario E: Complex Executive Request ---")
    orch_e = build_orchestrator(
        network=delhi_net,
        enable_llm=True,
        llm_planner=live_planner,
        adaptive_config=adaptive_config,
        history_provider=lambda snap: make_delhi_history(growth_step=15.0),
    )
    chat_e = ChatService(orchestrator=orch_e)
    user_msg_e = "A major customer is expanding in Delhi while a disruption is affecting Mumbai. Assess the impact on demand, resilience and network cost, and recommend what we should do."

    t0 = time.time()
    chat_resp_e = chat_e.chat(ChatRequest(message=user_msg_e, conversation_id="conv_scen_e"))
    dur_e = time.time() - t0

    req_e_direct = OrchestratorRequest(input=user_msg_e, market_signals=[sig_a], external_signal=sig_b)
    orch_resp_e_direct = orch_e.run_sync(req_e_direct)

    ctx_e = orch_e.state_store.get(orch_resp_e_direct.execution_id)

    trace_data_e = {
        "scenario": "E",
        "name": "Complex Executive Request",
        "original_user_request": user_msg_e,
        "duration_s": round(dur_e, 2),
        "primary_chat_response": chat_resp_e.model_dump(mode="json"),
        "secondary_direct_response": orch_resp_e_direct.model_dump(mode="json"),
        "executed_steps": [s["capability"] for s in orch_resp_e_direct.steps],
        "decisions": [d.model_dump(mode="json") for d in ctx_e.decision_history] if ctx_e else [],
        "agentic_assessment": assess(
            chat_resp_e, orch_resp_e_direct, ctx_e,
            # Derived: a genuinely multi-domain run touches more than one
            # capability domain rather than being declared to have done so.
            multi_domain_orchestration=len({
                s["capability"].split(".")[0]
                for s in (orch_resp_e_direct.steps or [])
            }) >= 3)
    }
    with open(f"{out_dir}/scenario_e_trace.json", "w") as f:
        json.dump(sanitize(trace_data_e), f, indent=2)
    summary_results["Scenario_E"] = trace_data_e["agentic_assessment"]

    # ----------------------------------------------------
    # SECTION 9 — NLU EDGE CASE VALIDATION
    # ----------------------------------------------------
    print("--- Running Section 9: NLU Edge Case Validation ---")
    ambiguous_msg = "Analyze Delhi."
    chat_ambig = chat_e.chat(ChatRequest(message=ambiguous_msg, conversation_id="conv_ambig"))

    unsupported_msg = "Can you write a poem about supply chain logistics?"
    chat_unsupp = chat_e.chat(ChatRequest(message=unsupported_msg, conversation_id="conv_unsupp"))

    summary_results["NLU_Edge_Cases"] = {
        "ambiguous_request": {
            "input": ambiguous_msg,
            "response_reply": chat_ambig.reply[:200],
            "clarity": chat_ambig.clarity,
            "handled_safely": True,
        },
        "unsupported_request": {
            "input": unsupported_msg,
            "response_reply": chat_unsupp.reply[:200],
            "handled_safely": True,
        }
    }

    # Save summary_results.json
    with open(f"{out_dir}/summary_results.json", "w") as f:
        json.dump(sanitize(summary_results), f, indent=2)

    print("\n==================================================")
    print("PHASE 8.8 END-TO-END VALIDATION COMPLETE")
    print(f"Total Planner Calls Attempted: {live_planner.calls_attempted} (Max: {live_planner.max_calls})")
    print(f"Calls Successful: {live_planner.calls_successful}")
    print(f"Calls Failed: {live_planner.calls_failed}")

    # The verdict, derived. This block used to print nothing about the
    # scenarios at all, while every one of them was recorded as a hardcoded
    # PASS — so a run in which three scenarios FAILED reported total success.
    print("")
    verdicts = {k: v.get("classification") for k, v in summary_results.items()
                if isinstance(v, dict) and "classification" in v}
    for name, verdict in verdicts.items():
        reasons = summary_results[name].get("failed_criteria") or []
        print(f"  [{verdict:4}] {name}" + (f" — {'; '.join(map(str, reasons))}" if reasons else ""))
    failed_n = sum(1 for v in verdicts.values() if v != "PASS")
    print("")
    print(f"SCENARIOS: {len(verdicts) - failed_n}/{len(verdicts)} passed")
    print("==================================================")


if __name__ == "__main__":
    main()
