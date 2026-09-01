"""
NetGravity — OpenID Connect single sign-on
==========================================
Authorization Code flow with PKCE, against a configured OIDC provider.

Why
---
The account store here is complete — password policy, lockout, hash-only
resets, TOTP, session management — and it is still the wrong place for an
organisation's identity to live. Joiners and leavers are managed in a directory,
not in a planning tool, and an account that survives someone's departure because
nobody thought to remove it from a supply-chain application is the ordinary way
access outlives employment.

So this lets the directory be the source of truth, and it does NOT replace the
local account system: both work, per deployment, and a local account is still
how the platform is used with no provider configured.

Configuration
-------------
    NETGRAVITY_OIDC_ISSUER          https://login.example.com   (required)
    NETGRAVITY_OIDC_CLIENT_ID       ...                          (required)
    NETGRAVITY_OIDC_CLIENT_SECRET   ...  (omit for a public client with PKCE)
    NETGRAVITY_OIDC_REDIRECT_URI    https://app.example.com/api/auth/oidc/callback
    NETGRAVITY_OIDC_SCOPES          openid email profile         (default)
    NETGRAVITY_OIDC_PROVIDER_NAME   Company SSO                  (for the button)
    NETGRAVITY_OIDC_ALLOWED_DOMAINS example.com,example.org      (optional)
    NETGRAVITY_OIDC_AUTO_PROVISION  1 to create an account on first sign-in

Unset issuer or client id means SSO is simply off, and `/api/auth/oidc/providers`
says so rather than offering a button that cannot work.

Decisions worth stating
-----------------------
**Linking is on (issuer, subject), never on e-mail alone.** An e-mail address is
an assertion a provider makes; the subject is the identity it is making the
assertion about. Matching on e-mail means any provider that will assert
`ceo@client.com` takes over that account. E-mail is used only to find an
EXISTING local account to link on a first sign-in, and only when the address is
verified by the provider and inside `ALLOWED_DOMAINS` if one is set.

**PKCE always, even with a client secret.** It costs nothing and it removes the
authorization-code interception class entirely.

**State and nonce are single-use and stored server-side.** A `state` in a cookie
the client also controls is not a CSRF defence. They live in the database, are
consumed on callback, and expire.

**Auto-provisioning is off by default.** A configured provider that will
authenticate anyone in the world would otherwise let anyone in the world create
a workspace. Turn it on when the provider's audience IS the intended audience.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.backend.services.jwt_verify import TokenError, verify_id_token

logger = logging.getLogger(__name__)

#: How long an authorization request stays redeemable.
_STATE_TTL_SECONDS = 600

#: How long a fetched discovery document and JWKS are reused.
_DISCOVERY_TTL_SECONDS = 3600
_JWKS_TTL_SECONDS = 3600

#: Network timeout for every provider call. A provider that is slow must not
#: hold a request open indefinitely.
_HTTP_TIMEOUT_SECONDS = 10.0


class OIDCError(Exception):
    """A sign-in that cannot proceed. The message is safe to show a user."""


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str
    provider_name: str
    allowed_domains: Tuple[str, ...]
    auto_provision: bool

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.client_id and self.redirect_uri)


def load_config() -> OIDCConfig:
    """Read the provider configuration from the environment."""
    domains = tuple(
        d.strip().lower()
        for d in (os.environ.get("NETGRAVITY_OIDC_ALLOWED_DOMAINS") or "").split(",")
        if d.strip()
    )
    return OIDCConfig(
        issuer=(os.environ.get("NETGRAVITY_OIDC_ISSUER") or "").strip().rstrip("/"),
        client_id=(os.environ.get("NETGRAVITY_OIDC_CLIENT_ID") or "").strip(),
        client_secret=os.environ.get("NETGRAVITY_OIDC_CLIENT_SECRET") or "",
        redirect_uri=(os.environ.get("NETGRAVITY_OIDC_REDIRECT_URI") or "").strip(),
        scopes=(os.environ.get("NETGRAVITY_OIDC_SCOPES")
                or "openid email profile").strip(),
        provider_name=(os.environ.get("NETGRAVITY_OIDC_PROVIDER_NAME")
                       or "Single sign-on").strip(),
        allowed_domains=domains,
        auto_provision=os.environ.get("NETGRAVITY_OIDC_AUTO_PROVISION") == "1",
    )


# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------

class _Cache:
    """A tiny TTL cache for the discovery document and the JWKS."""

    def __init__(self) -> None:
        self._values: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        entry = self._values.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() >= expires_at:
            self._values.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any, ttl: float) -> None:
        self._values[key] = (time.time() + ttl, value)

    def clear(self) -> None:
        self._values.clear()


_cache = _Cache()


def _fetch_json(url: str, *, what: str) -> Dict[str, Any]:
    """
    GET a JSON document from the provider.

    HTTPS is required unless the host is loopback. A discovery document fetched
    over HTTP can be rewritten in flight, and rewriting it means substituting
    the JWKS — which is the whole trust anchor.
    """
    parsed = urllib.parse.urlparse(url)
    is_local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not is_local:
        raise OIDCError(
            f"refusing to fetch {what} over {parsed.scheme}: the provider's "
            f"metadata is the trust anchor and must be fetched over HTTPS."
        )
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise OIDCError(f"{what} returned HTTP {response.status}")
            body = response.read(2_000_000)
    except OIDCError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OIDCError(f"could not fetch {what}: {type(exc).__name__}") from exc
    try:
        document = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OIDCError(f"{what} is not JSON") from exc
    if not isinstance(document, dict):
        raise OIDCError(f"{what} is not a JSON object")
    return document


def discovery(config: OIDCConfig) -> Dict[str, Any]:
    """The provider's `openid-configuration`, cached."""
    cached = _cache.get(f"discovery:{config.issuer}")
    if cached is not None:
        return cached
    url = f"{config.issuer}/.well-known/openid-configuration"
    document = _fetch_json(url, what="the OIDC discovery document")

    # The issuer in the document must match the one we asked. Otherwise a
    # misconfigured or hostile endpoint can name any issuer it likes and the
    # `iss` check on the token becomes a check against the attacker's value.
    stated = str(document.get("issuer") or "").rstrip("/")
    if stated != config.issuer:
        raise OIDCError(
            f"the discovery document declares issuer {stated!r} but was fetched "
            f"from {config.issuer!r}; refusing to continue."
        )
    for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not document.get(required):
            raise OIDCError(f"the discovery document has no {required}")

    _cache.put(f"discovery:{config.issuer}", document, _DISCOVERY_TTL_SECONDS)
    return document


