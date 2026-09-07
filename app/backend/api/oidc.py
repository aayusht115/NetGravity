"""
NetGravity — OpenID Connect sign-in endpoints
=============================================

    GET  /api/auth/oidc/providers   is SSO available, and what is it called?
    GET  /api/auth/oidc/start       begin sign-in (302 to the provider)
    GET  /api/auth/oidc/callback    the provider's redirect back
    GET  /api/auth/oidc/identities  the federated links on this account
    DELETE /api/auth/oidc/identities/<issuer>/<subject>   unlink one

Shape of the flow
-----------------
`/start` mints `state`, `nonce` and a PKCE verifier, stores them SERVER-SIDE
(`app_state`, not a cookie the client also controls), and redirects. `/callback`
looks the pending request up by `state`, consumes it, redeems the code with the
verifier, verifies the ID token, and issues an ordinary NetGravity session — the
same session a password login issues, through the same helper, so everything
downstream is unchanged.

Errors redirect back to the landing page with a short reason rather than
returning JSON. A browser arriving here has been sent by the provider and needs
to end up somewhere it can read; a JSON body would be shown as raw text.

Nothing in the reason parameter is provider output. A provider's error body can
carry the code or the client secret, and a message assembled from it would put
them in a URL, in a browser's history, and in an access log.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Any, Dict, Optional

from flask import Blueprint, g, jsonify, make_response, redirect, request

from app.backend.services.errors import (
    ApplicationError,
    NotFoundError,
    ValidationError,
)
from app.backend.services.oidc import (
    OIDCError,
    begin_authorization,
    email_from_claims,
    exchange_code,
    load_config,
    verified_claims,
)
from app.backend.services.ratelimit import rate_limit
from app.backend.services.security import (
    attach_session_cookies,
    auth_service,
    require_auth,
)

logger = logging.getLogger(__name__)

oidc_bp = Blueprint("oidc", __name__, url_prefix="/api/auth/oidc")

#: `app_state` key prefix for a pending authorization request.
_PENDING_PREFIX = "oidc_pending:"


def _client_description() -> str:
    agent = (request.headers.get("User-Agent") or "")[:120]
    return f"{request.remote_addr or 'unknown'} {agent}".strip()


def _store_pending(state: str, pending: Dict[str, Any]) -> None:
    from app.backend.services import persistence
    persistence.database.put_state(_PENDING_PREFIX + state, pending)


def _take_pending(state: str) -> Optional[Dict[str, Any]]:
    """
    Fetch and DELETE a pending request. Single-use by construction.

    Deleted before the code is redeemed, not after: a `state` that can be
    presented twice is a replayable callback, and the window between "checked"
    and "consumed" is where that replay lives.
    """
    from app.backend.services import persistence
    key = _PENDING_PREFIX + state
    pending = persistence.database.get_state(key)
    if pending is None:
        return None
    # Deleted outright rather than blanked: a row holding `null` is still a row
    # `get_state` would return, and "the value is None" and "there is no value"
    # must not be the same thing for a single-use token.
    persistence.database.execute("DELETE FROM app_state WHERE key = ?", (key,))
    return pending


def purge_expired_pending(now: Optional[float] = None) -> int:
    """
    Drop authorization requests nobody came back for.

    An abandoned sign-in leaves a row. On its own that is harmless — the row is
    useless after ten minutes — but a table that only ever grows is a table
    somebody eventually has to explain.
    """
    import json

    from app.backend.services import persistence
    cutoff = time.time() if now is None else now
    rows = persistence.database.query(
        "SELECT key, value FROM app_state WHERE key LIKE ?",
        (_PENDING_PREFIX + "%",))
    removed = 0
    for row in rows:
        try:
            pending = json.loads(row["value"])
            expires_at = float(pending.get("expires_at") or 0)
        except Exception:  # noqa: BLE001 — an unreadable row is a stale row
            expires_at = 0.0
        if cutoff > expires_at:
            persistence.database.execute(
                "DELETE FROM app_state WHERE key = ?", (row["key"],))
            removed += 1
    return removed


def _fail(reason: str, detail: str = "") -> Any:
    """
    Send the browser back to the landing page with a short reason.

    `reason` is one of a fixed set of tokens this file chooses. `detail` is
    logged, never put in the URL: it can contain provider output.
    """
    if detail:
        logger.warning("oidc.failed reason=%s detail=%s", reason, detail[:400])
    else:
        logger.warning("oidc.failed reason=%s", reason)
    query = urllib.parse.urlencode({"sso_error": reason})
    return redirect(f"/?{query}", code=302)


@oidc_bp.route("/providers", methods=["GET"])
def providers():
    """
    Whether single sign-on is available here, and what to call the button.

    Unauthenticated by design: a sign-in page has to know what sign-in methods
    exist before anyone has signed in. It discloses only the provider's display
    name and issuer, both of which every user sees the moment they click it.
    """
    config = load_config()
    if not config.enabled:
        return jsonify({
            "enabled": False,
            "reason": ("No identity provider is configured. Set "
                       "NETGRAVITY_OIDC_ISSUER, NETGRAVITY_OIDC_CLIENT_ID and "
                       "NETGRAVITY_OIDC_REDIRECT_URI to enable single sign-on."),
            "providers": [],
        }), 200
    return jsonify({
        "enabled": True,
        "providers": [{
            "name": config.provider_name,
            "issuer": config.issuer,
            "start_url": "/api/auth/oidc/start",
            "auto_provision": config.auto_provision,
            "allowed_domains": list(config.allowed_domains),
        }],
    }), 200


@oidc_bp.route("/start", methods=["GET"])
@rate_limit("auth.oidc_start", limit=30, window_seconds=300)
def start():
    """Begin an authorization code flow."""
    config = load_config()
    if not config.enabled:
        return _fail("sso_not_configured")

    next_path = request.args.get("next") or "/"
    try:
        url, pending = begin_authorization(config, next_path=next_path)
    except OIDCError as exc:
        return _fail("provider_unavailable", str(exc))

    _store_pending(pending["state"], pending)
    # Cheap, and only on the path that creates the rows.
    try:
        purge_expired_pending()
    except Exception as exc:  # noqa: BLE001 — housekeeping, never the request
        logger.warning("oidc.purge_failed error=%s", exc)
    logger.info("oidc.start issuer=%s", config.issuer)
    return redirect(url, code=302)


@oidc_bp.route("/callback", methods=["GET"])
@rate_limit("auth.oidc_callback", limit=30, window_seconds=300)
def callback():
    """The provider's redirect back, carrying `code` and `state`."""
    config = load_config()
    if not config.enabled:
        return _fail("sso_not_configured")

    # The provider may report a failure instead of a code. Its `error` is a
    # fixed OAuth token, so it is safe to name; `error_description` is free text
    # from the provider and is logged only.
    provider_error = request.args.get("error")
    if provider_error:
        return _fail("provider_declined",
                     f"{provider_error}: {request.args.get('error_description', '')}")

    state = request.args.get("state") or ""
    code = request.args.get("code") or ""
    if not state or not code:
        return _fail("malformed_callback", f"state={bool(state)} code={bool(code)}")

    pending = _take_pending(state)
    if pending is None:
        # Either a forged callback, a replayed one, or a genuine sign-in that
        # sat on the provider's page past the window. They are indistinguishable
        # here and must be treated as the worst of the three.
        return _fail("unknown_or_used_state")
    if time.time() > float(pending.get("expires_at") or 0):
        return _fail("expired_state")

    try:
        tokens = exchange_code(config, code=code,
                               code_verifier=str(pending["code_verifier"]))
        claims = verified_claims(config, id_token=str(tokens["id_token"]),
                                 nonce=str(pending["nonce"]))
    except OIDCError as exc:
        return _fail("token_verification_failed", str(exc))

    issuer = str(claims["iss"]).rstrip("/")
    subject = str(claims["sub"])

    from app.backend.services import persistence

    link = persistence.find_federated_identity(issuer, subject)
    user = auth_service.get_user(str(link["user_id"])) if link else None

    if user is None:
        email, problems = email_from_claims(config, claims)
        if problems:
            return _fail("email_not_usable", "; ".join(problems))

        existing = auth_service.find_by_email(email)
        if existing is not None:
            # First federated sign-in for an address that already has a local
            # account. Linked, not duplicated — and only because the provider
            # VERIFIED the address (see `email_from_claims`). Matching on an
            # unverified assertion would let any provider that will assert an
            # address take over the account under it.
            user = existing
            logger.info("oidc.linked_existing user_id=%s issuer=%s",
                        user.user_id, issuer)
        elif config.auto_provision:
            user = auth_service.register_federated(
                email=email,
                name=str(claims.get("name") or ""),
                organization=str(claims.get("organization")
                                 or "Client Workspace"),
            )
            logger.info("oidc.provisioned user_id=%s issuer=%s",
                        user.user_id, issuer)
        else:
            # A configured provider will often authenticate a whole directory.
            # Without provisioning turned on, being authenticated is not the
            # same as being invited.
            return _fail("no_account_and_provisioning_disabled", email)

    persistence.guarded(persistence.link_federated_identity)(
        issuer, subject, user.user_id,
        str(claims.get("email") or ""), time.time())

    token = auth_service.issue_session(user, client=_client_description())
    next_path = str(pending.get("next") or "/")
    response = make_response(redirect(next_path, code=302))
    attach_session_cookies(response, token)
    logger.info("oidc.signed_in user_id=%s issuer=%s", user.user_id, issuer)
    return response


