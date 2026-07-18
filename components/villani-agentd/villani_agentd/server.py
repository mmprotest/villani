"""Versioned authenticated local HTTP service."""

from __future__ import annotations

import base64
import binascii
import http.cookies
import importlib.resources
import json
import mimetypes
import os
import secrets
import signal
import threading
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from villani_ops.closed_loop.durable_io import write_json_atomic

from .client import is_loopback_host
from .config import AgentdPaths, ServerConfig
from .console import (
    ConsoleAuthorizationError,
    ConsoleDataError,
    ConsoleInputError,
    ConsoleService,
)
from .spool import LimitError, SQLiteSpool, SpoolError
from .structured_log import StructuredLogger
from .otlp import normalize_otlp_traces
from .config import SyncConfig


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
        console_service: ConsoleService | None = None,
    ) -> None:
        self.token = token
        self.spool = spool
        self.config = config
        self.structured_logger = logger
        self.console_service = console_service or ConsoleService(spool.paths, spool)
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

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
        console_cookie: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        if console_cookie:
            self.send_header(
                "Set-Cookie",
                f"villani_console={self.server.token}; HttpOnly; Path=/; SameSite=Strict",
            )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status: int, body: str, *, console_cookie: bool = False) -> None:
        self._send_bytes(
            status,
            body.encode("utf-8"),
            "text/html; charset=utf-8",
            console_cookie=console_cookie,
        )

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _console_asset(self, relative: str) -> bytes | None:
        parts = relative.replace("\\", "/").split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            return None
        target = importlib.resources.files("villani_agentd").joinpath("console_assets", *parts)
        try:
            return target.read_bytes()
        except (FileNotFoundError, IsADirectoryError, OSError):
            return None

    def _serve_console(self, relative: str = "index.html") -> None:
        content = self._console_asset(relative)
        if content is None:
            self._send_html(
                HTTPStatus.SERVICE_UNAVAILABLE,
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                "<title>Villani Console unavailable</title></head><body><main>"
                "<h1>Villani Console is unavailable</h1><p>Run: villani doctor</p>"
                "</main></body></html>",
            )
            return
        if relative == "index.html":
            self._send_bytes(
                HTTPStatus.OK,
                content,
                "text/html; charset=utf-8",
                console_cookie=True,
            )
            return
        content_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        self._send_bytes(
            HTTPStatus.OK,
            content,
            content_type,
            cache_control="public, max-age=31536000, immutable",
        )

    def _authenticated(self, *, allow_console_cookie: bool = False) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        authenticated = secrets.compare_digest(supplied, expected)
        if allow_console_cookie and not authenticated:
            cookie = http.cookies.SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                value = cookie.get("villani_console")
                authenticated = bool(
                    value and secrets.compare_digest(value.value, self.server.token)
                )
            except http.cookies.CookieError:
                authenticated = False
        if not authenticated:
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

    def _dispatch_console_api(
        self,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any] | None = None,
    ) -> bool:
        service = self.server.console_service
        if path == "/v1/console/run-options" and self.command == "GET":
            self._send(HTTPStatus.OK, service.run_options())
            return True
        if path == "/v1/console/validation:discover" and self.command == "POST":
            repository = (body or {}).get("repository")
            if not isinstance(repository, str):
                raise ConsoleInputError("repository is required")
            self._send(HTTPStatus.OK, service.validation_discovery(repository))
            return True
        if path == "/v1/console/models:detect" and self.command == "POST":
            self._send(HTTPStatus.OK, service.models_detect(body or {}))
            return True
        if path == "/v1/console/models:test" and self.command == "POST":
            self._send(HTTPStatus.OK, service.models_test(body or {}))
            return True
        if path == "/v1/console/models:add" and self.command == "POST":
            self._send(HTTPStatus.OK, service.models_add(body or {}))
            return True
        if path == "/v1/console/models:remove" and self.command == "POST":
            self._send(HTTPStatus.OK, service.models_remove(body or {}))
            return True
        if path == "/v1/console/models:default" and self.command == "POST":
            self._send(HTTPStatus.OK, service.models_default(body or {}))
            return True
        if path == "/v1/console/policies:select" and self.command == "POST":
            self._send(HTTPStatus.OK, service.policy_select(body or {}))
            return True
        if path == "/v1/console/policy:preview" and self.command == "POST":
            self._send(HTTPStatus.OK, service.policy_preview(body or {}))
            return True
        if path == "/v1/console/policies:simulate" and self.command == "POST":
            self._send(HTTPStatus.OK, service.policy_simulation(body or {}))
            return True
        if path == "/v1/console/runs" and self.command == "POST":
            self._send(HTTPStatus.ACCEPTED, service.start_run(body or {}))
            return True
        run_status_prefix = "/v1/console/runs/"
        cancel_suffix = "/cancel"
        if (
            self.command == "POST"
            and path.startswith(run_status_prefix)
            and path.endswith(cancel_suffix)
        ):
            encoded = path[len(run_status_prefix) : -len(cancel_suffix)]
            if not encoded or "/" in encoded:
                raise ConsoleInputError("run identifier is invalid")
            self._send(
                HTTPStatus.OK,
                service.cancel_run(urllib.parse.unquote(encoded)),
            )
            return True
        approval_suffix = "/approval"
        if (
            self.command == "POST"
            and path.startswith(run_status_prefix)
            and path.endswith(approval_suffix)
        ):
            encoded = path[len(run_status_prefix) : -len(approval_suffix)]
            if not encoded or "/" in encoded:
                raise ConsoleInputError("run identifier is invalid")
            sync = SyncConfig.load(self.server.spool.paths.sync_config)
            actor = (
                f"connected-console:{sync.installation_id}"
                if sync is not None
                else "local-console-session"
            )
            self._send(
                HTTPStatus.OK,
                service.approval_action(
                    urllib.parse.unquote(encoded),
                    body or {},
                    authenticated=True,
                    actor=actor,
                    authentication_type="agentd_authenticated_session",
                ),
            )
            return True
        run_events_suffix = "/events"
        if (
            self.command == "GET"
            and path.startswith(run_status_prefix)
            and path.endswith(run_events_suffix)
        ):
            encoded = path[len(run_status_prefix) : -len(run_events_suffix)]
            if not encoded or "/" in encoded:
                raise ConsoleInputError("run identifier is invalid")
            try:
                after_sequence = int(query.get("after", ["0"])[0])
                wait_seconds = float(query.get("wait", ["20"])[0])
            except (TypeError, ValueError) as error:
                raise ConsoleInputError("event cursor is invalid") from error
            self._send(
                HTTPStatus.OK,
                service.run_events(
                    urllib.parse.unquote(encoded),
                    after_sequence=after_sequence,
                    wait_seconds=wait_seconds,
                ),
            )
            return True
        run_status_suffix = "/status"
        if (
            self.command == "GET"
            and path.startswith(run_status_prefix)
            and path.endswith(run_status_suffix)
        ):
            encoded = path[len(run_status_prefix) : -len(run_status_suffix)]
            if not encoded or "/" in encoded:
                raise ConsoleInputError("run identifier is invalid")
            self._send(
                HTTPStatus.OK,
                service.run_status(urllib.parse.unquote(encoded)),
            )
            return True
        if self.command != "GET":
            return False
        if path == "/v1/console/bootstrap":
            self._send(HTTPStatus.OK, service.bootstrap())
            return True
        if path == "/v1/console/home":
            self._send(HTTPStatus.OK, service.home())
            return True
        if path == "/v1/console/history":
            refresh = query.get("refresh", [""])[0].lower() in {"1", "true", "yes"}
            self._send(HTTPStatus.OK, service.history(refresh=refresh))
            return True
        if path == "/v1/console/models":
            self._send(HTTPStatus.OK, service.models())
            return True
        if path == "/v1/console/policies":
            self._send(HTTPStatus.OK, service.policies())
            return True
        if path == "/v1/console/settings":
            bootstrap = service.bootstrap()
            self._send(
                HTTPStatus.OK,
                {
                    "schema_version": "villani.console.settings.v1",
                    "setup": bootstrap["setup"],
                    "service": bootstrap["service"],
                    "storage": bootstrap["storage"],
                    "privacy": {"secrets_exposed": False, "local_first": True},
                    "synchronization": bootstrap["synchronization"],
                    "workspace": bootstrap["workspace"],
                    "version": bootstrap["version"],
                    "entitlement": bootstrap["entitlement"],
                    "update": bootstrap["update"],
                    "commands": {
                        "doctor": "villani doctor",
                        "update_status": "villani update status",
                        "support_preview": "villani support preview",
                        "license_status": "villani license status",
                    },
                },
            )
            return True
        if path == "/v1/console/service":
            self._send(HTTPStatus.OK, service.bootstrap()["service"])
            return True
        if path == "/v1/console/synchronization":
            bootstrap = service.bootstrap()
            self._send(
                HTTPStatus.OK,
                {
                    "schema_version": "villani.console.synchronization.v1",
                    **bootstrap["synchronization"],
                    "workspace": bootstrap["workspace"],
                },
            )
            return True
        if path == "/v1/console/workspace":
            self._send(HTTPStatus.OK, service.workspace())
            return True
        workspace_prefix = "/v1/console/workspace/"
        if path.startswith(workspace_prefix):
            surface = urllib.parse.unquote(path[len(workspace_prefix) :])
            self._send(HTTPStatus.OK, service.workspace(surface))
            return True
        for prefix, kind in (
            ("/v1/console/runs/", "run"),
            ("/v1/console/sessions/", "session"),
        ):
            if path.startswith(prefix):
                encoded = path[len(prefix) :]
                if not encoded or "/" in encoded:
                    return False
                self._send(
                    HTTPStatus.OK,
                    service.replay(urllib.parse.unquote(encoded), kind),
                )
                return True
        return False

    def _dispatch(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if path == "/" and self.command == "GET":
            self._redirect("/console")
            return
        if path.startswith("/assets/") and self.command == "GET":
            self._serve_console(path.removeprefix("/"))
            return
        console_route = path == "/console" or path.startswith("/console/")
        legacy_route = path in {
            "/fleet",
            "/ask",
            "/history",
            "/replay",
            "/models",
            "/policies",
            "/settings",
        } or path.startswith(("/runs/", "/flight/", "/fleet/", "/ask/"))
        if (console_route or legacy_route) and self.command == "GET":
            self._serve_console()
            return
        if path == "/v1/health" and self.command == "GET":
            self._send(HTTPStatus.OK, {"status": "ok", "version": "v1"})
            return
        if path.startswith("/v1/console/"):
            if self.command not in {"GET", "POST"}:
                self._send(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
                return
            if not self._authenticated(allow_console_cookie=True):
                return
            body = self._json_body(maximum=524_288) if self.command == "POST" else None
            if self._dispatch_console_api(path, query, body):
                return
            self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._authenticated():
            return
        if path == "/v1/status" and self.command == "GET":
            sync_config = SyncConfig.load(self.server.spool.paths.sync_config)
            self._send(
                HTTPStatus.OK,
                {
                    "status": "running",
                    **self.server.spool.status(),
                    "upload_mode": ("synchronized" if sync_config else "offline"),
                    "remote_execution": (
                        "enabled"
                        if sync_config and sync_config.remote_execution_enabled
                        else "disabled"
                    ),
                    "limits": self.server.config.limits.as_dict(),
                },
            )
            return
        if self.command != "POST":
            self._send(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return

        otlp_path = path in {"/v1/traces", "/v1/otlp/v1/traces"}
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
        if path == "/v1/runs":
            created = self.server.spool.register_run(
                str(body.get("run_id") or ""),
                str(body["trace_id"]) if body.get("trace_id") is not None else None,
                str(body.get("created_at") or utc_now()),
            )
            self._send(HTTPStatus.CREATED if created else HTTPStatus.OK, {"created": created})
            return
        if path == "/v1/events:batch":
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
        if path == "/v1/artifacts/register":
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
        if path.startswith(prefix) and path.endswith(suffix):
            run_id = path[len(prefix) : -len(suffix)]
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
        except ConsoleInputError as error:
            self._send(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_console_request", "message": str(error)},
            )
        except ConsoleAuthorizationError as error:
            self._send(
                HTTPStatus.FORBIDDEN,
                {"error": "approval_not_authorized", "message": str(error)},
            )
        except ConsoleDataError as error:
            self._send(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "console_data_unavailable", "message": str(error)},
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
        try:
            endpoint_document = json.loads(paths.endpoint.read_text(encoding="utf-8"))
            if int(endpoint_document.get("pid", 0)) == os.getpid():
                paths.endpoint.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        logger.emit("info", "daemon_stopped", pid=os.getpid())
