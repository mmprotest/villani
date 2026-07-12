from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import KeyRotationMetadata, utc_now


@dataclass(frozen=True, slots=True)
class WrappedDataKey:
    provider: str
    key_id: str
    key_version: int
    wrapped_key: bytes


@dataclass(frozen=True, slots=True)
class EncryptedEnvelope:
    algorithm: str
    nonce: bytes
    ciphertext: bytes
    authentication_tag: bytes
    data_key: WrappedDataKey


class KeyProvider(Protocol):
    name: str

    def wrap(self, data_key: bytes) -> WrappedDataKey: ...
    def unwrap(self, wrapped: WrappedDataKey) -> bytes: ...


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    result = bytearray()
    counter = 0
    while len(result) < length:
        result.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(result[:length])


class DevelopmentKeyProvider:
    """Local/test provider. It is deliberately not a production KMS implementation."""

    name = "development"

    def __init__(self, key: bytes, key_id: str = "dev-key-v1", version: int = 1) -> None:
        if len(key) < 16:
            raise ValueError("development wrapping key must be at least 16 bytes")
        self.key = hashlib.sha256(key).digest()
        self.key_id = key_id
        self.version = version

    def wrap(self, data_key: bytes) -> WrappedDataKey:
        mask = _stream(self.key, self.key_id.encode(), len(data_key))
        return WrappedDataKey(
            self.name,
            self.key_id,
            self.version,
            bytes(a ^ b for a, b in zip(data_key, mask, strict=True)),
        )

    def unwrap(self, wrapped: WrappedDataKey) -> bytes:
        if wrapped.provider != self.name or wrapped.key_id != self.key_id:
            raise ValueError("wrapped data key provider mismatch")
        mask = _stream(self.key, self.key_id.encode(), len(wrapped.wrapped_key))
        return bytes(a ^ b for a, b in zip(wrapped.wrapped_key, mask, strict=True))


class KMSBYOKProvider(KeyProvider, Protocol):
    """Interface for tested KMS/BYOK adapters; no cloud implementation is bundled."""


class FakeKMSProvider(DevelopmentKeyProvider):
    name = "fake-kms"


class EnvelopeEncryptionService:
    """Authenticated envelope interface using a test-only portable construction.

    Production adapters must replace the content cipher with an approved AEAD implementation.
    """

    algorithm = "villani-dev-stream-hmac-sha256-v1"

    def __init__(self, provider: KeyProvider) -> None:
        self.provider = provider

    def encrypt(self, plaintext: bytes, associated_data: bytes = b"") -> EncryptedEnvelope:
        data_key = os.urandom(32)
        nonce = os.urandom(16)
        stream = _stream(data_key, nonce, len(plaintext))
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream, strict=True))
        tag = hmac.new(data_key, associated_data + nonce + ciphertext, hashlib.sha256).digest()
        return EncryptedEnvelope(
            self.algorithm, nonce, ciphertext, tag, self.provider.wrap(data_key)
        )

    def decrypt(self, envelope: EncryptedEnvelope, associated_data: bytes = b"") -> bytes:
        if envelope.algorithm != self.algorithm:
            raise ValueError("unsupported envelope algorithm")
        key = self.provider.unwrap(envelope.data_key)
        expected = hmac.new(
            key, associated_data + envelope.nonce + envelope.ciphertext, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, envelope.authentication_tag):
            raise ValueError("envelope authentication failed")
        stream = _stream(key, envelope.nonce, len(envelope.ciphertext))
        return bytes(a ^ b for a, b in zip(envelope.ciphertext, stream, strict=True))


def record_key_rotation(
    session: Session,
    organization_id: str,
    provider: str,
    key_id: str,
    version: int,
    previous_key_id: str | None = None,
) -> KeyRotationMetadata:
    if previous_key_id:
        previous = session.scalar(
            select(KeyRotationMetadata).where(
                KeyRotationMetadata.organization_id == organization_id,
                KeyRotationMetadata.key_id == previous_key_id,
                KeyRotationMetadata.retired_at.is_(None),
            )
        )
        if previous:
            previous.retired_at = utc_now()
    record = KeyRotationMetadata(
        organization_id=organization_id,
        key_id=key_id,
        provider=provider,
        version=version,
        previous_key_id=previous_key_id,
    )
    session.add(record)
    session.commit()
    return record
