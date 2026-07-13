from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from typing import Any

SENSITIVE_FIELDS = {
    "authorization",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "credentials",
    "access_token",
    "refresh_token",
}
SENSITIVE_TEXT = re.compile(
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}|"
    r"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{16,}\b"
)


def mask_sensitive_fields(value: Any) -> Any:
    """Mask named sensitive fields without changing the surrounding contract."""
    if isinstance(value, list):
        return [mask_sensitive_fields(item) for item in value]
    if isinstance(value, str):
        return SENSITIVE_TEXT.sub("********", value)
    if not isinstance(value, dict):
        return value
    return {
        key: "********"
        if key.lower().replace("-", "_") in SENSITIVE_FIELDS
        else mask_sensitive_fields(item)
        for key, item in value.items()
    }


SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def token_lookup_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_token(token: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(
        token.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_token(token: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            token.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(expected)),
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


@dataclass(frozen=True, slots=True)
class Principal:
    token_id: str
    organization_id: str
    workspace_id: str
    installation_id: str | None = None
    principal_type: str = "api_key"
    subject_id: str | None = None
    permissions: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset({"*"})
    session_id: str | None = None

    @property
    def actor_id(self) -> str:
        return self.subject_id or self.installation_id or self.token_id
