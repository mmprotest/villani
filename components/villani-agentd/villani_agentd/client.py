"""Authenticated loopback-only client for the local daemon."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .config import AgentdPaths


class ClientError(RuntimeError):
    pass


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalClient:
    def __init__(self, endpoint: str, token: str | None) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname is None
            or not is_loopback_host(parsed.hostname)
        ):
            raise ClientError("agentd client refuses non-loopback endpoints")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def from_files(cls, paths: AgentdPaths | None = None) -> "LocalClient":
        paths = paths or AgentdPaths.default()
        try:
            endpoint_document = json.loads(paths.endpoint.read_text(encoding="utf-8"))
            endpoint = endpoint_document["endpoint"]
            token = paths.token.read_text(encoding="utf-8").strip()
        except (OSError, KeyError, json.JSONDecodeError) as error:
            raise ClientError("villani-agentd is not configured or running") from error
        return cls(endpoint, token)

    def request(
        self, method: str, path: str, body: Mapping[str, Any] | None = None, *, auth: bool = True
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.token:
                raise ClientError("authenticated request requires a local token")
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            f"{self.endpoint}{path}", data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=5) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ClientError(f"agentd returned HTTP {error.code}: {detail}") from error
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise ClientError(f"cannot contact villani-agentd: {error}") from error
        if not isinstance(value, dict):
            raise ClientError("agentd returned a non-object response")
        return value

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/v1/health", auth=False)

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/v1/status")
