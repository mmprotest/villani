from __future__ import annotations

import http.cookiejar
import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from villani_agentd.config import AgentdPaths, Limits, ServerConfig, SyncConfig
from villani_agentd.console import ConsoleDataError, ConsoleService, VfrConsoleBridge
from villani_agentd.server import AgentdHTTPServer
from villani_agentd.spool import SQLiteSpool
from villani_agentd.structured_log import StructuredLogger


class FakeBridge:
    def history(self, *, refresh: bool = False) -> dict[str, Any]:
        return {
            "schema_version": "villani.console.history.v1",
            "entries": [
                {
                    "id": "run_same",
                    "logical_id": "run_same",
                    "kind": "run",
                    "source": "villani",
                    "source_label": "Villani",
                    "provider": "villani",
                    "repository": "repo",
                    "task": "Canonical task",
                    "status": "completed",
                    "model": "model-a",
                    "started_at": "2026-07-14T00:00:00Z",
                    "updated_at": "2026-07-14T00:01:00Z",
                    "duration_ms": 60_000,
                    "cost": None,
                    "currency": None,
                    "cost_available": False,
                    "synchronization_state": "LOCAL",
                    "deep_link": "/console/runs/run_same",
                },
                {
                    "id": "claude_1",
                    "logical_id": "claude_1",
                    "kind": "session",
                    "source": "claude",
                    "source_label": "Claude Code",
                    "provider": "claude",
                    "repository": "repo",
                    "task": "Imported task",
                    "status": "success",
                    "model": "claude-model",
                    "started_at": "2026-07-13T00:00:00Z",
                    "updated_at": "2026-07-13T00:01:00Z",
                    "duration_ms": 60_000,
                    "cost": None,
                    "currency": None,
                    "cost_available": False,
                    "synchronization_state": "LOCAL",
                    "deep_link": "/console/sessions/claude_1",
                },
            ],
            "warnings": [],
            "refreshed": refresh,
        }

    def replay(self, record_id: str, kind: str) -> dict[str, Any]:
        return {
            "schema_version": "villani.console.replay.v1",
            "id": record_id,
            "logical_id": record_id,
            "kind": kind,
            "source": "villani" if kind == "run" else "claude",
            "source_label": "Villani" if kind == "run" else "Claude Code",
            "provider": "villani" if kind == "run" else "claude",
            "synchronization_state": "LOCAL",
            "summary": {},
            "events": [],
            "attempts": [],
            "evidence": {},
            "verification": {},
            "candidate_comparison": [],
            "files": [],
            "artifacts": [],
            "cost": {},
            "logs": [],
            "canonical": None,
            "warnings": [],
            "deep_links": {"self": f"/console/{kind}s/{record_id}"},
        }


@pytest.fixture
def console(tmp_path: Path) -> tuple[ConsoleService, SQLiteSpool, AgentdPaths]:
    paths = AgentdPaths(tmp_path / "home" / "agentd")
    spool = SQLiteSpool(paths, Limits())
    return ConsoleService(paths, spool, bridge=FakeBridge()), spool, paths


def test_merged_history_has_no_duplicate_logical_run(console) -> None:
    service, spool, _paths = console
    spool.register_run("run_same", "trace", "2026-07-14T00:00:00Z")
    document = service.history()
    assert document["schema_version"] == "villani.console.history.v1"
    assert [item["logical_id"] for item in document["entries"]].count("run_same") == 1
    assert {item["source"] for item in document["entries"]} == {"villani", "claude"}
    run = next(item for item in document["entries"] if item["logical_id"] == "run_same")
    assert run["task"] == "Canonical task"
    assert run["synchronization_state"] == "LOCAL"
    assert "sourcePath" not in json.dumps(document)


