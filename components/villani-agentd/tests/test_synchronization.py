from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from io import BytesIO
from pathlib import Path

from villani_agentd.config import AgentdPaths, Limits, SyncConfig
from villani_agentd.spool import SQLiteSpool
from villani_agentd.uploader import ControlPlaneClient, RemoteError, SynchronizationWorker


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "integration" / "fixtures" / "protocol" / "v2" / "valid"


def event(sequence: int) -> dict:
    value = json.loads((FIXTURE / "telemetry-envelope.json").read_text(encoding="utf-8"))
    value.update(
        event_id=f"sync_evt_{sequence}",
        idempotency_key=f"sync:{sequence}",
        sequence=sequence,
        span_id=f"{sequence:016x}",
    )
    return value


class FakeClient:
    def __init__(self, failures: list[str] | None = None) -> None:
        self.failures = list(failures or [])
        self.batches: list[list[dict]] = []
        self.outcomes: list[dict] = []
        self.uploaded = b""

    def _fail(self, boundary: str) -> None:
        if self.failures and self.failures[0] == boundary:
            self.failures.pop(0)
            raise RemoteError(None, f"disconnected at {boundary}", retry_after=0)

    def request(self, method, path, body, auth=True):
        if path == "/v1/ingest/batches":
            self._fail("events")
            self.batches.append(body["events"])
            return {"inserted": len(body["events"])}
        if path == "/v1/artifacts/descriptors":
            self._fail("descriptor")
            return {
                "status": "upload_required",
                "upload_id": "upload",
                "upload_instruction": {"method": "PUT", "url": "http://upload", "headers": {}},
            }
        if path == "/v1/outcomes":
            self._fail("outcome")
            self.outcomes.append(body)
            return {"outcome": {"created": True}}
        if path.endswith("/complete"):
            self._fail("complete")
            return {"status": "available"}
        raise AssertionError(path)

    def upload(self, instruction, path):
        self._fail("upload")
        self.uploaded = path.read_bytes()


def worker(paths: AgentdPaths, client: FakeClient) -> SynchronizationWorker:
    value = SynchronizationWorker.__new__(SynchronizationWorker)
    value.paths = paths
    value.config = SyncConfig("http://localhost:8000", "installation", poll_seconds=0.01)
    value.spool = SQLiteSpool(paths, Limits())
    value.client = client
    value.random = random.Random(1)
    return value


def test_offline_retry_then_ack_deletes_events_in_causal_order(tmp_path) -> None:
    paths = AgentdPaths(tmp_path / "agentd")
    spool = SQLiteSpool(paths, Limits())
    spool.ingest_events([event(3), event(1), event(2)])
    client = FakeClient(["events"])
    sync = worker(paths, client)
    assert sync.sync_once()["events"] == 0
    assert spool.status()["pending_events"] == 3
    assert sync.sync_once()["events"] == 3
    assert [item["sequence"] for item in client.batches[0]] == [1, 2, 3]
    assert spool.status()["events"] == 0


def test_permanent_event_rejection_dead_letters_without_deletion(tmp_path) -> None:
    paths = AgentdPaths(tmp_path / "agentd")
    spool = SQLiteSpool(paths, Limits())
    spool.ingest_events([event(1)])
    client = FakeClient()

    def reject(*args, **kwargs):
        raise RemoteError(403, "permanent")

    client.request = reject
    assert worker(paths, client).sync_once()["events"] == 0
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT upload_state FROM events").fetchone()[0] == "dead_letter"


def test_artifact_disconnects_at_every_boundary_do_not_lose_bytes(tmp_path) -> None:
    paths = AgentdPaths(tmp_path / "agentd")
    spool = SQLiteSpool(paths, Limits())
    content = b"artifact"
    descriptor = json.loads((FIXTURE / "artifact-descriptor.json").read_text(encoding="utf-8"))
    descriptor.update(
        artifact_id="sync_artifact",
        digest={"algorithm": "sha256", "value": hashlib.sha256(content).hexdigest()},
        size_bytes=len(content),
        storage_reference=None,
    )
    spool.register_artifact("run_001", descriptor, content)
    client = FakeClient(["descriptor", "upload", "complete"])
    sync = worker(paths, client)
    assert sync.sync_once()["artifacts"] == 0
    assert sync.sync_once()["artifacts"] == 0
    assert sync.sync_once()["artifacts"] == 0
    assert sync.sync_once()["artifacts"] == 1
    assert client.uploaded == content
    with sqlite3.connect(paths.database) as connection:
        assert (
            connection.execute("SELECT upload_state FROM artifacts").fetchone()[0] == "acknowledged"
        )


