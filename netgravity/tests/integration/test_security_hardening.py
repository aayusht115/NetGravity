"""
Phase 10.8 — the controls that separate a credential store from an identity
system, and an application from a deployable one.

Every test here drives the real Flask application. None of them mock the
security layer they are checking, because a mocked lockout is not a lockout.
"""

from __future__ import annotations

import time
import uuid

import pytest

from app.backend.app import app
from app.backend.services import persistence, totp
from app.backend.services.ratelimit import limiter
from app.backend.services.security import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    auth_service,
    validate_password_strength,
)

PASSWORD = "Netgravity@2026"

def _without_comments(text: str) -> str:
    """
    Strip comments before looking for third-party hosts.

    A comment that NAMES the CDN it replaced is documentation, not a request.
    Scanning raw text made the check fail on its own explanation, which would
    have taught the next person to delete the explanation.
    """
    import re
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)     # CSS and JS block
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)     # HTML
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)      # JS line
    return text




@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _signup(client, password: str = PASSWORD):
    email = f"sec-{uuid.uuid4().hex[:10]}@example.com"
    res = client.post("/api/auth/signup", json={
        "name": "Security Check", "email": email, "password": password})
    assert res.status_code == 201, res.get_data(as_text=True)[:300]
    return email, res.get_json()["token"], res


# ===========================================================================
# Password policy
# ===========================================================================

class TestPasswordPolicy:

    def test_a_short_password_is_refused(self, client):
        res = client.post("/api/auth/signup", json={
            "name": "Short", "email": f"s-{uuid.uuid4().hex[:8]}@x.com",
            "password": "Sh0rt!12"})
        assert res.status_code == 400
        assert "12 characters" in res.get_data(as_text=True)

    def test_a_common_password_is_refused_even_when_long_enough(self):
        with pytest.raises(Exception) as excinfo:
            validate_password_strength("netgravity123")
        assert "commonly used" in str(excinfo.value)

    def test_a_long_passphrase_is_accepted(self):
        validate_password_strength("correct horse battery staple")


# ===========================================================================
# Brute force
# ===========================================================================

class TestAccountLockout:

    def test_repeated_failures_lock_the_account(self, client):
        email, _, _ = _signup(client)
        for _ in range(10):
            res = client.post("/api/auth/login",
                              json={"email": email, "password": "wrong-password-x"})
            assert res.status_code == 401

        # The CORRECT password is now refused too. A lock that can be probed
        # away by guessing right is not a lock.
        res = client.post("/api/auth/login",
                          json={"email": email, "password": PASSWORD})
        assert res.status_code == 401
        assert "Too many failed" in res.get_data(as_text=True)

    def test_the_lockout_lives_in_the_database_not_in_memory(self, client):
        """A lock that resets on restart, or that a second worker cannot see,
        is not a lock. It is a row."""
        email, _, _ = _signup(client)
        for _ in range(10):
            client.post("/api/auth/login",
                        json={"email": email, "password": "wrong-password-x"})
        state = persistence.login_lock_state(email)
        assert state is not None
        assert state["failures"] >= 10
        assert state["locked_until"] > time.time()

    def test_a_successful_login_clears_the_count(self, client):
        email, _, _ = _signup(client)
        for _ in range(3):
            client.post("/api/auth/login",
                        json={"email": email, "password": "wrong-password-x"})
        assert persistence.login_lock_state(email)["failures"] == 3

        res = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert res.status_code == 200
        assert persistence.login_lock_state(email) is None

    def test_an_unknown_address_is_counted_too(self, client):
        """
        Otherwise an attacker enumerating addresses is throttled on the ones
        that exist and unthrottled on the ones that do not — an oracle.
        """
        unknown = f"nobody-{uuid.uuid4().hex[:8]}@example.com"
        client.post("/api/auth/login", json={"email": unknown, "password": "x" * 12})
        assert persistence.login_lock_state(unknown)["failures"] == 1


# ===========================================================================
# Sessions and cookies
# ===========================================================================