def test_sync_states_are_authoritative_and_replay_uses_same_state(console) -> None:
    service, spool, paths = console
    spool.register_run("run_same", "trace", "2026-07-14T00:00:00Z")
    SyncConfig("https://workspace.invalid", "workspace_1").save(paths.sync_config)
    assert spool.console_run_states(True)["run_same"] == "SYNC PENDING"
    with sqlite3.connect(paths.database) as connection:
        connection.execute("UPDATE runs SET upload_state='acknowledged' WHERE run_id='run_same'")
    assert spool.console_run_states(True)["run_same"] == "SYNCHRONIZED"
    assert service.replay("run_same", "run")["synchronization_state"] == "SYNCHRONIZED"
    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            "UPDATE runs SET final_payload_json=? WHERE run_id='run_same'",
            ('{"villani_redaction":{"status":"redacted"}}',),
        )
    assert spool.console_run_states(True)["run_same"] == "REDACTED"
    with sqlite3.connect(paths.database) as connection:
        connection.execute("UPDATE runs SET upload_state='dead_letter' WHERE run_id='run_same'")
    assert spool.console_run_states(True)["run_same"] == "SYNC FAILED"
    fleet = service.workspace("fleet")
    assert fleet["connected"] is True
    assert fleet["items"] == [
        {
            "id": "run_same",
            "status": "SYNC FAILED",
            "summary": "Canonical task",
            "detail": "repo",
            "deep_link": "/console/runs/run_same",
        }
    ]
    costs = service.workspace("costs")
    assert costs["items"][0]["status"] == "UNKNOWN"
    assert costs["items"][0]["cost"] is None


def test_bootstrap_is_secret_safe_and_team_navigation_contract_is_enrolment_gated(
    console,
) -> None:
    service, _spool, paths = console
    home = paths.root.parent
    (home / "config.yaml").write_text(
        """config_version: 1
backends:
  default:
    provider: openai-compatible
    base_url: http://127.0.0.1:1234/v1
    model: local-model
    api_key_env: TOP_SECRET_ENV
    metadata:
      capability_status: unrated
policy:
  version: bootstrap_v1
""",
        encoding="utf-8",
    )
    local = service.bootstrap()
    assert local["mode"] == "local" and local["workspace"]["connected"] is False
    assert local["models"][0]["capability"] == "unrated"
    assert "TOP_SECRET_ENV" not in json.dumps(local)
    SyncConfig("https://workspace.invalid", "workspace_1").save(paths.sync_config)
    connected = service.bootstrap()
    assert connected["mode"] == "connected"
    assert connected["workspace"]["connected"] is True


def test_console_cookie_authenticates_only_console_gets(console) -> None:
    service, spool, paths = console
    server = AgentdHTTPServer(
        ("127.0.0.1", 0),
        "test-token",
        spool,
        ServerConfig(),
        StructuredLogger(paths.log),
        service,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    try:
        jar = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar)
        )
        with browser.open(f"{endpoint}/console/history") as response:
            assert "Villani Console" in response.read().decode("utf-8")
        with browser.open(f"{endpoint}/v1/console/bootstrap") as response:
            assert json.loads(response.read())["data_source"] == "local-service"
        with pytest.raises(urllib.error.HTTPError) as caught:
            browser.open(f"{endpoint}/v1/status")
        assert caught.value.code == 401
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as caught:
            direct.open(f"{endpoint}/v1/console/history")
        assert caught.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_packaged_console_references_existing_assets() -> None:
    root = Path(__file__).parents[1] / "villani_agentd" / "console_assets"
    html = (root / "index.html").read_text(encoding="utf-8")
    manifest = json.loads((root / "console-assets.json").read_text(encoding="utf-8"))
    assert "Villani Console" in html
    assert all((root / name).is_file() for name in manifest["files"])


def test_replay_bridge_redacts_child_process_diagnostics(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import sys\nsys.stderr.write('api_key=super-secret-value')\nraise SystemExit(7)\n",
        encoding="utf-8",
    )
    bridge = VfrConsoleBridge(command=[sys.executable, str(adapter)])
    with pytest.raises(ConsoleDataError) as caught:
        bridge.history()
    assert "super-secret-value" not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)
