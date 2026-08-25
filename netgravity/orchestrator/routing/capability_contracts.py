"""
Orchestrator — The capability catalogue.

One `CapabilityContract` per capability the system has, describing what it
consumes, what it produces, and where the authoritative answer lands. This is
the metadata a future planner reads to decide which capabilities a given
question needs — and, just as importantly, which it does NOT.

Sixteen capabilities are declared: the thirteen registered with handlers, plus
three that are real but are not plan steps (extraction, the Digital Twin
projection, forecast signal routing). They are declared because "what can this
system do?" has to have one answer; they are marked SERVICE or EMBEDDED because
a planner that tried to schedule them would be wrong.

WHAT IS DELIBERATELY ABSENT
---------------------------
Conversation/NLU. It is not a capability in this architecture: `ChatService`
runs `ConversationalNLU` to work out WHICH capabilities a turn needs, before any
execution exists. It is the caller of the capability layer, not a member of it,
and registering it would create a cycle in which understanding a request became
a step inside executing it. Its absence here is a finding, not an omission.

Every string in this module was read off the implementation it describes; the
tests in `test_agent_contract.py` re-check them against the live registry and
the real `ExecutionContext` fields, so a rename cannot leave the catalogue
quietly lying.
"""

from __future__ import annotations

from typing import Dict, Tuple

from netgravity.orchestrator.core.planner import (
    CAP_CREATE_SCEN,
    CAP_EXTRACT,
    CAP_FORECAST,
    CAP_GOVERN,
    CAP_INTERPRET_SIG,
    CAP_KPI,
    CAP_LOAD_NETWORK,
    CAP_OPTIMIZE,
    CAP_OPTIMIZE_SCEN,
    CAP_REASON,
    CAP_REI,
    CAP_RISK,
    CAP_ROUTE_SIGNAL,
    CAP_SCORE_MARKET,
    CAP_TWIN_PUBLISH,
    CAP_VALIDATE_SCEN,
)
from netgravity.orchestrator.schemas.capability import (
    CapabilityContract,
    CapabilityDomain,
    InvocationMode,
)
from netgravity.orchestrator.schemas.plans import ExecutionMode

D = CapabilityDomain
M = InvocationMode
DET = ExecutionMode.DETERMINISTIC
PROB = ExecutionMode.PROBABILISTIC


