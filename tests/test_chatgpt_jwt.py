"""``core.chatgpt.jwt``: RS256 verification (RFC 7515's own example, and keys the tests make), what is refused (other
algorithms, other keys, small keys, changed tokens), and the checks of an ID token's claims."""

from __future__ import annotations

import time

import pytest

from desktop.core.chatgpt import jwt
from desktop.core.chatgpt.errors import AuthError
from tests.fake_openai_auth import RsaKey, b64url, make_jwt, rsa_key, sign_hs256, sign_rs256, swap_payload, unsigned_jwt

# RFC 7515 appendix A.2: a JWS signed with RS256 by another implementation, with its key (lines as printed there).
RFC_N = "".join(
    (
        "ofgWCuLjybRlzo0tZWJjNiuSfb4p4fAkd_wWJcyQoTbji9k0l8W26mPddx",
        "HmfHQp-Vaw-4qPCJrcS2mJPMEzP1Pt0Bm4d4QlL-yRT-SFd2lZS-pCgNMs",
        "D1W_YpRPEwOWvG6b32690r2jZ47soMZo9wGzjb_7OMg0LOL-bSf63kpaSH",
        "SXndS5z5rexMdbBYUsLA9e-KXBdQOS-UTo7WTBEMa2R2CapHg665xsmtdV",
        "MTBQY4uDZlxvb3qCo5ZwKh9kG4LT6_I5IhlJH7aGhyxXFvUK-DWNmoudF8",
        "NAco9_h9iaGNj8q2ethFkMLs91kzk2PAcDTW9gb54h4FRWyuXpoQ",
    )
)
RFC_D = "".join(
    (
        "Eq5xpGnNCivDflJsRQBXHx1hdR1k6Ulwe2JZD50LpXyWPEAeP88vLNO97I",
        "jlA7_GQ5sLKMgvfTeXZx9SE-7YwVol2NXOoAJe46sui395IW_GO-pWJ1O0",
        "BkTGoVEn2bKVRUCgu-GjBVaYLU6f3l9kJfFNS3E0QbVdxzubSu3Mkqzjkn",
        "439X0M_V51gfpRLI9JYanrC4D4qAdGcopV_0ZHHzQlBjudU2QvXt4ehNYT",
        "CBr6XCLQUShb1juUO1ZdiYoFaFQT5Tw8bGUl_x_jTj3ccPDVZFD9pIuhLh",
        "BOneufuBiB4cS98l2SR_RQyGWSeWjnczT0QU91p1DhOVRuOopznQ",
    )
)
RFC_HEADER = "eyJhbGciOiJSUzI1NiJ9"
RFC_PAYLOAD = "eyJpc3MiOiJqb2UiLA0KICJleHAiOjEzMDA4MTkzODAsDQogImh0dHA6Ly9leGFtcGxlLmNvbS9pc19yb290Ijp0cnVlfQ"
RFC_SIGNATURE = "".join(
    (
        "cC4hiUPoj9Eetdgtv3hF80EGrhuB__dzERat0XF9g2VtQgr9PJbu3XOiZj5RZmh7",
        "AAuHIm4Bh-0Qc_lF5YKt_O8W2Fp5jujGbds9uJdbF9CUAr7t1dnZcAcQjbKBYNX4",
        "BAynRFdiuB--f_nZLgrnbyTyWzO75vRK5h6xBArLIARNPvkSjtQBMHlb1L07Qe7K",
        "0GarZRmB_eSN9383LcOLn6_dO--xi12jzDwusC-eOkHWEsqtFZESc6BfI7noOPqv",
        "hJ1phCnvWh6IeYI2w9QOYEUipUTI8np6LbgGY9Fs98rqVt5AXLIhWkWywlVmtVrB",
        "p0igcN_IoypGlUPQGe77Rw",
    )
)
RFC_TOKEN = f"{RFC_HEADER}.{RFC_PAYLOAD}.{RFC_SIGNATURE}"

ISSUER = "https://auth.example.test"
CLIENT = "oaiapp_Client1"
NONCE = "nonce-of-this-sign-in"


def keys(*members: RsaKey) -> dict:
    return {"keys": [member.public_jwk() for member in members]}


def claims(**changes) -> dict:
    now = int(time.time())
    base = {"iss": ISSUER, "aud": CLIENT, "sub": "user-1", "exp": now + 3600, "iat": now, "nonce": NONCE, "email": "a@b.c"}
    base.update(changes)
    return {key: value for key, value in base.items() if value is not None}


