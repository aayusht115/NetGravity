"""
NetGravity — Action Agent Email Templates
============================================
Pure string formatting. No model call, ever, in this file.

The two missing-data templates are fully deterministic: every number and
column name they contain comes straight from completeness.py's rule-based
check, never from a language model — matching the "logic calculates, AI
narrates" rule exactly where it applies (it never applies here).

The recommendation/investigate templates are also pure formatting, but their
INPUT is different: they take the Reasoning Agent's already-grounded
narrative text (headline/summary produced elsewhere, from real KPI results)
and drop it into a fixed envelope. This file never composes a sentence of
its own about what happened in the network — it only ever repeats what the
orchestrator already said.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from netgravity.action_agent.config import ActionAgentConfig, load_config


@dataclass
class EmailContent:
    subject: str
    body: str
    reply_to: Optional[str] = None


#: How much of a headline fits in a subject line.
#:
#: Measured against the live gateway: the Reasoning Agent's `headline` for a
#: facility-closure question came back as its whole first paragraph — 305
#: characters, opening "I find that closing DC_EAST increases total business
#: network cost by ..." — and the builder put all of it after
#: "[NetGravity] Please investigate:". Mail clients show roughly the first
#: 70 characters of a subject in a list view and the rest is a wall the
#: reader scrolls past, so the sentence that made the email worth opening
#: was invisible.
#:
#: The full text is not lost: it is the first line of the body, where it has
#: room. This only decides what fits on the envelope.
SUBJECT_LIMIT = 72


def _subject_line(headline: str, limit: int = SUBJECT_LIMIT) -> str:
    """
    One line of a headline, cut at a word boundary.

    Never mid-word, and never a bare truncation with no marker — an
    unmarked cut reads as a sentence the system failed to finish.
    """
    text = " ".join(str(headline or "").split())
    if not text:
        return "NetGravity update"
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return f"{clipped or text[:limit]}…"


def _reply_to_for_session(session_id: str, config: Optional[ActionAgentConfig] = None) -> Optional[str]:
    cfg = config or load_config()
    if not cfg.inbound_email_domain:
        # Blank is safe: the inbound-email domain/DNS setup is a separate
        # infrastructure prerequisite, not part of this codebase. Without
        # it, replies simply aren't parseable — the resume link still works.
        return None
    return f"ingest-{session_id}@{cfg.inbound_email_domain}"


def build_required_missing_email(
    *, source_name: str, upload_date: str, file_name: str, contact_name: str,
    missing_required: List[Dict[str, Any]], resume_link: str,
    ingestion_session_id: str, internal_contact_name: str = "the NetGravity team",
    config: Optional[ActionAgentConfig] = None,
) -> EmailContent:
    lines = [
        f"  - {m.get('entity_type', '')}: {m.get('entity_name', '')} — "
        f"{m.get('display_label', '')} not provided"
        for m in missing_required
    ]
    body = (
        f"Hi {contact_name},\n\n"
        f"Thanks for sending {file_name} on {upload_date}. Before this can be used in the\n"
        f"network analysis, a few required fields are missing:\n\n"
        + "\n".join(lines) + "\n\n"
        f"Until these are provided, this dataset cannot be used to run the analysis.\n\n"
        f"To fix this, reply to this email with a corrected file, or upload it here:\n"
        f"{resume_link}\n\n"
        f"Reference: {ingestion_session_id}\n\n"
        f"Questions? Contact {internal_contact_name} directly.\n\n"
        f"— NetGravity (automated message)"
    )
    subject = f"[NetGravity] Data needed: {source_name} upload from {upload_date}"
    return EmailContent(subject=subject, body=body,
                        reply_to=_reply_to_for_session(ingestion_session_id, config))


def build_optional_missing_email(
    *, source_name: str, contact_name: str, missing_optional: List[Dict[str, Any]],
    resume_link: str, ingestion_session_id: str,
    config: Optional[ActionAgentConfig] = None,
) -> EmailContent:
    lines = [
        f"  - {m.get('display_label', '')} — {m.get('what_it_unlocks', '')}"
        for m in missing_optional
    ]
    body = (
        f"Hi {contact_name},\n\n"
        f"We've gone ahead and run the analysis using the data you sent. A few optional fields\n"
        f"weren't included — providing them would let us go further:\n\n"
        + "\n".join(lines) + "\n\n"
        f"No action needed if this isn't available. If you'd like to add it, reply to this email\n"
        f"with the file, or upload it here: {resume_link}\n\n"
        f"— NetGravity (automated message)"
    )
    subject = f"[NetGravity] Optional: richer results available for {source_name}"
    return EmailContent(subject=subject, body=body,
                        reply_to=_reply_to_for_session(ingestion_session_id, config))


def build_recommendation_email(*, headline: str, narrative: str, deep_link: str) -> EmailContent:
    """
    `headline`/`narrative` are the Reasoning Agent's own already-grounded
    text (ExecutiveBriefing.headline / .narrative) — reused verbatim, never
    generated here.
    """
    body = (
        f"{headline}\n\n"
        f"{narrative}\n\n"
        f"Review and decide: {deep_link}\n\n"
        f"A full report is attached.\n\n"
        f"— NetGravity (automated message)"
    )
    subject = f"[NetGravity] Recommendation: {_subject_line(headline)}"
    return EmailContent(subject=subject, body=body)


def build_investigate_email(*, headline: str, narrative: str, deep_link: str) -> EmailContent:
    body = (
        f"{headline}\n\n"
        f"{narrative}\n\n"
        f"This requires a human decision — NetGravity cannot act on it automatically.\n\n"
        f"Review: {deep_link}\n\n"
        f"A full report is attached.\n\n"
        f"— NetGravity (automated message)"
    )
    subject = f"[NetGravity] Please investigate: {_subject_line(headline)}"
    return EmailContent(subject=subject, body=body)
