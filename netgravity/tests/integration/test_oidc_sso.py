"""
Single sign-on: the token verification, and the attacks it has to refuse.

`app/backend/services/jwt_verify.py` is hand-written because no JWT library is
installed here. Hand-written JWT verification is where token bugs live, so most
of this module is the attack cases rather than the happy path — a verifier that
accepts a correct token and also accepts `alg: none` has not verified anything.

The four classic failures, each with a test:

  1. `alg: none` — a token asserting it is unsigned.
  2. Algorithm confusion — a token signed HS256 with the provider's PUBLIC key
     bytes as the HMAC secret.
  3. Unverified claims — a real signature on a token minted for someone else,
     expired, or replayed into the wrong session.
  4. Key substitution — a token carrying its own key material, or naming a key
     the issuer does not publish.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest

from app.backend.services.jwt_verify import TokenError, verify_id_token

ISSUER = "https://login.example.com"
CLIENT_ID = "netgravity-test-client"
NONCE = "test-nonce-value"


# ---------------------------------------------------------------------------
# Test key material
# ---------------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="module")
def rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwks(rsa_key):
    numbers = rsa_key.public_key().public_numbers()
    return {"keys": [{
        "kty": "RSA", "kid": "test-key-1", "use": "sig", "alg": "RS256",
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }]}


def sign_rs256(rsa_key, header: dict, claims: dict) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(claims, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode("ascii")
    signature = rsa_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return ".".join(segments + [_b64url(signature)])


def valid_claims(**overrides) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": CLIENT_ID, "sub": "provider-subject-42",
        "exp": now + 300, "iat": now, "nonce": NONCE,
        "email": "person@example.com", "email_verified": True,
    }
    claims.update(overrides)
    return claims


def verify(token, jwks, **kwargs):
    params = {"jwks": jwks, "issuer": ISSUER, "audience": CLIENT_ID,
              "nonce": NONCE}
    params.update(kwargs)
    return verify_id_token(token, **params)


# ===========================================================================

class TestAValidTokenIsAccepted:

    def test_a_correctly_signed_token_verifies(self, rsa_key, jwks):
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims())
        claims = verify(token, jwks)
        assert claims["sub"] == "provider-subject-42"
        assert claims["email"] == "person@example.com"

    def test_an_audience_list_containing_this_client_verifies(self, rsa_key, jwks):
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(aud=["someone-else", CLIENT_ID]))
        assert verify(token, jwks)["sub"] == "provider-subject-42"

    def test_a_token_with_no_kid_verifies_against_the_only_key(self, rsa_key, jwks):
        token = sign_rs256(rsa_key, {"alg": "RS256"}, valid_claims())
        assert verify(token, jwks)["sub"] == "provider-subject-42"


class TestAlgNoneIsRefused:
    """A token asserting it is unsigned."""

    def test_alg_none_with_an_empty_signature(self, jwks):
        header = _b64url(json.dumps({"alg": "none", "kid": "test-key-1"}).encode())
        payload = _b64url(json.dumps(valid_claims()).encode())
        token = f"{header}.{payload}."
        with pytest.raises(TokenError, match="not accepted"):
            verify(token, jwks)

    def test_alg_none_uppercase_is_also_refused(self, jwks):
        header = _b64url(json.dumps({"alg": "NONE"}).encode())
        payload = _b64url(json.dumps(valid_claims()).encode())
        with pytest.raises(TokenError, match="not accepted"):
            verify(f"{header}.{payload}.", jwks)


class TestAlgorithmConfusionIsRefused:
    """
    The attack: take the provider's PUBLIC key, sign a token you wrote with
    HS256 using that key's bytes as the HMAC secret. A verifier that reads
    `alg` from the token and looks up "the key" accepts it, because the public
    key is exactly what it has.
    """

    def test_hs256_signed_with_the_public_key_is_refused(self, rsa_key, jwks):
        from cryptography.hazmat.primitives import serialization

        public_pem = rsa_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo)

        header = _b64url(json.dumps({"alg": "HS256", "kid": "test-key-1"}).encode())
        payload = _b64url(json.dumps(valid_claims(sub="attacker")).encode())
        signing_input = f"{header}.{payload}".encode("ascii")
        forged = _b64url(hmac.new(public_pem, signing_input, hashlib.sha256).digest())

        with pytest.raises(TokenError, match="not accepted"):
            verify(f"{header}.{payload}.{forged}", jwks)

    def test_no_hmac_algorithm_is_in_the_allowlist(self):
        from app.backend.services.jwt_verify import _ALLOWED_ALGORITHMS
        assert not any(a.startswith("HS") for a in _ALLOWED_ALGORITHMS)
        assert "none" not in {a.lower() for a in _ALLOWED_ALGORITHMS}


class TestKeySubstitutionIsRefused:

    def test_a_token_carrying_its_own_key_is_refused(self, jwks):
        """
        A `jwk` in the header is the attacker supplying the key their own
        signature verifies against. Key material comes from the issuer's JWKS
        or from nowhere.
        """
        header = _b64url(json.dumps({
            "alg": "RS256", "kid": "x",
            "jwk": {"kty": "RSA", "n": "AQAB", "e": "AQAB"}}).encode())
        payload = _b64url(json.dumps(valid_claims()).encode())
        with pytest.raises(TokenError, match="key material must come"):
            verify(f"{header}.{payload}.sig", jwks)

    @pytest.mark.parametrize("field", ["jku", "x5c", "x5u"])
    def test_every_key_smuggling_header_is_refused(self, jwks, field):
        header = _b64url(json.dumps(
            {"alg": "RS256", field: "https://attacker.example/keys"}).encode())
        payload = _b64url(json.dumps(valid_claims()).encode())
        with pytest.raises(TokenError, match="key material must come"):
            verify(f"{header}.{payload}.sig", jwks)

    def test_an_unknown_kid_is_refused(self, rsa_key, jwks):
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "not-published"},
                           valid_claims())
        with pytest.raises(TokenError, match="no key in the issuer's JWKS"):
            verify(token, jwks)

    def test_a_signature_from_a_different_key_is_refused(self, jwks):
        from cryptography.hazmat.primitives.asymmetric import rsa
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = sign_rs256(other, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims())
        with pytest.raises(TokenError, match="signature does not verify"):
            verify(token, jwks)

    def test_an_empty_jwks_is_refused(self, rsa_key):
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims())
        with pytest.raises(TokenError, match="no keys"):
            verify(token, {"keys": []})


class TestClaimsAreVerifiedNotJustTheSignature:
    """
    A valid signature says who wrote the token, not that it was written for us.
    """

    def test_a_token_for_a_different_audience_is_refused(self, rsa_key, jwks):
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(aud="some-other-application"))
        with pytest.raises(TokenError, match="does not contain this client"):
            verify(token, jwks)

    def test_a_token_from_a_different_issuer_is_refused(self, rsa_key, jwks):
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(iss="https://attacker.example"))
        with pytest.raises(TokenError, match="iss is"):
            verify(token, jwks)

    def test_an_azp_naming_another_client_is_refused(self, rsa_key, jwks):
        """
        `azp` names the party a multi-audience token was issued TO. If it is
        someone else, the token merely mentions us.
        """
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(aud=[CLIENT_ID, "other"], azp="other"))
        with pytest.raises(TokenError, match="azp is"):
            verify(token, jwks)

    def test_an_expired_token_is_refused(self, rsa_key, jwks):
        now = int(time.time())
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(exp=now - 3600, iat=now - 7200))
        with pytest.raises(TokenError, match="expired"):
            verify(token, jwks)

    def test_a_token_missing_exp_is_refused(self, rsa_key, jwks):
        claims = valid_claims()
        claims.pop("exp")
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"}, claims)
        with pytest.raises(TokenError, match="exp is missing"):
            verify(token, jwks)

    def test_a_token_issued_in_the_future_is_refused(self, rsa_key, jwks):
        now = int(time.time())
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(iat=now + 3600, exp=now + 7200))
        with pytest.raises(TokenError, match="issued in the future"):
            verify(token, jwks)

    def test_an_ancient_token_is_refused_even_with_a_long_exp(self, rsa_key, jwks):
        """
        A provider issuing ID tokens that live for days is a provider whose
        tokens must not live for days here.
        """
        now = int(time.time())
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(iat=now - 86400, exp=now + 86400))
        with pytest.raises(TokenError, match="older than"):
            verify(token, jwks)

    def test_a_token_missing_sub_is_refused(self, rsa_key, jwks):
        claims = valid_claims()
        claims.pop("sub")
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"}, claims)
        with pytest.raises(TokenError, match="sub is missing"):
            verify(token, jwks)

    def test_a_replayed_token_from_another_session_is_refused(self, rsa_key, jwks):
        """
        The nonce binds a token to OUR authorization request. Without the check,
        a token legitimately issued for a different session of the same client
        replays into this one.
        """
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(nonce="a-different-sessions-nonce"))
        with pytest.raises(TokenError, match="nonce does not match"):
            verify(token, jwks)

    def test_a_token_with_no_nonce_is_refused_when_one_was_sent(self, rsa_key, jwks):
        claims = valid_claims()
        claims.pop("nonce")
        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"}, claims)
        with pytest.raises(TokenError, match="the token has none"):
            verify(token, jwks)


class TestMalformedTokensAreRefusedNotGuessed:

    @pytest.mark.parametrize("token", [
        "", "not-a-token", "a.b", "a.b.c.d", "....",
    ])
    def test_a_malformed_token_is_refused(self, jwks, token):
        with pytest.raises(TokenError):
            verify(token, jwks)

    def test_a_non_json_payload_is_refused(self, jwks):
        header = _b64url(json.dumps({"alg": "RS256"}).encode())
        with pytest.raises(TokenError):
            verify(f"{header}.{_b64url(b'not json')}.sig", jwks)


# ===========================================================================
# The flow's own decisions
# ===========================================================================

class TestConfigurationAndProviderMetadata:

    def test_sso_is_off_with_no_issuer_configured(self, monkeypatch):
        from app.backend.services.oidc import load_config
        for key in ("NETGRAVITY_OIDC_ISSUER", "NETGRAVITY_OIDC_CLIENT_ID",
                    "NETGRAVITY_OIDC_REDIRECT_URI"):
            monkeypatch.delenv(key, raising=False)
        assert load_config().enabled is False

    def test_a_discovery_document_declaring_another_issuer_is_refused(self, monkeypatch):
        """
        If the document may name any issuer it likes, the `iss` check on the
        token becomes a check against the attacker's own value.
        """
        from app.backend.services import oidc

        monkeypatch.setattr(oidc, "_fetch_json",
                            lambda url, what: {"issuer": "https://attacker.example",
                                               "authorization_endpoint": "x",
                                               "token_endpoint": "y",
                                               "jwks_uri": "z"})
        oidc._cache.clear()
        config = oidc.OIDCConfig(
            issuer=ISSUER, client_id=CLIENT_ID, client_secret="", redirect_uri="r",
            scopes="openid", provider_name="P", allowed_domains=(),
            auto_provision=False)
        with pytest.raises(oidc.OIDCError, match="declares issuer"):
            oidc.discovery(config)
        oidc._cache.clear()

    def test_metadata_is_not_fetched_over_plain_http(self, monkeypatch):
        from app.backend.services import oidc
        oidc._cache.clear()
        with pytest.raises(oidc.OIDCError, match="must be fetched over HTTPS"):
            oidc._fetch_json("http://provider.example/.well-known/openid-configuration",
                             what="the OIDC discovery document")


class TestTheAuthorizationRequest:

    def _config(self, **overrides):
        from app.backend.services.oidc import OIDCConfig
        base = dict(issuer=ISSUER, client_id=CLIENT_ID, client_secret="",
                    redirect_uri="https://app.example.com/api/auth/oidc/callback",
                    scopes="openid email profile", provider_name="P",
                    allowed_domains=(), auto_provision=False)
        base.update(overrides)
        return OIDCConfig(**base)

    def _patched(self, monkeypatch):
        from app.backend.services import oidc
        monkeypatch.setattr(oidc, "discovery", lambda config: {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
        })
        return oidc

    def test_pkce_is_always_s256(self, monkeypatch):
        import urllib.parse
        oidc = self._patched(monkeypatch)
        url, pending = oidc.begin_authorization(self._config())
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"][0]
        assert pending["code_verifier"]
        # The challenge must actually be the SHA-256 of the verifier, or PKCE is
        # decoration.
        expected = _b64url(hashlib.sha256(
            pending["code_verifier"].encode("ascii")).digest())
        assert query["code_challenge"][0] == expected

    def test_state_and_nonce_are_long_and_distinct(self, monkeypatch):
        oidc = self._patched(monkeypatch)
        _, first = oidc.begin_authorization(self._config())
        _, second = oidc.begin_authorization(self._config())
        assert len(first["state"]) >= 32 and len(first["nonce"]) >= 32
        assert first["state"] != second["state"]
        assert first["nonce"] != second["nonce"]
        assert first["state"] != first["nonce"]

    @pytest.mark.parametrize("hostile", [
        "https://attacker.example/steal", "//attacker.example/steal",
        "http://attacker.example",
    ])
    def test_an_absolute_next_url_is_refused(self, monkeypatch, hostile):
        """
        An attacker-supplied redirect target is an open redirect that borrows
        this application's domain to look trustworthy.
        """
        oidc = self._patched(monkeypatch)
        _, pending = oidc.begin_authorization(self._config(), next_path=hostile)
        assert pending["next"] == "/"

    def test_a_local_next_path_is_kept(self, monkeypatch):
        oidc = self._patched(monkeypatch)
        _, pending = oidc.begin_authorization(
            self._config(), next_path="/projects/abc")
        assert pending["next"] == "/projects/abc"

    def test_sso_that_is_not_configured_refuses_to_start(self):
        from app.backend.services.oidc import OIDCError, begin_authorization
        with pytest.raises(OIDCError, match="not configured"):
            begin_authorization(self._config(issuer="", client_id=""))


class TestEmailIsNotTrustedBlindly:

    def _config(self, **overrides):
        from app.backend.services.oidc import OIDCConfig
        base = dict(issuer=ISSUER, client_id=CLIENT_ID, client_secret="",
                    redirect_uri="r", scopes="openid", provider_name="P",
                    allowed_domains=(), auto_provision=False)
        base.update(overrides)
        return OIDCConfig(**base)

    def test_a_verified_address_is_usable(self):
        from app.backend.services.oidc import email_from_claims
        email, problems = email_from_claims(
            self._config(), {"email": "Person@Example.com",
                             "email_verified": True})
        assert email == "person@example.com"
        assert problems == []

    def test_an_unverified_address_is_refused(self):
        """
        A provider that lets a user type any address into their profile and
        asserts it unverified lets a user claim someone else's account — and
        this address is what an existing local account is linked on.
        """
        from app.backend.services.oidc import email_from_claims
        email, problems = email_from_claims(
            self._config(), {"email": "ceo@client.com", "email_verified": False})
        assert email == ""
        assert any("NOT verified" in p for p in problems)

    def test_a_domain_outside_the_allowlist_is_refused(self):
        from app.backend.services.oidc import email_from_claims
        email, problems = email_from_claims(
            self._config(allowed_domains=("example.com",)),
            {"email": "person@elsewhere.com", "email_verified": True})
        assert email == ""
        assert any("allowed sign-in domains" in p for p in problems)

    def test_no_address_at_all_is_reported(self):
        from app.backend.services.oidc import email_from_claims
        email, problems = email_from_claims(self._config(), {})
        assert email == ""
        assert problems


class TestFederatedAccounts:

    def test_a_federated_account_has_no_password_to_guess(self):
        from app.backend.services.security import auth_service, verify_password

        user = auth_service.register_federated(
            email=f"sso-{int(time.time() * 1000)}@example.com", name="SSO User")
        assert user.password_hash == ""
        # Nothing may authenticate against an empty hash — not the empty string,
        # not the stored value itself.
        assert verify_password("", "") is False
        assert verify_password("anything", user.password_hash) is False

    def test_registering_the_same_federated_address_twice_returns_one_account(self):
        from app.backend.services.security import auth_service

        email = f"sso-dup-{int(time.time() * 1000)}@example.com"
        first = auth_service.register_federated(email=email)
        second = auth_service.register_federated(email=email)
        assert first.user_id == second.user_id

    def test_a_link_is_keyed_on_issuer_and_subject(self):
        """
        Never on e-mail. An e-mail is an assertion a provider makes; the subject
        is the identity it is making the assertion about. Matching on e-mail
        means any provider that will assert an address takes over the account
        under it.
        """
        from app.backend.services import persistence

        stamp = int(time.time() * 1000)
        persistence.link_federated_identity(
            "https://a.example", f"subject-{stamp}", "usr_a",
            "shared@example.com", time.time())
        persistence.link_federated_identity(
            "https://b.example", f"subject-{stamp}", "usr_b",
            "shared@example.com", time.time())
        try:
            a = persistence.find_federated_identity("https://a.example",
                                                    f"subject-{stamp}")
            b = persistence.find_federated_identity("https://b.example",
                                                    f"subject-{stamp}")
            assert a["user_id"] == "usr_a"
            assert b["user_id"] == "usr_b", \
                "the same subject at a DIFFERENT issuer is a different identity"
        finally:
            persistence.unlink_federated_identity("https://a.example",
                                                  f"subject-{stamp}")
            persistence.unlink_federated_identity("https://b.example",
                                                  f"subject-{stamp}")

    def test_relinking_updates_last_seen_rather_than_duplicating(self):
        from app.backend.services import persistence

        stamp = int(time.time() * 1000)
        subject = f"repeat-{stamp}"
        persistence.link_federated_identity(
            "https://a.example", subject, "usr_x", "x@example.com", 1000.0)
        persistence.link_federated_identity(
            "https://a.example", subject, "usr_x", "x@example.com", 2000.0)
        try:
            rows = persistence.federated_identities_for("usr_x")
            matching = [r for r in rows if str(r["subject"]) == subject]
            assert len(matching) == 1
            assert float(matching[0]["last_seen"]) == pytest.approx(2000.0)
        finally:
            persistence.unlink_federated_identity("https://a.example", subject)


class TestTheProvidersEndpoint:

    def test_it_says_sso_is_off_when_nothing_is_configured(self, monkeypatch):
        from app.backend.app import app

        for key in ("NETGRAVITY_OIDC_ISSUER", "NETGRAVITY_OIDC_CLIENT_ID",
                    "NETGRAVITY_OIDC_REDIRECT_URI"):
            monkeypatch.delenv(key, raising=False)
        with app.test_client() as client:
            response = client.get("/api/auth/oidc/providers")
        assert response.status_code == 200
        body = response.get_json()
        assert body["enabled"] is False
        assert body["providers"] == []
        assert "NETGRAVITY_OIDC_ISSUER" in body["reason"]

    def test_it_advertises_a_configured_provider(self, monkeypatch):
        from app.backend.app import app

        monkeypatch.setenv("NETGRAVITY_OIDC_ISSUER", ISSUER)
        monkeypatch.setenv("NETGRAVITY_OIDC_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("NETGRAVITY_OIDC_REDIRECT_URI",
                           "https://app.example.com/api/auth/oidc/callback")
        monkeypatch.setenv("NETGRAVITY_OIDC_PROVIDER_NAME", "Company SSO")
        with app.test_client() as client:
            response = client.get("/api/auth/oidc/providers")
        body = response.get_json()
        assert body["enabled"] is True
        assert body["providers"][0]["name"] == "Company SSO"
        assert body["providers"][0]["issuer"] == ISSUER
        # No secret may ever appear on this endpoint.
        assert "secret" not in json.dumps(body).lower()

    def test_a_forged_callback_state_is_refused(self, monkeypatch):
        from app.backend.app import app

        monkeypatch.setenv("NETGRAVITY_OIDC_ISSUER", ISSUER)
        monkeypatch.setenv("NETGRAVITY_OIDC_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("NETGRAVITY_OIDC_REDIRECT_URI",
                           "https://app.example.com/api/auth/oidc/callback")
        monkeypatch.setenv("NETGRAVITY_DISABLE_RATE_LIMIT", "1")
        with app.test_client() as client:
            response = client.get(
                "/api/auth/oidc/callback?state=never-issued&code=whatever")
        assert response.status_code == 302
        assert "sso_error=unknown_or_used_state" in response.headers["Location"]

    def test_a_state_is_single_use(self, monkeypatch):
        """
        A `state` that can be presented twice is a replayable callback.
        """
        from app.backend.api.oidc import _store_pending, _take_pending

        _store_pending("single-use-test", {"nonce": "n", "code_verifier": "v",
                                           "next": "/", "expires_at": 1e12})
        assert _take_pending("single-use-test") is not None
        assert _take_pending("single-use-test") is None

# ===========================================================================
# The whole flow, against a fake provider
# ===========================================================================

class TestTheFlowEndToEnd:
    """
    `/start` -> provider -> `/callback` -> a real NetGravity session.

    The provider is faked at the two points where this application talks to it
    — metadata and the token endpoint — and nothing else is stubbed. The state
    store, the ID token verification, the account linking and the session issue
    are all the real ones, because those are what could be wrong.
    """

    @pytest.fixture(autouse=True)
    def configured(self, monkeypatch, rsa_key, jwks):
        from app.backend.services import oidc

        monkeypatch.setenv("NETGRAVITY_OIDC_ISSUER", ISSUER)
        monkeypatch.setenv("NETGRAVITY_OIDC_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("NETGRAVITY_OIDC_REDIRECT_URI",
                           "https://app.example.com/api/auth/oidc/callback")
        monkeypatch.setenv("NETGRAVITY_OIDC_PROVIDER_NAME", "Fake SSO")
        monkeypatch.setenv("NETGRAVITY_DISABLE_RATE_LIMIT", "1")

        oidc._cache.clear()
        monkeypatch.setattr(oidc, "discovery", lambda config: {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
        })
        monkeypatch.setattr(oidc, "jwks", lambda config, force=False: jwks)
        yield
        oidc._cache.clear()

    def _start(self, client):
        """Begin a flow and return the state and nonce the app minted."""
        import urllib.parse
        response = client.get("/api/auth/oidc/start")
        assert response.status_code == 302
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(response.headers["Location"]).query)
        return query["state"][0], query["nonce"][0]

    def _issue(self, monkeypatch, rsa_key, nonce, **claim_overrides):
        """Make the fake provider hand back a signed ID token."""
        from app.backend.api import oidc as oidc_api

        token = sign_rs256(rsa_key, {"alg": "RS256", "kid": "test-key-1"},
                           valid_claims(nonce=nonce, **claim_overrides))
        monkeypatch.setattr(oidc_api, "exchange_code",
                            lambda config, code, code_verifier: {"id_token": token})
        return token

    def test_a_new_user_is_refused_when_provisioning_is_off(
            self, monkeypatch, rsa_key):
        from app.backend.app import app

        monkeypatch.delenv("NETGRAVITY_OIDC_AUTO_PROVISION", raising=False)
        stamp = int(time.time() * 1000)
        with app.test_client() as client:
            state, nonce = self._start(client)
            self._issue(monkeypatch, rsa_key, nonce,
                        email=f"stranger-{stamp}@example.com",
                        sub=f"stranger-{stamp}")
            response = client.get(
                f"/api/auth/oidc/callback?state={state}&code=abc")
        assert response.status_code == 302
        assert "no_account_and_provisioning_disabled" in response.headers["Location"]

    def test_auto_provisioning_creates_an_account_and_a_session(
            self, monkeypatch, rsa_key):
        from app.backend.app import app
        from app.backend.services import persistence
        from app.backend.services.security import auth_service

        monkeypatch.setenv("NETGRAVITY_OIDC_AUTO_PROVISION", "1")
        stamp = int(time.time() * 1000)
        email = f"provisioned-{stamp}@example.com"
        subject = f"sub-{stamp}"

        with app.test_client() as client:
            state, nonce = self._start(client)
            self._issue(monkeypatch, rsa_key, nonce, email=email, sub=subject)
            response = client.get(
                f"/api/auth/oidc/callback?state={state}&code=abc")

            assert response.status_code == 302
            assert "sso_error" not in response.headers["Location"]
            # A real session cookie, the same one a password login issues.
            cookies = response.headers.getlist("Set-Cookie")
            assert any("ng_session=" in c for c in cookies), cookies
            assert any("HttpOnly" in c for c in cookies if "ng_session=" in c)
            assert any("ng_csrf=" in c for c in cookies), \
                "the readable CSRF marker must be set alongside"

            # And it actually authenticates.
            me = client.get("/api/auth/me")
            assert me.status_code == 200
            assert me.get_json()["user"]["email"] == email

        try:
            user = auth_service.find_by_email(email)
            assert user is not None
            assert user.password_hash == "", \
                "a federated account must have no password to guess"
            link = persistence.find_federated_identity(ISSUER, subject)
            assert link is not None and link["user_id"] == user.user_id
        finally:
            persistence.unlink_federated_identity(ISSUER, subject)

    def test_a_second_sign_in_reuses_the_link_rather_than_the_email(
            self, monkeypatch, rsa_key):
        """
        Once linked, the account is found by (issuer, subject). A provider that
        later asserts a DIFFERENT e-mail for the same subject — a rename, a
        marriage, a domain migration — must still reach the same account.
        """
        from app.backend.app import app
        from app.backend.services import persistence
        from app.backend.services.security import auth_service

        monkeypatch.setenv("NETGRAVITY_OIDC_AUTO_PROVISION", "1")
        stamp = int(time.time() * 1000)
        subject = f"renamed-{stamp}"
        first_email = f"before-{stamp}@example.com"

        with app.test_client() as client:
            state, nonce = self._start(client)
            self._issue(monkeypatch, rsa_key, nonce,
                        email=first_email, sub=subject)
            client.get(f"/api/auth/oidc/callback?state={state}&code=abc")

        user = auth_service.find_by_email(first_email)
        assert user is not None

        try:
            with app.test_client() as client:
                state, nonce = self._start(client)
                self._issue(monkeypatch, rsa_key, nonce,
                            email=f"after-{stamp}@example.com", sub=subject)
                response = client.get(
                    f"/api/auth/oidc/callback?state={state}&code=abc")
                assert response.status_code == 302
                me = client.get("/api/auth/me")
                assert me.get_json()["user"]["email"] == first_email, \
                    "the link, not the new e-mail, decides which account this is"
        finally:
            persistence.unlink_federated_identity(ISSUER, subject)

    def test_an_existing_local_account_is_linked_not_duplicated(
            self, monkeypatch, rsa_key):
        from app.backend.app import app
        from app.backend.services import persistence
        from app.backend.services.security import auth_service

        monkeypatch.delenv("NETGRAVITY_OIDC_AUTO_PROVISION", raising=False)
        stamp = int(time.time() * 1000)
        email = f"local-{stamp}@example.com"
        subject = f"local-sub-{stamp}"
        local = auth_service.register(email=email, password="netgravity-local-123",
                                      name="Local User")

        try:
            with app.test_client() as client:
                state, nonce = self._start(client)
                self._issue(monkeypatch, rsa_key, nonce, email=email, sub=subject)
                response = client.get(
                    f"/api/auth/oidc/callback?state={state}&code=abc")
                assert response.status_code == 302
                assert "sso_error" not in response.headers["Location"], \
                    "an existing account must be linked even without provisioning"
                me = client.get("/api/auth/me")
                # `User.public()` projects the id as "id"; the hash never leaves
                # the class, and neither does the internal field name.
                assert me.get_json()["user"]["id"] == local.user_id
            link = persistence.find_federated_identity(ISSUER, subject)
            assert link["user_id"] == local.user_id
        finally:
            persistence.unlink_federated_identity(ISSUER, subject)

    def test_an_unverified_email_cannot_take_over_a_local_account(
            self, monkeypatch, rsa_key):
        """
        The attack the linking rule exists for: a provider asserting an address
        it has not verified, to reach an account that already exists under it.
        """
        from app.backend.app import app
        from app.backend.services.security import auth_service

        stamp = int(time.time() * 1000)
        email = f"victim-{stamp}@example.com"
        auth_service.register(email=email, password="netgravity-victim-123")

        with app.test_client() as client:
            state, nonce = self._start(client)
            self._issue(monkeypatch, rsa_key, nonce, email=email,
                        email_verified=False, sub=f"attacker-{stamp}")
            response = client.get(
                f"/api/auth/oidc/callback?state={state}&code=abc")
        assert "sso_error=email_not_usable" in response.headers["Location"]

    def test_a_replayed_callback_is_refused(self, monkeypatch, rsa_key):
        from app.backend.app import app
        from app.backend.services import persistence

        monkeypatch.setenv("NETGRAVITY_OIDC_AUTO_PROVISION", "1")
        stamp = int(time.time() * 1000)
        subject = f"replay-{stamp}"
        with app.test_client() as client:
            state, nonce = self._start(client)
            self._issue(monkeypatch, rsa_key, nonce,
                        email=f"replay-{stamp}@example.com", sub=subject)
            first = client.get(f"/api/auth/oidc/callback?state={state}&code=abc")
            second = client.get(f"/api/auth/oidc/callback?state={state}&code=abc")
        try:
            assert "sso_error" not in first.headers["Location"]
            assert "unknown_or_used_state" in second.headers["Location"], \
                "a state that can be presented twice is a replayable callback"
        finally:
            persistence.unlink_federated_identity(ISSUER, subject)

    def test_a_token_for_another_session_cannot_be_used_here(
            self, monkeypatch, rsa_key):
        """The nonce check, exercised through the endpoint rather than the unit."""
        from app.backend.app import app

        monkeypatch.setenv("NETGRAVITY_OIDC_AUTO_PROVISION", "1")
        with app.test_client() as client:
            state, _ = self._start(client)
            # Signed correctly, but for a DIFFERENT authorization request.
            self._issue(monkeypatch, rsa_key, "some-other-sessions-nonce",
                        email=f"crossed-{int(time.time()*1000)}@example.com")
            response = client.get(
                f"/api/auth/oidc/callback?state={state}&code=abc")
        assert "sso_error=token_verification_failed" in response.headers["Location"]

    def test_the_provider_declining_is_reported_not_swallowed(self, monkeypatch):
        from app.backend.app import app

        with app.test_client() as client:
            response = client.get(
                "/api/auth/oidc/callback?error=access_denied"
                "&error_description=User+cancelled")
        assert "sso_error=provider_declined" in response.headers["Location"]

    def test_the_provider_error_description_never_reaches_the_url(
            self, monkeypatch):
        """
        A provider's error body can carry the authorization code. It is logged,
        never put in a URL, a browser history or an access log.
        """
        from app.backend.app import app

        with app.test_client() as client:
            response = client.get(
                "/api/auth/oidc/callback?error=server_error"
                "&error_description=code+SECRETVALUE+failed")
        assert "SECRETVALUE" not in response.headers["Location"]