class TestSessionCookies:

    def test_signup_sets_an_httponly_session_cookie(self, client):
        _, _, res = _signup(client)
        cookies = res.headers.getlist("Set-Cookie")
        session = next(c for c in cookies if c.startswith(f"{SESSION_COOKIE}="))
        assert "HttpOnly" in session
        assert "SameSite=Lax" in session

    def test_the_csrf_cookie_is_readable_by_design(self, client):
        _, _, res = _signup(client)
        csrf = next(c for c in res.headers.getlist("Set-Cookie")
                    if c.startswith(f"{CSRF_COOKIE}="))
        # Readable on purpose: the page has to echo it in a header, which is
        # precisely what a cross-site attacker cannot do.
        assert "HttpOnly" not in csrf

    def test_the_cookie_alone_authenticates_a_read(self, client):
        _signup(client)
        # No Authorization header at all — the test client keeps the cookie.
        res = client.get("/api/auth/me")
        assert res.status_code == 200

    def test_an_unsafe_cookie_request_without_the_csrf_header_is_refused(self, client):
        _signup(client)
        res = client.post("/api/projects", json={"name": "CSRF probe"})
        assert res.status_code == 403
        assert "CSRF" in res.get_data(as_text=True)

    def test_the_same_request_succeeds_with_the_csrf_header(self, client):
        _signup(client)
        csrf = client.get_cookie(CSRF_COOKIE).value
        res = client.post("/api/projects", json={"name": "CSRF probe"},
                          headers={CSRF_HEADER: csrf})
        assert res.status_code in (200, 201)

    def test_a_bearer_token_needs_no_csrf_header(self, client):
        """
        A cross-site page cannot set an Authorization header without a CORS
        preflight this server does not grant, so the bearer path is not
        ridable and requiring a CSRF token there would break API clients for
        no gain.
        """
        _, token, _ = _signup(client)
        client.delete_cookie(SESSION_COOKIE)
        client.delete_cookie(CSRF_COOKIE)
        res = client.post("/api/projects", json={"name": "Bearer probe"},
                          headers={"Authorization": f"Bearer {token}"})
        assert res.status_code in (200, 201)

    def test_logout_clears_the_cookie_and_revokes_the_token(self, client):
        _, token, _ = _signup(client)
        res = client.post("/api/auth/logout")
        assert res.status_code == 200
        cleared = [c for c in res.headers.getlist("Set-Cookie")
                   if c.startswith(f"{SESSION_COOKIE}=")]
        assert cleared, "the session cookie was not cleared"
        # And revoked server-side, so a copied token is dead too.
        assert client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_a_session_has_an_absolute_deadline_idle_use_cannot_extend(self, client):
        email, token, _ = _signup(client)
        user = auth_service._users_by_email[email]  # noqa: SLF001
        session = auth_service._sessions[token]     # noqa: SLF001
        # Reached its maximum age, but well inside the idle window.
        session.absolute_expiry = time.time() - 1
        session.expires_at = time.time() + 3600
        client.delete_cookie(SESSION_COOKIE)
        res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 401
        assert "maximum age" in res.get_data(as_text=True)
        assert user is not None

    def test_sessions_are_listed_without_ever_returning_a_token(self, client):
        _, token, _ = _signup(client)
        res = client.get("/api/auth/sessions")
        assert res.status_code == 200
        body = res.get_json()
        assert body["total"] >= 1
        assert token not in res.get_data(as_text=True)
        assert any(s["current"] for s in body["sessions"])

    def test_sign_out_everywhere_keeps_the_current_session(self, client):
        email, first, _ = _signup(client)
        second = auth_service.issue_session(
            auth_service._users_by_email[email])  # noqa: SLF001
        csrf = client.get_cookie(CSRF_COOKIE).value
        res = client.delete("/api/auth/sessions", headers={CSRF_HEADER: csrf})
        assert res.status_code == 200
        assert res.get_json()["revoked"] >= 1
        # The one making the request survives; the other does not.
        assert client.get("/api/auth/me").status_code == 200
        client.delete_cookie(SESSION_COOKIE)
        assert client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {second}"}).status_code == 401
        assert first


# ===========================================================================
# Password reset
# ===========================================================================

