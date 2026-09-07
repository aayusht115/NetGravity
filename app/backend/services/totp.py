"""
NetGravity — Time-based one-time passwords (RFC 6238)
=====================================================
Self-contained: `hmac`, `hashlib`, `base64`, `struct`. No dependency, no
service, no network call — which is what makes a second factor available on a
deployment that has neither an SMS provider nor an identity provider.

Compatible with Google Authenticator, 1Password, Authy and anything else that
implements RFC 6238 with the default parameters (SHA-1, 6 digits, 30 seconds).
SHA-1 is specified here not as a security choice but as an interoperability one:
authenticator apps overwhelmingly implement only SHA-1, and its use inside HMAC
is not affected by the collision attacks that retired it for signatures.

What this module does NOT decide
--------------------------------
Replay. A six-digit code is valid for a whole time step, so a code observed in
transit can be used again within it. This module reports WHICH step a code
verified against; refusing a step that has already been spent is the caller's
job, and `persistence.claim_mfa_step` makes that claim atomic. A TOTP
implementation that returns only True/False cannot express the thing that has
to be enforced.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional, Tuple
from urllib.parse import quote

#: RFC 6238 defaults. Every mainstream authenticator assumes these, and an app
#: that cannot be enrolled with a plain otpauth:// URI is not a usable factor.
DIGITS = 6
PERIOD_SECONDS = 30

#: How many steps either side of "now" are accepted. One step covers ordinary
#: clock drift between a phone and a server; more than one widens the window a
#: captured code stays usable in for no real gain.
DRIFT_STEPS = 1

_SECRET_BYTES = 20  # 160 bits, the RFC's recommendation for HMAC-SHA1


def generate_secret() -> str:
    """A fresh base32 secret, in the form an authenticator app expects."""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def _normalise(secret: str) -> bytes:
    padded = secret.strip().replace(" ", "").upper()
    padded += "=" * (-len(padded) % 8)
    return base64.b32decode(padded, casefold=True)


def code_for_step(secret: str, step: int) -> str:
    """The six-digit code for one time step. The whole of RFC 4226, essentially."""
    digest = hmac.new(_normalise(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10 ** DIGITS)).zfill(DIGITS)


def current_step(now: Optional[float] = None) -> int:
    return int((now if now is not None else time.time()) // PERIOD_SECONDS)


def verify(secret: str, code: str, *, now: Optional[float] = None,
           drift: int = DRIFT_STEPS) -> Tuple[bool, Optional[int]]:
    """
    Check a submitted code.

    Returns `(ok, step)` — the step it matched, so the caller can refuse a
    replay of the same code within its window.

    Comparison is constant-time. A digit-by-digit comparison of a six-digit
    code is a small leak, but it is a free one to close.
    """
    cleaned = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(cleaned) != DIGITS:
        return False, None

    step_now = current_step(now)
    for delta in range(-drift, drift + 1):
        step = step_now + delta
        if step < 0:
            continue
        if hmac.compare_digest(code_for_step(secret, step), cleaned):
            return True, step
    return False, None


def provisioning_uri(secret: str, *, account: str, issuer: str = "NetGravity") -> str:
    """
    The `otpauth://` URI an authenticator app scans.

    Returned once, at enrolment, and never again — the secret is not readable
    back out of the application afterwards.
    """
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}"
        f"?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD_SECONDS}"
    )


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------
#
# A second factor with no recovery path is a way to lose an account. These are
# shown once, at enrolment, and only their HASHES are stored — so a database
# read does not hand over the ability to bypass the factor it protects.

RECOVERY_CODE_COUNT = 10


def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list:
    """Human-transcribable one-time codes, in `xxxx-xxxx` form."""
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(4)
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes


def hash_recovery_code(code: str) -> str:
    """
    A plain SHA-256 of the normalised code.

    Deliberately not a slow KDF: a recovery code is 32 bits of true randomness
    from `secrets`, not a human-chosen password, so there is no dictionary to
    stretch against — and a slow hash here would only add latency to the
    linear scan over a user's ten codes.
    """
    normalised = (code or "").strip().lower().replace(" ", "").replace("-", "")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()
