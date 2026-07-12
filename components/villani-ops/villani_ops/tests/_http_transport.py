"""Test adapter for mocking explicit backend HTTP clients."""

from __future__ import annotations


class _BackendClient:
    def __init__(self, post):
        self._post = post

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def post(self, url, **kwargs):
        return self._post(url, **kwargs)


def patch_backend_post(monkeypatch, post) -> None:
    monkeypatch.setattr(
        "villani_ops.verifier.llm.create_backend_http_client",
        lambda *_args, **_kwargs: _BackendClient(post),
    )