class TestPasswordReset:

    def test_an_unknown_address_answers_exactly_as_a_known_one(self, client):
        email, _, _ = _signup(client)
        known = client.post("/api/auth/password/reset", json={"email": email})
        unknown = client.post("/api/auth/password/reset",
                              json={"email": f"nobody-{uuid.uuid4().hex}@x.com"})
        assert known.status_code == unknown.status_code == 200
        assert known.get_json() == unknown.get_json()

    def test_only_the_hash_of_a_reset_token_is_stored(self, client):
        email, _, _ = _signup(client)
        issued = auth_service.begin_password_reset(email)
        assert issued is not None
        _, token = issued
        rows = persistence.database.query(
            "SELECT token_hash FROM password_resets", ())
        assert rows
        assert all(r["token_hash"] != token for r in rows), \
            "the token itself was stored, so a database read is an account takeover"

    def test_a_reset_changes_the_password_and_can_be_used_only_once(self, client):
        email, _, _ = _signup(client)
        _, token = auth_service.begin_password_reset(email)
        new_password = "A-brand-new-passphrase-1"

        res = client.post("/api/auth/password/reset/confirm",
                          json={"token": token, "password": new_password})
        assert res.status_code == 200
        assert client.post("/api/auth/login",
                           json={"email": email, "password": new_password}
                           ).status_code == 200
        assert client.post("/api/auth/login",
                           json={"email": email, "password": PASSWORD}
                           ).status_code == 401

        again = client.post("/api/auth/password/reset/confirm",
                            json={"token": token, "password": "Another-one-entirely-2"})
        assert again.status_code == 401

    def test_a_reset_signs_the_account_out_everywhere(self, client):
        email, token, _ = _signup(client)
        _, reset = auth_service.begin_password_reset(email)
        client.post("/api/auth/password/reset/confirm",
                    json={"token": reset, "password": "A-brand-new-passphrase-3"})
        client.delete_cookie(SESSION_COOKIE)
        assert client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_an_expired_token_is_refused(self, client):
        email, _, _ = _signup(client)
        _, token = auth_service.begin_password_reset(email)
        from app.backend.services.security import _token_hash
        persistence.database.execute(
            "UPDATE password_resets SET expires_at = ? WHERE token_hash = ?",
            (time.time() - 1, _token_hash(token)))
        res = client.post("/api/auth/password/reset/confirm",
                          json={"token": token, "password": "A-brand-new-passphrase-4"})
        assert res.status_code == 401

    def test_reset_requests_are_rate_limited_per_account(self, client):
        email, _, _ = _signup(client)
        issued = [auth_service.begin_password_reset(email) for _ in range(7)]
        assert sum(1 for i in issued if i is not None) == 5, \
            "an unlimited reset endpoint floods a mailbox and farms tokens"


# ===========================================================================
# Second factor
# ===========================================================================

