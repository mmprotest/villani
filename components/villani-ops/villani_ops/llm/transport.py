"""HTTP transport policy for model and provider backends."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import httpx


class BackendProxyConfigurationError(RuntimeError):
    """The environment-selected proxy transport cannot be constructed."""


def is_loopback_backend_url(url: str) -> bool:
    """Return whether *url* has an exact HTTP loopback host."""

    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return False
        host = parsed.hostname
        _ = parsed.port  # Validate non-numeric and out-of-range explicit ports.
    except (TypeError, ValueError):
        return False
    if not host:
        return False

    normalized = host[:-1] if host.endswith(".") else host
    normalized = normalized.casefold()
    if normalized == "localhost":
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return address in ipaddress.IPv4Network("127.0.0.0/8")
    return address == ipaddress.IPv6Address("::1")


def trust_environment_for_backend(url: str) -> bool:
    """Keep environment proxies for remote backends, never for loopback."""

    return not is_loopback_backend_url(url)


def create_backend_http_client(
    url: str, *, timeout: float | int
) -> httpx.Client:
    """Construct an HTTP client using Villani's backend proxy policy."""

    trust_env = trust_environment_for_backend(url)
    try:
        return httpx.Client(trust_env=trust_env, timeout=timeout)
    except ImportError as exc:
        if trust_env:
            raise BackendProxyConfigurationError(
                "Backend environment proxy configuration is unavailable"
            ) from exc
        raise
