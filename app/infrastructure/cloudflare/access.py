"""Offline JWT verification for assertions issued by Cloudflare Access."""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.request import urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_MAX_ASSERTION_BYTES = 16_384
_DEFAULT_KEY_CACHE_SECONDS = 3600.0


class CloudflareAccessAuthenticationError(ValueError):
    """A Cloudflare Access assertion could not be trusted."""


@dataclass(frozen=True)
class _CachedKeys:
    keys: tuple[Mapping[str, Any], ...]
    expires_at: float


class CloudflareAccessJwtVerifier:
    """Validate Access RS256 assertions, audience, issuer, and exact identity."""

    def __init__(
        self,
        *,
        team_domain: str,
        audience: str,
        allowed_email: str,
        jwks_loader: Callable[[], Mapping[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
        key_cache_seconds: float = _DEFAULT_KEY_CACHE_SECONDS,
    ) -> None:
        self._issuer = f"https://{team_domain}"
        self._audience = audience
        self._allowed_email = allowed_email.casefold()
        self._clock = clock
        self._key_cache_seconds = key_cache_seconds
        self._jwks_loader = jwks_loader or self._remote_jwks_loader(team_domain)
        self._cached_keys: _CachedKeys | None = None

    def verify(self, assertion: str) -> str:
        """Return the authorized email after full cryptographic verification."""
        if not assertion or len(assertion.encode("utf-8")) > _MAX_ASSERTION_BYTES:
            raise CloudflareAccessAuthenticationError("invalid Access assertion")
        parts = assertion.split(".")
        if len(parts) != 3:
            raise CloudflareAccessAuthenticationError("invalid Access assertion")
        header = self._decode_json(parts[0])
        payload = self._decode_json(parts[1])
        key_id = header.get("kid")
        if header.get("alg") != "RS256" or not isinstance(key_id, str):
            raise CloudflareAccessAuthenticationError("unsupported Access assertion")

        key = self._find_key(key_id)
        signature = self._decode_segment(parts[2])
        signed = f"{parts[0]}.{parts[1]}".encode("ascii")
        try:
            self._public_key(key).verify(
                signature,
                signed,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, TypeError, ValueError) as error:
            raise CloudflareAccessAuthenticationError(
                "invalid Access signature"
            ) from error

        now = self._clock()
        self._validate_claims(payload, now)
        email = payload.get("email")
        if not isinstance(email, str) or email.casefold() != self._allowed_email:
            raise CloudflareAccessAuthenticationError("Access identity is not allowed")
        return email.casefold()

    def _find_key(self, key_id: str) -> Mapping[str, Any]:
        keys = self._keys()
        match = next((key for key in keys if key.get("kid") == key_id), None)
        if match is not None:
            return match
        self._cached_keys = None
        refreshed = self._keys()
        match = next((key for key in refreshed if key.get("kid") == key_id), None)
        if match is None:
            raise CloudflareAccessAuthenticationError("unknown Access signing key")
        return match

    def _keys(self) -> tuple[Mapping[str, Any], ...]:
        now = self._clock()
        if self._cached_keys is not None and self._cached_keys.expires_at > now:
            return self._cached_keys.keys
        try:
            document = self._jwks_loader()
        except CloudflareAccessAuthenticationError:
            raise
        except (OSError, ValueError) as error:
            raise CloudflareAccessAuthenticationError(
                "Access signing keys are unavailable"
            ) from error
        raw_keys = document.get("keys")
        if not isinstance(raw_keys, list):
            raise CloudflareAccessAuthenticationError("invalid Access key set")
        keys = tuple(key for key in raw_keys if isinstance(key, Mapping))
        if not keys:
            raise CloudflareAccessAuthenticationError("empty Access key set")
        self._cached_keys = _CachedKeys(keys, now + self._key_cache_seconds)
        return keys

    def _validate_claims(self, payload: Mapping[str, Any], now: float) -> None:
        if payload.get("iss") != self._issuer:
            raise CloudflareAccessAuthenticationError("invalid Access issuer")
        audience = payload.get("aud")
        if isinstance(audience, str):
            audiences = {audience}
        elif isinstance(audience, list) and all(
            isinstance(value, str) for value in audience
        ):
            audiences = set(audience)
        else:
            audiences = set()
        if self._audience not in audiences:
            raise CloudflareAccessAuthenticationError("invalid Access audience")
        expiration = payload.get("exp")
        if not isinstance(expiration, (int, float)) or expiration <= now:
            raise CloudflareAccessAuthenticationError("expired Access assertion")
        not_before = payload.get("nbf")
        if isinstance(not_before, (int, float)) and not_before > now + 30:
            raise CloudflareAccessAuthenticationError("Access assertion is not active")

    @staticmethod
    def _decode_json(segment: str) -> Mapping[str, Any]:
        try:
            value = json.loads(CloudflareAccessJwtVerifier._decode_segment(segment))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CloudflareAccessAuthenticationError(
                "invalid Access assertion"
            ) from error
        if not isinstance(value, Mapping):
            raise CloudflareAccessAuthenticationError("invalid Access assertion")
        return value

    @staticmethod
    def _decode_segment(segment: str) -> bytes:
        try:
            return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        except (ValueError, binascii.Error) as error:
            raise CloudflareAccessAuthenticationError(
                "invalid Access assertion"
            ) from error

    @staticmethod
    def _public_key(key: Mapping[str, Any]) -> rsa.RSAPublicKey:
        if key.get("kty") != "RSA" or key.get("alg") not in {None, "RS256"}:
            raise CloudflareAccessAuthenticationError("unsupported Access key")
        modulus = key.get("n")
        exponent = key.get("e")
        if not isinstance(modulus, str) or not isinstance(exponent, str):
            raise CloudflareAccessAuthenticationError("invalid Access key")
        return rsa.RSAPublicNumbers(
            e=int.from_bytes(CloudflareAccessJwtVerifier._decode_segment(exponent)),
            n=int.from_bytes(CloudflareAccessJwtVerifier._decode_segment(modulus)),
        ).public_key()

    @staticmethod
    def _remote_jwks_loader(team_domain: str) -> Callable[[], Mapping[str, Any]]:
        url = f"https://{team_domain}/cdn-cgi/access/certs"

        def load() -> Mapping[str, Any]:
            try:
                with urlopen(url, timeout=5.0) as response:
                    document = json.load(response)
            except (OSError, ValueError) as error:
                raise CloudflareAccessAuthenticationError(
                    "Access signing keys are unavailable"
                ) from error
            if not isinstance(document, Mapping):
                raise CloudflareAccessAuthenticationError("invalid Access key set")
            return document

        return load