class TestMultiFactor:

    def test_enrolment_is_not_active_until_confirmed(self, client):
        _signup(client)
        csrf = client.get_cookie(CSRF_COOKIE).value
        res = client.post("/api/auth/mfa/enrol", headers={CSRF_HEADER: csrf})
        assert res.status_code == 200
        body = res.get_json()
        assert body["secret"] and body["otpauth_uri"].startswith("otpauth://totp/")
        assert len(body["recovery_codes"]) == 10
        # Not confirmed: a mis-scanned QR that activated immediately would lock
        # the user out of their own account.
        assert client.get("/api/auth/mfa").get_json()["confirmed"] is False

    def test_a_confirmed_factor_turns_login_into_two_steps(self, client):
        email, _, _ = _signup(client)
        csrf = client.get_cookie(CSRF_COOKIE).value
        secret = client.post("/api/auth/mfa/enrol",
                             headers={CSRF_HEADER: csrf}).get_json()["secret"]
        code = totp.code_for_step(secret, totp.current_step())
        assert client.post("/api/auth/mfa/confirm", json={"code": code},
                           headers={CSRF_HEADER: csrf}).status_code == 200

        fresh = app.test_client()
        first = fresh.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        body = first.get_json()
        assert body["mfa_required"] is True
        assert "token" not in body, "a session was issued before the second factor"

        # The challenge is NOT a session.
        assert fresh.get("/api/auth/me",
                         headers={"Authorization": f"Bearer {body['mfa_token']}"}
                         ).status_code == 401

        # A code from the NEXT step. The one used to confirm enrolment has
        # already spent its own, which is the replay guard working — a real
        # sign-in happens later and reads a different code off the app.
        second = fresh.post("/api/auth/login/mfa", json={
            "mfa_token": body["mfa_token"],
            "code": totp.code_for_step(secret, totp.current_step() + 1)})
        assert second.status_code == 200
        assert second.get_json()["user"]["email"] == email

    def test_a_code_cannot_be_replayed_inside_its_own_window(self, client):
        email, _, _ = _signup(client)
        csrf = client.get_cookie(CSRF_COOKIE).value
        secret = client.post("/api/auth/mfa/enrol",
                             headers={CSRF_HEADER: csrf}).get_json()["secret"]
        code = totp.code_for_step(secret, totp.current_step())
        client.post("/api/auth/mfa/confirm", json={"code": code},
                    headers={CSRF_HEADER: csrf})

        user = auth_service._users_by_email[email]  # noqa: SLF001
        # The SAME code, still inside its 30-second window.
        assert auth_service.verify_second_factor(user, code) is False, \
            "a captured code stayed usable for the rest of its time step"

    def test_a_recovery_code_works_once(self, client):
        email, _, _ = _signup(client)
        csrf = client.get_cookie(CSRF_COOKIE).value
        enrolment = client.post("/api/auth/mfa/enrol",
                                headers={CSRF_HEADER: csrf}).get_json()
        client.post("/api/auth/mfa/confirm", headers={CSRF_HEADER: csrf}, json={
            "code": totp.code_for_step(enrolment["secret"], totp.current_step())})

        user = auth_service._users_by_email[email]  # noqa: SLF001
        recovery = enrolment["recovery_codes"][0]
        assert auth_service.verify_second_factor(user, recovery) is True
        assert auth_service.verify_second_factor(user, recovery) is False

    def test_removing_a_factor_costs_the_password(self, client):
        _signup(client)
        csrf = client.get_cookie(CSRF_COOKIE).value
        client.post("/api/auth/mfa/enrol", headers={CSRF_HEADER: csrf})
        wrong = client.delete("/api/auth/mfa", json={"password": "not-the-password"},
                              headers={CSRF_HEADER: csrf})
        assert wrong.status_code == 401
        right = client.delete("/api/auth/mfa", json={"password": PASSWORD},
                              headers={CSRF_HEADER: csrf})
        assert right.status_code == 200
        assert client.get("/api/auth/mfa").get_json()["enrolled"] is False


# ===========================================================================
# Rate limiting
# ===========================================================================

class TestRateLimiting:

    def test_the_login_endpoint_refuses_a_burst(self, client):
        limiter.reset()
        email, _, _ = _signup(client)
        statuses = [
            client.post("/api/auth/login",
                        json={"email": email, "password": "wrong-password-x"}).status_code
            for _ in range(25)
        ]
        assert 429 in statuses, "a spray across accounts never trips a per-account lock"
        assert statuses.index(429) >= 20

    def test_a_refusal_says_when_to_come_back(self, client):
        limiter.reset()
        last = None
        for _ in range(25):
            last = client.post("/api/auth/login",
                               json={"email": "x@y.com", "password": "x" * 12})
            if last.status_code == 429:
                break
        assert last.status_code == 429
        assert int(last.headers["Retry-After"]) > 0


# ===========================================================================
# Security headers
# ===========================================================================