def test_offline_finalization_retries_and_uploads_one_outcome(tmp_path) -> None:
    paths = AgentdPaths(tmp_path / "agentd")
    spool = SQLiteSpool(paths, Limits())
    outcome = json.loads((FIXTURE / "outcome.json").read_text(encoding="utf-8"))
    run_id = outcome["run_id"]
    spool.finalize_run(run_id, {"outcome": outcome}, "2026-07-12T00:00:00Z")
    spool.finalize_run(run_id, {"outcome": outcome}, "2026-07-12T00:00:00Z")
    client = FakeClient(["outcome"])
    sync = worker(paths, client)

    assert sync.sync_once()["outcomes"] == 0
    assert spool.status()["pending_outcomes"] == 1
    assert sync.sync_once()["outcomes"] == 1
    assert client.outcomes == [outcome]
    assert sync.sync_once()["outcomes"] == 0
    with sqlite3.connect(paths.database) as connection:
        assert (
            connection.execute(
                "SELECT upload_state FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            == "acknowledged"
        )


def test_backoff_is_exponential_with_full_jitter_and_retry_after_wins(tmp_path) -> None:
    sync = worker(AgentdPaths(tmp_path / "agentd"), FakeClient())
    assert 0 <= sync._delay(3, None) <= 8
    assert sync._delay(10, 17) == 17


def test_retry_after_supports_seconds_and_http_dates() -> None:
    numeric = urllib.error.HTTPError(
        "http://localhost", 429, "limited", {"Retry-After": "2.5"}, BytesIO(b"limited")
    )
    assert ControlPlaneClient._http_error(numeric).retry_after == 2.5

    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    dated = urllib.error.HTTPError(
        "http://localhost", 503, "busy", {"Retry-After": future}, BytesIO(b"busy")
    )
    delay = ControlPlaneClient._http_error(dated).retry_after
    assert delay is not None and 28 <= delay <= 30


def test_version_one_spool_upgrades_without_losing_pending_event(tmp_path) -> None:
    paths = AgentdPaths(tmp_path / "agentd")
    paths.root.mkdir(parents=True)
    with sqlite3.connect(paths.database) as connection:
        connection.executescript(
            """
            CREATE TABLE runs(run_id TEXT PRIMARY KEY,trace_id TEXT,created_at TEXT NOT NULL,
              finalized_at TEXT,final_payload_json TEXT);
            CREATE TABLE events(event_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
              sequence_scope TEXT NOT NULL,sequence INTEGER NOT NULL,occurred_at TEXT NOT NULL,
              observed_at TEXT NOT NULL,payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
              upload_state TEXT NOT NULL DEFAULT 'offline',retry_count INTEGER NOT NULL DEFAULT 0,
              next_retry_at TEXT,UNIQUE(run_id,sequence_scope,sequence));
            CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,digest TEXT NOT NULL,
              size_bytes INTEGER NOT NULL,descriptor_json TEXT NOT NULL,storage_reference TEXT NOT NULL,
              upload_state TEXT NOT NULL DEFAULT 'offline',UNIQUE(run_id,digest));
            PRAGMA user_version=1;
            """
        )
        document = event(1)
        payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                document["event_id"],
                document["run_id"],
                document["sequence_scope"],
                1,
                document["occurred_at"],
                document["observed_at"],
                payload,
                hashlib.sha256(payload.encode()).hexdigest(),
                "offline",
                0,
                None,
            ),
        )
    spool = SQLiteSpool(paths, Limits())
    assert spool.status()["pending_events"] == 1
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert "dead_lettered_at" in {
            row[1] for row in connection.execute("PRAGMA table_info(events)")
        }
