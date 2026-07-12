from typer.testing import CliRunner
import httpx
import json
import pytest
import socket

from villani_ops.cli.main import app, run


def test_run_help_shows_adaptive_default_and_graph_legacy():
    res = CliRunner().invoke(app, ["run", "--help"])
    assert res.exit_code == 0
    defaults = run.__defaults__
    orchestrator_default = defaults[12].default
    orchestrator_help = defaults[12].help
    assert orchestrator_default == "adaptive"
    assert "adaptive (default" in orchestrator_help
    assert "agentic (decomposition-capable)" in orchestrator_help
    assert "graph (explicit legacy)" in orchestrator_help


def _hostile_proxy_environment(monkeypatch):
    proxy = "http://proxy-user:proxy-password@127.0.0.1:41295"
    for name in ("ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "socks5h://127.0.0.1:36363")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, proxy)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")


def _closed_port(family=socket.AF_INET, host="127.0.0.1"):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _configured_run(tmp_path, monkeypatch, *, failure=None, base_url=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("x")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "backend",
                "add",
                "local",
                "--provider",
                "local",
                "--base-url",
                base_url or "http://127.0.0.1:9/v1",
                "--model",
                "m",
                "--roles",
                "coding,review,selection,policy,investigation",
            ],
        ).exit_code
        == 0
    )
    if failure is not None:

        def fail(*_args, **_kwargs):
            raise failure

        monkeypatch.setattr(
            "villani_ops.agentic.runner.ToolCallingLLMClient.create_message", fail
        )
    res = runner.invoke(app, ["run", "--repo", str(repo), "--task", "do x", "--no-ui"])
    rd = next((tmp_path / ".villani-ops" / "runs").iterdir())
    return res, rd


def _assert_finalized_failure(res, rd, expected_kind):
    assert res.exit_code != 0
    assert "Traceback" not in res.output and "ConnectError" not in res.output
    assert "socksio" not in res.output.lower()
    assert (
        "Villani Ops run failed" in res.output
        and "Run directory:" in res.output
        and "Next step:" in res.output
    )
    state = json.loads((rd / "state.json").read_text())
    assert state["status"] == "failed" and state["failure_kind"] == expected_kind
    if expected_kind == "backend_connection_error":
        assert state["recoverable"] is True
    assert (
        (rd / "final_report.md").exists()
        and (rd / "event_digest.json").exists()
        and (rd / "usage.json").exists()
    )
    runtime_events = [
        json.loads(line)
        for line in (rd / "runtime_events.jsonl").read_text().splitlines()
    ]
    provider_failure = next(
        event for event in runtime_events if event["type"] == "provider_failure"
    )
    assert provider_failure["payload"]["failure_kind"] == expected_kind
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in rd.rglob("*")
        if path.is_file()
    )
    assert "socksio" not in persisted.lower()
    assert "proxy-user" not in persisted and "proxy-password" not in persisted


def test_run_missing_local_server_finalizes_without_traceback(tmp_path, monkeypatch):
    _hostile_proxy_environment(monkeypatch)
    port = _closed_port()
    res, rd = _configured_run(
        tmp_path, monkeypatch, base_url=f"http://127.0.0.1:{port}/v1"
    )
    _assert_finalized_failure(res, rd, "backend_connection_error")


def test_run_missing_localhost_server_bypasses_hostile_proxy(tmp_path, monkeypatch):
    _hostile_proxy_environment(monkeypatch)
    port = _closed_port()
    res, rd = _configured_run(
        tmp_path, monkeypatch, base_url=f"http://localhost:{port}/v1"
    )
    _assert_finalized_failure(res, rd, "backend_connection_error")


def test_run_missing_ipv6_loopback_server_bypasses_hostile_proxy(
    tmp_path, monkeypatch
):
    try:
        port = _closed_port(socket.AF_INET6, "::1")
    except OSError as exc:
        pytest.skip(f"host cannot create an IPv6 loopback socket: {exc}")
    _hostile_proxy_environment(monkeypatch)
    res, rd = _configured_run(
        tmp_path, monkeypatch, base_url=f"http://[::1]:{port}/v1"
    )
    _assert_finalized_failure(res, rd, "backend_connection_error")


@pytest.mark.parametrize(
    ("failure", "expected_kind"),
    [
        (ConnectionRefusedError("connection refused"), "backend_connection_error"),
        (
            httpx.ConnectTimeout("timed out during connection establishment"),
            "backend_connection_error",
        ),
        (RuntimeError("internal runner protocol violation"), "runner_error"),
    ],
)
def test_public_cli_preserves_backend_failure_category(
    tmp_path, monkeypatch, failure, expected_kind
):
    res, rd = _configured_run(tmp_path, monkeypatch, failure=failure)
    _assert_finalized_failure(res, rd, expected_kind)
