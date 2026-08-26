"""
Phase 8.5 Live Validation — Stage 1 (Core Cases 1 through 5).

Executes exactly 5 live LLM planner calls against the OpenAI Gateway.
Stops immediately after Call 5.
"""

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

# Ensure repository root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import netgravity.ingestion.config  # Loads .env
from netgravity.ingestion.schemas.signal import MarketIntelligenceSignal
from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMGatewayConfig
from netgravity.orchestrator.planner.llm_planner import LiveLLMPlanner
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.schemas.plans import StepStatus
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


def run_stage1():
    print("=" * 80)
    print("NETGRAVITY PHASE 8.5 LIVE VALIDATION — STAGE 1 (CORE CASES 1 TO 5)")
    print("=" * 80)

    # 1. Resolve Token
    token = os.environ.get("TEXT_API_TOKEN") or os.environ.get("NETGRAVITY_GATEWAY_TOKEN")
    if not token:
        print("[ERROR] Neither TEXT_API_TOKEN nor NETGRAVITY_GATEWAY_TOKEN is configured.")
        sys.exit(1)
    os.environ["TEXT_API_TOKEN"] = token

    url = os.environ.get("TEXT_API_URL", "https://rapidinsights-openai-gateway-dev.azurewebsites.net").rstrip("/")
    gateway_config = LLMGatewayConfig(base_url=url, token=token, enabled=True)
    gateway = LLMGateway(gateway_config)

    if not gateway.available:
        print(f"[ERROR] LLM Gateway is unavailable: {gateway.unavailable_reason()}")
        sys.exit(1)

    print(f"Gateway Connected: {url}")
    print(f"Model: {gateway_config.model_name}")

    # Build Network & Services
    network = build_case16_network()
    base_orch = build_orchestrator(enable_llm=False)
    registry = base_orch.registry

    # Initialize Live Planner with strict 15 call cap
    live_planner = LiveLLMPlanner(gateway=gateway, registry=registry, max_calls=15)

    # Signal provider for Core Case 4 & 5
    def sample_signal_provider(snapshot):
        sig_delhi = MarketIntelligenceSignal(
            signal_id="SIG_DELHI_EXPANSION",
            title="Major customer expansion in Delhi increasing volume by 20%",
            bucket="CUSTOMER",
            direction="UP",
            magnitude="+20%",
            affected_entities=["CUST_01", "DC_DELHI"],
            confidence="HIGH",
        )
        sig_mumbai = MarketIntelligenceSignal(
            signal_id="SIG_MUMBAI_PORT",
            title="Unrelated port tariff adjustment in Mumbai",
            bucket="LOGISTICS",
            direction="UP",
            magnitude="+5%",
            affected_entities=["PORT_MUMBAI"],
            confidence="HIGH",
        )
        return ([sig_delhi, sig_mumbai], [])

    orch = build_orchestrator(
        network=network,
        gateway=gateway,
        enable_llm=True,
        llm_planner=live_planner,
        signal_provider=sample_signal_provider,
    )

    stage1_cases = [
        {
            "case_num": 1,
            "title": "CORE CASE 1 — NETWORK STATE",
            "prompt": "What is the current state of the network?",
            "request": OrchestratorRequest(
                input="What is the current state of the network?",
                explicit_intent=Intent.NETWORK_STATE_QUERY,
                actor=Actor(role=ActorRole.PLANNER, actor_id="live_validator"),
            ),
        },
        {
            "case_num": 2,
            "title": "CORE CASE 2 — FORECAST",
            "prompt": "Forecast demand for Delhi for the next 6 months.",
            "request": OrchestratorRequest(
                input="Forecast demand for Delhi for the next 6 months.",
                explicit_intent=Intent.FORECAST,
                actor=Actor(role=ActorRole.PLANNER, actor_id="live_validator"),
            ),
        },
        {
            "case_num": 3,
            "title": "CORE CASE 3 — SCENARIO / OPTIMIZATION",
            "prompt": "What happens if demand in Delhi increases by 20%?",
            "request": OrchestratorRequest(
                input="What happens if demand in Delhi increases by 20%?",
                explicit_intent=Intent.SCENARIO_ANALYSIS,
                explicit_scenarios=[ScenarioIntentSpec(
                    action=ScenarioActionType.CHANGE_DEMAND,
                    facility_ids=["CUST_01"],
                    demand_multiplier=1.2,
                )],
                actor=Actor(role=ActorRole.PLANNER, actor_id="live_validator"),
            ),
        },
        {
            "case_num": 4,
            "title": "CORE CASE 4 — EXTERNAL SIGNAL -> FORECAST",
            "prompt": "A major customer is expanding in Delhi. Assess how this could affect demand.",
            "request": OrchestratorRequest(
                input="A major customer is expanding in Delhi. Assess how this could affect demand.",
                explicit_intent=Intent.FORECAST,
                actor=Actor(role=ActorRole.PLANNER, actor_id="live_validator"),
            ),
        },
        {
            "case_num": 5,
            "title": "CORE CASE 5 — COMPLEX END-TO-END REQUEST",
            "prompt": "A major customer is expanding in Delhi. Assess the impact on the network and recommend what we should do.",
            "request": OrchestratorRequest(
                input="A major customer is expanding in Delhi. Assess the impact on the network and recommend what we should do.",
                explicit_intent=Intent.SCENARIO_ANALYSIS,
                explicit_scenarios=[ScenarioIntentSpec(
                    action=ScenarioActionType.CHANGE_DEMAND,
                    facility_ids=["CUST_01"],
                    demand_multiplier=1.2,
                )],
                actor=Actor(role=ActorRole.PLANNER, actor_id="live_validator"),
            ),
        },
    ]

    results = []

    for test_info in stage1_cases:
        num = test_info["case_num"]
        title = test_info["title"]
        prompt = test_info["prompt"]
        req = test_info["request"]

        print("\n" + "-" * 80)
        print(f"[{num}/5] {title}")
        print(f"Prompt: \"{prompt}\"")
        print(f"Current Live Calls Issued Before Request: {live_planner.calls_attempted} / 15")

        t0 = time.time()
        try:
            resp = orch.run_sync(req)
            dur = time.time() - t0
            trace = orch.get_trace(resp.execution_id)

            executed_caps = [s["capability"] for s in resp.steps]
            step_statuses = {s["capability"]: s["status"] for s in resp.steps}
            warnings = list(resp.warnings)
            fallback_used = any("fallback" in w.lower() or "falling back" in w.lower() or "fell back" in w.lower() for w in warnings)

            print(f"  Execution Time: {dur:.2f}s")
            print(f"  Response Status: {resp.status}")
            print(f"  Plan Origin: {getattr(resp, 'plan_origin', 'N/A')}")
            print(f"  Executed Capabilities ({len(executed_caps)}): {executed_caps}")
            print(f"  Fallback Triggered: {fallback_used}")
            if warnings:
                print(f"  Warnings ({len(warnings)}): {warnings}")

            results.append({
                "case_num": num,
                "title": title,
                "prompt": prompt,
                "duration_s": dur,
                "response_status": resp.status,
                "executed_capabilities": executed_caps,
                "step_statuses": step_statuses,
                "fallback_triggered": fallback_used,
                "warnings": warnings,
                "results_keys": list(resp.results.keys()),
                "governance": resp.governance.classification.value if resp.governance else None,
                "raw_model_available": True if trace and trace.llm_calls else False,
            })
        except Exception as exc:
            dur = time.time() - t0
            print(f"  FAILED with Exception: {exc} ({dur:.2f}s)")
            results.append({
                "case_num": num,
                "title": title,
                "prompt": prompt,
                "duration_s": dur,
                "error": str(exc),
                "fallback_triggered": False,
            })

    print("\n" + "=" * 80)
    print("STAGE 1 COMPLETED (5 CALLS ISSUED)")
    print("=" * 80)
    print(f"Total Live Calls Attempted: {live_planner.calls_attempted} / 15")
    print(f"Calls Successful:           {live_planner.calls_successful}")
    print(f"Calls Failed:               {live_planner.calls_failed}")
    print(f"Remaining Call Allowance:   {live_planner.max_calls - live_planner.calls_attempted}")
    print("=" * 80)

    # Save stage 1 results
    out_path = os.path.join(os.path.dirname(__file__), "stage1_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "calls_attempted": live_planner.calls_attempted,
            "calls_successful": live_planner.calls_successful,
            "calls_failed": live_planner.calls_failed,
            "results": results,
        }, f, indent=2)
    print(f"Results written to {out_path}")


if __name__ == "__main__":
    run_stage1()