def refused(token: str, jwks: dict, **options) -> str:
    with pytest.raises(AuthError) as caught:
        jwt.verify_rs256(token, jwks, **options)
    assert caught.value.code == jwt.CODE and caught.value.terminal is False
    return caught.value.message


def test_the_rfc_7515_example_verifies():
    header, payload = jwt.verify_rs256(RFC_TOKEN, {"keys": [{"kty": "RSA", "n": RFC_N, "e": "AQAB"}]})
    assert header == {"alg": "RS256"}
    assert payload == {"iss": "joe", "exp": 1300819380, "http://example.com/is_root": True}


def test_the_test_signer_reproduces_the_rfc_signature():
    # PKCS#1 v1.5 is deterministic: the tests' own signer is checked against the RFC as well, not only against jwt.py.
    to_int = lambda text: int.from_bytes(jwt.b64url_decode(text), "big")  # noqa: E731
    key = RsaKey(n=to_int(RFC_N), e=65537, d=to_int(RFC_D))
    assert b64url(key.sign(f"{RFC_HEADER}.{RFC_PAYLOAD}".encode())) == RFC_SIGNATURE


def test_the_key_is_chosen_by_kid_among_several():
    main, other = rsa_key(), rsa_key("other")
    token = sign_rs256(claims(), main)
    assert jwt.verify_rs256(token, keys(other, main))[1]["sub"] == "user-1"
    # the same signature presented as the other key's
    assert "does not verify" in refused(sign_rs256(claims(), main, kid=other.kid), keys(other, main))
    assert "kid 'kid-nobody'" in refused(sign_rs256(claims(), main, kid="kid-nobody"), keys(other, main))


def test_a_token_without_kid_needs_exactly_one_key():
    main = rsa_key()
    token = make_jwt({"alg": "RS256"}, claims(), main.sign)
    assert jwt.verify_rs256(token, keys(main))[1]["sub"] == "user-1"
    assert "2 RS256 signing keys" in refused(token, keys(main, rsa_key("other")))
    # a set whose only key has no kid serves a token that names one
    bare = {key: value for key, value in main.public_jwk().items() if key != "kid"}
    assert jwt.verify_rs256(sign_rs256(claims(), main), {"keys": [bare]})[1]["sub"] == "user-1"


def test_changed_claims_do_not_verify():
    token = sign_rs256(claims(), rsa_key())
    assert "does not verify" in refused(swap_payload(token, claims(sub="someone-else")), keys(rsa_key()))
    head, payload, signature = token.split(".")
    flipped = signature[:100] + ("A" if signature[100] != "A" else "B") + signature[101:]
    assert "does not verify" in refused(f"{head}.{payload}.{flipped}", keys(rsa_key()))


@pytest.mark.parametrize(
    "make, text",
    [
        (lambda key: unsigned_jwt(claims(), kid=key.kid), "'none'"),
        (lambda key: unsigned_jwt(claims(), kid=key.kid) + b64url(key.sign(b"x")), "'none'"),
        # the public key as an HMAC secret (the classic confusion of RS256 and HS256)
        (lambda key: sign_hs256(claims(), key.public_jwk()["n"].encode(), kid=key.kid), "'HS256'"),
        (lambda key: make_jwt({"alg": "RS512", "kid": key.kid}, claims(), key.sign), "'RS512'"),
        (lambda key: make_jwt({"kid": key.kid}, claims(), key.sign), "None"),
        (lambda key: make_jwt({"alg": "RS256", "kid": key.kid, "crit": ["exp"]}, claims(), key.sign), "crit"),
    ],
)
def test_other_algorithms_are_refused_before_any_key(make, text):
    assert text in refused(make(rsa_key()), keys(rsa_key()))


def test_keys_that_are_not_rs256_signing_keys_are_not_used():
    main = rsa_key()
    token = sign_rs256(claims(), main)
    for unusable in (
        {"kty": "EC", "kid": main.kid, "crv": "P-256", "x": "AA", "y": "AA"},
        main.public_jwk(alg="RS512"),
        main.public_jwk(use="enc"),
        main.public_jwk(key_ops=["encrypt"]),
    ):
        assert "no RS256 signing key" in refused(token, {"keys": [unusable]})
    assert jwt.verify_rs256(token, {"keys": [main.public_jwk(key_ops=["verify"])]})


