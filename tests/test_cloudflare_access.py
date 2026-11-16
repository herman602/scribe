"""Cloudflare Access assertion verification tests."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

import pytest
from app.infrastructure.cloudflare.access import (
    CloudflareAccessAuthenticationError,
    CloudflareAccessJwtVerifier,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_NOW = 1_800_000_000.0
_AUDIENCE = "a" * 64


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _json_segment(value: Mapping[str, Any]) -> str:
    return _encoded(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _fixture() -> tuple[CloudflareAccessJwtVerifier, rsa.RSAPrivateKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    key = {
        "kid": "fixture-key",
        "kty": "RSA",
        "alg": "RS256",
        "n": _encoded(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8)),
        "e": _encoded(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8)),
    }
    verifier = CloudflareAccessJwtVerifier(
        team_domain="team.cloudflareaccess.com",
        audience=_AUDIENCE,
        allowed_email="herman@example.com",
        jwks_loader=lambda: {"keys": [key]},
        clock=lambda: _NOW,
    )
    return verifier, private_key


def _assertion(private_key: rsa.RSAPrivateKey, **claims: Any) -> str:
    payload = {
        "iss": "https://team.cloudflareaccess.com",
        "aud": [_AUDIENCE],
        "email": "herman@example.com",
        "exp": _NOW + 300,
        **claims,
    }
    header = _json_segment({"alg": "RS256", "kid": "fixture-key"})
    body = _json_segment(payload)
    signed = f"{header}.{body}".encode("ascii")
    signature = private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_encoded(signature)}"


def test_verifier_accepts_signed_assertion_for_exact_identity() -> None:
    verifier, private_key = _fixture()

    assert verifier.verify(_assertion(private_key)) == "herman@example.com"


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"iss": "https://other.cloudflareaccess.com"}, "issuer"),
        ({"aud": ["b" * 64]}, "audience"),
        ({"email": "attacker@example.com"}, "not allowed"),
        ({"exp": _NOW - 1}, "expired"),
    ],
)
def test_verifier_rejects_untrusted_claims(
    claims: dict[str, Any], message: str
) -> None:
    verifier, private_key = _fixture()

    with pytest.raises(CloudflareAccessAuthenticationError, match=message):
        verifier.verify(_assertion(private_key, **claims))


def test_verifier_rejects_tampered_assertion() -> None:
    verifier, private_key = _fixture()
    assertion = _assertion(private_key)
    header, body, signature = assertion.split(".")
    signature_bytes = bytearray(base64.urlsafe_b64decode(signature + "=="))
    signature_bytes[-1] ^= 1
    tampered = f"{header}.{body}.{_encoded(bytes(signature_bytes))}"

    with pytest.raises(CloudflareAccessAuthenticationError, match="signature"):
        verifier.verify(tampered)
