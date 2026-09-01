"""
NetGravity — Authentication & Session Security
==============================================
Replaces the prototype auth path, which verified no password, auto-provisioned
any unknown email as a valid user, and fell back to an authenticated default
user whenever a token was missing or invalid.

Credentials are PBKDF2-HMAC-SHA256 with per-user salts, stored in PostgreSQL.
On top of that this module now provides the things that separate a credential
store from an identity system:

  * **Brute-force resistance.** Failed attempts are counted per identity IN THE
    DATABASE and the account is locked for a cooling-off period. A lockout held
    in process memory resets on restart and is invisible to a second web
    worker, so it is not a lockout.
  * **Password reset.** Single-use, expiring, rate-limited tokens of which only
    the HASH is stored, delivered through a pluggable channel. Redeeming one
    revokes every existing session for that account.
  * **Second factor.** TOTP (RFC 6238) with recovery codes, verified against a
    step that is claimed atomically so a code cannot be replayed inside its own
    30-second window.
  * **Session hygiene.** An idle timeout that slides, an ABSOLUTE deadline that
    does not, rotation on credential change, and "sign out everywhere".
  * **httpOnly cookies.** The browser no longer holds a token JavaScript can
    read. See `session_cookie` below for why, and for what still uses bearer
    tokens.

It is still not an identity provider — there is no SSO, no SCIM, no directory —
and a deployment with an IdP should front it with one. What it now is, is an
account system that survives the attacks a login form actually meets.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import g, jsonify, request

from app.backend.services.errors import (
    ApplicationError,
    ForbiddenError,
    UnauthenticatedError,
    ValidationError,
)

logger = logging.getLogger(__name__)

# PBKDF2 cost. 240k iterations is the OWASP floor for SHA-256 at time of
# writing; it is a constant rather than a setting so it cannot be tuned down
# accidentally in a deployment config.
_PBKDF2_ITERATIONS = 240_000
_SALT_BYTES = 16

#: Idle timeout. Slides forward on use.
_SESSION_TTL_SECONDS = 8 * 60 * 60
#: Absolute deadline, independent of activity. A session that has been renewed
#: continuously for a week is re-authenticated regardless: idle expiry alone
#: means a stolen token stays valid for as long as the thief keeps using it.
_SESSION_ABSOLUTE_SECONDS = 7 * 24 * 60 * 60

#: 12, not 8. Eight characters is below every current guideline, and this store
#: is the only thing standing between an attacker and a client's network data.
_MIN_PASSWORD_LENGTH = 12

#: Brute-force policy. Ten failures inside fifteen minutes locks the account
#: for fifteen. Generous enough that a person who mistypes is never locked out;
#: tight enough that an online guessing attack gets ~40 attempts an hour.
_LOGIN_FAILURE_WINDOW = 15 * 60
_LOGIN_FAILURE_THRESHOLD = 10
_LOGIN_LOCK_SECONDS = 15 * 60

#: Reset tokens: short-lived, and capped so the endpoint cannot be used to
#: flood a mailbox or to farm tokens.
_RESET_TTL_SECONDS = 30 * 60
_RESET_MAX_PER_HOUR = 5

#: The pre-authentication token issued between password and second factor.
_MFA_CHALLENGE_TTL_SECONDS = 5 * 60

#: Token prefixes. A pre-authentication challenge and a real session are
#: DIFFERENT KINDS OF THING and are told apart by their prefix, so an MFA
#: challenge can never be presented as a session.
_SESSION_PREFIX = "ngt_"
_MFA_PREFIX = "ngm_"

#: The most common passwords, which no length rule catches. Not a substitute
#: for a breach corpus — it is the floor.
_TRIVIAL_PASSWORDS = frozenset({
    "password", "passw0rd", "password1", "password123", "123456789012",
    "qwertyuiop", "administrator", "letmeinplease", "welcome12345",
    "iloveyou1234", "1234567890ab", "abcd1234abcd", "netgravity123",
})


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """Derive a storable `pbkdf2$<iters>$<salt_hex>$<hash_hex>` string."""
    if salt is None:
        salt = os.urandom(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """
    Constant-time verification against a stored derivation.

    Returns False for any malformed record rather than raising, so a corrupt
    row denies access instead of crashing the login route.
    """
    try:
        scheme, iters_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s)
        )
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


@dataclass
class User:
    user_id: str
    name: str
    email: str
    role: str
    organization: str
    password_hash: str = ""
    created_at: float = field(default_factory=time.time)

    def public(self) -> Dict[str, Any]:
        """Client-safe projection. The password hash never leaves this class."""
        return {
            "id": self.user_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "organization": self.organization,
        }

    def stored(self) -> Dict[str, Any]:
        """
        The full record, INCLUDING the password derivation, for the database.

        Distinct from `public()` on purpose: the two projections differ by
        exactly the field that must never reach a client, and keeping them as
        separate named methods is what stops one being used where the other
        belongs.
        """
        return {
            "user_id": self.user_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "organization": self.organization,
            "password_hash": self.password_hash,
            "created_at": self.created_at,
        }


def validate_password_strength(password: str) -> None:
    """
    Refuse a password that would not survive an online guessing attack.

    Length first, because it is the only property that reliably matters, then a
    small list of the passwords everyone actually picks. Deliberately NOT a
    composition rule ("one uppercase, one symbol"): those push people towards
    `Password1!` and are worse than a length floor.
    """
    if len(password or "") < _MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at least {_MIN_PASSWORD_LENGTH} characters. "
            f"A longer passphrase is stronger than a short complicated one."
        )
    if (password or "").strip().lower() in _TRIVIAL_PASSWORDS:
        raise ValidationError(
            "That password is among the most commonly used ones and would be "
            "guessed almost immediately. Choose something else."
        )


@dataclass
class Session:
    token: str
    user_id: str
    created_at: float
    expires_at: float
    #: When this session was last used. Written through on renewal.
    last_seen_at: float = 0.0
    #: The deadline no amount of activity extends. `0.0` on a session restored
    #: from a database written before absolute expiry existed — treated as
    #: "derive one from `created_at`" rather than as "never expires".
    absolute_expiry: float = 0.0
    #: The user agent the session was issued to, for the sessions listing.
    client: str = ""

    def deadline(self) -> float:
        if self.absolute_expiry > 0:
            return self.absolute_expiry
        return self.created_at + _SESSION_ABSOLUTE_SECONDS


@dataclass
class AuthService:
    """
    Credential and session store, held in memory and written through to disk.

    Thread-safe because Flask serves concurrently and every store in the prior
    application layer was an unguarded module-level dict.

    Accounts and sessions now SURVIVE A RESTART. They did not: both lived only
    in the dictionaries below, so restarting the server signed every user out
    and deleted their account with them. The dictionaries are still the read
    path — an in-process lookup is the right shape for something consulted on
    every single request — and the database is the record of truth they are
    rebuilt from on start-up.
    """

    _users_by_email: Dict[str, User] = field(default_factory=dict)
    _users_by_id: Dict[str, User] = field(default_factory=dict)
    _sessions: Dict[str, Session] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _loaded: bool = False

    # ------------------------------------------------------------------
    def load(self) -> None:
        """
        Rebuild accounts and live sessions from the database.

        Idempotent, and safe to call before the first request. Expired sessions
        are purged rather than loaded, so a restart does not resurrect a session
        that had already timed out.
        """
        with self._lock:
            if self._loaded:
                return
            self._loaded = True

        from app.backend.services import persistence

        now = time.time()
        users = persistence.load_users()
        purged = persistence.purge_expired_sessions(now)
        sessions = persistence.load_session_records(now)

        with self._lock:
            for doc in users:
                if not doc.get("user_id") or not doc.get("email"):
                    continue
                user = User(
                    user_id=doc["user_id"],
                    name=doc.get("name", ""),
                    email=doc["email"],
                    role=doc.get("role", "PLANNER"),
                    organization=doc.get("organization", ""),
                    password_hash=doc.get("password_hash", ""),
                    created_at=float(doc.get("created_at") or now),
                )
                self._users_by_email[user.email] = user
                self._users_by_id[user.user_id] = user
            for row in sessions:
                user_id = row["user_id"]
                if user_id not in self._users_by_id:
                    continue
                token = row["token"]
                expires_at = float(row["expires_at"])
                # A session written before migration 2 has no `created_at`; it
                # is derived rather than left at 0, which `deadline()` would
                # otherwise read as "expired in 1970" and sign the user out.
                created_at = float(row.get("created_at")
                                   or (expires_at - _SESSION_TTL_SECONDS))
                self._sessions[token] = Session(
                    token=token, user_id=user_id,
                    created_at=created_at,
                    expires_at=expires_at,
                    last_seen_at=float(row.get("last_seen_at") or created_at),
                    absolute_expiry=float(row.get("absolute_expiry")
                                          or (created_at + _SESSION_ABSOLUTE_SECONDS)),
                    client=row.get("client") or "",
                )
        logger.info(
            "auth.loaded users=%d sessions=%d purged_expired=%d",
            len(users), len(sessions), purged,
        )

    # ------------------------------------------------------------------
    # Registration / login
    # ------------------------------------------------------------------
    def register(
        self,
        *,
        email: str,
        password: str,
        name: str = "",
        role: str = "PLANNER",
        organization: str = "Client Workspace",
    ) -> User:
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise ValidationError("A valid email address is required.")
        validate_password_strength(password)

        with self._lock:
            if email in self._users_by_email:
                # Deliberately explicit: this endpoint is for first-party
                # workspace signup, not a public registration form where
                # account enumeration would matter.
                raise ValidationError("An account already exists for this email.")

            user = User(
                user_id=f"usr_{secrets.token_hex(6)}",
                name=(name or email.split("@")[0].replace(".", " ").title()),
                email=email,
                role=role,
                organization=organization,
                password_hash=hash_password(password),
            )
            self._users_by_email[email] = user
            self._users_by_id[user.user_id] = user

        self._persist_user(user)
        logger.info("auth.user.registered user_id=%s", user.user_id)
        return user

    @staticmethod
    def _persist_user(user: User) -> None:
        from app.backend.services import persistence
        persistence.guarded(persistence.save_user)(
            user.user_id, user.email, user.stored(), user.created_at,
        )

    def authenticate(self, *, email: str, password: str) -> User:
        """
        Verify credentials, with a lockout.

        Unknown email and wrong password return the SAME error, and both paths
        run a dummy derivation so response timing does not reveal which case
        occurred. The prior implementation instead created an account for any
        unknown email, which made authentication decorative.

        Failures are counted per identity in the database. Past the threshold
        the account is locked for a cooling-off period and the correct password
        is refused too — otherwise the lock could be probed away. The count is
        cleared on a successful login.
        """
        from app.backend.services import persistence

        email = (email or "").strip().lower()
        now = time.time()

        lock = persistence.login_lock_state(email)
        if lock and lock.get("locked_until") and float(lock["locked_until"]) > now:
            remaining = int(float(lock["locked_until"]) - now)
            logger.warning("auth.login.locked identity_hash=%s remaining=%ds",
                           _identity_hash(email), remaining)
            raise UnauthenticatedError(
                f"Too many failed sign-in attempts. Try again in "
                f"{max(1, remaining // 60)} minute(s), or reset your password."
            )

        with self._lock:
            user = self._users_by_email.get(email)

        if user is None:
            # Burn equivalent CPU so a missing account is not detectable by timing.
            hash_password(password or "")
            # Counted too: without this, an attacker enumerating addresses is
            # throttled on the ones that exist and unthrottled on the ones that
            # do not, which is itself an oracle.
            persistence.guarded(persistence.record_login_failure)(
                email, now, _LOGIN_FAILURE_WINDOW, _LOGIN_FAILURE_THRESHOLD,
                _LOGIN_LOCK_SECONDS)
            raise UnauthenticatedError("Invalid email or password.")

        if not verify_password(password or "", user.password_hash):
            state = persistence.guarded(persistence.record_login_failure)(
                email, now, _LOGIN_FAILURE_WINDOW, _LOGIN_FAILURE_THRESHOLD,
                _LOGIN_LOCK_SECONDS) or {}
            logger.warning("auth.login.failed identity_hash=%s failures=%s",
                           _identity_hash(email), state.get("failures"))
            if state.get("locked_until"):
                raise UnauthenticatedError(
                    "Too many failed sign-in attempts. This account is locked "
                    "for 15 minutes. Reset your password to sign in sooner."
                )
            raise UnauthenticatedError("Invalid email or password.")

        persistence.guarded(persistence.clear_login_failures)(email)
        return user

    # ------------------------------------------------------------------
    # Second factor
    # ------------------------------------------------------------------
    def mfa_status(self, user_id: str) -> Dict[str, Any]:
        from app.backend.services import persistence
        row = persistence.load_mfa_enrolment(user_id)
        if row is None:
            return {"enrolled": False, "confirmed": False, "recovery_codes_left": 0}
        return {
            "enrolled": True,
            "confirmed": row.get("confirmed_at") is not None,
            "recovery_codes_left": persistence.count_unused_recovery_codes(user_id),
        }

    def begin_mfa_enrolment(self, user: User) -> Dict[str, Any]:
        """
        Issue a secret and recovery codes. Not active until confirmed.

        The secret and the codes are returned exactly once. Enrolment is only
        CONFIRMED once the user proves they can generate a code from it —
        otherwise a mis-scanned QR would lock them out of their own account.
        """
        from app.backend.services import persistence
        from app.backend.services import totp

        secret = totp.generate_secret()
        codes = totp.generate_recovery_codes()
        now = time.time()
        persistence.save_mfa_enrolment(user.user_id, secret, now, None)
        persistence.save_recovery_codes(
            user.user_id, [totp.hash_recovery_code(c) for c in codes])
        logger.info("auth.mfa.enrolment_started user_id=%s", user.user_id)
        return {
            "secret": secret,
            "otpauth_uri": totp.provisioning_uri(secret, account=user.email),
            "recovery_codes": codes,
        }

    def confirm_mfa_enrolment(self, user: User, code: str) -> None:
        from app.backend.services import persistence

        row = persistence.load_mfa_enrolment(user.user_id)
        if row is None:
            raise ValidationError("Start enrolment before confirming it.")
        if not self._verify_totp(user.user_id, row["secret"], code):
            raise UnauthenticatedError("That code is not valid. Check your authenticator's clock.")
        persistence.confirm_mfa_enrolment(user.user_id, time.time())
        logger.info("auth.mfa.confirmed user_id=%s", user.user_id)

    def disable_mfa(self, user: User, password: str) -> None:
        """Removing a factor is a credential change, so it costs the password."""
        if not verify_password(password or "", user.password_hash):
            raise UnauthenticatedError("Password is incorrect.")
        from app.backend.services import persistence
        persistence.delete_mfa_enrolment(user.user_id)
        logger.info("auth.mfa.disabled user_id=%s", user.user_id)

    @staticmethod
    def _verify_totp(user_id: str, secret: str, code: str) -> bool:
        """Verify a code AND spend its time step, so it cannot be replayed."""
        from app.backend.services import persistence
        from app.backend.services import totp

        ok, step = totp.verify(secret, code)
        if not ok or step is None:
            return False
        if not persistence.claim_mfa_step(user_id, step):
            logger.warning("auth.mfa.replay_refused user_id=%s step=%s", user_id, step)
            return False
        return True

    def verify_second_factor(self, user: User, code: str) -> bool:
        """A TOTP code, or a recovery code. Either spends itself."""
        from app.backend.services import persistence
        from app.backend.services import totp

        row = persistence.load_mfa_enrolment(user.user_id)
        if row is None or row.get("confirmed_at") is None:
            return True  # no factor configured

        if self._verify_totp(user.user_id, row["secret"], code):
            return True

        if persistence.consume_recovery_code(
                user.user_id, totp.hash_recovery_code(code)):
            left = persistence.count_unused_recovery_codes(user.user_id)
            logger.warning("auth.mfa.recovery_code_used user_id=%s remaining=%d",
                           user.user_id, left)
            return True
        return False

    def issue_mfa_challenge(self, user: User) -> str:
        """
        A short-lived, pre-authentication token.

        Stored in the sessions table but under a DIFFERENT PREFIX, so
        `resolve_session` refuses it everywhere and only the second-factor
        endpoint will accept it. Two kinds of token that must never be
        interchangeable are told apart by their shape, not by a flag someone
        has to remember to check.
        """
        from app.backend.services import persistence

        token = f"{_MFA_PREFIX}{secrets.token_urlsafe(32)}"
        now = time.time()
        expires_at = now + _MFA_CHALLENGE_TTL_SECONDS
        with self._lock:
            self._sessions[token] = Session(
                token=token, user_id=user.user_id, created_at=now,
                expires_at=expires_at, last_seen_at=now,
                absolute_expiry=expires_at, client="mfa-challenge",
            )
        persistence.guarded(persistence.save_session_record)(
            token, user.user_id, expires_at, now, now, expires_at, "mfa-challenge")
        return token

    def resolve_mfa_challenge(self, token: str) -> User:
        if not token or not token.startswith(_MFA_PREFIX):
            raise UnauthenticatedError("Start again from the sign-in form.")
        with self._lock:
            session = self._sessions.get(token)
            if session is None or session.expires_at < time.time():
                raise UnauthenticatedError(
                    "That sign-in attempt has expired. Start again.")
            user = self._users_by_id.get(session.user_id)
        if user is None:
            raise UnauthenticatedError("Sign-in attempt refers to an unknown user.")
        return user

    # ------------------------------------------------------------------
    # Password reset
    # ------------------------------------------------------------------
    def begin_password_reset(self, email: str) -> Optional[Tuple[User, str]]:
        """
        Issue a reset token, or return None if there is nothing to reset.

        The CALLER must respond identically either way: revealing that an
        address is unknown turns the reset form into an account-enumeration
        oracle, which is the whole reason this returns None instead of raising.

        Only the token's hash is stored. A database read therefore does not
        hand over the ability to take over every account with a reset in
        flight — which is exactly what storing the token itself would do.
        """
        from app.backend.services import persistence

        email = (email or "").strip().lower()
        with self._lock:
            user = self._users_by_email.get(email)
        if user is None:
            return None

        now = time.time()
        if persistence.count_recent_password_resets(user.user_id, now - 3600) \
                >= _RESET_MAX_PER_HOUR:
            logger.warning("auth.reset.rate_limited user_id=%s", user.user_id)
            return None

        token = f"ngr_{secrets.token_urlsafe(32)}"
        persistence.save_password_reset(
            _token_hash(token), user.user_id, now, now + _RESET_TTL_SECONDS)
        logger.info("auth.reset.issued user_id=%s", user.user_id)
        return user, token

    def complete_password_reset(self, token: str, new_password: str) -> User:
        """
        Redeem a reset token.

        Every existing session for the account is revoked. A password reset is
        the action a user takes when they believe someone else has their
        credentials, and leaving that someone else signed in would defeat it.
        """
        from app.backend.services import persistence

        validate_password_strength(new_password)
        now = time.time()
        row = persistence.load_password_reset(_token_hash(token or ""))
        if row is None or row.get("used_at") is not None \
                or float(row["expires_at"]) < now:
            raise UnauthenticatedError(
                "That reset link is not valid or has already been used. "
                "Request a new one."
            )
        # Single-use under concurrency: exactly one caller updates the row.
        if persistence.consume_password_reset(_token_hash(token), now) != 1:
            raise UnauthenticatedError("That reset link has already been used.")

        user = self.get_user(row["user_id"])
        if user is None:
            raise UnauthenticatedError("Reset refers to an unknown account.")

        self._set_password(user, new_password)
        persistence.invalidate_password_resets(user.user_id, now)
        persistence.clear_login_failures(user.email)
        self.revoke_all_sessions(user.user_id)
        logger.info("auth.reset.completed user_id=%s", user.user_id)
        return user

    def change_password(self, user: User, current_password: str,
                        new_password: str) -> None:
        """Change a known password. Also revokes other sessions."""
        if not verify_password(current_password or "", user.password_hash):
            raise UnauthenticatedError("Current password is incorrect.")
        if current_password == new_password:
            raise ValidationError("The new password must be different.")
        validate_password_strength(new_password)
        self._set_password(user, new_password)
        logger.info("auth.password.changed user_id=%s", user.user_id)

    def _set_password(self, user: User, new_password: str) -> None:
        with self._lock:
            user.password_hash = hash_password(new_password)
        self._persist_user(user)

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    def issue_session(self, user: User, *, client: str = "") -> str:
        from app.backend.services import persistence

        token = f"{_SESSION_PREFIX}{secrets.token_urlsafe(32)}"
        now = time.time()
        expires_at = now + _SESSION_TTL_SECONDS
        absolute = now + _SESSION_ABSOLUTE_SECONDS
        with self._lock:
            self._sessions[token] = Session(
                token=token,
                user_id=user.user_id,
                created_at=now,
                expires_at=expires_at,
                last_seen_at=now,
                absolute_expiry=absolute,
                client=(client or "")[:200],
            )
        # Written through so a restart does not sign everyone out mid-session.
        persistence.guarded(persistence.save_session_record)(
            token, user.user_id, expires_at, now, now, absolute,
            (client or "")[:200])
        return token

    def resolve_session(self, token: str) -> User:
        """
        Resolve a session token to a user, or raise.

        There is no anonymous fallback. The prior `/me` returned a default
        authenticated planner for missing or invalid tokens, which meant no
        endpoint was ever genuinely protected.

        Two deadlines are enforced. The IDLE one slides forward on use, so an
        active user is not signed out mid-afternoon. The ABSOLUTE one does not,
        so a token that has been renewed continuously for a week is
        re-authenticated anyway — with idle expiry alone, a stolen token stays
        valid for exactly as long as the thief keeps using it.
        """
        if not token:
            raise UnauthenticatedError("Authentication required.")
        if token.startswith(_MFA_PREFIX):
            # A pre-authentication challenge is not a session and can never be
            # spent as one.
            raise UnauthenticatedError("Second factor not yet provided.")

        from app.backend.services import persistence

        now = time.time()
        renew = False
        with self._lock:
            session = self._sessions.get(token)
            if session is None:
                raise UnauthenticatedError("Invalid or expired session.")
            if session.expires_at < now:
                del self._sessions[token]
                persistence.guarded(persistence.delete_session)(token)
                raise UnauthenticatedError("Session expired.")
            if session.deadline() < now:
                del self._sessions[token]
                persistence.guarded(persistence.delete_session)(token)
                raise UnauthenticatedError(
                    "This session has reached its maximum age. Sign in again.")
            # Slide the idle window, but only when it has actually moved on —
            # writing through on every request would put a database round trip
            # in front of every read in the application.
            if now - session.last_seen_at > 60:
                session.last_seen_at = now
                session.expires_at = min(now + _SESSION_TTL_SECONDS,
                                         session.deadline())
                renew = True
            user = self._users_by_id.get(session.user_id)
            expires_at, last_seen = session.expires_at, session.last_seen_at

        if renew:
            persistence.guarded(persistence.touch_session)(token, expires_at, last_seen)
        if user is None:
            raise UnauthenticatedError("Session refers to an unknown user.")
        return user

    def revoke_session(self, token: str) -> None:
        from app.backend.services import persistence
        with self._lock:
            self._sessions.pop(token, None)
        persistence.guarded(persistence.delete_session)(token)

    def revoke_all_sessions(self, user_id: str, keep_token: str = "") -> int:
        """Sign out everywhere. Used after a reset, and offered to the user."""
        from app.backend.services import persistence
        with self._lock:
            doomed = [t for t, s in self._sessions.items()
                      if s.user_id == user_id and t != keep_token]
            for t in doomed:
                del self._sessions[t]
        persistence.guarded(persistence.delete_sessions_for_user)(user_id, keep_token)
        logger.info("auth.sessions.revoked user_id=%s count=%d", user_id, len(doomed))
        return len(doomed)

    def sessions_for(self, user_id: str) -> List[Dict[str, Any]]:
        """
        The live sessions on an account, for the user to inspect.

        The token itself is never returned — only a short fingerprint, enough
        to recognise the current one.
        """
        now = time.time()
        with self._lock:
            rows = [s for s in self._sessions.values()
                    if s.user_id == user_id and s.expires_at >= now
                    and not s.token.startswith(_MFA_PREFIX)]
        return sorted(
            [{
                "id": hashlib.sha256(s.token.encode()).hexdigest()[:12],
                "created_at": s.created_at,
                "last_seen_at": s.last_seen_at or s.created_at,
                "expires_at": s.expires_at,
                "absolute_expiry": s.deadline(),
                "client": s.client,
            } for s in rows],
            key=lambda r: r["last_seen_at"], reverse=True)

    def get_user(self, user_id: str) -> Optional[User]:
        with self._lock:
            return self._users_by_id.get(user_id)

    def find_by_email(self, email: str) -> Optional[User]:
        """
        The account for an address, or None.

        Public because federated sign-in needs it: linking an identity-provider
        subject to an EXISTING local account on first sign-in requires looking
        the account up. Read-only, and it does not disclose anything a caller who
        already holds a verified assertion about that address does not know.
        """
        with self._lock:
            return self._users_by_email.get((email or "").strip().lower())

    def register_federated(
        self,
        *,
        email: str,
        name: str = "",
        role: str = "PLANNER",
        organization: str = "Client Workspace",
    ) -> User:
        """
        Create an account whose credential lives at an identity provider.

        No password is set, and that is the point: there is nothing to guess,
        nothing to reset, and nothing to leak. `password_hash` is empty, and
        `authenticate()` must therefore never accept a password for this
        account — which it cannot, because `verify_password` fails against an
        empty hash.

        Separate from `register()` rather than `register(password=None)` so no
        code path can create a passwordless account by accident: an account you
        can sign into without a credential is the worst possible default, and it
        should take a differently-named method to make one.
        """
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            raise ValidationError("A valid email address is required.")

        with self._lock:
            existing = self._users_by_email.get(email)
            if existing is not None:
                return existing
            user = User(
                user_id=f"usr_{secrets.token_hex(6)}",
                name=(name or email.split("@")[0].replace(".", " ").title()),
                email=email,
                role=role,
                organization=organization,
                password_hash="",
            )
            self._users_by_email[email] = user
            self._users_by_id[user.user_id] = user

        self._persist_user(user)
        logger.info("auth.user.registered_federated user_id=%s", user.user_id)
        return user

    def purge_expired(self) -> int:
        from app.backend.services import persistence
        now = time.time()
        with self._lock:
            stale = [t for t, s in self._sessions.items() if s.expires_at < now]
            for t in stale:
                del self._sessions[t]
        persistence.guarded(persistence.purge_expired_sessions)(now)
        return len(stale)


# Single application-wide instance.
auth_service = AuthService()


def _identity_hash(identity: str) -> str:
    """A short, stable, non-reversible tag for logs. Never the address itself."""
    return hashlib.sha256((identity or "").encode("utf-8")).hexdigest()[:12]


def _token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cookies and CSRF
# ---------------------------------------------------------------------------
#
# The session token used to be handed to the browser and kept in
# `localStorage`, where any script running on the page can read it — so a
# single XSS anywhere in the application exfiltrated a token that stayed valid
# for eight hours, on any machine, with no further access needed.
#
# It now lives in an httpOnly cookie. Script cannot read it, so an XSS can act
# WITHIN the page's origin while it runs but cannot carry the session away.
#
# A cookie is sent automatically, which is what CSRF exploits, so two controls
# apply:
#
#   1. `SameSite=Lax` — the browser does not attach the cookie to a
#      cross-site POST at all. This is the primary control.
#   2. A double-submit CSRF token: a readable `ng_csrf` cookie whose value must
#      be echoed in `X-CSRF-Token` on every unsafe method. Required only when
#      the request authenticated VIA THE COOKIE; a bearer token is not sent
#      automatically and so cannot be ridden.
#
# `Bearer` is still accepted, for scripts, tests and the validation harnesses.
# It is the deliberate exception, not the default.

SESSION_COOKIE = "ng_session"
CSRF_COOKIE = "ng_csrf"
CSRF_HEADER = "X-CSRF-Token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _secure_cookies() -> bool:
    """
    `Secure` in production, so the cookie is never sent over plain HTTP.

    Off in development because the platform is served over http://localhost,
    where a Secure cookie would simply never be stored and nobody could sign in.
    """
    return os.environ.get("NETGRAVITY_ENV", "development").strip().lower() == "production"


def attach_session_cookies(response, token: str) -> None:
    """Put the session in an httpOnly cookie and issue its CSRF partner."""
    secure = _secure_cookies()
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=_SESSION_TTL_SECONDS, httponly=True, secure=secure,
        samesite="Lax", path="/",
    )
    # Readable by design: the page has to echo it back in a header, which is
    # precisely what a cross-site attacker cannot do.
    response.set_cookie(
        CSRF_COOKIE, secrets.token_urlsafe(24),
        max_age=_SESSION_TTL_SECONDS, httponly=False, secure=secure,
        samesite="Lax", path="/",
    )


def clear_session_cookies(response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


def _bearer_token() -> str:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return ""


def _request_token() -> Tuple[str, str]:
    """
    (token, source) for this request — an explicit bearer first, then the cookie.

    Order matters, and this way round is both more correct and safe.

    An `Authorization` header is a DELIBERATE act by the caller. A cookie is
    ambient: the browser attaches it to whatever the page happens to request,
    which is what CSRF exploits. Preferring the explicit credential means a
    script that passes a token gets the identity it asked for, rather than
    silently being answered as whoever is signed in in that browser.

    It does not weaken the CSRF control. A cross-site page cannot set a custom
    header on a request without a CORS preflight that this server does not
    grant, and an HTML form cannot set headers at all — so an attacker can
    never cause a "bearer" request to be made on a victim's behalf. Only the
    cookie path is ridable, and only the cookie path is CSRF-checked.
    """
    bearer = _bearer_token()
    if bearer:
        return bearer, "bearer"
    return request.cookies.get(SESSION_COOKIE, ""), "cookie"


def _check_csrf(source: str) -> None:
    """
    Double-submit check, for cookie-authenticated unsafe requests only.

    A bearer token is attached by the caller's own code and is never sent
    automatically by a browser, so a cross-site page cannot cause one to be
    presented; requiring a CSRF header there would break every API client for
    no gain.
    """
    if source != "cookie" or request.method in _SAFE_METHODS:
        return
    expected = request.cookies.get(CSRF_COOKIE, "")
    presented = request.headers.get(CSRF_HEADER, "")
    if not expected or not presented or not hmac.compare_digest(expected, presented):
        raise ForbiddenError(
            "CSRF check failed. Reload the page and try again.",
            context={"header": CSRF_HEADER},
        )


def current_user():
    """The authenticated user for this request, or None."""
    return getattr(g, "current_user", None)


def require_auth(fn: Callable) -> Callable:
    """
    Route guard. Resolves the bearer token and pins the user onto `g`.

    Applied to every route that reads or writes project-scoped data. Routes
    without this decorator are public by deliberate choice, not by omission —
    see `app/backend/api/__init__.py` for the audited list.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        token, source = _request_token()
        try:
            user = auth_service.resolve_session(token)
            _check_csrf(source)
        except ApplicationError as exc:
            return jsonify(exc.to_payload()), exc.http_status
        g.current_user = user
        g.session_token = token
        g.auth_source = source
        return fn(*args, **kwargs)

    return wrapper


def register_error_handler(app) -> None:
    """Serialize every ApplicationError uniformly (brief §24)."""

    @app.errorhandler(ApplicationError)
    def _handle_app_error(exc: ApplicationError):
        return jsonify(exc.to_payload()), exc.http_status