@oidc_bp.route("/identities", methods=["GET"])
@require_auth
def identities():
    """The identity-provider links on the signed-in account."""
    from app.backend.services import persistence
    rows = persistence.federated_identities_for(g.current_user.user_id)
    return jsonify({
        "identities": [
            {
                "issuer": r["issuer"],
                # Truncated: the full subject is an opaque provider identifier
                # and there is no reason for a screen to carry all of it.
                "subject": str(r["subject"])[:12] + "…"
                           if len(str(r["subject"])) > 12 else str(r["subject"]),
                "email": r.get("email") or "",
                "created_at": r.get("created_at"),
                "last_seen": r.get("last_seen"),
            }
            for r in rows
        ],
        # Whether this account can still be signed into WITHOUT the provider.
        # A user about to unlink their only sign-in method should be told.
        "has_local_password": bool(
            getattr(auth_service.get_user(g.current_user.user_id),
                    "password_hash", "")),
    }), 200


@oidc_bp.route("/identities/<issuer>/<subject>", methods=["DELETE"])
@require_auth
def unlink(issuer: str, subject: str):
    """
    Remove one identity-provider link.

    Refused when it is the last way into the account and there is no local
    password: unlinking then locks the owner out of their own workspace, which
    is a worse outcome than leaving the link in place.
    """
    from app.backend.services import persistence

    user = auth_service.get_user(g.current_user.user_id)
    links = persistence.federated_identities_for(g.current_user.user_id)
    matching = [r for r in links
                if str(r["issuer"]) == issuer and str(r["subject"]) == subject]
    if not matching:
        raise NotFoundError("No such identity is linked to this account.")

    if len(links) == 1 and not getattr(user, "password_hash", ""):
        raise ValidationError(
            "This is the only way to sign in to this account and it has no "
            "password, so unlinking it would lock you out. Set a password "
            "first, then unlink."
        )

    removed = persistence.unlink_federated_identity(issuer, subject)
    logger.info("oidc.unlinked user_id=%s issuer=%s removed=%d",
                g.current_user.user_id, issuer, removed)
    return jsonify({"unlinked": removed > 0}), 200


@oidc_bp.errorhandler(ApplicationError)
def _oidc_error(exc: ApplicationError):
    return jsonify(exc.to_payload()), exc.http_status