def jwks(config: OIDCConfig, *, force: bool = False) -> Dict[str, Any]:
    """
    The provider's signing keys, cached.

    `force` refetches, for the one legitimate case: a token signed with a key
    that rotated in since the cache was filled. Rate-limited by the cache's own
    TTL on the way back in, so a stream of bad `kid`s cannot be used to hammer
    the provider.
    """
    key = f"jwks:{config.issuer}"
    if not force:
        cached = _cache.get(key)
        if cached is not None:
            return cached
    document = _fetch_json(discovery(config)["jwks_uri"], what="the provider's JWKS")
    _cache.put(key, document, _JWKS_TTL_SECONDS)
    return document


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def begin_authorization(config: OIDCConfig, *,
                        next_path: str = "/") -> Tuple[str, Dict[str, Any]]:
    """
    Build the authorization URL, and the request state to store server-side.

    Returns `(url, pending)`. The caller persists `pending` and redirects the
    browser to `url`.
    """
    if not config.enabled:
        raise OIDCError("Single sign-on is not configured for this deployment.")

    document = discovery(config)
    state = _b64url(secrets.token_bytes(32))
    nonce = _b64url(secrets.token_bytes(32))
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())

    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": config.redirect_uri,
        "scope": config.scopes,
        "state": state,
        "nonce": nonce,
        # PKCE with S256, always. `plain` is accepted by some providers and is
        # not a defence — the verifier travels in the clear.
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = (str(document["authorization_endpoint"])
           + ("&" if "?" in str(document["authorization_endpoint"]) else "?")
           + urllib.parse.urlencode(params))

    pending = {
        "state": state,
        "nonce": nonce,
        "code_verifier": verifier,
        # Only a local path is ever redirected to after sign-in. An
        # attacker-supplied absolute URL here is an open redirect that borrows
        # this application's domain to look trustworthy.
        "next": next_path if next_path.startswith("/") and not next_path.startswith("//")
                else "/",
        "created_at": time.time(),
        "expires_at": time.time() + _STATE_TTL_SECONDS,
    }
    return url, pending


