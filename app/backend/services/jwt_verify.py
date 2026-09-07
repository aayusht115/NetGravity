"""
NetGravity — ID token verification
==================================
Verifies a signed JWT against a JWKS, with an explicit algorithm allowlist.

Why this is written here rather than imported
--------------------------------------------
No JWT library is installed in this environment, and `cryptography` is (it comes
with psycopg). Adding a dependency to a deployment is a decision for whoever
runs it; this module is written so that decision is not forced, and so that
`verify_id_token` can be swapped for PyJWT's `decode` without any caller
changing — the signature and the exception are the same shape.

If you are adding dependencies to this project, prefer PyJWT or Authlib. A
maintained library is better than a correct one you have to keep correct.

The attacks this is written against
-----------------------------------
Hand-written JWT verification is where token bugs live, and they are the same
four every time:

1. **`alg: none`.** A token that says it is unsigned. Refused: the algorithm
   must be in `_ALLOWED_ALGORITHMS`, which contains only asymmetric ones.
2. **Algorithm confusion.** An attacker takes the provider's PUBLIC key,
   signs a token they wrote with HS256 using that key's bytes as the HMAC
   secret, and a naive verifier that reads `alg` from the token and looks up
   "the key" accepts it. Refused for the same reason: no HMAC algorithm is
   allowed, ever, because these tokens are always asymmetrically signed.
3. **Unverified claims.** A signature that is valid proves who wrote the token,
   not that it was written for you. `iss`, `aud`, `exp` and `nonce` are all
   checked, and a missing claim is a failure rather than a skipped check.
4. **Key substitution.** The `kid` must name a key in the issuer's JWKS. A
   token carrying its own key material, or naming a key that is not there, is
   refused rather than trusted.

Everything here raises `TokenError` on any failure. There is deliberately no
path that returns claims with a warning attached: a partially verified identity
token is not an identity.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: Signature algorithms this verifier will accept.
#:
#: Asymmetric only, and stated as a closed set rather than read from the token.
#: An OIDC ID token is signed by the provider with a key whose public half is
#: published; there is no legitimate case for an HMAC algorithm here, and
#: allowing one is the algorithm-confusion attack.
_ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512",
                                 "ES256", "ES384", "ES512",
                                 "PS256", "PS384", "PS512"})

#: Clock skew tolerated on `exp`, `iat` and `nbf`. Small: a token is minutes
#: long, and a generous leeway is an extension of a stolen token's life.
_LEEWAY_SECONDS = 60

#: Refuse a token whose `iat` is further in the past than this even if `exp`
#: allows it. A provider that issues long-lived ID tokens is a provider whose
#: tokens should not be long-lived here.
_MAX_TOKEN_AGE_SECONDS = 3600


class TokenError(Exception):
    """A token that could not be verified. Never carries partial claims."""


def _b64url_decode(segment: str) -> bytes:
    """Base64url without padding, as JWT uses it."""
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as exc:  # noqa: BLE001
        raise TokenError(f"malformed base64url segment: {exc}") from exc


def _json_segment(segment: str, what: str) -> Dict[str, Any]:
    try:
        value = json.loads(_b64url_decode(segment))
    except json.JSONDecodeError as exc:
        raise TokenError(f"{what} is not JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise TokenError(f"{what} is not a JSON object")
    return value


def _int_to_bytes(value: int) -> bytes:
    length = max(1, (value.bit_length() + 7) // 8)
    return value.to_bytes(length, "big")


def _rsa_key_from_jwk(jwk: Dict[str, Any]):
    from cryptography.hazmat.primitives.asymmetric import rsa

    try:
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    except KeyError as exc:
        raise TokenError(f"RSA JWK is missing {exc}") from exc
    return rsa.RSAPublicNumbers(e, n).public_key()


def _ec_key_from_jwk(jwk: Dict[str, Any]):
    from cryptography.hazmat.primitives.asymmetric import ec

    curves = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1(),
              "P-521": ec.SECP521R1()}
    curve = curves.get(str(jwk.get("crv")))
    if curve is None:
        raise TokenError(f"unsupported EC curve {jwk.get('crv')!r}")
    try:
        x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
    except KeyError as exc:
        raise TokenError(f"EC JWK is missing {exc}") from exc
    return ec.EllipticCurvePublicNumbers(x, y, curve).public_key()


def public_key_from_jwk(jwk: Dict[str, Any]):
    """A `cryptography` public key from one JWKS entry."""
    kty = str(jwk.get("kty") or "")
    if kty == "RSA":
        return _rsa_key_from_jwk(jwk)
    if kty == "EC":
        return _ec_key_from_jwk(jwk)
    raise TokenError(f"unsupported key type {kty!r}")


def _verify_signature(algorithm: str, key: Any, signed: bytes,
                      signature: bytes) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    digests = {"256": hashes.SHA256(), "384": hashes.SHA384(),
               "512": hashes.SHA512()}
    digest = digests.get(algorithm[-3:])
    if digest is None:
        raise TokenError(f"unsupported digest for {algorithm}")

    try:
        if algorithm.startswith("RS"):
            if not isinstance(key, rsa.RSAPublicKey):
                raise TokenError(f"{algorithm} needs an RSA key")
            key.verify(signature, signed, padding.PKCS1v15(), digest)
        elif algorithm.startswith("PS"):
            if not isinstance(key, rsa.RSAPublicKey):
                raise TokenError(f"{algorithm} needs an RSA key")
            key.verify(signature, signed,
                       padding.PSS(mgf=padding.MGF1(digest),
                                   salt_length=padding.PSS.DIGEST_LENGTH),
                       digest)
        elif algorithm.startswith("ES"):
            if not isinstance(key, ec.EllipticCurvePublicKey):
                raise TokenError(f"{algorithm} needs an EC key")
            # JWS ES* signatures are raw r||s; `cryptography` wants DER.
            from cryptography.hazmat.primitives.asymmetric.utils import (
                encode_dss_signature,
            )
            half = len(signature) // 2
            if half * 2 != len(signature) or half == 0:
                raise TokenError("malformed ECDSA signature length")
            r = int.from_bytes(signature[:half], "big")
            s = int.from_bytes(signature[half:], "big")
            key.verify(encode_dss_signature(r, s), signed, ec.ECDSA(digest))
        else:
            raise TokenError(f"unsupported algorithm {algorithm}")
    except InvalidSignature as exc:
        raise TokenError("signature does not verify against the issuer's key") from exc


def verify_id_token(
    token: str,
    *,
    jwks: Dict[str, Any],
    issuer: str,
    audience: str,
    nonce: Optional[str] = None,
    now: Optional[float] = None,
    leeway: int = _LEEWAY_SECONDS,
) -> Dict[str, Any]:
    """
    Verify an OIDC ID token and return its claims.

    Args:
        token:    the compact JWS.
        jwks:     the issuer's JWKS document (`{"keys": [...]}`).
        issuer:   the exact `iss` this token must carry.
        audience: this client's id, which `aud` must contain.
        nonce:    the nonce sent on the authorization request. Required when the
                  request carried one; a token that omits it is refused.

    Raises:
        TokenError on ANY failure. There is no partial success.
    """
    now = time.time() if now is None else now

    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError(f"a JWS has three segments, this has {len(parts)}")
    header_segment, payload_segment, signature_segment = parts

    header = _json_segment(header_segment, "header")
    algorithm = str(header.get("alg") or "")

    # The allowlist check comes FIRST, before any key lookup. `alg: none` and
    # `alg: HS256` are both refused here, which is what closes the
    # algorithm-confusion attack: an attacker cannot get this far with a token
    # signed by the provider's PUBLIC key used as an HMAC secret.
    if algorithm not in _ALLOWED_ALGORITHMS:
        raise TokenError(
            f"algorithm {algorithm!r} is not accepted. Only asymmetric "
            f"signatures are: {', '.join(sorted(_ALLOWED_ALGORITHMS))}."
        )

    # A key embedded IN the token proves nothing about the issuer.
    for smuggled in ("jwk", "jku", "x5c", "x5u"):
        if smuggled in header:
            raise TokenError(
                f"the token header carries {smuggled!r}; key material must come "
                f"from the issuer's JWKS, never from the token itself."
            )

    kid = header.get("kid")
    keys = list(jwks.get("keys") or [])
    if not keys:
        raise TokenError("the issuer's JWKS contains no keys")

    candidates: List[Dict[str, Any]] = [
        k for k in keys
        if (kid is None or k.get("kid") == kid)
        and str(k.get("use") or "sig") == "sig"
        and (k.get("alg") in (None, algorithm))
    ]
    if not candidates:
        raise TokenError(
            f"no key in the issuer's JWKS matches kid={kid!r} for {algorithm}"
        )

    signed = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = _b64url_decode(signature_segment)

    last_error: Optional[Exception] = None
    for jwk in candidates:
        try:
            _verify_signature(algorithm, public_key_from_jwk(jwk), signed, signature)
            break
        except TokenError as exc:
            last_error = exc
    else:
        raise TokenError(str(last_error or "no candidate key verified the signature"))

    claims = _json_segment(payload_segment, "payload")

    # ---- Claims. A valid signature says who wrote it, not that it is for us.
    token_issuer = str(claims.get("iss") or "")
    if token_issuer != issuer:
        raise TokenError(f"iss is {token_issuer!r}, expected {issuer!r}")

    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else list(aud or [])
    if audience not in audiences:
        raise TokenError(f"aud {audiences!r} does not contain this client")

    # `azp` identifies the party the token was issued TO when there are several
    # audiences. If present it must be us, or the token was minted for someone
    # else and merely mentions us.
    azp = claims.get("azp")
    if azp is not None and str(azp) != audience:
        raise TokenError(f"azp is {azp!r}, not this client")

    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise TokenError("exp is missing or not a number")
    if now > float(exp) + leeway:
        raise TokenError("the token has expired")

    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now + leeway < float(nbf):
        raise TokenError("the token is not valid yet")

    iat = claims.get("iat")
    if not isinstance(iat, (int, float)):
        raise TokenError("iat is missing or not a number")
    if now + leeway < float(iat):
        raise TokenError("the token was issued in the future")
    if now - float(iat) > _MAX_TOKEN_AGE_SECONDS + leeway:
        raise TokenError(
            f"the token is older than {_MAX_TOKEN_AGE_SECONDS}s; an ID token "
            f"that lives this long is not acceptable here whatever exp says"
        )

    # A nonce binds the token to OUR authorization request. Without it a token
    # legitimately issued for another of this client's sessions can be replayed
    # into this one.
    if nonce is not None:
        token_nonce = claims.get("nonce")
        if not token_nonce:
            raise TokenError("the request carried a nonce and the token has none")
        if str(token_nonce) != nonce:
            raise TokenError("nonce does not match the authorization request")

    subject = claims.get("sub")
    if not subject or not str(subject).strip():
        raise TokenError("sub is missing; there is no identity without it")

    return claims


__all__ = ["TokenError", "verify_id_token", "public_key_from_jwk"]
