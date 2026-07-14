"""Canonical provider compatibility rules for the public closed loop."""

from __future__ import annotations

import re
from collections.abc import Mapping

from villani_ops.core.backend import Backend


CANONICAL_PROVIDERS = frozenset({"local", "openai-compatible", "openai"})
OPENAI_COMPATIBLE_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class ProviderConfigurationError(ValueError):
    """Raised before a run when a configured backend cannot be invoked safely."""


def canonical_provider(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "openai compatible": "openai-compatible",
        "openai-compatible": "openai-compatible",
        "openai": "openai",
        "local": "local",
    }
    return aliases.get(normalized, normalized)


def resolved_base_url(provider: str, base_url: str | None) -> str | None:
    normalized = canonical_provider(provider)
    if base_url and base_url.strip():
        return base_url.strip().rstrip("/")
    if normalized == "openai":
        return OPENAI_COMPATIBLE_DEFAULT_BASE_URL
    return None


def validate_closed_loop_backend(backend: Backend) -> None:
    """Validate backend structure without resolving a credential secret."""

    provider = canonical_provider(backend.provider)
    if provider not in CANONICAL_PROVIDERS:
        values = ", ".join(sorted(CANONICAL_PROVIDERS))
        raise ProviderConfigurationError(
            f"backend {backend.name!r} has unsupported provider {backend.provider!r}; "
            f"use one of: {values}"
        )
    if (
        provider in {"local", "openai-compatible"}
        and not str(backend.base_url or "").strip()
    ):
        raise ProviderConfigurationError(
            f"backend {backend.name!r} with provider {provider!r} requires an explicit base_url"
        )
    # Villani Code's OpenAI-compatible client accepts an omitted Authorization
    # header.  Local servers commonly run without authentication, so only the
    # cloud OpenAI provider requires a configured key here.
    if backend.api_key_env and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", backend.api_key_env
    ):
        raise ProviderConfigurationError(
            f"backend {backend.name!r} has an invalid credential environment-variable name"
        )
    if provider == "openai" and not backend.credential_reference_configured():
        raise ProviderConfigurationError(
            f"backend {backend.name!r} with provider 'openai' requires a credential "
            "reference (set api_key_env or a legacy api_key)"
        )


def validate_runtime_credentials(
    backend: Backend, environ: Mapping[str, str] | None = None
) -> None:
    """Fail closed before an authenticated backend can spend model tokens."""

    provider = canonical_provider(backend.provider)
    authentication_configured = backend.credential_reference_configured()
    if provider != "openai" and not authentication_configured:
        return
    try:
        backend.require_runtime_credential(environ)
    except ValueError as error:
        raise ProviderConfigurationError(str(error)) from error


def villani_code_provider(provider: str) -> str:
    """Villani Code accepts the OpenAI protocol name for all canonical backends."""

    normalized = canonical_provider(provider)
    if normalized in CANONICAL_PROVIDERS:
        return "openai"
    return normalized
