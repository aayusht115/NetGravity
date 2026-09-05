"""
NetGravity — Action Agent Triggers
=====================================
The five entry points. This module is a DISPATCHER, not a second
decision-maker: everything it sends traces back to something the
orchestrator/MILP already computed (a GovernanceDecision, an
ApprovalRequest, a ReasoningResult) or a data-completeness check that is
purely rule-based (completeness.py). It never runs its own scenario and
never originates a recommendation.

Called from:
  - netgravity/ingestion/service.py       -> on_completeness_failure
  - netgravity/orchestrator/core/orchestrator.py (_govern) ->
        on_recommendation_card_created, on_investigate_card_created
  - netgravity/action_agent/api.py (inbound webhook) -> nothing here
        directly; that path re-enters the ingestion pipeline instead.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from netgravity.action_agent import email_builder, report_builder
from netgravity.action_agent.config import load_config
from netgravity.action_agent.dispatch_log import DispatchLogStore, DispatchRecord
from netgravity.action_agent.email_sender import get_sender
from netgravity.action_agent.recipients import NotificationRecipientStore, SourceContactStore
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.storage import get_storage

logger = logging.getLogger(__name__)


def _storage():
    # Reuses the same StorageBackend the ingestion pipeline uses — one data
    # lake, one NETGRAVITY_STORAGE_BACKEND switch, no second copy of the
    # local/Azure decision.
    return get_storage(IngestionConfig())


def _resume_link(run_id: str) -> str:
    cfg = load_config()
    base = (cfg.app_base_url or "").rstrip("/")
    return f"{base}/ingestion/{run_id}/review" if base else f"/ingestion/{run_id}/review"


def _deep_link(execution_id: str) -> str:
    cfg = load_config()
    base = (cfg.app_base_url or "").rstrip("/")
    return f"{base}/insights/{execution_id}" if base else f"/insights/{execution_id}"


def on_completeness_failure(session: Any, kind: str) -> None:
    """
    Fires the required- or optional-missing-data email for one ingestion
    session. `kind` is "required" or "optional" — the two fire
    independently, matching the spec (an optional-only gap does not need a
    required email, and vice versa).

    Idempotency for this trigger is the caller's job (session.py checks
    required_notified_at/optional_notified_at before calling this), since
    those flags already live on the session and are the natural place for
    it — this function does not re-check them.
    """
    storage = _storage()
    contacts = SourceContactStore(storage)
    contact = contacts.get(session.client_id) or contacts.get(session.run_id)
    contact_name = contact.label if contact else "there"
    to_email = contact.email if contact else None
    if not to_email:
        logger.warning(
            "on_completeness_failure: no source contact registered for "
            "client_id=%s run_id=%s — nothing to send to", session.client_id, session.run_id)
        return

    source_name = session.client_id
    resume_link = _resume_link(session.run_id)

    if kind == "required":
        missing = (session.report or {}).get("missing_required") or []
        if not missing:
            return
        content = email_builder.build_required_missing_email(
            source_name=source_name, upload_date=session.created_at[:10],
            file_name=session.source.rsplit("/", 1)[-1], contact_name=contact_name,
            missing_required=missing, resume_link=resume_link,
            ingestion_session_id=session.run_id,
        )
        trigger_type = "required_data"
    elif kind == "optional":
        missing = (session.report or {}).get("missing_optional") or []
        if not missing:
            return
        content = email_builder.build_optional_missing_email(
            source_name=source_name, contact_name=contact_name,
            missing_optional=missing, resume_link=resume_link,
            ingestion_session_id=session.run_id,
        )
        trigger_type = "optional_data"
    else:
        raise ValueError(f"unknown completeness kind: {kind!r}")

    result = get_sender().send(to=[to_email], subject=content.subject,
                               body=content.body, reply_to=content.reply_to)
    DispatchLogStore(storage).record(DispatchRecord(
        trigger_type=trigger_type, reference_id=session.run_id,
        recipients=[to_email], subject=content.subject,
        result="stubbed" if result.stubbed else ("failed" if result.failed else "sent"),
    ))


def on_data_request_raised(request: Any, *, source_id: str = "") -> str:
    """
    Notify the source that the orchestrator has raised a data request.

    The orchestrator has already decided and RECORDED the request
    (orchestrator/data_requests.py); this only tells the owner of the data
    that one exists, and reports back what happened so the record can say so.
    A dispatcher, never a second decision-maker — the same division as
    `on_recommendation_card_created`.

    Returns one of "notified" | "no_contact", and never raises: a failed
    notification must not lose the request that has already been recorded.
    """
    storage = _storage()
    contacts = SourceContactStore(storage)
    contact = contacts.get(source_id or request.subject_id)
    if contact is None or not contact.email:
        logger.warning(
            "on_data_request_raised: no source contact for subject_id=%s — "
            "the request stands, but there is nobody to ask",
            request.subject_id)
        return "no_contact"

    resume_link = _resume_link(request.subject_id)
    missing = list(request.fields)
    if request.tier == "required":
        content = email_builder.build_required_missing_email(
            source_name=request.subject_id,
            upload_date=request.requested_at[:10],
            file_name="your uploaded data",
            contact_name=contact.label or "there",
            missing_required=missing, resume_link=resume_link,
            ingestion_session_id=request.subject_id,
        )
        trigger_type = "required_data"
    else:
        content = email_builder.build_optional_missing_email(
            source_name=request.subject_id, contact_name=contact.label or "there",
            missing_optional=missing, resume_link=resume_link,
            ingestion_session_id=request.subject_id,
        )
        trigger_type = "optional_data"

    result = get_sender().send(to=[contact.email], subject=content.subject,
                               body=content.body, reply_to=content.reply_to)
    DispatchLogStore(storage).record(DispatchRecord(
        trigger_type=trigger_type, reference_id=request.request_id,
        recipients=[contact.email], subject=content.subject,
        result="stubbed" if result.stubbed else ("failed" if result.failed else "sent"),
    ))
    return "notified"


def _briefing_text(context: Any) -> tuple:
    """
    Pull already-grounded headline/narrative text off an ExecutionContext.

    headline/narrative live on ExecutiveBriefing.kpi_insights[i]
    (KPIInsight), not on ExecutiveBriefing itself — it only carries
    opening/context/recommendation at the top level. Falls back through
    each in turn so a briefing with no KPI insights (or no briefing at all,
    just the legacy ReasoningResult.summary) still produces sensible text.
    """
    reasoning = getattr(context, "reasoning", None)
    briefing = getattr(reasoning, "briefing", None) if reasoning else None
    if briefing is not None:
        if briefing.kpi_insights:
            insight = briefing.kpi_insights[0]
            return insight.headline, insight.narrative
        headline = briefing.opening or briefing.recommendation or "NetGravity recommendation"
        narrative = briefing.context or briefing.recommendation or ""
        return headline, narrative
    if reasoning is not None:
        return reasoning.summary or "NetGravity recommendation", reasoning.recommendation or reasoning.summary
    return "NetGravity recommendation", ""


def on_recommendation_card_created(approval: Any, context: Any) -> None:
    """
    Fires when governance classifies an action APPROVAL_REQUIRED and the
    orchestrator has just created the resulting ApprovalRequest
    (orchestrator/core/orchestrator.py _govern()). `approval.approval_id` is
    the stable id — no new persisted "card" is created for this; the
    ApprovalRequest the orchestrator already stores IS the card.
    """
    storage = _storage()
    dispatch_log = DispatchLogStore(storage)
    if dispatch_log.already_dispatched("recommendation", approval.approval_id):
        return

    headline, narrative = _briefing_text(context)
    deep_link = _deep_link(getattr(context, "execution_id", approval.execution_id))
    content = email_builder.build_recommendation_email(
        headline=headline, narrative=narrative, deep_link=deep_link)

    recipients = NotificationRecipientStore(storage).emails()
    if not recipients:
        logger.warning("on_recommendation_card_created: no recipients configured")
        return

    pdf_bytes = report_builder.build_recommendation_pdf(headline=headline, narrative=narrative)
    attachment_path = storage.save(
        "standardized", f"action_agent/reports/{approval.approval_id}.pdf", pdf_bytes)

    result = get_sender().send(to=recipients, subject=content.subject,
                               body=content.body, attachment_path=attachment_path)
    dispatch_log.record(DispatchRecord(
        trigger_type="recommendation", reference_id=approval.approval_id,
        recipients=recipients, subject=content.subject,
        result="stubbed" if result.stubbed else ("failed" if result.failed else "sent"),
    ))


def on_investigate_card_created(execution_id: str, decision: Any, context: Any) -> None:
    """
    Fires when governance classifies an action HUMAN_ONLY. There is no
    ApprovalRequest for this classification (a human must decide AND act —
    there is nothing to approve), so `execution_id` itself — already a
    stable id retrievable via GET /orchestrator/executions/<execution_id> —
    is the dedup key and the card reference.
    """
    storage = _storage()
    dispatch_log = DispatchLogStore(storage)
    if dispatch_log.already_dispatched("investigate", execution_id):
        return

    headline, narrative = _briefing_text(context)
    deep_link = _deep_link(execution_id)
    content = email_builder.build_investigate_email(
        headline=headline, narrative=narrative, deep_link=deep_link)

    recipients = NotificationRecipientStore(storage).emails()
    if not recipients:
        logger.warning("on_investigate_card_created: no recipients configured")
        return

    pdf_bytes = report_builder.build_investigate_pdf(
        headline=headline, narrative=narrative, reason=getattr(decision, "reason", ""))
    attachment_path = storage.save(
        "standardized", f"action_agent/reports/{execution_id}.pdf", pdf_bytes)

    result = get_sender().send(to=recipients, subject=content.subject,
                               body=content.body, attachment_path=attachment_path)
    dispatch_log.record(DispatchRecord(
        trigger_type="investigate", reference_id=execution_id,
        recipients=recipients, subject=content.subject,
        result="stubbed" if result.stubbed else ("failed" if result.failed else "sent"),
    ))
