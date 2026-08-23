"""
Orchestrator — Canonical observability event names.

One module so the set is enumerable, greppable and testable, rather than a
scatter of string literals. `ExecutionTrace.record()` stamps every event with
the execution / workflow / snapshot correlation identifiers, so an event is
self-describing when read on its own.

Two scopes are deliberately distinct and both are recorded:

    EXECUTION_*   the whole run, including intent interpretation, which happens
                  BEFORE a workflow has been chosen.
    WORKFLOW_*    the planned graph, which only exists once intent resolved.

An execution that fails to classify its intent therefore has EXECUTION_STARTED
and EXECUTION_COMPLETED but no WORKFLOW_STARTED — which is the correct record of
what happened.

What is never recorded here: credentials, model prompts, or raw client business
data. Events carry identifiers, statuses and deterministic numbers only.
"""

from __future__ import annotations

from typing import Set

# --- lifecycle -------------------------------------------------------------
EXECUTION_STARTED    = "execution_started"
EXECUTION_COMPLETED  = "execution_completed"
WORKFLOW_STARTED     = "workflow_started"
WORKFLOW_COMPLETED   = "workflow_completed"

# --- planning --------------------------------------------------------------
INTENT_RESOLVED      = "intent_resolved"
PLAN_BUILT           = "plan_built"
SNAPSHOT_VALIDATED   = "snapshot_validated"

# --- step execution --------------------------------------------------------
STEP_STARTED         = "step_started"
STEP_COMPLETED       = "step_completed"
STEP_FAILED          = "step_failed"
STEP_BLOCKED         = "step_blocked"
STEP_DEGRADED        = "step_degraded"
STEP_EXCEPTION       = "step_exception"
EVIDENCE_UNAVAILABLE = "evidence_unavailable"

# --- deterministic risk chain ---------------------------------------------
REI_LOOKUP           = "rei_lookup"
RF_CALCULATED        = "rf_calculated"
RF_NOT_COMPUTABLE    = "rf_not_computable"
SOLVER_INFEASIBLE    = "solver_infeasible"

# --- reasoning and governance ---------------------------------------------
REASONING_COMPLETED  = "reasoning_completed"
GROUNDING_COMPLETED  = "grounding_completed"
#: The orchestrator's routing decision over extracted external signals:
#: which may inform a forecast, and why the rest may not. Emitted before
#: the forecast itself, so the decision is auditable independently of what
#: the forecast then did with it.
SIGNALS_ROUTED       = "signals_routed"

#: One forecast produced. Carries per-status counts so a run where most
#: series failed is visible without reading the payload.
FORECAST_COMPLETED   = "forecast_completed"

GOVERNANCE_DECISION  = "governance_decision"
GOVERNANCE_APPLIED   = "governance_applied"

# --- digital twin ----------------------------------------------------------
#: One network state published to the Digital Twin. Emitted per state, so a
#: comparison run producing three scenarios emits three.
TWIN_STATE_PUBLISHED = "twin_state_published"

# --- conversational layer (Phase 3) ---------------------------------------
CHAT_REQUEST_RECEIVED    = "chat_request_received"
INTENT_CLASSIFIED        = "intent_classified"
INTENT_VALIDATION_FAILED = "intent_validation_failed"
ENTITY_RESOLUTION_FAILED = "entity_resolution_failed"
CLARIFICATION_REQUIRED   = "clarification_required"
WORKFLOW_SELECTED        = "workflow_selected"
LLM_FAILURE              = "llm_failure"
CHAT_RESPONSE_GENERATED  = "chat_response_generated"

#: Chat events that occur BEFORE an orchestrator execution exists, and so are
#: recorded on the conversation rather than on an execution trace.
PRE_EXECUTION_CHAT_EVENTS: Set[str] = {
    CHAT_REQUEST_RECEIVED, INTENT_CLASSIFIED, INTENT_VALIDATION_FAILED,
    ENTITY_RESOLUTION_FAILED, CLARIFICATION_REQUIRED, LLM_FAILURE,
}

#: Every event the control plane is expected to be able to emit. Asserted by the
#: observability test so a renamed constant cannot silently drop an event.
CANONICAL_EVENTS: Set[str] = {
    EXECUTION_STARTED, EXECUTION_COMPLETED,
    WORKFLOW_STARTED, WORKFLOW_COMPLETED,
    INTENT_RESOLVED, PLAN_BUILT, SNAPSHOT_VALIDATED,
    STEP_STARTED, STEP_COMPLETED, STEP_FAILED, STEP_BLOCKED,
    STEP_DEGRADED, STEP_EXCEPTION, EVIDENCE_UNAVAILABLE,
    REI_LOOKUP, RF_CALCULATED, RF_NOT_COMPUTABLE, SOLVER_INFEASIBLE,
    REASONING_COMPLETED, GROUNDING_COMPLETED,
    GOVERNANCE_DECISION, GOVERNANCE_APPLIED,
    SIGNALS_ROUTED, FORECAST_COMPLETED, TWIN_STATE_PUBLISHED,
}

#: Conversational events. Kept as a separate set because most of them fire
#: before an execution exists, so they are not reachable from an execution
#: trace and must not be asserted as part of `CANONICAL_EVENTS` coverage.
CHAT_EVENTS: Set[str] = {
    CHAT_REQUEST_RECEIVED, INTENT_CLASSIFIED, INTENT_VALIDATION_FAILED,
    ENTITY_RESOLUTION_FAILED, CLARIFICATION_REQUIRED, WORKFLOW_SELECTED,
    LLM_FAILURE, CHAT_RESPONSE_GENERATED,
}

#: Correlation keys stamped onto every event detail. §26 requires these four.
CORRELATION_KEYS = ("execution_id", "workflow_id", "step_id", "snapshot_id")
