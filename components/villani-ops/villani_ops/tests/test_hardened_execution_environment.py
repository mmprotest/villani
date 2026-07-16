from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from villani_ops.closed_loop.event_writer import redact_data
from villani_ops.execution_environment.container import ContainerProvider
from villani_ops.execution_environment.devcontainer import DevcontainerProvider
from villani_ops.execution_environment.models import (
    ActionPolicy,
    CommandResult,
    ExecutionEnvironmentConfig,
    SecretRequest,
)
from villani_ops.execution_environment.candidate_execution import (
    execute_candidate_command,
)
from villani_ops.execution_environment.security import (
    ExecutionPolicyDenied,
    check_command,
    check_file_mode,
    check_path,
    inspect_workspace,
)
from villani_ops.execution_environment.secrets import LocalSecretBroker
from villani_ops.execution_environment.providers import provider_from_configuration


def _container_config(**overrides) -> ExecutionEnvironmentConfig:
    value = {
        "provider": "container",
        "mode": "controlled",
        "container": {"image": "fixture:image"},
        "limits": {
            "timeout_seconds": 5,
            "cpu_count": 1.5,
            "memory_bytes": 134_217_728,
            "process_count": 8,
            "disk_bytes": 16_777_216,
            "tmpfs_bytes": 8_388_608,
        },
    }
    value.update(overrides)
    return ExecutionEnvironmentConfig.model_validate(value)


def _fake_runtime(
    monkeypatch,
    module,
    *,
    up_container_id: str = "container-fixture",
    execute_devcontainer: bool = False,
):
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/fake/{name}")
    original_run = subprocess.run

    def fake_run(command, **kwargs):
        args = [str(item) for item in command]
        if not args or not args[0].startswith("/fake/"):
            return original_run(command, **kwargs)
        if "--version" in args:
            return subprocess.CompletedProcess(command, 0, "fixture 1.0\n", "")
        if args[1:2] == ["info"]:
            return subprocess.CompletedProcess(command, 0, "ok", "")
        if "inspect" in args:
            return subprocess.CompletedProcess(command, 0, "sha256:fixture\n", "")
        if args[1:2] == ["up"]:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "outcome": "success",
                        "containerId": up_container_id,
                        "remoteUser": "vscode",
                    }
                )
                + "\n",
                "",
            )
        if execute_devcontainer and args[1:2] == ["exec"]:
            command_start = args.index(sys.executable)
            return original_run(args[command_start:], **kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)


