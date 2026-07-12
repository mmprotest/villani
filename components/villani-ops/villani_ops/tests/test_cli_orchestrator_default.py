from typer.testing import CliRunner
import httpx
import json
import pytest

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


def _configured_run(tmp_path, monkeypatch, *, failure=None):
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
                "http://127.0.0.1:9/v1",
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
    assert (
        "Villani Ops run failed" in res.output
        and "Run directory:" in res.output
        and "Next step:" in res.output
    )
    state = json.loads((rd / "state.json").read_text())
    assert state["status"] == "failed" and state["failure_kind"] == expected_kind
    assert (
        (rd / "final_report.md").exists()
        and (rd / "event_digest.json").exists()
        and (rd / "usage.json").exists()
    )
    assert "provider_failure" in (rd / "runtime_events.jsonl").read_text()


def test_run_missing_local_server_finalizes_without_traceback(tmp_path, monkeypatch):
    res, rd = _configured_run(tmp_path, monkeypatch)
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
