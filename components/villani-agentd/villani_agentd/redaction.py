"""Structured remote redaction shared by live ingestion and local backfill."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from villani_ops.execution_environment.secrets import registered_secret_values

REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = {
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
_TOKEN_METRIC_KEYS = {"input_tokens", "output_tokens", "total_tokens"}
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}")
_API_KEY = re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9_-]{16,}\b", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(password|secret|api[_-]?key|private[_-]?key)\s*[:=]\s*"
    r"(['\"]?)(?!test(?:-|_|\b))[^\s,;\"']{8,}\2"
)


@dataclass(frozen=True, slots=True)
class RedactionResult:
    value: Any
    count: int
    categories: tuple[str, ...]


def _registered() -> tuple[str, ...]:
    return tuple(value for value in registered_secret_values() if value)


def redact_registered_secrets(value: str) -> tuple[str, int]:
    output = value
    count = 0
    for secret in _registered():
        occurrences = output.count(secret)
        if occurrences:
            output = output.replace(secret, REDACTED)
            count += occurrences
    return output, count


def redact_sensitive_text(value: str) -> RedactionResult:
    output, registered = redact_registered_secrets(value)
    categories: set[str] = {"registered_secret"} if registered else set()
    count = registered
    for pattern, category in (
        (_BEARER, "bearer_token"),
        (_API_KEY, "api_key"),
        (_CREDENTIAL_ASSIGNMENT, "credential_assignment"),
    ):
        output, matches = pattern.subn(REDACTED, output)
        if matches:
            count += matches
            categories.add(category)
    return RedactionResult(output, count, tuple(sorted(categories)))


def redact_sensitive_fields(value: Any) -> RedactionResult:
    count = 0
    categories: set[str] = set()
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            normalized = key.lower().replace("-", "_")
            if normalized in _SENSITIVE_KEYS and normalized not in _TOKEN_METRIC_KEYS:
                output[key] = REDACTED
                count += 1
                categories.add("sensitive_field")
                continue
            result = redact_sensitive_fields(item)
            output[key] = result.value
            count += result.count
            categories.update(result.categories)
        return RedactionResult(output, count, tuple(sorted(categories)))
    if isinstance(value, list):
        output_list = []
        for item in value:
            result = redact_sensitive_fields(item)
            output_list.append(result.value)
            count += result.count
            categories.update(result.categories)
        return RedactionResult(output_list, count, tuple(sorted(categories)))
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return RedactionResult(value, 0, ())


def redact_remote_document(value: Any) -> RedactionResult:
    """Return a redacted copy; never mutate the local canonical input."""

    return redact_sensitive_fields(value)


def unsafe_artifact_categories(content: bytes) -> tuple[str, ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    result = redact_sensitive_text(text)
    return result.categories if result.count else ()
