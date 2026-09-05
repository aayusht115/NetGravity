"""
NetGravity — Action Agent Configuration
=========================================
Single source of truth for every environment variable the Action Agent
needs. Mirrors netgravity/ingestion/config.py's shape deliberately: same
blank-means-stub-mode rule, same "real env vars win over .env" loading (the
ingestion config module already loads .env once at import time; importing it
here is enough to guarantee that has happened before these are read).

ENVIRONMENT VARIABLES
----------------------
    NETGRAVITY_SMTP_HOST / NETGRAVITY_EMAIL_API_KEY
        Outbound email credential. Neither set => STUB MODE: no email is
        actually sent, every send is logged and recorded in the dispatch
        log as "would have emailed ...". This is deliberately the default —
        no outbound credential exists yet, and none should be requested as
        part of this work without explicit sign-off.
    NETGRAVITY_SMTP_PORT / NETGRAVITY_SMTP_USERNAME / NETGRAVITY_SMTP_PASSWORD
        Only read once NETGRAVITY_SMTP_HOST is set (i.e. once live mode is
        actually requested). Port defaults to 587 (STARTTLS). Most real
        providers (Gmail included) require both a username and a password —
        for Gmail specifically that password must be an App Password, not
        the account password, since SMTP auth needs 2FA-compatible
        credentials.
    NETGRAVITY_SMTP_USE_TLS
        "false" to disable STARTTLS (e.g. against a local test relay that
        has no TLS at all). Defaults true — real providers require it.
    NETGRAVITY_SMTP_FROM_ADDRESS
        The From: header. Falls back to NETGRAVITY_SMTP_USERNAME, then to
        a placeholder — set this explicitly when it differs from the login
        account (e.g. a shared mailbox with per-app credentials).
    NETGRAVITY_EMAIL_STRICT
        "true" => a failed live send raises, instead of degrading to a
        labelled stub. Mirrors NETGRAVITY_LLM_STRICT.
    NETGRAVITY_INBOUND_EMAIL_DOMAIN
        Domain used to build the Reply-To address on missing-data emails
        (ingest-{session_id}@<domain>). Blank is safe — the address is just
        left unset — since the inbound-email provider/DNS setup is a
        separate infrastructure prerequisite, not part of this codebase.
    NETGRAVITY_DEFAULT_RECIPIENT_EMAIL / NETGRAVITY_DEFAULT_TEST_RECIPIENT_EMAIL
        Seed values only, used once to populate the notification-recipients
        store if it is empty. Fully editable afterward through the
        recipients store; never treated as permanent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Reuses the ingestion package's .env loader (it runs once at import time)
# and its env-reading helpers, rather than re-implementing dotenv parsing.
from netgravity.ingestion.config import _env, _flag  # noqa: F401 (loads .env as a side effect)


@dataclass
class ActionAgentConfig:
    smtp_host: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("NETGRAVITY_SMTP_PORT", "587") or 587))
    smtp_username: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_SMTP_USERNAME"))
    smtp_password: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_SMTP_PASSWORD"))
    smtp_use_tls: bool = field(default_factory=lambda: _flag("NETGRAVITY_SMTP_USE_TLS", True))
    smtp_from_address: Optional[str] = field(
        default_factory=lambda: _env("NETGRAVITY_SMTP_FROM_ADDRESS"))
    email_api_key: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_EMAIL_API_KEY"))
    email_strict: bool = field(default_factory=lambda: _flag("NETGRAVITY_EMAIL_STRICT"))
    inbound_email_domain: Optional[str] = field(
        default_factory=lambda: _env("NETGRAVITY_INBOUND_EMAIL_DOMAIN"))
    default_recipient_email: Optional[str] = field(
        default_factory=lambda: _env("NETGRAVITY_DEFAULT_RECIPIENT_EMAIL"))
    default_test_recipient_email: Optional[str] = field(
        default_factory=lambda: _env("NETGRAVITY_DEFAULT_TEST_RECIPIENT_EMAIL"))
    #: Base URL used to build resume/deep links in emails. Blank => a
    #: relative path is used instead, which is honest about the fact that
    #: the frontend has no router to receive it yet (see handoff notes).
    app_base_url: Optional[str] = field(default_factory=lambda: _env("NETGRAVITY_APP_BASE_URL"))

    @property
    def stub_mode(self) -> bool:
        """True when no outbound email credential is configured."""
        return not bool(self.smtp_host or self.email_api_key)


def load_config() -> ActionAgentConfig:
    return ActionAgentConfig()
