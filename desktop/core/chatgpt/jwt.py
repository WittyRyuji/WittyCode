"""JSON Web Tokens: reading one, and checking an RS256 signature against a JSON Web Key Set, in pure Python.

The project has no crypto package and needs none here: verifying RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 section
8.2.2) is one modular exponentiation (``pow``) and a byte comparison. The expected encoded message
``00 01 FF..FF 00 || DigestInfo(SHA-256) || hash`` is rebuilt and compared whole, in constant time, instead of parsing
the decrypted block: lax parsers of that block are how RSA signatures were forged (Bleichenbacher 2006, e = 3).

OpenAI signs ID tokens with RS256 only (its discovery document says so) and publishes several keys at once, each named
by ``kid``. Any other ``alg`` (``none``; ``HS256``, which would make the public key an HMAC secret; another RSA hash) is
refused before a key is looked at, and keys under 2048 bits are refused (RFC 7518 section 3.3 requires 2048 or more).

Every failure is an ``AuthError`` (not terminal: stored tokens are not affected) whose message says what was wrong.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from desktop.core.chatgpt.errors import AuthError

MIN_RSA_BITS = 2048
CLOCK_SKEW = 60.0
"""Seconds by which our clock and the issuer's may disagree when ``exp`` and ``nbf`` are checked."""

CODE = "invalid_id_token"
"""The ``code`` of every error raised here."""

_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
"""DER of ``DigestInfo{sha256, NULL}`` up to the digest (RFC 8017 section 9.2, note 1)."""

_BASE64URL = re.compile(r"[A-Za-z0-9_-]*")


def b64url_encode(data: bytes) -> str:
    """Base64url without padding (JWS, PKCE)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Strict base64url without padding: any other character raises ``ValueError``."""
    if not isinstance(text, str) or not _BASE64URL.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _parts(token: Any) -> list[str]:
    if not isinstance(token, str) or not token:
        raise AuthError("not a JWT: the token is empty", code=CODE)
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError(f"not a JWT: {len(parts)} dot-separated parts instead of 3", code=CODE)
    return parts


def _segment(part: str, what: str) -> dict[str, Any]:
    try:
        raw = b64url_decode(part)
    except (ValueError, binascii.Error):
        raise AuthError(f"not a JWT: the {what} is not base64url", code=CODE) from None
    try:
        value = json.loads(raw.decode("utf-8"))
    except ValueError:  # (UnicodeDecodeError is one)
        raise AuthError(f"not a JWT: the {what} is not JSON", code=CODE) from None
    if not isinstance(value, dict):
        raise AuthError(f"not a JWT: the {what} is not a JSON object", code=CODE)
    return value


