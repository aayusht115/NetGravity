"""
Orchestrator — Audit trail and execution tracing.

The audit record must be able to answer two questions long after a run:

    "Why did NetGravity make this recommendation?"
    "Which data, model and scenario version produced this result?"

So the trail stores DECISION PROVENANCE, not just the final answer: the
interpreted intent and how it was derived, the plan, every tool invocation with
timing and retries, the data version in play, engine outputs, risk inputs and
formula, the governance verdict and the rule that produced it, and any model
output that contributed.

Credential material is never recorded. Model prompts are not stored (they can
contain business data and add little forensic value); model *outputs* are,
truncated, because they influenced the response.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from netgravity.orchestrator.audit import events

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AuditEvent:
    """One recorded moment in an execution."""
    sequence: int
    at: str
    event_type: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionTrace:
    """
    Full provenance for one execution.

    Append-only during a run; serialised at the end.
    """
    execution_id: str
    request_id: str = ""
    actor_id: str = ""
    actor_role: str = ""
    started_at: str = field(default_factory=_utc_now)
    completed_at: Optional[str] = None

    raw_input: str = ""
    interpreted_intent: Optional[str] = None
    intent_source: Optional[str] = None
    intent_confidence: Optional[float] = None

    workflow_id: Optional[str] = None
    plan_steps: List[Dict[str, Any]] = field(default_factory=list)

    # Immutable data references this run operated on.
    baseline_snapshot_id: Optional[str] = None
    data_version: Optional[str] = None
    scenario_ids: List[str] = field(default_factory=list)
    scenario_overrides: List[str] = field(default_factory=list)

    events: List[AuditEvent] = field(default_factory=list)
    tool_invocations: List[Dict[str, Any]] = field(default_factory=list)
    engine_results: Dict[str, Any] = field(default_factory=dict)
    risk_calculation: Optional[Dict[str, Any]] = None
    governance_decision: Optional[Dict[str, Any]] = None
    llm_outputs: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    state_history: List[Dict[str, str]] = field(default_factory=list)

    final_status: Optional[str] = None
    final_summary: str = ""
    action_taken: Optional[str] = None
    approval: Optional[Dict[str, Any]] = None

    _seq: int = 0

    def record(self, event_type: str, **detail: Any) -> None:
        """
        Append one event, stamped with correlation identifiers.

        The stamp is applied here rather than at each call site so no emitter can
        forget it: §26 requires execution_id, workflow_id, step_id and
        snapshot_id on every structured event. A `step_id` passed by the caller
        wins; the rest come from the trace, which owns them.
        """
        self._seq += 1
        stamped: Dict[str, Any] = {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "snapshot_id": self.baseline_snapshot_id,
            "step_id": None,
        }
        stamped.update(detail)
        self.events.append(AuditEvent(
            sequence=self._seq, at=_utc_now(), event_type=event_type, detail=stamped,
        ))
        logger.info(
            "orchestrator.event type=%s execution_id=%s workflow_id=%s step_id=%s "
            "snapshot_id=%s",
            event_type, stamped["execution_id"], stamped["workflow_id"],
            stamped["step_id"], stamped["snapshot_id"],
        )

    def events_of(self, event_type: str) -> List[AuditEvent]:
        """Every recorded event of one type, in order. For assertions and replay."""
        return [e for e in self.events if e.event_type == event_type]

    def has_event(self, event_type: str) -> bool:
        return any(e.event_type == event_type for e in self.events)

    def record_tool(
        self,
        *,
        step_id: str,
        capability: str,
        success: bool,
        duration_seconds: float,
        attempts: int,
        execution_mode: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        self.tool_invocations.append({
            "step_id": step_id,
            "capability": capability,
            "success": success,
            "duration_seconds": round(duration_seconds, 4),
            "attempts": attempts,
            "execution_mode": execution_mode,
            "error_code": error_code,
            "error_message": error_message,
            "at": _utc_now(),
        })

    def record_llm(self, purpose: str, source: str, output_excerpt: str) -> None:
        """
        Record that a model contributed, and what it said.

        Prompts are deliberately not stored; outputs are truncated.
        """
        self.llm_outputs.append({
            "purpose": purpose,
            "source": source,
            "output_excerpt": (output_excerpt or "")[:1500],
            "at": _utc_now(),
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "request_id": self.request_id,
            "actor": {"actor_id": self.actor_id, "role": self.actor_role},
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input": self.raw_input,
            "intent": {
                "interpreted": self.interpreted_intent,
                "source": self.intent_source,
                "confidence": self.intent_confidence,
            },
            "workflow_id": self.workflow_id,
            "plan_steps": self.plan_steps,
            "data_references": {
                "baseline_snapshot_id": self.baseline_snapshot_id,
                "data_version": self.data_version,
                "scenario_ids": self.scenario_ids,
                "scenario_overrides": self.scenario_overrides,
            },
            "state_history": self.state_history,
            "tool_invocations": self.tool_invocations,
            "engine_results": self.engine_results,
            "risk_calculation": self.risk_calculation,
            "governance_decision": self.governance_decision,
            "llm_outputs": self.llm_outputs,
            "errors": self.errors,
            "events": [
                {"sequence": e.sequence, "at": e.at, "type": e.event_type,
                 "detail": e.detail}
                for e in self.events
            ],
            "outcome": {
                "status": self.final_status,
                "summary": self.final_summary,
                "action_taken": self.action_taken,
                "approval": self.approval,
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    @classmethod
    def from_dict(cls, document: Dict[str, Any]) -> "ExecutionTrace":
        """
        Rebuild a trace from what `to_dict()` wrote.

        The exact inverse, so a trace read back from a durable store answers
        the same questions as one still in memory. Written beside `to_dict()`
        deliberately: the two must be changed together, and a test asserts a
        round trip preserves every field.
        """
        actor = document.get("actor") or {}
        intent = document.get("intent") or {}
        refs = document.get("data_references") or {}
        outcome = document.get("outcome") or {}
        trace = cls(
            execution_id      = str(document.get("execution_id") or ""),
            request_id        = str(document.get("request_id") or ""),
            actor_id          = str(actor.get("actor_id") or ""),
            actor_role        = str(actor.get("role") or ""),
            started_at        = str(document.get("started_at") or _utc_now()),
            completed_at      = document.get("completed_at"),
            raw_input         = str(document.get("input") or ""),
            interpreted_intent= intent.get("interpreted"),
            intent_source     = intent.get("source"),
            intent_confidence = intent.get("confidence"),
            workflow_id       = document.get("workflow_id"),
            plan_steps        = list(document.get("plan_steps") or []),
            baseline_snapshot_id = refs.get("baseline_snapshot_id"),
            data_version      = refs.get("data_version"),
            scenario_ids      = list(refs.get("scenario_ids") or []),
            scenario_overrides= list(refs.get("scenario_overrides") or []),
            tool_invocations  = list(document.get("tool_invocations") or []),
            engine_results    = dict(document.get("engine_results") or {}),
            risk_calculation  = document.get("risk_calculation"),
            governance_decision = document.get("governance_decision"),
            llm_outputs       = list(document.get("llm_outputs") or []),
            errors            = list(document.get("errors") or []),
            state_history     = list(document.get("state_history") or []),
            final_status      = outcome.get("status"),
            final_summary     = str(outcome.get("summary") or ""),
            action_taken      = outcome.get("action_taken"),
            approval          = outcome.get("approval"),
        )
        trace.events = [
            AuditEvent(sequence=int(e.get("sequence") or 0), at=str(e.get("at") or ""),
                       event_type=str(e.get("type") or ""), detail=dict(e.get("detail") or {}))
            for e in (document.get("events") or [])
        ]
        trace._seq = max((e.sequence for e in trace.events), default=0)
        return trace

    def explain(self) -> str:
        """Human-readable provenance narrative — the 'why' in one block."""
        lines = [
            f"Execution {self.execution_id} (request {self.request_id})",
            f"  Actor          : {self.actor_id} [{self.actor_role}]",
            f"  Input          : {self.raw_input[:160]}",
            f"  Intent         : {self.interpreted_intent} "
            f"(source={self.intent_source}, confidence={self.intent_confidence})",
            f"  Workflow       : {self.workflow_id}",
            f"  Snapshot       : {self.baseline_snapshot_id} (data_version={self.data_version})",
            f"  Scenarios      : {self.scenario_ids or '(none)'}",
            f"  Overrides      : {self.scenario_overrides or '(none)'}",
            "  Tools invoked  :",
        ]
        for inv in self.tool_invocations:
            mark = "ok " if inv["success"] else "FAIL"
            lines.append(
                f"    [{mark}] {inv['capability']:<28} {inv['duration_seconds']:>7.3f}s "
                f"attempts={inv['attempts']}"
                + (f" error={inv['error_code']}" if inv.get("error_code") else "")
            )
        if self.risk_calculation:
            lines.append(f"  Risk           : {self.risk_calculation}")
        if self.governance_decision:
            g = self.governance_decision
            lines.append(
                f"  Governance     : {g.get('classification')} — {g.get('reason')}"
            )
            lines.append(f"  Rules fired    : {g.get('triggered_rules')}")
        lines.append(f"  Outcome        : {self.final_status}")
        return "\n".join(lines)


class TraceSink(Protocol):
    """
    Somewhere a sealed trace can be kept beyond this process.

    A protocol rather than an import, so `netgravity.orchestrator` keeps
    knowing nothing about the application's database. The application supplies
    an implementation at start-up; without one the logger behaves exactly as it
    did.
    """

    def save(self, trace: "ExecutionTrace") -> None: ...

    def load(self, execution_id: str) -> Optional[Dict[str, Any]]: ...

    def recent(self, n: int) -> List[Dict[str, Any]]: ...


class AuditLogger:
    """
    Creates, stores and retrieves execution traces.

    A bounded in-memory ring buffer in front of an optional durable sink.

    The ring buffer alone was the whole store, which meant the answers outlived
    the workings: a KPI survived a restart because it was persisted, while the
    record of which capability produced it, from which snapshot, with which
    warnings and which governance verdict, did not. It also meant the 501st
    execution silently evicted the 1st. Both are the wrong way round for the
    one record whose entire purpose is to be readable long afterwards.

    With a sink attached, `finish()` writes the sealed trace through and `get()`
    reads back from it on a miss, so an eviction or a restart costs a database
    round trip rather than the trail. Writes are guarded: a trace that cannot be
    stored is logged and the execution still returns its answer.
    """

    def __init__(self, max_traces: int = 500,
                 sink: Optional[TraceSink] = None) -> None:
        self._traces: Dict[str, ExecutionTrace] = {}
        self._order: List[str] = []
        self._max = max_traces
        self._lock = threading.RLock()
        self._sink = sink

    def attach_sink(self, sink: Optional[TraceSink]) -> None:
        """Install (or remove) the durable store. Called once at start-up."""
        self._sink = sink

    @property
    def is_durable(self) -> bool:
        return self._sink is not None

    def start(self, context: Any) -> ExecutionTrace:
        trace = ExecutionTrace(
            execution_id=context.execution_id,
            request_id=context.request_id,
            actor_id=context.actor.actor_id,
            actor_role=context.actor.role.value,
            raw_input=context.raw_input,
            baseline_snapshot_id=context.baseline_snapshot_id,
        )
        trace.record(events.EXECUTION_STARTED, intent="(pending interpretation)")
        with self._lock:
            self._traces[context.execution_id] = trace
            self._order.append(context.execution_id)
            while len(self._order) > self._max:
                self._traces.pop(self._order.pop(0), None)
        return trace

    def finish(self, trace: ExecutionTrace, context: Any) -> None:
        """Seal a trace with the final outcome and full state history."""
        trace.completed_at = _utc_now()
        trace.final_status = context.current_state.value
        trace.state_history = [
            {"from": t.from_state, "to": t.to_state, "at": t.at, "note": t.note}
            for t in context.state_history
        ]
        trace.errors = list(context.errors)
        trace.scenario_ids = list(context.scenario_ids or
                                  ([context.scenario_id] if context.scenario_id else []))

        # Collect the hypothetical overrides every scenario step applied, so the
        # trail records exactly WHAT was changed, not merely that it changed.
        overrides: List[str] = []
        for result in context.step_results.values():
            if result.success and result.capability.startswith("scenario."):
                for item in result.output.get("overrides", []) or []:
                    if item not in overrides:
                        overrides.append(str(item))
        if overrides:
            trace.scenario_overrides = overrides
        if context.governance_result is not None:
            trace.governance_decision = context.governance_result.model_dump()
        if context.risk_results is not None:
            trace.risk_calculation = context.risk_results.model_dump()
        if context.approval_request is not None:
            trace.approval = context.approval_request.model_dump()
        if context.reasoning is not None:
            trace.final_summary = context.reasoning.summary
            trace.record_llm("reasoning", context.reasoning.source, context.reasoning.summary)
        if trace.workflow_id:
            trace.record(
                events.WORKFLOW_COMPLETED,
                status=trace.final_status,
                completed_steps=len(context.completed_steps),
                failed_steps=len(context.failed_steps),
                blocked_steps=len(context.blocked_steps),
            )
        trace.record(events.EXECUTION_COMPLETED, status=trace.final_status)

        logger.info(
            "orchestrator.audit.sealed execution_id=%s status=%s tools=%d errors=%d",
            trace.execution_id, trace.final_status,
            len(trace.tool_invocations), len(trace.errors),
        )

        # Written once, when the trace is sealed and complete. Writing on every
        # event would put a database round trip inside the execution loop for a
        # record nobody can read until it is finished anyway.
        if self._sink is not None:
            try:
                self._sink.save(trace)
            except Exception as exc:  # noqa: BLE001
                # A trace that cannot be stored must not fail an execution that
                # has already succeeded — but it is never silent.
                logger.error("orchestrator.audit.persist_failed execution_id=%s error=%s",
                             trace.execution_id, exc)

    def get(self, execution_id: str) -> Optional[ExecutionTrace]:
        trace = self._traces.get(execution_id)
        if trace is not None or self._sink is None:
            return trace
        # Evicted from the buffer, or written by a process that has since
        # restarted. Read it back rather than reporting that it never happened.
        try:
            document = self._sink.load(execution_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("orchestrator.audit.read_failed execution_id=%s error=%s",
                         execution_id, exc)
            return None
        return ExecutionTrace.from_dict(document) if document else None

    def list_ids(self) -> List[str]:
        return list(self._order)

    def recent(self, n: int = 10) -> List[ExecutionTrace]:
        """
        The most recent traces, oldest first.

        Drawn from the durable store when there is one, so "recent" means
        recent across the deployment rather than recent in this process since
        its last restart.
        """
        if self._sink is not None:
            try:
                documents = self._sink.recent(n)
            except Exception as exc:  # noqa: BLE001
                logger.error("orchestrator.audit.recent_failed error=%s", exc)
            else:
                if documents:
                    return list(reversed(
                        [ExecutionTrace.from_dict(d) for d in documents]))
        with self._lock:
            return [self._traces[i] for i in self._order[-n:] if i in self._traces]
