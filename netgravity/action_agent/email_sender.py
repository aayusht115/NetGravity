"""
NetGravity — Email Sender (single integration point)
========================================================
EVERY outbound email in the Action Agent goes through this file, mirroring
the rule netgravity/ingestion/ai/client.py already establishes for LLM
calls: one integration point, so swapping providers later is a one-file
change and nobody can mistake a stub send for a real one.

STUB MODE
---------
No NETGRAVITY_SMTP_HOST / NETGRAVITY_EMAIL_API_KEY configured (the default —
no outbound email credential exists yet, and none is requested by this
work) => every send is logged as "would have emailed ..." and returns a
success-shaped, clearly-labelled stub result. The whole Action Agent, and
its test suite, runs end to end with no network calls and no credentials.

A live send that raises degrades to the same labelled-stub shape unless
NETGRAVITY_EMAIL_STRICT is set, in which case it raises — exactly the
NETGRAVITY_LLM_STRICT contract.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from netgravity.action_agent.config import ActionAgentConfig, load_config

logger = logging.getLogger(__name__)

#: Sentinel embedded in .notes when a live send failed and we degraded to a
#: stub result. Mirrors ai/client.py's LLM_FAILURE_MARKER.
EMAIL_FAILURE_MARKER = "EMAIL SEND FAILED"


class EmailSendError(RuntimeError):
    """A live send failed and strict mode forbids degrading to a stub result."""


@dataclass
class EmailSendResult:
    #: Success-SHAPED, deliberately: a stub returns True here so a caller that
    #: only wants to know "did this blow up" does not have to special-case the
    #: default configuration. It is NOT the delivery verdict — read `outcome`
    #: for that, never this flag.
    sent: bool
    stubbed: bool
    notes: str = ""
    failed: bool = False
    recipients: List[str] = field(default_factory=list)
    #: Addresses the mail server rejected by name. `SMTP.send_message` raises
    #: only when it rejects EVERY address; a partial rejection comes back as a
    #: return value and no exception, which is how a mistyped address in a
    #: list of four went unreported.
    refused: List[str] = field(default_factory=list)

    @property
    def delivered(self) -> List[str]:
        """The addresses that actually took the message."""
        if self.stubbed or self.failed:
            return [] if not self.refused else [
                r for r in self.recipients if r not in self.refused]
        return [r for r in self.recipients if r not in self.refused]

    @property
    def outcome(self) -> str:
        """
        One verdict, derived once.

        Every caller used to reconstruct this from the flags, and the dispatch
        endpoint got the precedence wrong: it tested `stubbed` before `failed`,
        and a degraded live failure sets both. A configured mail server that
        rejected the message was therefore reported as "no mail server is
        configured".

        "partial" is a real state and not a rounding of the other two: some
        people were asked and some were not, and re-sending to everyone would
        ask the first group twice.
        """
        if self.refused and self.delivered:
            return "partial"
        if self.failed or (self.refused and not self.delivered):
            return "failed"
        if self.stubbed:
            return "stubbed"
        return "sent"


class EmailSender:
    def __init__(self, config: Optional[ActionAgentConfig] = None):
        self.config = config or load_config()

    @property
    def stub_mode(self) -> bool:
        return self.config.stub_mode

    def send(self, *, to: List[str], subject: str, body: str,
             reply_to: Optional[str] = None,
             attachment_path: Optional[str] = None) -> EmailSendResult:
        if self.stub_mode:
            logger.info(
                "[EMAIL STUB] would have emailed %s: subject=%r reply_to=%r "
                "attachment=%r\n%s",
                ", ".join(to), subject, reply_to, attachment_path, body,
            )
            return EmailSendResult(
                sent=True, stubbed=True, recipients=list(to),
                notes="stubbed (no NETGRAVITY_SMTP_HOST / NETGRAVITY_EMAIL_API_KEY configured)",
            )

        try:
            refused = self._send_live(to=to, subject=subject, body=body,
                                      reply_to=reply_to,
                                      attachment_path=attachment_path) or {}
            if refused:
                # Some addresses were taken and some were not. Naming the ones
                # that were not is the whole point: "sent" on a list of four
                # with one mistyped address means one person is still waiting
                # to be asked and nobody knows it.
                detail = "; ".join(
                    f"{addr} ({code} {str(msg, 'utf-8', 'replace') if isinstance(msg, bytes) else msg})"
                    for addr, (code, msg) in refused.items())
                logger.warning("email partially refused: %s", detail)
                return EmailSendResult(
                    sent=True, stubbed=False, failed=False,
                    recipients=list(to), refused=sorted(refused),
                    notes=f"The mail server refused {len(refused)} of {len(to)} "
                          f"addresses: {detail}",
                )
            return EmailSendResult(sent=True, stubbed=False, recipients=list(to),
                                   notes="live send")
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("email send failed: %s", detail)
            if self.config.email_strict:
                raise EmailSendError(
                    f"live email send failed and NETGRAVITY_EMAIL_STRICT is set — "
                    f"refusing to substitute a stub result. {detail}"
                ) from exc
            return EmailSendResult(
                sent=False, stubbed=True, failed=True, recipients=list(to),
                notes=f"{EMAIL_FAILURE_MARKER} ({detail}) — degraded to stub. "
                      f"No email was actually sent. Set NETGRAVITY_EMAIL_STRICT=true "
                      f"to fail loudly instead.",
            )

    def _send_live(self, *, to: List[str], subject: str, body: str,
                   reply_to: Optional[str],
                   attachment_path: Optional[str]) -> Dict[str, Any]:
        """
        Returns the addresses the server REFUSED, `{addr: (code, message)}`,
        which is empty on a clean send.

        This used to return None and the refusals were dropped on the floor.
        `SMTP.send_message` raises `SMTPRecipientsRefused` only when it rejects
        every address; reject one of four and it returns them quietly, so the
        one person whose address was mistyped was never asked and the screen
        said everyone had been.

        The only provider-specific code in this file. Plain SMTP + STARTTLS,
        which is what Gmail, most Google Workspace/Microsoft 365 mailboxes,
        and generic SMTP relays all speak the same way — swapping to a
        dedicated provider SDK later (SendGrid, Postmark, ...) means
        rewriting this one method, nothing else in the package.
        """
        import smtplib
        from email.message import EmailMessage

        from_address = (self.config.smtp_from_address or self.config.smtp_username
                       or "netgravity@localhost")

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_address
        msg["To"] = ", ".join(to)
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)

        if attachment_path:
            with open(attachment_path, "rb") as fh:
                msg.add_attachment(fh.read(), maintype="application",
                                   subtype="pdf",
                                   filename=attachment_path.split("/")[-1])

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port,
                          timeout=self.config.smtp_timeout_seconds) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls()
            if self.config.smtp_username:
                smtp.login(self.config.smtp_username, self.config.smtp_password or "")
            return smtp.send_message(msg, from_addr=from_address, to_addrs=to) or {}


    def describe(self) -> Dict[str, Any]:
        """
        Whether mail can actually leave this deployment, and if not, why.

        For `/api/status`, in the shape `reset_delivery` already uses there.
        Both answer a question that is invisible until the day it matters: a
        deployment where nothing can be sent looks identical to a working one
        until somebody presses send.

        Reports the CHANNEL and the reason, never a credential.
        """
        import os

        production = (os.environ.get("NETGRAVITY_ENV", "development")
                      .strip().lower() == "production")
        if self.config.smtp_host:
            channel = "smtp"
        elif self.config.email_api_key:
            channel = "api"
        else:
            channel = "none"

        if channel == "none":
            return {
                "channel": "none",
                "configured": False,
                # Degraded rather than broken IN DEVELOPMENT: the request is
                # still recorded and the message still logged, which is what
                # this repository ships and is useful. In production it is a
                # real misconfiguration and says so.
                "severity": "error" if production else "info",
                "reason": ("No outbound mail server is configured, so requests "
                           "are recorded and logged but not delivered. Set "
                           "NETGRAVITY_SMTP_HOST (and the matching _PORT, "
                           "_USERNAME, _PASSWORD, _FROM_ADDRESS) to send."),
            }
        if channel == "smtp" and not self.config.smtp_username:
            # Not fatal — plenty of internal relays accept unauthenticated mail
            # from inside the network — so this is stated, not refused.
            return {"channel": "smtp", "configured": True, "severity": "info",
                    "reason": ("Sending unauthenticated: NETGRAVITY_SMTP_USERNAME "
                               "is not set. Correct for an internal relay, wrong "
                               "for every hosted provider.")}
        return {"channel": channel, "configured": True, "severity": "ok",
                "reason": ""}


def get_sender(config: Optional[ActionAgentConfig] = None) -> EmailSender:
    return EmailSender(config)
