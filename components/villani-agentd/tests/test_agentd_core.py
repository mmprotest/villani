from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import signal
import sqlite3
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from villani_agentd import process as process_module
from villani_agentd import platform_process
from villani_agentd import wrapper as wrapper_module
from villani_agentd.cli import build_parser
from villani_agentd.client import ClientError, LocalClient
from villani_agentd.config import AgentdPaths, Limits, ServerConfig
from villani_agentd.lifecycle import start_background, stop_background, write_token
from villani_agentd.process import CapturedStream, ProcessResult, run_process
from villani_agentd.server import AgentdHTTPServer, serve
from villani_agentd.spool import CollisionError, LimitError, SQLiteSpool, SpoolError
from villani_agentd.structured_log import StructuredLogger
from villani_ops.closed_loop.protocol_v2 import ResourceV2, TelemetryEnvelopeV2
from villani_ops.closed_loop.event_writer import EventWriter
from villani_ops.closed_loop.run_store import RunStore
from villani_ops.cli.agentd_sink import AgentdEventSink


def _now() -> datetime:
    return datetime(2026, 7, 11, tzinfo=timezone.utc)


def _event(
    sequence: int,
    *,
    event_id: str | None = None,
    run_id: str = "run_1",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = TelemetryEnvelopeV2(
        schema_version="villani.telemetry_envelope.v2",
        event_id=event_id or f"evt_{sequence}",
        idempotency_key=f"test:{event_id or sequence}",
        occurred_at=_now(),
        observed_at=_now(),
        sequence=sequence,
        sequence_scope=f"run:{run_id}",
        organization_id=None,
        workspace_id=None,
        project_id=None,
        repository_id=None,
        run_id=run_id,
        trace_id="1" * 32,
        span_id=f"{sequence:016x}",
        parent_span_id=None,
        attempt_id=None,
        source="agentd",
        kind="command",
        name="command_completed",
        status="ok",
        resource=ResourceV2(
            schema_version="villani.resource.v2",
            service_name="test",
            service_version=None,
            deployment_environment="local",
            host_id=None,
            process_id=None,
            attributes={},
        ),
        attributes={},
        body=body or {},
    )
    return event.model_dump(mode="json")


@pytest.fixture
def paths(tmp_path: Path) -> AgentdPaths:
    return AgentdPaths(tmp_path / "agentd")


@pytest.fixture
def running_server(paths: AgentdPaths):
    limits = Limits(
        stdout_bytes=1024,
        stderr_bytes=1024,
        event_body_bytes=4096,
        artifact_file_bytes=4096,
        total_run_artifact_bytes=8192,
        spool_bytes=1_000_000,
        otlp_payload_bytes=2048,
    )
    token = "local-test-token"
    spool = SQLiteSpool(paths, limits)
    server = AgentdHTTPServer(
        ("127.0.0.1", 0),
        token,
        spool,
        ServerConfig(limits=limits),
        StructuredLogger(paths.log),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        yield server, LocalClient(endpoint, token), endpoint, spool
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_all_required_cli_commands_exist() -> None:
    parser = build_parser()
    for command in ("start", "status", "stop", "doctor", "wrap"):
        if command == "wrap":
            parsed = parser.parse_args([command, "--adapter", "generic", "--", "echo", "x"])
        else:
            parsed = parser.parse_args([command])
        assert parsed.command == command


def test_health_is_public_but_status_requires_authentication(running_server) -> None:
    _server, client, endpoint, _spool = running_server
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{endpoint}/v1/health") as response:
        assert json.loads(response.read())["status"] == "ok"
    with pytest.raises(urllib.error.HTTPError) as caught:
        opener.open(f"{endpoint}/v1/status")
    assert caught.value.code == 401
    assert client.status()["upload_mode"] == "offline"


def test_authenticated_otlp_endpoint_is_idempotent_and_bounded(running_server) -> None:
    _server, client, _endpoint, spool = running_server
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "1" * 32,
                                "spanId": "2" * 16,
                                "name": "fixture",
                                "startTimeUnixNano": "1783728000000000000",
                                "attributes": [],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    assert client.request("POST", "/v1/traces", payload)["inserted"] == 1
    assert client.request("POST", "/v1/traces", payload)["duplicates"] == 1
    assert spool.status()["events"] == 1
    with pytest.raises(ClientError, match="413"):
        client.request("POST", "/v1/traces", {"resourceSpans": [], "padding": "x" * 3000})


def test_every_non_health_endpoint_rejects_missing_authentication(running_server) -> None:
    _server, _client, endpoint, _spool = running_server
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    targets = [
        ("GET", "/v1/status"),
        ("POST", "/v1/runs"),
        ("POST", "/v1/events:batch"),
        ("POST", "/v1/artifacts/register"),
        ("POST", "/v1/runs/run_1/finalize"),
        ("POST", "/v1/traces"),
    ]
    for method, path in targets:
        request = urllib.request.Request(
            f"{endpoint}{path}",
            data=b"{}" if method == "POST" else None,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            opener.open(request)
        assert caught.value.code == 401


def test_non_loopback_binding_and_client_are_refused(paths: AgentdPaths) -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        serve(ServerConfig(host="0.0.0.0"), paths, "token")
    with pytest.raises(ClientError, match="non-loopback"):
        LocalClient("http://192.0.2.1:9999", "token")
    with pytest.raises(ValueError, match="non-loopback"):
        start_background(ServerConfig(host="0.0.0.0"), paths)
    assert not paths.token.exists()


def test_identical_event_batches_are_idempotent(paths: AgentdPaths) -> None:
    spool = SQLiteSpool(paths, Limits())
    first = spool.ingest_events([_event(1), _event(2)])
    second = spool.ingest_events([_event(1), _event(2)])
    assert (first.inserted, first.duplicates) == (2, 0)
    assert (second.inserted, second.duplicates) == (0, 2)
    assert spool.status()["events"] == 2


def test_event_redaction_preserves_safe_metadata_and_numeric_token_metrics(
    paths: AgentdPaths,
) -> None:
    from villani_ops.execution_environment.secrets import register_secret_values

    secret = "registered-release-secret-91c4"
    register_secret_values([secret])
    spool = SQLiteSpool(paths, Limits())
    document = _event(
        1,
        body={
            "task_instruction": "Repair authentication using a variable named token",
            "fixture": "test-token",
            "authorization": f"Bearer {secret}",
            "registered": secret,
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
        },
    )
    assert spool.ingest_events([document]).inserted == 1
    raw = paths.database.read_bytes()
    assert secret.encode() not in raw
    with sqlite3.connect(paths.database) as connection:
        payload = json.loads(connection.execute("SELECT payload_json FROM events").fetchone()[0])
    assert payload["body"]["task_instruction"].endswith("named token")
    assert payload["body"]["fixture"] == "test-token"
    assert payload["body"]["authorization"] == "[REDACTED]"
    assert payload["body"]["registered"] == "[REDACTED]"
    assert payload["body"]["total_tokens"] == 18
    assert payload["body"]["villani_redaction"]["status"] == "redacted"


def test_canonical_event_writer_spools_same_run_identity(running_server, tmp_path) -> None:
    _server, client, _endpoint, spool = running_server
    store = RunStore(tmp_path / "runs", "run_canonical_1")
    store.create()
    writer = EventWriter(
        store,
        "trace_canonical_1",
        _now,
        event_sink=AgentdEventSink(client),
    )

    first = writer.emit("run_created", {"task_id": "task_1"})
    second = writer.emit("classification_started")

    assert [first.sequence, second.sequence] == [1, 2]
    with sqlite3.connect(spool.paths.database) as connection:
        run = connection.execute("SELECT run_id FROM runs").fetchone()
        rows = connection.execute(
            "SELECT run_id,sequence,payload_json FROM events ORDER BY sequence"
        ).fetchall()
    assert run[0] == "run_canonical_1"
    assert [(row[0], row[1]) for row in rows] == [
        ("run_canonical_1", 1),
        ("run_canonical_1", 2),
    ]
    assert all(json.loads(row[2])["run_id"] == "run_canonical_1" for row in rows)


def test_sequence_collision_with_different_content_is_rejected(paths: AgentdPaths) -> None:
    spool = SQLiteSpool(paths, Limits())
    spool.ingest_events([_event(1, event_id="evt_original")])
    with pytest.raises(CollisionError, match="sequence"):
        spool.ingest_events([_event(1, event_id="evt_other", body={"different": True})])
    assert spool.status()["events"] == 1


def test_event_id_collision_with_different_content_is_rejected(paths: AgentdPaths) -> None:
    spool = SQLiteSpool(paths, Limits())
    spool.ingest_events([_event(1, event_id="evt_same")])
    with pytest.raises(CollisionError, match="event_id"):
        spool.ingest_events([_event(2, event_id="evt_same", body={"different": True})])


def test_committed_events_survive_spool_restart_and_wal_is_enabled(paths: AgentdPaths) -> None:
    SQLiteSpool(paths, Limits()).ingest_events([_event(1)])
    restarted = SQLiteSpool(paths, Limits())
    assert restarted.status()["pending_events"] == 1
    import sqlite3

    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4


def test_unknown_legacy_spool_layout_is_refused(paths: AgentdPaths) -> None:
    import sqlite3

    paths.root.mkdir(parents=True)
    with sqlite3.connect(paths.database) as connection:
        connection.execute("CREATE TABLE mystery(value TEXT)")
    with pytest.raises(SpoolError, match="unsupported table layout"):
        SQLiteSpool(paths, Limits())


def test_concurrent_writers_commit_distinct_events(paths: AgentdPaths) -> None:
    spool = SQLiteSpool(paths, Limits())
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(lambda sequence: spool.ingest_events([_event(sequence)]), range(1, 17))
        )
    assert sum(result.inserted for result in results) == 16
    assert spool.status()["events"] == 16


def test_event_body_and_spool_limits_are_enforced(paths: AgentdPaths) -> None:
    body_limited = SQLiteSpool(paths, Limits(event_body_bytes=8))
    with pytest.raises(LimitError, match="event body"):
        body_limited.ingest_events([_event(1, body={"value": "too large"})])

    other_paths = AgentdPaths(paths.root.parent / "small-spool")
    spool_limited = SQLiteSpool(other_paths, Limits(spool_bytes=100))
    with pytest.raises(LimitError, match="event spool"):
        spool_limited.ingest_events([_event(1)])


def test_replayed_batch_does_not_consume_spool_limit(paths: AgentdPaths) -> None:
    event = _event(1)
    encoded_size = len(json.dumps(event, sort_keys=True, separators=(",", ":")).encode())
    spool = SQLiteSpool(paths, Limits(spool_bytes=encoded_size + 10))
    spool.ingest_events([event])
    assert spool.ingest_events([event]).duplicates == 1


def test_artifact_digest_and_size_are_verified_and_content_is_addressed(paths: AgentdPaths) -> None:
    spool = SQLiteSpool(paths, Limits())
    content = b"patch bytes"
    digest = hashlib.sha256(content).hexdigest()
    descriptor = {
        "schema_version": "villani.artifact_descriptor.v2",
        "artifact_id": "artifact_1",
        "digest": {"algorithm": "sha256", "value": digest},
        "size_bytes": len(content),
        "media_type": "text/plain",
        "logical_role": "command.stdout",
        "sensitivity": "internal",
        "retention_class": "run",
        "encryption_status": "unencrypted",
        "storage_reference": None,
        "provenance_status": "recorded",
        "attributes": {},
    }
    stored = spool.register_artifact("run_1", descriptor, content)
    assert stored.storage_reference == f"sha256/{digest[:2]}/{digest}"
    assert (paths.artifacts / digest[:2] / digest).read_bytes() == content
    mismatch = json.loads(json.dumps(descriptor))
    mismatch["artifact_id"] = "artifact_bad"
    mismatch["digest"]["value"] = "0" * 64
    with pytest.raises(SpoolError, match="digest mismatch"):
        spool.register_artifact("run_1", mismatch, content)


def test_artifact_file_and_per_run_limits_are_enforced(paths: AgentdPaths) -> None:
    content = b"12345"
    descriptor = {
        "schema_version": "villani.artifact_descriptor.v2",
        "artifact_id": "large",
        "digest": {"algorithm": "sha256", "value": hashlib.sha256(content).hexdigest()},
        "size_bytes": len(content),
        "media_type": "text/plain",
        "logical_role": "log",
        "sensitivity": "internal",
        "retention_class": "run",
        "encryption_status": "unencrypted",
        "storage_reference": None,
        "provenance_status": "recorded",
        "attributes": {},
    }
    with pytest.raises(LimitError, match="artifact exceeds"):
        SQLiteSpool(paths, Limits(artifact_file_bytes=4)).register_artifact(
            "run_1", descriptor, content
        )

    total_paths = AgentdPaths(paths.root.parent / "total-artifacts")
    total_spool = SQLiteSpool(
        total_paths,
        Limits(artifact_file_bytes=10, total_run_artifact_bytes=8),
    )
    first = json.loads(json.dumps(descriptor))
    first["artifact_id"] = "first"
    total_spool.register_artifact("run_1", first, content)
    second_content = b"67890"
    second = json.loads(json.dumps(descriptor))
    second["artifact_id"] = "second"
    second["digest"]["value"] = hashlib.sha256(second_content).hexdigest()
    with pytest.raises(LimitError, match="run artifact total"):
        total_spool.register_artifact("run_1", second, second_content)


def test_http_run_event_artifact_and_finalize_endpoints(running_server) -> None:
    _server, client, _endpoint, spool = running_server
    assert client.request("POST", "/v1/runs", {"run_id": "run_1"})["created"] is True
    assert client.request("POST", "/v1/events:batch", {"events": [_event(1)]})["inserted"] == 1
    content = b"ok"
    digest = hashlib.sha256(content).hexdigest()
    descriptor = {
        "schema_version": "villani.artifact_descriptor.v2",
        "artifact_id": "artifact_http",
        "digest": {"algorithm": "sha256", "value": digest},
        "size_bytes": len(content),
        "media_type": "text/plain",
        "logical_role": "log",
        "sensitivity": "internal",
        "retention_class": "run",
        "encryption_status": "unencrypted",
        "storage_reference": None,
        "provenance_status": "recorded",
        "attributes": {},
    }
    response = client.request(
        "POST",
        "/v1/artifacts/register",
        {
            "run_id": "run_1",
            "descriptor": descriptor,
            "content_base64": base64.b64encode(content).decode(),
        },
    )
    assert response["descriptor"]["digest"]["value"] == digest
    assert client.request("POST", "/v1/runs/run_1/finalize", {"status": "completed"})["finalized"]
    assert spool.status() == {
        "runs": 1,
        "events": 1,
        "artifacts": 1,
        "pending_events": 1,
        "pending_outcomes": 0,
        "dead_letters": 0,
        "upload_mode": "offline",
    }


def test_bounded_stdout_stderr_and_nonzero_exit_without_shell() -> None:
    result = run_process(
        [
            sys.executable,
            "-c",
            "import sys; print('x'*1000); print('y'*1000,file=sys.stderr); raise SystemExit(7)",
        ],
        32,
        24,
    )
    assert result.exit_code == 7
    assert result.stdout.captured_bytes == 32 and result.stdout.truncated
    assert result.stderr.captured_bytes == 24 and result.stderr.truncated


def test_subprocess_cancellation_propagates_to_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 123
        stdout = io.BytesIO(b"out")
        stderr = io.BytesIO(b"err")

        def wait(self):
            raise KeyboardInterrupt

    fake = FakeProcess()
    terminated: list[int] = []
    monkeypatch.setattr(process_module.subprocess, "Popen", lambda *args, **kwargs: fake)
    monkeypatch.setattr(
        process_module, "terminate_process_tree", lambda process: terminated.append(process.pid)
    )
    result = run_process(["fake"], 10, 10)
    assert result.cancelled is True
    assert result.exit_code == 130
    assert terminated == [123]


def test_windows_process_group_flags_are_used_through_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class FakeProcess:
        pid = 456
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def wait(self):
            return 0

    def fake_popen(*args, **kwargs):
        seen.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(process_module, "is_windows", lambda: True)
    monkeypatch.setattr(process_module.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(process_module.subprocess, "Popen", fake_popen)
    assert run_process(["fake"], 10, 10).exit_code == 0
    assert seen["shell"] is False
    assert seen["creationflags"] == 512
    assert seen["start_new_session"] is False


def test_posix_process_path_never_requests_windows_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class FakeProcess:
        pid = 457
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def wait(self):
            return 0

    monkeypatch.setattr(process_module, "is_windows", lambda: False)
    monkeypatch.setattr(
        process_module,
        "windows_creation_flags",
        lambda: pytest.fail("POSIX path requested Windows constants"),
    )
    monkeypatch.setattr(
        process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: seen.update(kwargs) or FakeProcess(),
    )

    assert run_process(["fake"], 10, 10).exit_code == 0
    assert seen["creationflags"] == 0
    assert seen["start_new_session"] is True


def test_windows_helpers_use_available_flags_and_safely_handle_missing_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_process.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    monkeypatch.setattr(platform_process.subprocess, "DETACHED_PROCESS", 8, raising=False)
    assert platform_process.windows_creation_flags() == 512
    assert platform_process.windows_creation_flags(detached=True) == 520

    monkeypatch.delattr(platform_process.subprocess, "CREATE_NEW_PROCESS_GROUP", raising=False)
    monkeypatch.delattr(platform_process.subprocess, "DETACHED_PROCESS", raising=False)
    monkeypatch.delattr(platform_process.signal, "CTRL_BREAK_EVENT", raising=False)
    assert platform_process.windows_creation_flags(detached=True) == 0
    assert platform_process.windows_ctrl_break_event() == 0


def test_portable_agentd_modules_import_without_windows_apis() -> None:
    __import__("villani_agentd.process")
    __import__("villani_agentd.lifecycle")
    __import__("villani_agentd.remote_worker")


def test_generic_wrapper_returns_child_exit_and_emits_two_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str, dict[str, Any]]] = []

    class FakeClient:
        def request(self, method, path, body):
            requests.append((method, path, body))
            return {}

    monkeypatch.setattr(
        wrapper_module,
        "run_process",
        lambda *args: ProcessResult(
            9,
            321,
            12,
            CapturedStream("out", 3, 3, False),
            CapturedStream("err", 3, 3, False),
            False,
        ),
    )
    exit_code = wrapper_module.wrap_generic(
        ["definitely-not-a-shell", "arg"], FakeClient(), Limits()
    )
    assert exit_code == 9
    batch = next(body for _, path, body in requests if path == "/v1/events:batch")
    assert len(batch["events"]) == 2
    assert batch["events"][1]["body"]["process"]["exit_code"] == 9
    assert batch["events"][0]["body"]["shell"] is False


def test_structured_logging_redacts_tokens_and_artifact_content(paths: AgentdPaths) -> None:
    logger = StructuredLogger(paths.log)
    logger.emit(
        "info",
        "test",
        authorization="Bearer top-secret-token",
        artifact_content="private bytes",
        message="token=also-secret",
    )
    text = paths.log.read_text(encoding="utf-8")
    assert "top-secret-token" not in text
    assert "private bytes" not in text
    assert "also-secret" not in text
    assert "[REDACTED]" in text


def test_token_file_is_user_only_on_posix(paths: AgentdPaths) -> None:
    write_token(paths.token, "unguessable")
    assert paths.token.read_text(encoding="utf-8").strip() == "unguessable"
    if os.name != "nt":
        assert stat.S_IMODE(paths.token.stat().st_mode) == 0o600


def test_daemon_kill_restart_preserves_pending_events_and_real_wrap_exit(
    paths: AgentdPaths,
) -> None:
    limits = Limits(
        stdout_bytes=128,
        stderr_bytes=128,
        event_body_bytes=4096,
        artifact_file_bytes=4096,
        total_run_artifact_bytes=8192,
        spool_bytes=1_000_000,
    )
    first = start_background(ServerConfig(limits=limits), paths)
    first_pid = int(first["pid"])
    token = paths.token.read_text(encoding="utf-8").strip()
    assert len(token) >= 48
    assert token not in paths.endpoint.read_text(encoding="utf-8")
    assert str(first["endpoint"]).startswith("http://127.0.0.1:")
    client = LocalClient.from_files(paths)
    assert (
        wrapper_module.wrap_generic([sys.executable, "-c", "raise SystemExit(6)"], client, limits)
        == 6
    )
    assert client.status()["pending_events"] == 2

    os.kill(first_pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            client.health()
        except ClientError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("daemon did not stop after termination")

    second = start_background(ServerConfig(limits=limits), paths)
    assert int(second["pid"]) != first_pid
    assert LocalClient.from_files(paths).status()["pending_events"] == 2
    assert stop_background(paths) is True
