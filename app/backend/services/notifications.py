"""
NetGravity — Outbound delivery for account emails
==================================================
One thing is delivered today: a password-reset link.

Why this is a seam and not an SMTP call
----------------------------------------
Every deployment sends mail differently — SES, SendGrid, Postmark, a corporate
relay, or a queue that something else drains. Hardcoding one of them would make
the reset feature un-deployable everywhere except where that choice happened to
be right, and mocking it in tests would mean the code path shipped untested.

So delivery is a channel selected by configuration:

    NETGRAVITY_RESET_DELIVERY=log       (default) writes the link to the log
    NETGRAVITY_RESET_DELIVERY=webhook   POSTs it to NETGRAVITY_RESET_WEBHOOK
    NETGRAVITY_RESET_DELIVERY=smtp      sends it via NETGRAVITY_SMTP_*

`log` is the default because it is the only one that is honest on a machine
with no mail configured: the operator can complete a reset from the log, and
nobody is misled into thinking a mail was sent. It refuses to run in production
— a reset link written to a log file where support staff can read it is a
credential in a log file.

What is never logged
--------------------
Under `webhook` and `smtp` the token never reaches the log. Under `log` it
necessarily does, which is exactly why that channel is development-only.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL = "log"


def _reset_url(token: str, request: Any = None) -> str:
    """
    The link the user clicks.

    `NETGRAVITY_PUBLIC_URL` wins, because the request's own Host header is
    attacker-controllable and a reset link is exactly the thing not to build
    from it — a poisoned Host turns "reset your password" into "send me your
    reset token". The request origin is used only as a development fallback,
    and only when no public URL is configured.
    """
    base = (os.environ.get("NETGRAVITY_PUBLIC_URL") or "").strip().rstrip("/")
    if not base and request is not None:
        base = request.host_url.rstrip("/")
    return f"{base}/?reset_token={token}"


class PasswordResetDelivery:
    """Sends a reset link through the configured channel."""

    @property
    def channel(self) -> str:
        return (os.environ.get("NETGRAVITY_RESET_DELIVERY") or _DEFAULT_CHANNEL) \
            .strip().lower()

    @property
    def production(self) -> bool:
        return os.environ.get("NETGRAVITY_ENV", "development").strip().lower() \
            == "production"

    def describe(self) -> Dict[str, Any]:
        """For `/api/status`: how resets are delivered, and whether that works."""
        channel = self.channel
        configured = True
        reason = ""
        if channel == "webhook" and not os.environ.get("NETGRAVITY_RESET_WEBHOOK"):
            configured, reason = False, "NETGRAVITY_RESET_WEBHOOK is not set"
        elif channel == "smtp" and not os.environ.get("NETGRAVITY_SMTP_HOST"):
            configured, reason = False, "NETGRAVITY_SMTP_HOST is not set"
        elif channel == "log" and self.production:
            configured, reason = False, (
                "the 'log' channel writes reset tokens to the log and is "
                "refused in production")
        return {"channel": channel, "configured": configured, "reason": reason}

    # ------------------------------------------------------------------
    def send(self, *, user: Any, token: str, request: Any = None) -> bool:
        """
        Deliver the link. Returns whether it went out.

        Never raises: a delivery failure must not change the endpoint's
        response, because the response is deliberately identical for a known
        address, an unknown one and a rate-limited one.
        """
        url = _reset_url(token, request)
        channel = self.channel
        try:
            if channel == "webhook":
                return self._webhook(user, url)
            if channel == "smtp":
                return self._smtp(user, url)
            return self._log(user, url)
        except Exception as exc:  # noqa: BLE001
            logger.error("notifications.reset.delivery_failed channel=%s error=%s",
                         channel, exc)
            return False

    # ------------------------------------------------------------------
    def _log(self, user: Any, url: str) -> bool:
        if self.production:
            logger.error(
                "notifications.reset.refused reason=log_channel_in_production "
                "user_id=%s — set NETGRAVITY_RESET_DELIVERY to webhook or smtp",
                getattr(user, "user_id", "?"))
            return False
        logger.warning(
            "PASSWORD RESET (development delivery) for %s: %s",
            getattr(user, "email", "?"), url)
        return True

    def _webhook(self, user: Any, url: str) -> bool:
        endpoint = (os.environ.get("NETGRAVITY_RESET_WEBHOOK") or "").strip()
        if not endpoint:
            logger.error("notifications.reset.webhook_unconfigured")
            return False
        import requests

        headers = {"Content-Type": "application/json"}
        secret = (os.environ.get("NETGRAVITY_RESET_WEBHOOK_TOKEN") or "").strip()
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        response = requests.post(
            endpoint,
            data=json.dumps({
                "type": "password_reset",
                "email": getattr(user, "email", ""),
                "name": getattr(user, "name", ""),
                "reset_url": url,
                "expires_in_seconds": 1800,
            }),
            headers=headers, timeout=10,
        )
        ok = 200 <= response.status_code < 300
        # The URL carries the token, so only the outcome is logged.
        logger.info("notifications.reset.webhook status=%s ok=%s",
                    response.status_code, ok)
        return ok

    def _smtp(self, user: Any, url: str) -> bool:
        import smtplib
        from email.message import EmailMessage

        host = (os.environ.get("NETGRAVITY_SMTP_HOST") or "").strip()
        if not host:
            logger.error("notifications.reset.smtp_unconfigured")
            return False
        port = int(os.environ.get("NETGRAVITY_SMTP_PORT", "587"))
        sender = (os.environ.get("NETGRAVITY_SMTP_FROM")
                  or "no-reply@netgravity.local")

        message = EmailMessage()
        message["Subject"] = "Reset your NetGravity password"
        message["From"] = sender
        message["To"] = getattr(user, "email", "")
        message.set_content(
            f"Someone asked to reset the password for this NetGravity account.\n\n"
            f"{url}\n\n"
            f"The link is valid for 30 minutes and can be used once. If this "
            f"was not you, no action is needed — the link cannot be used "
            f"without opening it, and your current password still works."
        )

        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.ehlo()
            if os.environ.get("NETGRAVITY_SMTP_STARTTLS", "1") == "1":
                smtp.starttls()
                smtp.ehlo()
            username = os.environ.get("NETGRAVITY_SMTP_USER")
            password = os.environ.get("NETGRAVITY_SMTP_PASSWORD")
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
        logger.info("notifications.reset.smtp_sent host=%s", host)
        return True


password_reset_delivery = PasswordResetDelivery()
