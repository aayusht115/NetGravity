"""
Verify outbound email actually leaves the machine.

    PYTHONPATH=. python scripts/verify_email_delivery.py you@example.com

WHY THIS EXISTS. A stubbed send and a real one are indistinguishable from the
screen: both report success, and with no SMTP credential configured the sender
stubs by design. So "the email feature works" cannot be established from the
application — it has to be established from an inbox. This script says which
of the two happened, in words, and exits non-zero when nothing was delivered.

Safe to run without a credential: it reports the stub and sends nothing.

See docs/email_delivery_requirements.md for what to set.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from netgravity.action_agent.config import load_config          # noqa: E402
from netgravity.action_agent.email_sender import EmailSender    # noqa: E402

SUBJECT = "NetGravity — outbound email verification"
BODY = (
    "This is a verification message from NetGravity.\n\n"
    "If you are reading it in your inbox, the outbound email path is "
    "configured correctly: host, credentials, TLS and the From address all "
    "work, and the data-request emails the Action Agent composes will be "
    "delivered.\n\n"
    "Nothing in this message is a finding about any network."
)


def main(argv: list) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    recipient = argv[1].strip()
    if "@" not in recipient:
        print(f"'{recipient}' is not an email address.")
        return 2

    config = load_config()
    print("Configuration")
    print(f"  stub mode          {config.stub_mode}")
    print(f"  SMTP host          {config.smtp_host or '(unset)'}")
    print(f"  SMTP port          {config.smtp_port}")
    print(f"  SMTP username      {config.smtp_username or '(unset)'}")
    print(f"  From address       "
          f"{getattr(config, 'smtp_from_address', None) or '(unset)'}")
    print(f"  strict failures    {getattr(config, 'email_strict', False)}")
    print()

    result = EmailSender(config).send(
        to=[recipient], subject=SUBJECT, body=BODY)

    print("Result")
    print(f"  sent               {result.sent}")
    print(f"  stubbed            {result.stubbed}")
    print(f"  failed             {getattr(result, 'failed', False)}")
    print(f"  refused            {getattr(result, 'refused', []) or 'none'}")
    print(f"  notes              {result.notes}")
    print()

    if result.stubbed and not getattr(result, "failed", False):
        print("NOTHING WAS SENT.")
        print("  No SMTP credential is configured, so the sender took its stub")
        print("  branch — which is the correct default, not a fault. Set")
        print("  NETGRAVITY_SMTP_HOST, _USERNAME, _PASSWORD and")
        print("  _FROM_ADDRESS, then run this again.")
        print("  See docs/email_delivery_requirements.md.")
        return 1

    if getattr(result, "failed", False):
        print("THE SEND FAILED and was degraded to a stub.")
        print("  Nothing arrived. The reason is in `notes` above. Set")
        print("  NETGRAVITY_EMAIL_STRICT=true to make this raise in the")
        print("  application rather than report success.")
        return 1

    print(f"A live send was accepted by the relay for {recipient}.")
    print("  Confirm it in the inbox — a relay accepting a message is not the")
    print("  same as a mailbox delivering it, and spam filtering happens after")
    print("  this point.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