class TestSecurityHeaders:

    def test_every_response_carries_a_content_security_policy(self, client):
        res = client.get("/api/status")
        csp = res.headers["Content-Security-Policy"]
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp.split("style-src")[0], \
            "inline SCRIPT is what the policy exists to stop"
        assert "'unsafe-eval'" not in csp
        assert "frame-ancestors 'none'" in csp
        assert "connect-src 'self'" in csp

    def test_the_usual_headers_are_present(self, client):
        res = client.get("/api/status")
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert "strict-origin" in res.headers["Referrer-Policy"]
        assert "camera=()" in res.headers["Permissions-Policy"]

    def test_api_responses_are_not_cacheable(self, client):
        res = client.get("/api/status")
        assert res.headers["Cache-Control"] == "no-store"

    def test_no_markup_or_module_carries_an_inline_event_handler(self):
        """
        `script-src 'self'` blocks inline handlers, and they cannot be nonced.
        One `onclick=` anywhere would break the page it is on.
        """
        import pathlib
        frontend = pathlib.Path(app.root_path).parent / "frontend"
        offenders = []
        for path in list(frontend.glob("*.html")) + list(frontend.glob("js/**/*.js")):
            # `actions.js` documents the attributes it replaced.
            if path.name == "actions.js":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for handler in ("onclick=", "onchange=", "onsubmit=", "onload=\""):
                if handler in text:
                    offenders.append(f"{path.name}: {handler}")
        assert not offenders, offenders

    def test_the_inlined_standalone_build_is_not_served_by_the_application(self):
        """
        The single-file build concatenates every module into inline script, so
        it can never satisfy `script-src 'self'`. It is a separate deliverable
        produced by `scripts/build_standalone.py`; served from this origin it
        would be a stale copy of the application under a policy that breaks it.
        """
        import pathlib
        served = pathlib.Path(app.root_path).parent / "frontend"
        assert not (served / "netgravity_standalone.html").exists()

    def test_no_page_asset_is_fetched_from_a_third_party(self):
        """
        Four libraries came from three CDNs. A script tag pointed at someone
        else's server is a supply chain on a page that renders client network
        data, and `connect-src 'self'` would block them anyway.
        """
        import pathlib
        front = pathlib.Path(app.root_path).parent / "frontend"
        sources = {
            path.name: _without_comments(
                path.read_text(encoding="utf-8", errors="replace"))
            for path in [front / "index.html"] + list((front / "css").glob("*.css"))
        }
        for remote in ("unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com",
                       "basemaps.cartocdn.com", "fonts.googleapis.com",
                       "fonts.gstatic.com"):
            offenders = [name for name, text in sources.items() if remote in text]
            assert not offenders, f"{offenders} still load from {remote}"


# ===========================================================================
# Schema migrations
# ===========================================================================

class TestSchemaMigrations:

    def test_the_live_schema_is_at_the_version_this_build_expects(self):
        from app.backend.services.migrations import SCHEMA_VERSION
        assert persistence.database.schema_version == SCHEMA_VERSION

    def test_every_migration_is_recorded(self):
        from app.backend.services.migrations import MIGRATIONS
        rows = persistence.database.query(
            "SELECT version, name FROM schema_migrations ORDER BY version", ())
        assert [int(r["version"]) for r in rows] == [m.version for m in MIGRATIONS]

    def test_migrations_are_numbered_uniquely_and_in_order(self):
        from app.backend.services.migrations import MIGRATIONS
        versions = [m.version for m in MIGRATIONS]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)

    def test_applying_twice_is_a_no_op(self):
        from app.backend.services.migrations import apply_migrations
        assert apply_migrations(persistence.database._backend) == []  # noqa: SLF001


class TestTheClientCanTellItHasASession:

    def test_the_readable_marker_is_the_csrf_cookie_not_the_session_one(self):
        """
        The browser client cannot test for the session cookie: it is httpOnly,
        which is the entire point of it, so `document.cookie` never contains
        it. Reading it made `restoreSession()` return early on every page load
        and a refresh dropped a signed-in user to the landing page with a
        perfectly valid session in their browser.

        `ng_csrf` is set and cleared alongside the session and IS readable, so
        it is the honest marker.
        """
        import pathlib
        client_js = (pathlib.Path(app.root_path).parent / "frontend" / "js"
                     / "integration" / "api-client.js").read_text(encoding="utf-8")
        marker = client_js.split("get hasSession()")[1].split("}")[0]
        assert "CSRF_COOKIE" in marker
        assert "SESSION_COOKIE" not in marker

    def test_both_cookies_are_issued_and_cleared_together(self, client):
        _signup(client)
        assert client.get_cookie(CSRF_COOKIE) is not None
        res = client.post("/api/auth/logout")
        cleared = " ".join(res.headers.getlist("Set-Cookie"))
        assert f"{SESSION_COOKIE}=" in cleared and f"{CSRF_COOKIE}=" in cleared


