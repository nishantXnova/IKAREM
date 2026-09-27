"""Exhaustive JWT verification: 7 branches."""

import base64
import json

import pytest

from ikarem import create_token, verify_token

SECRET = "test-secret-123"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _craft(header: dict, payload: dict, sig: str = "") -> str:
    return f"{_b64e(json.dumps(header).encode())}.{_b64e(json.dumps(payload).encode())}.{sig}"


def test_1_valid_token():
    tok = create_token("u1", SECRET, roles=["admin"])
    claims = verify_token(tok, SECRET)
    assert claims["sub"] == "u1" and claims["roles"] == ["admin"]


def test_2_expired_token():
    tok = create_token("u1", SECRET, expires_in=-10)
    with pytest.raises(ValueError, match="expired"):
        verify_token(tok, SECRET)


def test_3_wrong_signature():
    tok = create_token("u1", SECRET)
    bad = tok[:-1] + ("a" if tok[-1] != "a" else "b")
    with pytest.raises(ValueError, match="signature"):
        verify_token(bad, SECRET)


def test_4_malformed_token():
    for malformed in ("abc", "a.b", "....", "", "a.b.c.d"):
        with pytest.raises(ValueError, match="malformed|signature|header|payload"):
            verify_token(malformed, SECRET)


def test_5_wrong_algorithm_rejected():
    # alg=none confusion attack: must be rejected even with empty signature
    none_tok = _craft({"alg": "none", "typ": "JWT"}, {"sub": "u1"})
    with pytest.raises(ValueError, match="[Aa]lgorithm"):
        verify_token(none_tok, SECRET)
    # RS256 confusion: correctly-signed HS256 body relabeled RS256 must fail
    real = create_token("u1", SECRET)
    h, p, s = real.split(".")
    relabeled = f"{_b64e(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())}.{p}.{s}"
    with pytest.raises(ValueError, match="[Aa]lgorithm"):
        verify_token(relabeled, SECRET)


def test_6_missing_claims():
    tok = create_token("u1", SECRET)
    h, p, s = tok.split(".")

    payload = {"exp": 9999999999, "roles": ["admin"]}  # no sub
    forged_p = _b64e(json.dumps(payload).encode())
    import hashlib
    import hmac

    sig = _b64e(hmac.new(SECRET.encode(), f"{h}.{forged_p}".encode(), hashlib.sha256).digest())
    with pytest.raises(ValueError, match="sub"):
        verify_token(f"{h}.{forged_p}.{sig}", SECRET)


def test_7_authorization_failure_wrong_secret():
    tok = create_token("u1", "other-secret")
    with pytest.raises(ValueError, match="signature"):
        verify_token(tok, SECRET)
