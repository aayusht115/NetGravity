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
GOVERNANCE_DECISION  = "governance_decision"
GOVERNANCE_APPLIED   = "governance_applied"

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
}

#: Correlation keys stamped onto every event detail. §26 requires these four.
CORRELATION_KEYS = ("execution_id", "workflow_id", "step_id", "snapshot_id")