class TestExplicitTargetWins:

    def test_an_explicit_path_is_not_overridden_by_the_environment(self, monkeypatch):
        """
        `Database(path=...)` used to open the configured PostgreSQL instead of
        the file it was handed. The caller that names a specific database — a
        restore verification, a migration source — is exactly the one that
        needs to get it.
        """
        import os
        import tempfile
        import uuid as _uuid
        from app.backend.services.persistence import Database

        monkeypatch.setenv("NETGRAVITY_DATABASE_URL",
                           "postgresql://u:p@db.invalid:5432/never-reached")
        target = os.path.join(tempfile.gettempdir(),
                              f"explicit-{_uuid.uuid4().hex[:8]}.db")
        db = Database(path=target)
        try:
            assert db.kind == "sqlite"
            assert db.path == target
        finally:
            db.close()
            os.path.exists(target) and os.unlink(target)


class TestTransportSecurity:

    def test_a_remote_url_without_sslmode_gets_one(self):
        from app.backend.services.persistence import enforce_transport_security
        out = enforce_transport_security(
            "postgresql://u:p@db.example.com:5432/ng", production=False)
        assert "sslmode=require" in out

    def test_production_refuses_an_unencrypted_remote_connection(self):
        from app.backend.services.persistence import enforce_transport_security
        with pytest.raises(RuntimeError, match="unencrypted"):
            enforce_transport_security(
                "postgresql://u:p@db.example.com/ng?sslmode=disable", production=True)

    def test_a_local_socket_is_exempt(self):
        from app.backend.services.persistence import enforce_transport_security
        url = "postgresql://u:p@127.0.0.1:5432/ng"
        assert enforce_transport_security(url, production=True) == url

    def test_an_operators_explicit_choice_is_respected(self):
        from app.backend.services.persistence import enforce_transport_security
        url = "postgresql://u:p@db.example.com/ng?sslmode=verify-full"
        assert enforce_transport_security(url, production=True) == url


