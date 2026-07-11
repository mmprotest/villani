"""Versioned authenticated local HTTP service."""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import signal
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from villani_ops.closed_loop.durable_io import write_json_atomic

from .client import is_loopback_host
from .config import AgentdPaths, ServerConfig
from .spool import LimitError, SQLiteSpool, SpoolError
from .structured_log import StructuredLogger
from .otlp import normalize_otlp_traces


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AgentdHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        spool: SQLiteSpool,
        config: ServerConfig,
        logger: StructuredLogger,
    ) -> None:
        self.token = token
        self.spool = spool
        self.config = config
        self.structured_logger = logger
        super().__init__(address, AgentdRequestHandler)


class AgentdRequestHandler(BaseHTTPRequestHandler):
    server: AgentdHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        self.server.structured_logger.emit(
            "info",
            "http_request",
            method=self.command,
            path=self.path,
            status=args[1] if len(args) > 1 else None,
        )

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _authenticated(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if not secrets.compare_digest(supplied, expected):
            self._send(HTTPStatus.UNAUTHORIZED, {"error": "authentication_required"})
            return False
        return True

    def _json_body(self, maximum: int | None = None) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise SpoolError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise SpoolError("invalid Content-Length") from error
        maximum = maximum or max(
            self.server.config.limits.spool_bytes, self.server.config.limits.artifact_file_bytes * 2
        )
        if length < 0 or length > maximum:
            raise LimitError(f"request exceeds {maximum} bytes")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SpoolError("request body must be valid UTF-8 JSON") from error
        if not isinstance(value, dict):
            raise SpoolError("request body must be an object")
        return value

    def _dispatch(self) -> None:
        if self.path == "/v1/health" and self.command == "GET":
            self._send(HTTPStatus.OK, {"status": "ok", "version": "v1"})
            return
        if not self._authenticated():
            return
        if self.path == "/v1/status" and self.command == "GET":
            self._send(
                HTTPStatus.OK,
                {
                    "status": "running",
                    **self.server.spool.status(),
                    "limits": self.server.config.limits.as_dict(),
                },
            )
            return
        if self.command != "POST":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        otlp_path = self.path in {"/v1/traces", "/v1/otlp/v1/traces"}
        body = self._json_body(self.server.config.limits.otlp_payload_bytes if otlp_path else None)
        if otlp_path:
            events = normalize_otlp_traces(body)
            result = self.server.spool.ingest_events(
                event.model_dump(mode="json") for event in events
            )
            self._send(
                HTTPStatus.OK,
                {
                    "partialSuccess": {"rejectedSpans": 0},
                    "inserted": result.inserted,
                    "duplicates": result.duplicates,
                },
            )
            return
        if self.path == "/v1/runs":
            created = self.server.spool.register_run(
                str(body.get("run_id") or ""),
                str(body["trace_id"]) if body.get("trace_id") is not None else None,
                str(body.get("created_at") or utc_now()),
            )
            self._send(HTTPStatus.CREATED if created else HTTPStatus.OK, {"created": created})
            return
        if self.path == "/v1/events:batch":
            batch_events = body.get("events")
            if not isinstance(batch_events, list) or not all(
                isinstance(item, dict) for item in batch_events
            ):
                raise SpoolError("events must be an array of objects")
            result = self.server.spool.ingest_events(batch_events)
            self._send(
                HTTPStatus.OK,
                {
                    "inserted": result.inserted,
                    "duplicates": result.duplicates,
                    "upload_state": "offline",
                },
            )
            return
        if self.path == "/v1/artifacts/register":
            descriptor = body.get("descriptor")
            content_base64 = body.get("content_base64")
            if not isinstance(descriptor, dict) or not isinstance(content_base64, str):
                raise SpoolError("descriptor and content_base64 are required")
            try:
                content = base64.b64decode(content_base64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise SpoolError("content_base64 is invalid") from error
            stored = self.server.spool.register_artifact(
                str(body.get("run_id") or ""), descriptor, content
            )
            self._send(HTTPStatus.CREATED, {"descriptor": stored.model_dump(mode="json")})
            return
        prefix = "/v1/runs/"
        suffix = "/finalize"
        if self.path.startswith(prefix) and self.path.endswith(suffix):
            run_id = self.path[len(prefix) : -len(suffix)]
            if not run_id or "/" in run_id:
                raise SpoolError("invalid run_id")
            self.server.spool.finalize_run(run_id, body, utc_now())
            self._send(HTTPStatus.OK, {"finalized": True, "upload_state": "offline"})
            return
        self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._dispatch()
        except SpoolError as error:
            self._send(
                error.status_code, {"error": error.__class__.__name__, "message": str(error)}
            )
        except Exception as error:
            self.server.structured_logger.emit(
                "error", "request_failed", error_class=type(error).__name__
            )
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    def do_POST(self) -> None:  # noqa: N802
        self.do_GET()


def serve(
    config: ServerConfig,
    paths: AgentdPaths,
    token: str,
    *,
    insecure_development: bool = False,
    ready: threading.Event | None = None,
) -> None:
    if not is_loopback_host(config.host) and not insecure_development:
        raise ValueError("non-loopback binding requires --insecure-development")
    spool = SQLiteSpool(paths, config.limits)
    logger = StructuredLogger(paths.log)
    server = AgentdHTTPServer((config.host, config.port), token, spool, config, logger)
    selected_host, selected_port = server.server_address[:2]
    if isinstance(selected_host, bytes):
        selected_host = selected_host.decode("ascii")
    advertised_host = selected_host if is_loopback_host(str(selected_host)) else "127.0.0.1"
    endpoint = f"http://{advertised_host}:{selected_port}"
    write_json_atomic(
        paths.endpoint,
        {
            "schema_version": "villani.agentd_endpoint.v1",
            "endpoint": endpoint,
            "pid": os.getpid(),
            "started_at": utc_now(),
            "limits": config.limits.as_dict(),
        },
    )
    logger.emit("info", "daemon_started", endpoint=endpoint, pid=os.getpid())
    if ready is not None:
        ready.set()

    def request_shutdown(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, request_shutdown)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, request_shutdown)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        logger.emit("info", "daemon_stopped", pid=os.getpid())
