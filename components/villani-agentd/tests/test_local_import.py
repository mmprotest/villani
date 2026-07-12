from __future__ import annotations

import json
import shutil
import sqlite3
import random
from pathlib import Path

from villani_agentd.config import AgentdPaths, Limits, SyncConfig
from villani_agentd.local_import import LocalRunImporter
from villani_agentd.spool import SQLiteSpool
from villani_agentd.uploader import SynchronizationWorker
from villani_ops.execution_environment.secrets import register_secret_values


ROOT = Path(__file__).resolve().parents[3]
CANONICAL = ROOT / "integration" / "fixtures" / "protocol" / "v1" / "valid_run"


def copy_run(home: Path, run_id: str = "run_protocol_fixture") -> Path:
    destination = home / "runs" / run_id
    shutil.copytree(CANONICAL, destination)
    if run_id != "run_protocol_fixture":
        for name in ("manifest.json", "state.json"):
            path = destination / name
            value = json.loads(path.read_text(encoding="utf-8"))
            value["run_id"] = run_id
            path.write_text(json.dumps(value), encoding="utf-8")
        events = []
        for line in (destination / "events.jsonl").read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            value["run_id"] = run_id
            events.append(json.dumps(value))
        (destination / "events.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
    return destination


def counts(database: Path) -> tuple[int, int, int, int]:
    with sqlite3.connect(database) as connection:
        return tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("runs", "events", "artifacts", "local_run_imports")
        )


def test_absent_agentd_run_is_backfilled_with_original_identity_and_no_duplicates(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    copy_run(home)
    paths = AgentdPaths(home / "agentd")
    importer = LocalRunImporter(paths, Limits())

    first = importer.run_once()
    assert first["counts"]["imported"] == 1
    assert counts(paths.database) == (1, 24, 4, 1)
    with sqlite3.connect(paths.database) as connection:
        run = connection.execute("SELECT run_id,final_payload_json FROM runs").fetchone()
        assert run[0] == "run_protocol_fixture"
        assert json.loads(run[1])["outcome"]["run_id"] == "run_protocol_fixture"
        documents = [
            json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM events")
        ]
    assert {row["attributes"]["villani.legacy.event_id"] for row in documents} == {
        f"evt_{sequence:03d}" for sequence in range(1, 25)
    }

    second = importer.run_once()
    assert second["counts"]["already_imported"] == 1
    assert counts(paths.database) == (1, 24, 4, 1)


def test_partial_run_resumes_sequences_under_one_run_identity(tmp_path: Path) -> None:
    home = tmp_path / "home"
    run = copy_run(home)
    full_events = (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
    (run / "events.jsonl").write_text("\n".join(full_events[:5]) + "\n", encoding="utf-8")
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest.update(
        completed_at=None, final_state="CLASSIFIED", attempt_ids=[], selected_attempt_id=None
    )
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    state = json.loads((run / "state.json").read_text(encoding="utf-8"))
    state.update(
        state="CLASSIFIED",
        previous_state="CLASSIFYING",
        terminal=False,
        last_event_id="evt_005",
        last_sequence=5,
        attempt_count=0,
        accepted_candidate_ids=[],
    )
    (run / "state.json").write_text(json.dumps(state), encoding="utf-8")
    paths = AgentdPaths(home / "agentd")
    importer = LocalRunImporter(paths, Limits())
    assert importer.run_once()["diagnostics"][0]["imported_events"] == 5

    shutil.rmtree(run)
    copy_run(home)
    assert importer.run_once()["diagnostics"][0]["imported_events"] == 19
    assert counts(paths.database)[:2] == (1, 24)


def test_corrupt_run_does_not_block_later_valid_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    corrupt = home / "runs" / "a-corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "manifest.json").write_text("{broken", encoding="utf-8")
    (corrupt / "state.json").write_text("{}", encoding="utf-8")
    (corrupt / "events.jsonl").write_text("", encoding="utf-8")
    copy_run(home, "z-valid")

    report = LocalRunImporter(AgentdPaths(home / "agentd"), Limits()).run_once()
    assert [row["category"] for row in report["diagnostics"]] == ["malformed", "imported"]


def test_registered_secret_artifact_never_enters_spool(tmp_path: Path) -> None:
    home = tmp_path / "home"
    run = copy_run(home)
    canary = "release-backfill-secret-canary-91c4"
    register_secret_values([canary])
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    manifest["metadata"]["unsafe"] = canary
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    paths = AgentdPaths(home / "agentd")

    report = LocalRunImporter(paths, Limits()).run_once()
    assert report["counts"]["sensitive_content_rejected"] == 1
    assert canary.encode() not in paths.database.read_bytes()
    for artifact in paths.artifacts.rglob("*"):
        if artifact.is_file():
            assert canary.encode() not in artifact.read_bytes()


def test_absent_agentd_offline_backfill_then_control_plane_sync_is_exactly_once(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    copy_run(home)
    paths = AgentdPaths(home / "agentd")
    assert not paths.database.exists()
    LocalRunImporter(paths, Limits()).run_once()

    class Remote:
        def __init__(self) -> None:
            self.events: dict[str, dict] = {}
            self.outcomes: dict[str, dict] = {}

        def request(self, method, path, body, auth=True):
            if path == "/v1/ingest/batches":
                for event in body["events"]:
                    self.events.setdefault(event["event_id"], event)
                return {"inserted": len(body["events"])}
            if path == "/v1/outcomes":
                self.outcomes.setdefault(body["run_id"], body)
                return {"outcome": {"created": True}}
            if path == "/v1/artifacts/descriptors":
                return {"status": "already_present"}
            raise AssertionError((method, path))

    remote = Remote()
    sync = SynchronizationWorker.__new__(SynchronizationWorker)
    sync.paths = paths
    sync.config = SyncConfig("http://localhost:8000", "installation", batch_size=100)
    sync.spool = SQLiteSpool(paths, Limits())
    sync.client = remote
    sync.random = random.Random(1)

    assert sync.sync_once() == {"events": 24, "artifacts": 2, "outcomes": 1}
    assert sync.sync_once() == {"events": 0, "artifacts": 2, "outcomes": 0}
    assert sync.sync_once() == {"events": 0, "artifacts": 0, "outcomes": 0}
    assert len(remote.events) == 24
    assert set(remote.outcomes) == {"run_protocol_fixture"}