class TestTheAuthFormStatesThePolicyTheServerEnforces:
    """
    What the sign-up form tells a user about their password, and what
    `validate_password_strength` will accept, have to be the same claim.

    They were not. The form advertised "8+ characters", checked eight, and
    sent the result to a server whose floor is twelve — so a password the
    user had been told was fine came back rejected, and the rejection was
    appended to the bottom of a fixed-height page where it could not be
    read. These tests hold the two ends together.
    """

    @staticmethod
    def _frontend():
        import pathlib
        return pathlib.Path(app.root_path).parent / "frontend"

    @staticmethod
    def _text(*parts):
        path = TestTheAuthFormStatesThePolicyTheServerEnforces._frontend()
        for part in parts:
            path = path / part
        return path.read_text(encoding="utf-8", errors="replace")

    def test_no_auth_asset_advertises_a_floor_the_server_does_not_have(self):
        from app.backend.services.security import _MIN_PASSWORD_LENGTH
        assert _MIN_PASSWORD_LENGTH == 12
        for parts in [("index.html",), ("js", "auth.js"), ("css", "auth.css"),
                      ("css", "landing.css")]:
            text = _without_comments(self._text(*parts))
            for claim in ("8+ characters", "at least 8", "minimum 8",
                          "8 characters"):
                assert claim.lower() not in text.lower(), (parts, claim)

    def test_the_form_checks_the_same_length_the_server_refuses_below(self):
        from app.backend.services.security import _MIN_PASSWORD_LENGTH
        source = self._text("js", "auth.js")
        assert f"MIN_PASSWORD_LENGTH = {_MIN_PASSWORD_LENGTH}" in source

    def test_every_requirement_the_checklist_shows_starts_unmet(self):
        """
        The list is a rule, not a verdict. It shipped with each item already
        wearing the satisfied colour, so it agreed with an empty field.
        """
        import re
        markup = self._text("index.html")
        items = re.findall(r'<li class="auth-req-item"[^>]*>', markup)
        assert len(items) == 5, items
        assert not [i for i in items if "is-met" in i]
        for key in ("length", "upper", "lower", "digit", "symbol"):
            assert f'data-req="{key}"' in markup, key

    def test_a_password_is_confirmed_before_it_is_sent(self):
        markup = self._text("index.html")
        assert 'id="signup-password-confirm"' in markup
        assert "Confirm password" in markup

    def test_the_form_does_not_ask_for_consent_to_documents_that_do_not_exist(self):
        markup = _without_comments(self._text("index.html"))
        assert "Terms of Service" not in markup
        assert "Privacy Policy" not in markup

    def test_every_panel_carries_its_failure_slot_in_the_markup(self):
        """
        Built on demand and appended to the form, the message landed below
        the bottom of a page that is a fixed 100vh with `overflow: hidden`.
        In the markup, the reserved panel height can account for it.
        """
        markup = self._text("index.html")
        for slot in ("signin-error", "signup-error", "reset-error"):
            assert f'id="{slot}"' in markup, slot
            assert markup.count(f'id="{slot}"') == 1

    def test_the_reset_confirmation_has_an_element_to_write_the_copy_into(self):
        """
        `confirmation.textContent = ...` replaced every child of the box —
        the icon, the heading and the button back to sign-in went with it.
        """
        markup = self._text("index.html")
        assert 'id="panel-reset-conf-desc"' in markup
        source = self._text("js", "auth.js")
        assert "confirmation.textContent" not in source

    def test_the_reset_confirmation_claims_only_what_the_server_will_confirm(self):
        """
        An unknown address, a rate-limited one and a real one are
        indistinguishable at the API by design. "We've sent a password reset
        link to <address>" told the visitor which one they had.
        """
        markup = _without_comments(self._text("index.html"))
        desc = markup.split('id="panel-reset-conf-desc"', 1)[1].split("</div>", 1)[0]
        assert "If an account exists" in desc
        assert "We've sent" not in desc
        assert "panel-conf-email-target" not in markup

    def test_the_reset_panel_offers_a_way_back_to_sign_in(self):
        """Once on the form that asks for an address, and once on the screen
        that confirms it was sent — which is where it used to be destroyed."""
        markup = self._text("index.html")
        panel = markup.split('id="panel-reset"', 1)[1].split('id="landing-map-stage"', 1)[0]
        form, confirmation = panel.split('id="panel-reset-confirmation"', 1)
        assert "Back to sign in" in form
        assert "Back to sign in" in confirmation
        for half in (form, confirmation):
            assert 'data-arg="signin"' in half

    def test_only_one_handler_answers_the_reset_form(self):
        """
        A second handler in landing.js hid the form and showed "Check your
        email" before the request was made and without reading its result,
        so a refused reset reported success — and the failure message was
        written into the form it had just hidden.
        """
        source = self._text("js", "landing.js")
        body = source.split("function bindLandingEvents", 1)[1]
        body = _without_comments(body)
        assert "panel-reset-confirmation" not in body

    def test_accounts_are_offered_only_for_the_work_domain(self):
        """
        A guard on the form, not on the API — `POST /api/auth/signup` still
        accepts any valid address, and this asserts only what the form does.
        """
        source = self._text("js", "auth.js")
        assert "ALLOWED_EMAIL_DOMAIN = 'kearney.com'" in source
        assert 'placeholder="you@kearney.com"' in self._text("index.html")

    def test_the_form_never_accepts_what_the_server_would_refuse(self):
        """
        The form's composition rules are its own; what matters is the
        direction. Anything it lets through must clear the server's floor.
        """
        from app.backend.services.security import validate_password_strength
        for accepted in ("Netgravity@2026", "Abcdefghij1!", "Zx9!qwertyuiop"):
            validate_password_strength(accepted)   # raises if it would not
