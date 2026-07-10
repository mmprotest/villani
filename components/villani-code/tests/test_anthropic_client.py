from __future__ import annotations

import httpx
import pytest

from villani_code import anthropic_client as module
from villani_code.anthropic_client import AnthropicClient


class _ClientFactory:
    def __init__(self, transport: httpx.MockTransport, original_client: type[httpx.Client]) -> None:
        self._transport = transport
        self._original_client = original_client

    def __call__(self, timeout: float):
        return self._original_client(timeout=timeout, transport=self._transport)


def test_create_message_non_stream_includes_response_text_on_400(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, request=request, text='{"error":"tool_result blocks must come FIRST"}')
    )
    original_client = module.httpx.Client
    monkeypatch.setattr(module.httpx, "Client", _ClientFactory(transport, original_client))
    client = AnthropicClient(base_url="http://example.test")

    with pytest.raises(RuntimeError, match="Response body") as excinfo:
        client.create_message({"messages": []}, stream=False)

    assert "tool_result blocks must come FIRST" in str(excinfo.value)


def test_create_message_stream_includes_response_text_on_400(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(400, request=request, text='{"error":"Tool result must immediately follow tool_use"}')
    )
    original_client = module.httpx.Client
    monkeypatch.setattr(module.httpx, "Client", _ClientFactory(transport, original_client))
    client = AnthropicClient(base_url="http://example.test")

    with pytest.raises(RuntimeError, match="Response body") as excinfo:
        list(client.create_message({"messages": []}, stream=True))

    assert "immediately follow tool_use" in str(excinfo.value)
