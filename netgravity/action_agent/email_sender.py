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
from typing import List, Optional

from netgravity.action_agent.config import ActionAgentConfig, load_config

logger = logging.getLogger(__name__)

#: Sentinel embedded in .notes when a live send failed and we degraded to a
#: stub result. Mirrors ai/client.py's LLM_FAILURE_MARKER.
EMAIL_FAILURE_MARKER = "EMAIL SEND FAILED"


class EmailSendError(RuntimeError):
    """A live send failed and strict mode forbids degrading to a stub result."""


@dataclass
class EmailSendResult:
    sent: bool
    stubbed: bool
    notes: str = ""
    failed: bool = False
    recipients: List[str] = field(default_factory=list)


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
            self._send_live(to=to, subject=subject, body=body,
                            reply_to=reply_to, attachment_path=attachment_path)
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
                   reply_to: Optional[str], attachment_path: Optional[str]) -> None:
        """
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

        with smtplib.SMTP(self.config.smtp_host, self.config.smtp_port, timeout=30) as smtp:
            if self.config.smtp_use_tls:
                smtp.starttls()
            if self.config.smtp_username:
                smtp.login(self.config.smtp_username, self.config.smtp_password or "")
            smtp.send_message(msg, from_addr=from_address, to_addrs=to)


def get_sender(config: Optional[ActionAgentConfig] = None) -> EmailSender:
    return EmailSender(config)