def test_a_key_under_2048_bits_is_refused():
    small = rsa_key("small", 1024)
    token = sign_rs256(claims(), small)
    assert "1024 bits" in refused(token, keys(small))
    assert jwt.verify_rs256(token, keys(small), min_bits=1024)[1]["sub"] == "user-1"


def test_the_signature_must_have_the_key_size_and_be_below_the_modulus():
    main = rsa_key()
    head, payload, signature = sign_rs256(claims(), main).split(".")
    raw = jwt.b64url_decode(signature)
    for wrong in (raw[1:], b"\x00" + raw, b"", main.n.to_bytes(256, "big")):
        assert "does not verify" in refused(f"{head}.{payload}.{b64url(wrong)}", keys(main))


@pytest.mark.parametrize(
    "token, text",
    [
        ("", "empty"),
        ("a.b", "2 dot-separated parts"),
        ("a.b.c.d.e", "5 dot-separated parts"),
        ("e30=.e30.", "header is not base64url"),
        ("e3+.e30.", "header is not base64url"),
        (b64url(b"not json") + ".e30.", "header is not JSON"),
        (b64url(b"[1]") + ".e30.", "header is not a JSON object"),
        (b64url(b'{"alg":"RS256"}') + "." + b64url(b"\xff") + ".", "payload is not JSON"),
    ],
)
def test_malformed_tokens_are_named(token, text):
    with pytest.raises(AuthError) as caught:
        jwt.decode(token)
    assert text in caught.value.message


def test_decode_reads_without_checking():
    header, payload = jwt.decode(unsigned_jwt({"sub": "x"}))
    assert header["alg"] == "none" and payload == {"sub": "x"}


def test_base64url_is_strict():
    assert jwt.b64url_decode(jwt.b64url_encode(b"\xfb\xff")) == b"\xfb\xff"
    assert jwt.b64url_encode(b"\xfb\xff") == "-_8"
    for bad in ("-_8=", "+/8", "a", "a b"):
        with pytest.raises(ValueError):
            jwt.b64url_decode(bad)


def validate(token_claims: dict, **options) -> None:
    arguments = {"issuer": ISSUER, "client_id": CLIENT, "nonce": NONCE, "now": time.time(), **options}
    jwt.validate_id_token_claims(token_claims, **arguments)


@pytest.mark.parametrize(
    "changes, text",
    [
        ({"iss": "https://elsewhere.test"}, "issuer"),
        ({"iss": None}, "issuer"),
        ({"aud": "oaiapp_Other"}, "not for this client"),
        ({"aud": ["oaiapp_Other", "resource"]}, "not for this client"),
        ({"aud": None}, "not for this client"),
        ({"aud": [CLIENT, "other"], "azp": "oaiapp_Other"}, "azp"),
        ({"exp": int(time.time()) - 120}, "expired"),
        ({"exp": None}, "no valid expiry"),
        ({"exp": "tomorrow"}, "no valid expiry"),
        ({"exp": True}, "no valid expiry"),
        ({"exp": float("nan")}, "no valid expiry"),
        ({"nbf": int(time.time()) + 600}, "not valid yet"),
        ({"nonce": "another-nonce"}, "nonce"),
        ({"nonce": None}, "nonce"),
        ({"sub": ""}, "no subject"),
        ({"sub": None}, "no subject"),
    ],
)
def test_id_token_claims_are_checked(changes, text):
    with pytest.raises(AuthError) as caught:
        validate(claims(**changes))
    assert text in caught.value.message and caught.value.code == jwt.CODE


def test_good_claims_pass_and_the_audience_may_be_a_list():
    validate(claims())
    validate(claims(aud=[CLIENT, "https://api.openai.com/v1"]))
    validate(claims(aud=[CLIENT], azp=CLIENT))


def test_expiry_has_sixty_seconds_of_grace():
    now = time.time()
    validate(claims(exp=int(now) - 30), now=now)
    with pytest.raises(AuthError):
        validate(claims(exp=int(now) - 90), now=now)
    validate(claims(exp=int(now) - 90), now=now, skew=120)


def test_verify_id_token_checks_signature_and_claims():
    main = rsa_key()
    token = sign_rs256(claims(), main)
    arguments = {"issuer": ISSUER, "client_id": CLIENT, "nonce": NONCE, "now": time.time()}
    assert jwt.verify_id_token(token, keys(main), **arguments)["email"] == "a@b.c"
    with pytest.raises(AuthError, match="nonce"):
        jwt.verify_id_token(token, keys(main), **{**arguments, "nonce": "other"})