def _host_container_popen(original_popen):
    class FakeContainerProcess:
        def __init__(self, command, **kwargs):
            args = list(command)
            inner = (
                args[args.index("fixture:image") + 1 :]
                if "fixture:image" in args
                else args
            )
            self._process = original_popen(inner, **kwargs)
            self.args = self._process.args
            self.stdout = self._process.stdout
            self.stderr = self._process.stderr

        @property
        def returncode(self):
            return self._process.returncode

        def poll(self):
            return self._process.poll()

        def wait(self, *args, **kwargs):
            return self._process.wait(*args, **kwargs)

        def kill(self):
            return self._process.kill()

        def communicate(self, *args, **kwargs):
            return self._process.communicate(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._process.__exit__(*args)

    return FakeContainerProcess


def test_container_uses_hard_runtime_limits_and_denied_network(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.container as module

    _fake_runtime(monkeypatch, module)
    provider = ContainerProvider(_container_config(), source_environment={})
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    prefix = provider.command_prefix(prepared)

    assert ["--network", "none"] == prefix[prefix.index("--network") :][:2]
    assert prefix[prefix.index("--cpus") + 1] == "1.5"
    assert prefix[prefix.index("--memory") + 1] == "134217728"
    assert prefix[prefix.index("--pids-limit") + 1] == "8"
    assert "--read-only" in prefix
    assert "--tmpfs" in prefix
    assert "--user" not in prefix
    assert "villani.execution.managed=true" in prefix
    provider.cleanup(prepared)
    provider.cleanup(prepared)


def test_named_execution_provider_selection_is_strict() -> None:
    configuration = {
        "execution_environment": {"provider": "inherit"},
        "execution_environments": {
            "sandbox": {
                "provider": "container",
                "mode": "controlled",
                "container": {"image": "fixture:image"},
            }
        },
    }
    selected = provider_from_configuration(
        configuration, selection="sandbox", source_environment={}
    )
    assert isinstance(selected, ContainerProvider)
    assert selected.config.container.network.mode == "deny"
    with pytest.raises(ValueError, match="not configured"):
        provider_from_configuration(configuration, selection="missing")


def test_network_defaults_and_verified_proxy_allowlist(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.container as module

    assert (
        ExecutionEnvironmentConfig(
            provider="container", container={"image": "fixture:image"}
        ).container.network.mode
        == "inherit"
    )
    _fake_runtime(monkeypatch, module)
    provider = ContainerProvider(
        _container_config(
            container={
                "image": "fixture:image",
                "network": {
                    "mode": "allowlist",
                    "allowed_domains": ["packages.example"],
                    "proxy_url": "http://policy-proxy:3128",
                    "proxy_network": "villani-policy-proxy",
                    "proxy_boundary_verified": True,
                },
            }
        ),
        source_environment={},
    )
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    prefix = provider.command_prefix(prepared)

    assert prefix[prefix.index("--network") + 1] == "villani-policy-proxy"
    assert prepared.policy_decisions[0]["allowlist_count"] == 1
    assert "http://policy-proxy:3128" not in json.dumps(prepared.durable_report())
    assert {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"} <= {
        prefix[index + 1] for index, value in enumerate(prefix[:-1]) if value == "--env"
    }
    provider.cleanup(prepared)


def test_container_secret_canary_is_name_forwarded_and_removed(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.container as module

    _fake_runtime(monkeypatch, module)
    canary = "secret-canary-container-91f8"
    config = _container_config(
        secrets=[
            {
                "name": "CANARY",
                "source": "environment",
                "target": "file",
                "target_name": "canary",
            },
            {
                "name": "TOKEN",
                "source": "environment",
                "target": "environment",
            },
        ]
    )
    provider = ContainerProvider(
        config, source_environment={"CANARY": canary, "TOKEN": canary}
    )
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    prefix = provider.command_prefix(prepared)
    serialized = json.dumps(prepared.durable_report()) + json.dumps(prefix)
    temporary = next(
        iter(
            provider._leases[
                str(prepared.runtime_state["container_name"])
            ].files.values()
        )
    )

    assert canary not in serialized
    assert temporary.read_text() == canary
    assert prefix[prefix.index("--env") + 1] == "TOKEN"
    provider.cleanup(prepared)
    provider.cleanup(prepared)
    assert not temporary.exists()
    assert canary not in json.dumps(prepared.durable_report())
    assert (
        prepared.durable_report()["runtime_state"]["secret_lease"][
            "temporary_files_cleaned"
        ]
        is True
    )


def test_parallel_preparations_keep_separate_secret_leases(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.container as module

    _fake_runtime(monkeypatch, module)
    first_tree = tmp_path / "first"
    second_tree = tmp_path / "second"
    first_tree.mkdir()
    second_tree.mkdir()
    provider = ContainerProvider(
        _container_config(
            secrets=[
                {
                    "name": "TOKEN",
                    "source": "environment",
                    "target": "file",
                }
            ]
        ),
        source_environment={"TOKEN": "parallel-secret-canary"},
    )
    first = provider.prepare(repository=tmp_path, worktree=first_tree)
    second = provider.prepare(repository=tmp_path, worktree=second_tree)
    assert first.fingerprint == second.fingerprint
    first_file = next(
        iter(
            provider._leases[str(first.runtime_state["container_name"])].files.values()
        )
    )
    second_file = next(
        iter(
            provider._leases[str(second.runtime_state["container_name"])].files.values()
        )
    )
    assert first_file != second_file

    provider.cleanup(first)
    assert not first_file.exists() and second_file.exists()
    provider.cleanup(second)
    assert not second_file.exists()


def test_container_fixture_runs_tests_and_produces_patch(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.container as module

    _fake_runtime(monkeypatch, module)
    original_popen = subprocess.Popen

    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n"
        "class T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n"
    )
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        module.subprocess, "Popen", _host_container_popen(original_popen)
    )
    provider = ContainerProvider(
        _container_config(policy={"command_allow": [Path(sys.executable).name]}),
        source_environment={},
    )
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    script = (
        "from pathlib import Path; import subprocess,sys;"
        "Path('calculator.py').write_text('def add(a, b):\\n    return a + b\\n');"
        "raise SystemExit(subprocess.run([sys.executable,'-m','unittest','-q']).returncode)"
    )
    result = provider.execute(prepared, [sys.executable, "-c", script])
    patch = subprocess.run(
        ["git", "diff", "--binary"], cwd=tmp_path, text=True, capture_output=True
    ).stdout

    assert result.exit_code == 0, result.stderr
    assert "return a + b" in patch
    provider.cleanup(prepared)


def test_container_abusive_commands_are_terminated_and_classified(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.container as module

    _fake_runtime(monkeypatch, module)
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        _host_container_popen(subprocess.Popen),
    )
    timeout_provider = ContainerProvider(
        _container_config(limits={"timeout_seconds": 1}), source_environment={}
    )
    timed = timeout_provider.prepare(repository=tmp_path, worktree=tmp_path)
    timeout_result = timeout_provider.execute(
        timed, [sys.executable, "-c", "import time; time.sleep(5)"]
    )
    assert timeout_result.exit_code == 124
    assert timeout_result.failure_classification == "timeout"
    timeout_provider.cleanup(timed)

    disk_provider = ContainerProvider(
        _container_config(limits={"timeout_seconds": 5, "disk_bytes": 1024}),
        source_environment={},
    )
    disk = disk_provider.prepare(repository=tmp_path, worktree=tmp_path)
    disk_result = disk_provider.execute(
        disk,
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import time; Path('abuse.bin').write_bytes(b'x'*4096); time.sleep(5)",
        ],
    )
    assert disk_result.exit_code == 125
    assert disk_result.failure_classification == "disk_limit"
    disk_provider.cleanup(disk)
    (tmp_path / "abuse.bin").unlink(missing_ok=True)

    pids_provider = ContainerProvider(_container_config(), source_environment={})
    pids = pids_provider.prepare(repository=tmp_path, worktree=tmp_path)
    pids_result = pids_provider.execute(
        pids,
        [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('fork: Resource temporarily unavailable\\n'); raise SystemExit(1)",
        ],
    )
    assert pids_result.failure_classification == "process_limit"
    assert pids_result.process_limit_exceeded is True
    pids_provider.cleanup(pids)


def test_devcontainer_uses_documented_up_exec_boundary_and_hardened_config(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.devcontainer as module

    _fake_runtime(monkeypatch, module)
    config_dir = tmp_path / ".devcontainer"
    config_dir.mkdir()
    (config_dir / "devcontainer.json").write_text(
        '{// jsonc\n"image":"fixture:image",}', encoding="utf-8"
    )
    config = ExecutionEnvironmentConfig.model_validate(
        {
            "provider": "devcontainer",
            "mode": "controlled",
            "limits": {"process_count": 7, "memory_bytes": 134_217_728},
        }
    )
    provider = DevcontainerProvider(config, source_environment={})
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    hardened = json.loads(
        Path(prepared.runtime_state["config_path"]).read_text(encoding="utf-8")
    )
    prefix = provider.command_prefix(prepared)

    assert prefix[1] == "exec"
    assert "--config" in prefix and "--workspace-folder" in prefix
    assert hardened["image"] == "fixture:image"
    assert hardened["runArgs"][hardened["runArgs"].index("--network") + 1] == "none"
    assert hardened["runArgs"][hardened["runArgs"].index("--pids-limit") + 1] == "7"
    temporary = Path(prepared.runtime_state["config_path"]).parent
    provider.cleanup(prepared)
    provider.cleanup(prepared)
    assert not temporary.exists()


def test_devcontainer_fixture_runs_tests_and_produces_patch(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.devcontainer as module

    _fake_runtime(monkeypatch, module, execute_devcontainer=True)
    config_dir = tmp_path / ".devcontainer"
    config_dir.mkdir()
    (config_dir / "devcontainer.json").write_text(
        json.dumps({"image": "fixture:image"}), encoding="utf-8"
    )
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a - b\n")
    (tmp_path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n"
        "class T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n"
    )
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    provider = DevcontainerProvider(
        ExecutionEnvironmentConfig(
            provider="devcontainer",
            mode="controlled",
            policy={"command_allow": [Path(sys.executable).name]},
        ),
        source_environment={},
    )
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    script = (
        "from pathlib import Path; import subprocess,sys;"
        "Path('calculator.py').write_text('def add(a, b):\\n    return a + b\\n');"
        "raise SystemExit(subprocess.run([sys.executable,'-m','unittest','-q']).returncode)"
    )
    result = provider.execute(prepared, [sys.executable, "-c", script])
    patch = subprocess.run(
        ["git", "diff", "--binary"], cwd=tmp_path, text=True, capture_output=True
    ).stdout

    assert result.exit_code == 0, result.stderr
    assert "return a + b" in patch
    provider.cleanup(prepared)


def test_candidate_execution_dispatches_through_container_provider(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.container as module

    _fake_runtime(monkeypatch, module)
    provider = ContainerProvider(_container_config(), source_environment={})
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    calls: list[list[str]] = []

    def execute(prepared_value, argv):
        assert prepared_value is prepared
        calls.append(list(argv))
        return CommandResult(
            exit_code=0,
            duration_ms=1,
            stdout="ok",
            stderr="",
            stdout_bytes=2,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            disk_limit_exceeded=False,
            process_limit_exceeded=False,
        )

    monkeypatch.setattr(provider, "execute", execute)
    result = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=["fixture-validation"],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="container",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )

    assert result.status == "passed"
    assert result.execution_provider == "container"
    assert calls == [["fixture-validation"]]
    provider.cleanup(prepared)


def test_candidate_execution_dispatches_through_devcontainer_provider(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.devcontainer as module

    _fake_runtime(monkeypatch, module)
    config_dir = tmp_path / ".devcontainer"
    config_dir.mkdir()
    (config_dir / "devcontainer.json").write_text(
        json.dumps({"image": "fixture:image"}), encoding="utf-8"
    )
    provider = DevcontainerProvider(
        ExecutionEnvironmentConfig(
            provider="devcontainer",
            mode="controlled",
        ),
        source_environment={},
    )
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    calls: list[list[str]] = []

    def execute(prepared_value, argv):
        assert prepared_value is prepared
        calls.append(list(argv))
        return CommandResult(
            exit_code=0,
            duration_ms=1,
            stdout="ok",
            stderr="",
            stdout_bytes=2,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            disk_limit_exceeded=False,
            process_limit_exceeded=False,
        )

    monkeypatch.setattr(provider, "execute", execute)
    result = execute_candidate_command(
        provider=provider,
        prepared_environment=prepared,
        argv=["fixture-validation"],
        command_role="repository_validation",
        run_id="run_1",
        attempt_id="attempt_001",
        validation_id="devcontainer",
        baseline_sha256="a" * 64,
        candidate_state="post_mutation",
    )

    assert result.status == "passed"
    assert result.execution_provider == "devcontainer"
    assert calls == [["fixture-validation"]]
    provider.cleanup(prepared)


@pytest.mark.parametrize(
    ("feature", "diagnostic"),
    [
        ({"dockerComposeFile": "compose.yml", "service": "app"}, "Compose"),
        ({"postCreateCommand": "curl bad.example"}, "lifecycle"),
        ({"privileged": True}, "privileged"),
        ({"mounts": ["source=/,target=/host,type=bind"]}, "mounts"),
    ],
)
def test_devcontainer_refuses_unsupported_features(
    tmp_path: Path, monkeypatch, feature: dict[str, object], diagnostic: str
) -> None:
    import villani_ops.execution_environment.devcontainer as module

    _fake_runtime(monkeypatch, module)
    config_dir = tmp_path / ".devcontainer"
    config_dir.mkdir()
    (config_dir / "devcontainer.json").write_text(
        json.dumps({"image": "fixture:image", **feature}), encoding="utf-8"
    )
    provider = DevcontainerProvider(
        ExecutionEnvironmentConfig(provider="devcontainer"), source_environment={}
    )
    with pytest.raises(RuntimeError, match=diagnostic):
        provider.prepare(repository=tmp_path, worktree=tmp_path)


def test_command_path_and_domain_policies_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ExecutionPolicyDenied) as denied:
        check_command(
            ["curl", "https://blocked.example"], ActionPolicy(command_deny=["curl"])
        )
    assert denied.value.event["decision"] == "deny"
    with pytest.raises(ExecutionPolicyDenied, match="traversal"):
        check_path(tmp_path.parent / "outside", tmp_path, ActionPolicy())
    with pytest.raises(ValueError, match="domain policies require"):
        _container_config(
            mode="local",
            container={
                "image": "fixture:image",
                "network": {"mode": "inherit", "denied_domains": ["bad.example"]},
            },
        )
    with pytest.raises(ValueError, match="proxy_boundary_verified"):
        _container_config(
            container={
                "image": "fixture:image",
                "network": {"mode": "allowlist", "allowed_domains": ["good.example"]},
            }
        )


def test_denied_command_fails_before_process_spawn(tmp_path: Path, monkeypatch) -> None:
    import villani_ops.execution_environment.container as module

    _fake_runtime(monkeypatch, module)
    provider = ContainerProvider(
        _container_config(policy={"command_deny": ["curl"]}),
        source_environment={},
    )
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)

    def unexpected_spawn(*args, **kwargs):
        raise AssertionError("denied command reached process creation")

    monkeypatch.setattr(module.subprocess, "Popen", unexpected_spawn)
    with pytest.raises(ExecutionPolicyDenied) as denied:
        provider.execute(prepared, ["curl", "https://blocked.example"])
    assert denied.value.event == {
        "schema_version": "villani.execution_policy_event.v1",
        "decision": "deny",
        "policy": "command",
        "action": "curl",
        "reason": "matched command deny policy",
    }
    provider.cleanup(prepared)


def test_workspace_rejects_symlink_oversized_and_decompression_bomb(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-canary"
    outside.write_text("outside")
    link = tmp_path / "link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    with pytest.raises(ExecutionPolicyDenied, match="symlink"):
        inspect_workspace(tmp_path, ActionPolicy())
    link.unlink()
    (tmp_path / "large.bin").write_bytes(b"x" * 128)
    with pytest.raises(ExecutionPolicyDenied, match="oversized"):
        inspect_workspace(tmp_path, ActionPolicy(max_file_bytes=64))
    (tmp_path / "large.bin").unlink()
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("huge.txt", b"0" * 1_000_000)
    with pytest.raises(ExecutionPolicyDenied, match="compression-ratio"):
        inspect_workspace(tmp_path, ActionPolicy(max_archive_ratio=2.0))


def test_workspace_rejects_socket(tmp_path: Path) -> None:
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("host Python does not support Unix-domain sockets")
    socket_path = tmp_path / "fixture.sock"
    try:
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(socket_path))
    except OSError as error:
        pytest.skip(f"host cannot create a Unix-domain socket: {error}")
    try:
        with pytest.raises(ExecutionPolicyDenied, match="socket"):
            inspect_workspace(tmp_path, ActionPolicy())
    finally:
        server.close()
    socket_path.unlink(missing_ok=True)


def test_workspace_rejects_fifo(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("host Python does not support FIFO creation")
    fifo = tmp_path / "fixture.fifo"
    try:
        os.mkfifo(fifo)
    except OSError as error:
        pytest.skip(f"host cannot create a FIFO: {error}")
    with pytest.raises(ExecutionPolicyDenied, match="device or fifo"):
        inspect_workspace(tmp_path, ActionPolicy())


def test_device_file_modes_are_rejected_portably() -> None:
    with pytest.raises(ExecutionPolicyDenied, match="device or fifo"):
        check_file_mode(stat.S_IFCHR, "host-device")


def test_secret_broker_command_source_redacts_and_cleans_on_failure(
    tmp_path: Path,
) -> None:
    canary = "secret-canary-command-44a1"
    broker = LocalSecretBroker(source_environment={"CANARY": canary})
    lease = broker.acquire(
        [
            SecretRequest(
                name="FROM_COMMAND",
                source="command",
                command_argv=[sys.executable, "-c", f"print({canary!r})"],
                target="file",
            )
        ]
    )
    secret_file = next(iter(lease.files.values()))
    assert redact_data(f"value={canary}") == "value=[REDACTED]"
    lease.cleanup()
    lease.cleanup()
    assert not secret_file.exists()
    with pytest.raises(RuntimeError, match="exit code"):
        broker.acquire(
            [
                SecretRequest(
                    name="FAIL",
                    source="command",
                    command_argv=[sys.executable, "-c", "raise SystemExit(7)"],
                )
            ]
        )


def test_secret_file_traversal_is_refused_and_partial_failure_cleans(
    tmp_path: Path, monkeypatch
) -> None:
    with pytest.raises(ValueError, match="portable file name"):
        SecretRequest(name="TOKEN", target="file", target_name="../escape")

    secret_root = tmp_path / "secrets"
    import villani_ops.execution_environment.secrets as module

    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: str(secret_root))
    secret_root.mkdir()
    broker = LocalSecretBroker(source_environment={"FIRST": "canary-first"})
    with pytest.raises(RuntimeError, match="required secret"):
        broker.acquire(
            [
                SecretRequest(name="FIRST", target="file"),
                SecretRequest(name="MISSING", target="file"),
            ]
        )
    assert not secret_root.exists()


def test_secret_broker_scavenges_files_from_crashed_owner(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.secrets as module

    stale = tmp_path / "villani-secret-999999-stale"
    stale.mkdir()
    (stale / "canary").write_text("crash-canary", encoding="utf-8")
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))

    LocalSecretBroker(source_environment={})

    assert not stale.exists()


def test_pids_limit_is_present_for_fork_bomb_containment(
    tmp_path: Path, monkeypatch
) -> None:
    import villani_ops.execution_environment.container as module

    _fake_runtime(monkeypatch, module)
    provider = ContainerProvider(_container_config(), source_environment={})
    prepared = provider.prepare(repository=tmp_path, worktree=tmp_path)
    prefix = provider.command_prefix(prepared)
    assert prefix[prefix.index("--pids-limit") + 1] == "8"
    provider.cleanup(prepared)
