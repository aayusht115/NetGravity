"""
Phase 8.5 — Live OpenAI Planner Validation Script.

FOR USE TOMORROW AFTER API QUOTA RESETS.
DO NOT RUN IN AUTOMATED TEST SUITES.

Usage:
    export TEXT_API_TOKEN="<your-token>"
    python validation/agentic_phase_8_5_live.py

Guarantees:
  - Strict HARD limit of 15 live planner calls.
  - Stops before making call 16.
  - Uses existing LLMGateway and LiveLLMPlanner.
  - Never bypasses circuit breaker or rate limits.
  - Reports detailed execution statistics.
"""

import os
import sys
import time

from netgravity.orchestrator.agents.llm_gateway import LLMGateway, LLMGatewayConfig
from netgravity.orchestrator.planner.llm_planner import LiveLLMPlanner, MAX_LIVE_PLANNER_CALLS
from netgravity.orchestrator.registry import build_orchestrator
from netgravity.orchestrator.schemas.requests import (
    Actor,
    ActorRole,
    Intent,
    OrchestratorRequest,
    ScenarioActionType,
    ScenarioIntentSpec,
)
from netgravity.tests.fixtures.case16_synthetic import build_case16_network


# 15 distinct representative prompts for live evaluation
LIVE_VALIDATION_PROMPTS = [
    # 1-3: Network state & status
    ("What is the current business cost and health of the network?", Intent.NETWORK_STATE_QUERY, None),
    ("How many active warehouses and distribution centers are operating?", Intent.STATUS_QUERY, None),
    ("Provide a full baseline KPI summary of our supply chain.", Intent.NETWORK_STATE_QUERY, None),

    # 4-6: Forecast queries
    ("Forecast customer demand across Northern regions for the next quarter.", Intent.FORECAST, None),
    ("Estimate demand trends for customer markets in Western India.", Intent.FORECAST, None),
    ("Generate a demand projection assuming seasonal peak volume.", Intent.FORECAST, None),

    # 7-9: Resilience & Exposure
    ("Which distribution facility is most economically exposed to disruption?", Intent.RESILIENCE_QUERY, None),
    ("Rank our top three facilities by Resilience Exposure Index (REI).", Intent.RESILIENCE_QUERY, None),
    ("What is the single point of failure in our supply network?", Intent.RESILIENCE_QUERY, None),

    # 10-12: Scenario Analysis
    (
        "What happens to total network cost if we close DC_EAST?",
        Intent.SCENARIO_ANALYSIS,
        [ScenarioIntentSpec(action=ScenarioActionType.CLOSE_FACILITY, facility_ids=["DC_EAST"])],
    ),
    (
        "Evaluate the impact of reducing DC_WEST capacity by 2,000 units.",
        Intent.SCENARIO_ANALYSIS,
        [ScenarioIntentSpec(action=ScenarioActionType.CHANGE_CAPACITY, facility_ids=["DC_WEST"], capacity_delta_units=-2000)],
    ),
    (
        "Simulate increasing demand in customer market CUST_01 by 25%.",
        Intent.SCENARIO_ANALYSIS,
        [ScenarioIntentSpec(action=ScenarioActionType.CHANGE_DEMAND, facility_ids=["CUST_01"], demand_multiplier=1.25)],
    ),

    # 13-15: Market Intelligence & Explanations
    ("Diesel transport surcharge increased by 7% across all line-haul lanes.", Intent.MARKET_INTELLIGENCE, None),
    ("Port handling tariffs at Mumbai have risen by 10%. Record this intelligence.", Intent.MARKET_INTELLIGENCE, None),
    ("Explain why the baseline network allocated volume through DC_CENTRAL.", Intent.EXPLANATION, None),
]


def run_live_validation():
    print("=" * 70)
    print("NetGravity Phase 8.5 — Live OpenAI Planner Validation")
    print("=" * 70)

    token = os.environ.get("TEXT_API_TOKEN")
    if not token:
        print("[ERROR] TEXT_API_TOKEN environment variable is not set.")
        print("Please export TEXT_API_TOKEN before running live validation.")
        sys.exit(1)

    network = build_case16_network()
    gateway = LLMGateway(LLMGatewayConfig.from_env())

    if not gateway.available:
        print(f"[ERROR] LLMGateway is not available: {gateway.unavailable_reason()}")
        sys.exit(1)

    print(f"LLM Gateway configured. Base URL: {gateway.config.base_url}")
    print(f"HARD Maximum Calls Allowed: {MAX_LIVE_PLANNER_CALLS}")
    print("-" * 70)

    # Initialize Live Planner and Orchestrator
    temp_orch = build_orchestrator(enable_llm=False)
    live_planner = LiveLLMPlanner(gateway=gateway, registry=temp_orch.registry, max_calls=MAX_LIVE_PLANNER_CALLS)
    orch = build_orchestrator(
        network=network,
        gateway=gateway,
        enable_llm=True,
        llm_planner=live_planner,
    )

    results = []
    start_time = time.time()

    for idx, (prompt_text, intent, scenarios) in enumerate(LIVE_VALIDATION_PROMPTS[:MAX_LIVE_PLANNER_CALLS], 1):
        print(f"\n[Test {idx:02d}/{MAX_LIVE_PLANNER_CALLS}] Prompt: '{prompt_text}'")
        req = OrchestratorRequest(
            input=prompt_text,
            explicit_intent=intent,
            explicit_scenarios=list(scenarios) if scenarios else [],
            actor=Actor(role=ActorRole.PLANNER, actor_id="live_tester"),
        )

        t0 = time.time()
        try:
            resp = orch.run_sync(req)
            dur = time.time() - t0
            print(f"  -> Status: {resp.status} | Intent: {resp.intent} | Duration: {dur:.2f}s | Steps: {len(resp.steps)}")
            results.append({
                "test": idx,
                "prompt": prompt_text,
                "status": resp.status,
                "intent": resp.intent,
                "duration": dur,
                "success": resp.status in ("COMPLETED", "REQUIRES_APPROVAL", "REQUIRES_HUMAN"),
            })
        except Exception as exc:
            dur = time.time() - t0
            print(f"  -> FAILED: {exc} ({dur:.2f}s)")
            results.append({
                "test": idx,
                "prompt": prompt_text,
                "status": "EXCEPTION",
                "error": str(exc),
                "duration": dur,
                "success": False,
            })

    total_duration = time.time() - start_time
    print("\n" + "=" * 70)
    print("LIVE VALIDATION SUMMARY REPORT")
    print("=" * 70)
    print(f"Total Calls Attempted: {live_planner.calls_attempted} / {MAX_LIVE_PLANNER_CALLS}")
    print(f"Gateway Successful Responses: {live_planner.calls_successful}")
    print(f"Gateway Failed Responses:     {live_planner.calls_failed}")
    print(f"Total Execution Time:         {total_duration:.2f}s")
    success_count = sum(1 for r in results if r.get("success"))
    print(f"End-to-End Successful Runs:   {success_count} / {len(results)}")
    print("=" * 70)


if __name__ == "__main__":
    run_live_validation()
