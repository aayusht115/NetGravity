"""
NetGravity — Action Agent API
================================
    POST /api/inbound-email   webhook target for the inbound-email provider
                              (see netgravity/action_agent/inbound_email.py
                              for the provider choice and format).

Routes stay dumb; inbound_email.py and recipients.py own the actual logic —
same "thin blueprint over a service" shape as netgravity/ingestion/api.py.
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from netgravity.action_agent.dispatch_log import DispatchLogStore, DispatchRecord
from netgravity.action_agent.email_builder import EmailContent
from netgravity.action_agent.email_sender import get_sender
from netgravity.action_agent.inbound_email import ProcessedEmailStore, parse_sendgrid_payload
from netgravity.action_agent.recipients import NotificationRecipientStore, SourceContactStore
from netgravity.ingestion.config import IngestionConfig
from netgravity.ingestion.service import IngestionService, SessionNotFound
from netgravity.ingestion.storage import get_storage

logger = logging.getLogger(__name__)


def create_action_agent_blueprint(url_prefix: str = "/api") -> Blueprint:
    bp = Blueprint("action_agent", __name__, url_prefix=url_prefix)
    storage = get_storage(IngestionConfig())
    processed = ProcessedEmailStore(storage)
    dispatch_log = DispatchLogStore(storage)
    service = IngestionService()

    @bp.route("/inbound-email", methods=["POST"])
    def inbound_email():
        inbound = parse_sendgrid_payload(request.form, request.files)

        if processed.already_processed(inbound.message_id):
            return jsonify({"status": "already_processed",
                            "message_id": inbound.message_id}), 200

        if not inbound.session_id:
            processed.mark_processed(inbound.message_id, "no_session_id")
            return jsonify({"error": "could not determine ingestion session "
                                     "from reply-to address"}), 400

        try:
            session = service.get(inbound.session_id)
        except SessionNotFound:
            processed.mark_processed(inbound.message_id, "session_not_found")
            return jsonify({"error": f"session '{inbound.session_id}' not found"}), 404

        contacts = SourceContactStore(storage)
        verified = contacts.verify_sender(session.client_id, inbound.from_address) or \
            contacts.verify_sender(session.run_id, inbound.from_address)

        if not verified:
            # Email sender addresses can be spoofed; this is a basic guard,
            # not bulletproof. A mismatch is held for manual review, never
            # accepted as real data.
            recipients = NotificationRecipientStore(storage).emails()
            if recipients:
                content = EmailContent(
                    subject=f"[NetGravity] Unverified reply held for review — {session.run_id}",
                    body=(f"A reply to session {session.run_id} arrived from "
                          f"{inbound.from_address}, which does not match the "
                          f"registered contact for this source. It has NOT been "
                          f"applied. Review and re-send manually if it's legitimate."),
                )
                get_sender().send(to=recipients, subject=content.subject, body=content.body)
            dispatch_log.record(DispatchRecord(
                trigger_type="inbound_hold", reference_id=session.run_id,
                recipients=recipients if recipients else [],
                subject="unverified inbound reply held", result="sent",
            ))
            processed.mark_processed(inbound.message_id, "held_unverified_sender")
            return jsonify({"status": "held_for_review",
                            "reason": "sender does not match registered contact"}), 202

        if not inbound.attachments:
            processed.mark_processed(inbound.message_id, "no_attachment")
            return jsonify({"error": "reply carried no attachment"}), 400

        attachment = inbound.attachments[0]
        refreshed = service.resume_with_file(
            session.run_id, attachment.filename, attachment.content)
        processed.mark_processed(inbound.message_id, "applied")
        return jsonify({"status": "applied", "session": refreshed.as_dict(include_draft=False)}), 200

    return bp