CAPABILITY_CONTRACTS: Tuple[CapabilityContract, ...] = (

    # ---------------------------------------------------------------
    # Data in
    # ---------------------------------------------------------------
    CapabilityContract(
        capability_id=CAP_EXTRACT,
        domain=D.EXTRACTION,
        provider="ExtractionParsingAgent",
        description="Parse client files into a validated CanonicalNetwork.",
        input_type="ExtractionRequest",
        output_type="ExtractionResult",
        authoritative_field="extraction_result",
        required_inputs=("source",),
        validations=("row_rules", "referential_integrity", "adjudication"),
        execution_mode=DET,
        invocation=M.SERVICE,
        planner_selectable=False,
        notes="Runs before any execution exists — it produces the network a run "
              "is later pinned to, so it cannot be a step inside that run. "
              "Reached through the ingestion API.",
    ),

    CapabilityContract(
        capability_id=CAP_LOAD_NETWORK,
        domain=D.NETWORK_STATE,
        provider="SnapshotManager",
        description="Pin and verify the observed network snapshot for this run.",
        output_type="NetworkSnapshot",
        authoritative_field="baseline_snapshot_id",
        authoritative_is_reference=True,
        required_inputs=(),
        validations=("snapshot_freshness",),
        execution_mode=DET,
    ),

    # ---------------------------------------------------------------
    # Signals — two domains, never one
    # ---------------------------------------------------------------
    CapabilityContract(
        capability_id=CAP_INTERPRET_SIG,
        domain=D.SIGNAL_INTERPRETATION,
        provider="ExternalSignalAgent",
        description="Interpret external evidence into a likelihood with provenance.",
        input_type="ExternalSignal",
        output_type="ExternalSignal",
        authoritative_field="external_signal",
        dependencies=(CAP_LOAD_NETWORK,),
        execution_mode=PROB,
        llm_backed=True,
        notes="Produces the P that feeds RF. The probability itself is read from "
              "the signal deterministically; the model only helps interpret the "
              "text around it. PROBABILISTIC, so RF treats a missing likelihood "
              "as NOT_COMPUTABLE rather than defaulting it.",
    ),

    CapabilityContract(
        capability_id=CAP_SCORE_MARKET,
        domain=D.SIGNAL_ROUTING,
        provider="relevance guardrail (ingestion.guardrails.relevance)",
        description="Score reported market signals against the guardrail policy.",
        input_type="MarketIntelligenceSignal",
        output_type="MarketIntelligenceSignal",
        authoritative_field="market_signals",
        dependencies=(CAP_LOAD_NETWORK,),
        validations=("relevance_guardrail",),
        execution_mode=DET,
        notes="Attaches a relevance verdict. Carries NO probability and can "
              "never reach RF — a separate domain from SIGNAL_INTERPRETATION "
              "for exactly that reason.",
    ),

    CapabilityContract(
        capability_id=CAP_ROUTE_SIGNAL,
        domain=D.SIGNAL_ROUTING,
        provider="ExternalSignalRouter",
        description="Decide whether a signal is eligible to adjust a forecast.",
        input_type="MarketIntelligenceSignal",
        output_type="SignalRoutingDecision",
        authoritative_field="signal_routing",
        execution_mode=DET,
        invocation=M.EMBEDDED,
        host_capability=CAP_FORECAST,
        planner_selectable=False,
        notes="A gate inside the forecast handler with no independent entry "
              "point. Declared so its authority is on the record: its "
              "confidence score decides forecast eligibility only, and is not "
              "the RF probability.",
    ),

    # ---------------------------------------------------------------
    # Scenarios
    # ---------------------------------------------------------------
    CapabilityContract(
        capability_id=CAP_CREATE_SCEN,
        domain=D.SCENARIO,
        provider="ScenarioBuilder",
        description="Materialise a hypothetical network, isolated from observed state.",
        input_type="ScenarioIntentSpec",
        output_type="ScenarioRecord",
        authoritative_field="scenario_id",
        authoritative_is_reference=True,
        dependencies=(CAP_LOAD_NETWORK,),
        execution_mode=DET,
    ),

    CapabilityContract(
        capability_id=CAP_VALIDATE_SCEN,
        domain=D.SCENARIO,
        provider="ScenarioValidator",
        description="Validate scenario overrides and provenance before solving.",
        output_type="ValidationReport",
        dependencies=(CAP_CREATE_SCEN,),
        validations=("scenario_overrides", "scenario_provenance"),
        execution_mode=DET,
        notes="A HARD dependency of the scenario solve. Solving an unvalidated "
              "scenario would produce numbers nobody should trust.",
    ),

    # ---------------------------------------------------------------
    # Optimization — the authoritative engine
    # ---------------------------------------------------------------
    CapabilityContract(
        capability_id=CAP_OPTIMIZE,
        domain=D.OPTIMIZATION,
        provider="OptimizationClient (MILP)",
        description="Solve the observed network with the authoritative MILP.",
        input_type="CanonicalNetwork",
        output_type="NetworkStateResult",
        authoritative_field="network_states",
        dependencies=(CAP_LOAD_NETWORK,),
        validations=("validate_optimization",),
        execution_mode=DET,
        notes="`engine_results` holds a flattened projection; per-facility "
              "utilisation and per-lane flow survive only in `network_states`.",
    ),

    CapabilityContract(
        capability_id=CAP_OPTIMIZE_SCEN,
        domain=D.OPTIMIZATION,
        provider="OptimizationClient (MILP)",
        description="Re-optimise a hypothetical scenario network.",
        input_type="CanonicalNetwork",
        output_type="NetworkStateResult",
        authoritative_field="network_states",
        dependencies=(CAP_VALIDATE_SCEN,),
        validations=("validate_optimization",),
        execution_mode=DET,
    ),

    CapabilityContract(
        capability_id=CAP_KPI,
        domain=D.KPI,
        provider="KPIClient",
        description="Project KPIs from an optimization result.",
        output_type="NetworkKPIs",
        dependencies=(CAP_OPTIMIZE,),
        execution_mode=DET,
    ),

    # ---------------------------------------------------------------
    # Resilience and risk
    # ---------------------------------------------------------------
    CapabilityContract(
        capability_id=CAP_REI,
        domain=D.RESILIENCE,
        provider="REIClient",
        description="Assess facility resilience exposure (REI registry).",
        input_type="CanonicalNetwork",
        output_type="FacilityResilienceRegistry",
        authoritative_field="rei_registry",
        dependencies=(CAP_LOAD_NETWORK,),
        validations=("validate_rei",),
        execution_mode=DET,
        notes="Read the registry, not the flattened projection: rebuilding one "
              "from the dict would default a FAILED node's status to OK.",
    ),

    CapabilityContract(
        capability_id=CAP_RISK,
        domain=D.RISK,
        provider="risk_factor.assess_network_risk",
        description="Combine likelihood and exposure into RF deterministically.",
        output_type="RiskAssessment",
        authoritative_field="risk_results",
        dependencies=(CAP_REI, CAP_INTERPRET_SIG),
        # BOTH optional, and this is the single most important criticality
        # declaration in the catalogue. Refusing to run RF when only one input
        # is present would replace an explicit NOT_COMPUTABLE row — which names
        # exactly what was missing — with a capability that simply did not run.
        # The handler reports the absence far better than a refusal could.
        optional_dependencies=(CAP_REI, CAP_INTERPRET_SIG),
        execution_mode=DET,
        notes="RF = P + REI - P*REI, computed with no model involvement. Both "
              "inputs are SOFT: either may be genuinely absent, and absence is "
              "reported as NOT_COMPUTABLE rather than defaulted to zero.",
    ),

    # ---------------------------------------------------------------
    # Forecasting
    # ---------------------------------------------------------------
    CapabilityContract(
        capability_id=CAP_FORECAST,
        domain=D.FORECAST,
        provider="ForecastingService",
        description="Estimate future demand from observed history.",
        input_type="ForecastRequest",
        output_type="ForecastResult",
        authoritative_field="forecast_result",
        dependencies=(CAP_LOAD_NETWORK,),
        execution_mode=DET,
        notes="DETERMINISTIC: no model call exists anywhere in the forecasting "
              "package. With no history available it reports that rather than "
              "inventing a series.",
    ),

    # ---------------------------------------------------------------
    # Explanation and control
    # ---------------------------------------------------------------
    CapabilityContract(
        capability_id=CAP_REASON,
        domain=D.REASONING,
        provider="ReasoningAgent",
        description="Explain deterministic results in natural language (advisory).",
        output_type="ReasoningResult",
        authoritative_field="reasoning",
        # Intentionally EMPTY. Reasoning explains whatever evidence exists, and
        # every edge into it is SOFT in the plan. Declaring hard dependencies
        # here would suggest a missing input should suppress the narrative,
        # when the requirement is the opposite: say what is missing.
        dependencies=(),
        # Terminal: explains whatever the analytic work produced. Rank 1 so it
        # runs before governance, which then rules on the explanation.
        terminal_rank=1,
        validations=("numeric_grounding",),
        execution_mode=PROB,
        llm_backed=True,
        notes="ADVISORY and never authoritative. Its numbers are checked against "
              "the deterministic results by `numeric_grounding`, and an "
              "ungrounded figure is stripped rather than published.",
    ),

    CapabilityContract(
        capability_id=CAP_GOVERN,
        domain=D.GOVERNANCE,
        provider="GovernancePolicy",
        description="Apply deterministic governance rules and classify the action.",
        output_type="GovernanceDecision",
        authoritative_field="governance_result",
        # Also empty, for a different reason: governance must ALWAYS return a
        # verdict. Missing evidence makes it more conservative, never absent, so
        # nothing it reads can be a precondition that blocks it.
        dependencies=(),
        # Terminal, and LAST: every response leaves with a verdict, and the
        # verdict is passed on the narrative as well as the numbers.
        terminal_rank=2,
        execution_mode=DET,
        notes="Runs both as a plan step and as a post-run safety net, so a run "
              "that failed before reaching the step is still governed.",
    ),

    # ---------------------------------------------------------------
    # Representation
    # ---------------------------------------------------------------
    CapabilityContract(
        capability_id=CAP_TWIN_PUBLISH,
        domain=D.DIGITAL_TWIN,
        provider="DigitalTwinService",
        description="Publish this run's authoritative results as a twin state.",
        output_type="TwinStateRef",
        authoritative_field="twin_refs",
        dependencies=(CAP_OPTIMIZE, CAP_REI, CAP_RISK),
        # All three optional. The twin publishes in every condition on purpose,
        # including a failed run — refusing to draw the picture would leave a
        # viewer looking at a stale one with no sign anything had gone wrong.
        # It marks each absence with an explicit UnavailableValue instead.
        optional_dependencies=(CAP_OPTIMIZE, CAP_REI, CAP_RISK),
        execution_mode=DET,
        invocation=M.SERVICE,
        planner_selectable=False,
        notes="The ONLY path into the twin, invoked by the orchestrator after "
              "the plan settles. It composes results that are already "
              "authoritative and computes nothing itself — which is why it is "
              "not a plan step and why no engine may reach it.",
    ),
)


#: capability_id -> contract. Built once; the registry copies from it.
CONTRACTS_BY_ID: Dict[str, CapabilityContract] = {
    c.capability_id: c for c in CAPABILITY_CONTRACTS
}


def _assert_catalogue_is_coherent() -> None:
    """
    Fail at import time if the catalogue contradicts itself.

    Cheap, and it turns a class of silent metadata drift into an immediate
    ImportError rather than a planner that builds an impossible plan.
    """
    ids = [c.capability_id for c in CAPABILITY_CONTRACTS]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"Duplicate capability contracts declared: {duplicates}")

    known = set(ids)
    for contract in CAPABILITY_CONTRACTS:
        unknown = [d for d in contract.dependencies if d not in known]
        if unknown:
            raise ValueError(
                f"Capability '{contract.capability_id}' declares unknown "
                f"dependencies {unknown}."
            )
        if contract.host_capability and contract.host_capability not in known:
            raise ValueError(
                f"Capability '{contract.capability_id}' names unknown host "
                f"'{contract.host_capability}'."
            )


_assert_catalogue_is_coherent()


__all__ = ["CAPABILITY_CONTRACTS", "CONTRACTS_BY_ID"]