def exchange_code(config: OIDCConfig, *, code: str, code_verifier: str) -> Dict[str, Any]:
    """Redeem an authorization code at the token endpoint."""
    document = discovery(config)
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "client_id": config.client_id,
        "code_verifier": code_verifier,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    if config.client_secret:
        # Basic auth is the form every provider supports; sending the secret in
        # the body as well is common and unnecessary.
        credentials = f"{urllib.parse.quote(config.client_id)}:" \
                      f"{urllib.parse.quote(config.client_secret)}"
        headers["Authorization"] = "Basic " + base64.b64encode(
            credentials.encode("utf-8")).decode("ascii")

    endpoint = str(document["token_endpoint"])
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise OIDCError("refusing to send an authorization code over plain HTTP")

    request = urllib.request.Request(
        endpoint, data=urllib.parse.urlencode(payload).encode("ascii"),
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            body = response.read(2_000_000)
    except Exception as exc:  # noqa: BLE001
        # Never echo the provider's body: it can contain the code or the secret.
        raise OIDCError(
            f"the identity provider refused the authorization code "
            f"({type(exc).__name__})."
        ) from exc
    try:
        tokens = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OIDCError("the token endpoint did not return JSON") from exc
    if not isinstance(tokens, dict) or not tokens.get("id_token"):
        raise OIDCError("the token response carried no id_token")
    return tokens


def verified_claims(config: OIDCConfig, *, id_token: str,
                    nonce: str) -> Dict[str, Any]:
    """
    Verify the ID token, refetching the JWKS once if the key is unknown.

    One retry, because a provider that has rotated its signing key legitimately
    presents a `kid` the cache has never seen. More than one would make an
    invalid token a way to generate provider traffic.
    """
    try:
        return verify_id_token(
            id_token, jwks=jwks(config), issuer=config.issuer,
            audience=config.client_id, nonce=nonce)
    except TokenError as first:
        if "no key in the issuer's JWKS matches" not in str(first):
            raise OIDCError(f"the identity token could not be verified: {first}") from first
        logger.info("oidc.jwks.refetch reason=unknown_kid")
        try:
            return verify_id_token(
                id_token, jwks=jwks(config, force=True), issuer=config.issuer,
                audience=config.client_id, nonce=nonce)
        except TokenError as second:
            raise OIDCError(
                f"the identity token could not be verified: {second}") from second


def email_from_claims(config: OIDCConfig, claims: Dict[str, Any]) -> Tuple[str, List[str]]:
    """
    A usable e-mail address from the claims, and any reasons it is not usable.

    Returns `(email, problems)`. `email` is empty whenever `problems` is not.

    An UNVERIFIED address is refused. A provider that lets a user type any
    address into their profile and asserts it unverified is a provider that lets
    a user claim someone else's identity — and this address is what an existing
    local account is linked on.
    """
    problems: List[str] = []
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        return "", ["the provider asserted no e-mail address"]

    verified = claims.get("email_verified")
    if verified is False:
        problems.append(
            "the provider states this e-mail address is NOT verified, so it "
            "cannot be used to identify an account")
    if config.allowed_domains:
        domain = email.rsplit("@", 1)[-1]
        if domain not in config.allowed_domains:
            problems.append(
                f"{domain} is not in this deployment's allowed sign-in domains")
    return ("" if problems else email), problems


__all__ = [
    "OIDCConfig",
    "OIDCError",
    "begin_authorization",
    "discovery",
    "email_from_claims",
    "exchange_code",
    "jwks",
    "load_config",
    "verified_claims",
]