def decode(token: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The header and the claims, checking nothing (for a token whose origin is already trusted, or a hint)."""
    header, payload, _ = _parts(token)
    return _segment(header, "header"), _segment(payload, "payload")


def verify_rs256(token: str, jwks: Mapping[str, Any], *, min_bits: int = MIN_RSA_BITS) -> tuple[dict[str, Any], dict[str, Any]]:
    """The header and claims of ``token`` once its RS256 signature holds for the matching key of ``jwks``
    (``{"keys": [JWK, ...]}``). The key is the one named by the header's ``kid``; a token without ``kid`` needs a set
    with exactly one RS256 signing key."""
    header_part, payload_part, signature_part = _parts(token)
    header = _segment(header_part, "header")
    algorithm = header.get("alg")
    if algorithm != "RS256":
        raise AuthError(f"unsupported JWT algorithm {algorithm!r}: only RS256 is accepted", code=CODE)
    if "crit" in header:
        raise AuthError("the JWT header lists critical extensions (crit), which are not supported", code=CODE)
    modulus, exponent = _public_key(header.get("kid"), jwks, min_bits)
    try:
        signature = b64url_decode(signature_part)
    except (ValueError, binascii.Error):
        raise AuthError("the JWT signature is not base64url", code=CODE) from None
    # (the parts passed the base64url check, so they are ASCII)
    if not _pkcs1_sha256_holds(f"{header_part}.{payload_part}".encode("ascii"), signature, modulus, exponent):
        raise AuthError("the JWT signature does not verify", code=CODE)
    return header, _segment(payload_part, "payload")


def _pkcs1_sha256_holds(message: bytes, signature: bytes, modulus: int, exponent: int) -> bool:
    size = (modulus.bit_length() + 7) // 8
    if len(signature) != size:
        return False
    value = int.from_bytes(signature, "big")
    if value >= modulus:
        return False
    encoded = pow(value, exponent, modulus).to_bytes(size, "big")
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding = size - len(digest_info) - 3
    if padding < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


def _signs_rs256(key: Any) -> bool:
    if not isinstance(key, Mapping) or key.get("kty") != "RSA":
        return False
    if key.get("alg", "RS256") != "RS256" or key.get("use", "sig") != "sig":
        return False
    operations = key.get("key_ops")
    return operations is None or (isinstance(operations, list) and "verify" in operations)


def _unsigned_int(value: Any) -> int:
    if not isinstance(value, str):
        raise ValueError("not a string")
    return int.from_bytes(b64url_decode(value.rstrip("=")), "big")


def _public_key(kid: Any, jwks: Mapping[str, Any], min_bits: int) -> tuple[int, int]:
    keys = jwks.get("keys") if isinstance(jwks, Mapping) else None
    if not isinstance(keys, list):
        raise AuthError("the JWKS has no list of keys", code=CODE)
    usable = [key for key in keys if _signs_rs256(key)]
    if kid is None:
        if len(usable) != 1:
            raise AuthError(f"the token names no key (kid) and the JWKS has {len(usable)} RS256 signing keys", code=CODE)
        chosen = usable
    else:
        chosen = [key for key in usable if key.get("kid") == kid]
        if not chosen and len(usable) == 1 and "kid" not in usable[0]:
            chosen = usable
        if not chosen:
            raise AuthError(f"no RS256 signing key in the JWKS has kid {kid!r}", code=CODE)
        if len(chosen) > 1:
            raise AuthError(f"the JWKS has {len(chosen)} keys with kid {kid!r}", code=CODE)
    try:
        modulus = _unsigned_int(chosen[0].get("n"))
        exponent = _unsigned_int(chosen[0].get("e"))
    except (ValueError, binascii.Error):
        raise AuthError(f"the JWKS key {kid!r} is malformed", code=CODE) from None
    if modulus.bit_length() < min_bits:
        raise AuthError(f"the signing key has {modulus.bit_length()} bits; at least {min_bits} are required", code=CODE)
    if exponent < 3 or exponent % 2 == 0 or exponent >= modulus:
        raise AuthError("the signing key's public exponent is not valid", code=CODE)
    return modulus, exponent


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_id_token_claims(
    claims: Mapping[str, Any],
    *,
    issuer: str,
    client_id: str,
    nonce: str,
    now: float,
    skew: float = CLOCK_SKEW,
) -> None:
    """The checks of OpenID Connect Core 3.1.3.7 that apply to a public client: the issuer is ``issuer``, the audience
    is (or includes) ``client_id`` (and ``azp``, when present, is it), the token has not expired (``skew`` seconds of
    grace) and is valid already (``nbf``), the ``nonce`` is the one of this sign-in, and there is a subject."""
    if claims.get("iss") != issuer:
        raise AuthError(f"the ID token's issuer is {claims.get('iss')!r}, expected {issuer!r}", code=CODE)
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
    if client_id not in audiences:
        raise AuthError(f"the ID token is for {audience!r}, not for this client ({client_id})", code=CODE)
    party = claims.get("azp")
    if party is not None and party != client_id:
        raise AuthError(f"the ID token was issued to {party!r} (azp), not to this client ({client_id})", code=CODE)
    expiry = claims.get("exp")
    if not _number(expiry):
        raise AuthError("the ID token has no valid expiry (exp)", code=CODE)
    if now > expiry + skew:
        raise AuthError(f"the ID token expired {now - expiry:.0f} s ago", code=CODE)
    not_before = claims.get("nbf")
    if not_before is not None and (not _number(not_before) or not_before > now + skew):
        raise AuthError("the ID token is not valid yet (nbf)", code=CODE)
    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce.encode("utf-8"), nonce.encode("utf-8")):
        raise AuthError("the ID token's nonce does not match this sign-in", code=CODE)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthError("the ID token has no subject (sub)", code=CODE)


def verify_id_token(
    token: str,
    jwks: Mapping[str, Any],
    *,
    issuer: str,
    client_id: str,
    nonce: str,
    now: float,
    skew: float = CLOCK_SKEW,
    min_bits: int = MIN_RSA_BITS,
) -> dict[str, Any]:
    """``verify_rs256`` and then ``validate_id_token_claims``: the claims of a valid ID token."""
    _, claims = verify_rs256(token, jwks, min_bits=min_bits)
    validate_id_token_claims(claims, issuer=issuer, client_id=client_id, nonce=nonce, now=now, skew=skew)
    return claims
