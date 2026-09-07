"""
NetGravity — Authentication & Session API Blueprint
===================================================
Session issuance and credential verification.

Phase 10.0 rewrite. The prototype version of this blueprint accepted any
password, auto-provisioned any unknown email as a valid account, and returned a
default authenticated user from `/me` whenever the bearer token was missing or
invalid — which meant no endpoint in the application was ever protected. All
three behaviours are removed; credentials are verified against PBKDF2 hashes in
`app.backend.services.security`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from flask import Blueprint, g, jsonify, make_response, request

from app.backend.services.errors import (
    ApplicationError,
    UnauthenticatedError,
    ValidationError,
)
from app.backend.services.notifications import password_reset_delivery
from app.backend.services.ratelimit import rate_limit
from app.backend.services.security import (
    SESSION_COOKIE,
    attach_session_cookies,
    auth_service,
    clear_session_cookies,
    require_auth,
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


#: Account role -> control-plane actor role.
#:
#: Kept explicit rather than passing the string through, so a role the account
#: store does not recognise cannot become an orchestrator role by accident. An
#: unrecognised role gets the LEAST authority, not the most.
_ACTOR_ROLES = {
    "VIEWER": "VIEWER",
    "PLANNER": "PLANNER",
    "APPROVER": "APPROVER",
    "ADMIN": "ADMIN",
}


def bearer_actor():
    """
    The orchestrator `Actor` for the current request's bearer token.

    Raises `AuthenticationRequired` when there is no valid session, which is
    what makes `/orchestrator/*` deny anonymous callers.

    This is the ONLY place an account becomes a control-plane identity. The
    orchestrator's own endpoints used to build the actor from the request body,
    so a caller chose their own role — including `APPROVER`, the role
    `resolve_approval` checks before letting a governed action proceed.
    """
    from netgravity.orchestrator.api import AuthenticationRequired
    from netgravity.orchestrator.schemas.requests import Actor, ActorRole

    # An explicit bearer first, then the cookie — the same order the rest of
    # the application uses, so the control plane cannot be reachable by a
    # credential the API is no longer willing to accept, or vice versa.
    header = request.headers.get("Authorization", "")
    token = (header[7:].strip() if header.startswith("Bearer ") else "")         or request.cookies.get(SESSION_COOKIE, "")
    if not token:
        raise AuthenticationRequired("Authentication is required.")
    try:
        user = auth_service.resolve_session(token)
    except ApplicationError as exc:
        raise AuthenticationRequired(exc.message if hasattr(exc, "message")
                                     else "Invalid or expired session.")

    role_name = _ACTOR_ROLES.get(str(user.role or "").upper(), "VIEWER")
    return Actor(
        actor_id=user.user_id,
        role=ActorRole(role_name),
        display_name=user.name,
    )


def _client_description() -> str:
    """A short label for the sessions list. Never an IP, never a fingerprint."""
    return (request.headers.get("User-Agent", "") or "unknown")[:200]


def _authenticated_response(user, payload: Dict[str, Any], status: int):
    """
    Issue a session and return it BOTH ways.

    The httpOnly cookie is what the browser uses; the token in the body is what
    scripts, tests and the validation harnesses use. The browser client no
    longer stores the body token — see `app/frontend/js/integration/api-client.js`.
    """
    token = auth_service.issue_session(user, client=_client_description())
    body = dict(payload)
    body["token"] = token
    body["user"] = user.public()
    body["mfa"] = auth_service.mfa_status(user.user_id)
    response = make_response(jsonify(body), status)
    attach_session_cookies(response, token)
    return response


@auth_bp.route("/login", methods=["POST"])
@rate_limit("auth.login", limit=20, window_seconds=300)
def login():
    """
    Verify credentials and issue a session — unless a second factor is enrolled.

    With MFA confirmed, this returns a short-lived CHALLENGE token and no
    session. The challenge carries a different prefix from a session token and
    `resolve_session` refuses it everywhere, so a client that ignores
    `mfa_required` gets nothing usable rather than a bypass.
    """
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")

    if not email:
        raise ValidationError("Email is required.")
    if not password:
        raise ValidationError("Password is required.")

    # Raises UnauthenticatedError (401) for both unknown email and wrong
    # password, with equivalent timing, and enforces the lockout.
    user = auth_service.authenticate(email=email, password=password)

    if auth_service.mfa_status(user.user_id).get("confirmed"):
        logger.info("auth.login.mfa_required user_id=%s", user.user_id)
        return jsonify({
            "status": "mfa_required",
            "mfa_required": True,
            "mfa_token": auth_service.issue_mfa_challenge(user),
        }), 200

    logger.info("auth.login.ok user_id=%s", user.user_id)
    return _authenticated_response(user, {"status": "authenticated"}, 200)


@auth_bp.route("/login/mfa", methods=["POST"])
@rate_limit("auth.mfa", limit=20, window_seconds=300)
def login_mfa():
    """Complete a sign-in with a TOTP code or a recovery code."""
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    challenge = str(body.get("mfa_token") or "")
    code = str(body.get("code") or "").strip()
    if not code:
        raise ValidationError("Enter the code from your authenticator app.")

    user = auth_service.resolve_mfa_challenge(challenge)
    if not auth_service.verify_second_factor(user, code):
        logger.warning("auth.mfa.failed user_id=%s", user.user_id)
        raise UnauthenticatedError("That code is not valid.")

    # The challenge is spent whether or not it was used successfully.
    auth_service.revoke_session(challenge)
    logger.info("auth.login.ok user_id=%s mfa=true", user.user_id)
    return _authenticated_response(user, {"status": "authenticated"}, 200)


@auth_bp.route("/signup", methods=["POST"])
# Raised from 10 once the counter became shared across workers: at 10 per
# hour per address, a team behind one office NAT could register ten people
# and no more. Still a real control against scripted account creation, and
# settable per deployment with NETGRAVITY_RATELIMIT_AUTH_SIGNUP.
@rate_limit("auth.signup", limit=60, window_seconds=3600)
def signup():
    """Register a new workspace account."""
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    name = str(body.get("name") or "").strip()
    organization = str(body.get("organization") or "Client Workspace").strip()

    user = auth_service.register(
        email=email,
        password=password,
        name=name,
        organization=organization,
    )
    return _authenticated_response(user, {"status": "registered"}, 201)


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------

@auth_bp.route("/password/reset", methods=["POST"])
@rate_limit("auth.reset", limit=10, window_seconds=3600)
def request_password_reset():
    """
    Start a reset. Always answers the same way.

    An unknown address, a rate-limited one and a real one produce an identical
    response. Anything else turns this endpoint into an account-enumeration
    oracle, which is a worse leak than the inconvenience it saves.
    """
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip()
    if not email:
        raise ValidationError("Email is required.")

    issued = auth_service.begin_password_reset(email)
    if issued is not None:
        user, token = issued
        password_reset_delivery.send(user=user, token=token, request=request)

    return jsonify({
        "status": "sent",
        "message": ("If an account exists for that address, a reset link is on "
                    "its way. The link is valid for 30 minutes and can be used "
                    "once."),
    }), 200


@auth_bp.route("/password/reset/confirm", methods=["POST"])
@rate_limit("auth.reset_confirm", limit=20, window_seconds=3600)
def confirm_password_reset():
    """Redeem a reset token. Signs the account out everywhere."""
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    token = str(body.get("token") or "")
    new_password = str(body.get("password") or "")
    if not token:
        raise ValidationError("A reset token is required.")

    user = auth_service.complete_password_reset(token, new_password)
    logger.info("auth.reset.applied user_id=%s", user.user_id)
    return jsonify({
        "status": "reset",
        "message": "Your password has been changed and every other session "
                   "signed out. Sign in with the new password.",
    }), 200


@auth_bp.route("/password", methods=["POST"])
@require_auth
def change_password():
    """Change a known password, then re-issue this session and drop the rest."""
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    auth_service.change_password(
        g.current_user,
        str(body.get("current_password") or ""),
        str(body.get("password") or ""),
    )
    # Rotate: the token in flight was issued against the old credential.
    auth_service.revoke_all_sessions(g.current_user.user_id)
    return _authenticated_response(
        g.current_user,
        {"status": "password_changed",
         "message": "Password changed. Other sessions have been signed out."},
        200)


# ---------------------------------------------------------------------------
# Second factor
# ---------------------------------------------------------------------------

@auth_bp.route("/mfa", methods=["GET"])
@require_auth
def mfa_state():
    return jsonify(auth_service.mfa_status(g.current_user.user_id)), 200


@auth_bp.route("/mfa/enrol", methods=["POST"])
@require_auth
def mfa_enrol():
    """
    Begin enrolment. The secret and recovery codes are returned ONCE.

    Not active until confirmed with a working code — a mis-scanned QR that
    activated immediately would lock the user out of their own account.
    """
    enrolment = auth_service.begin_mfa_enrolment(g.current_user)
    return jsonify({
        "status": "enrolment_started",
        **enrolment,
        "message": "Scan the code, then confirm with a six-digit code. Store "
                   "the recovery codes now — they are not shown again.",
    }), 200


@auth_bp.route("/mfa/confirm", methods=["POST"])
@require_auth
def mfa_confirm():
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    auth_service.confirm_mfa_enrolment(g.current_user, str(body.get("code") or ""))
    return jsonify({"status": "mfa_enabled",
                    **auth_service.mfa_status(g.current_user.user_id)}), 200


@auth_bp.route("/mfa", methods=["DELETE"])
@require_auth
def mfa_disable():
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    auth_service.disable_mfa(g.current_user, str(body.get("password") or ""))
    return jsonify({"status": "mfa_disabled"}), 200


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@auth_bp.route("/sessions", methods=["GET"])
@require_auth
def list_sessions():
    """Live sessions on this account. Tokens are never returned, only digests."""
    current = getattr(g, "session_token", "")
    digest = __import__("hashlib").sha256(current.encode()).hexdigest()[:12]
    sessions = auth_service.sessions_for(g.current_user.user_id)
    for row in sessions:
        row["current"] = row["id"] == digest
    return jsonify({"sessions": sessions, "total": len(sessions)}), 200


@auth_bp.route("/sessions", methods=["DELETE"])
@require_auth
def revoke_other_sessions():
    """Sign out everywhere else, keeping the session making the request."""
    revoked = auth_service.revoke_all_sessions(
        g.current_user.user_id, keep_token=getattr(g, "session_token", ""))
    return jsonify({"status": "revoked", "revoked": revoked}), 200


@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    """
    Current authenticated user.

    Returns 401 without a valid session. There is deliberately no anonymous
    fallback: the previous implementation's default-planner response is what
    made every downstream authorization check meaningless.
    """
    return jsonify({"user": g.current_user.public(), "status": "authenticated"}), 200


@auth_bp.route("/logout", methods=["POST"])
def logout():
    """
    Invalidate the presented session. Idempotent.

    Clears the cookie as well as revoking the token server-side: revoking
    without clearing leaves the browser presenting a dead credential on every
    request, and clearing without revoking leaves a live session behind for
    anyone who copied the token.
    """
    header = request.headers.get("Authorization", "")
    token = ((header[7:].strip() if header.startswith("Bearer ") else "")
             or request.cookies.get(SESSION_COOKIE, ""))
    auth_service.revoke_session(token)
    response = make_response(jsonify({"status": "logged_out"}), 200)
    clear_session_cookies(response)
    return response


@auth_bp.errorhandler(ApplicationError)
def _auth_error(exc: ApplicationError):
    return jsonify(exc.to_payload()), exc.http_status
