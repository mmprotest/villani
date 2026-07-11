from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass

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
