from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Sequence

import typer

from villani_ops.cli.unified import app
from villani_ops.closed_loop.agent_systems.discovery import discover_agent_harnesses
from villani_ops.closed_loop.agent_systems.management import (
    DoctorStatus,
    detect_cli_agent_systems,
    validate_cli_model,
    write_management_evidence,
)
from villani_ops.closed_loop.agent_systems.registry import build_agent_system_registry
from villani_ops.closed_loop.agent_systems.role_models import CliAgentSystemConfig
from villani_ops.diagnostics import RepositoryDiagnosticError
from villani_ops.self_service.entitlements import (
    EntitlementError,
    development_license_bytes,
    install_license,
    load_entitlement,
)

from .migrations import MigrationError, check_upgrade
from .maintenance import CleanupError, cleanup
from .diagnostics import (
    render_human,
    render_json,
    run_doctor,
)
from .onboarding import (
    ProviderDetection,
    SetupError,
    build_cli_configuration,
    build_configuration,
    create_sample_repository,
    detect_providers,
    detect_repository,
    detect_session_sources,
    load_configuration,
    recommend_backend,
    run_capability_probe,
    run_sample_task,
    test_backend,
    validate_configuration,
    write_configuration_atomic,
    write_setup_record,
)
from .services import (
    ServiceError,
    install_service,
    restart_service,
    service_status,
    start_service,
    stop_service,
    uninstall_service,
    villani_home,
)
from .support_bundle import SupportBundleBuilder, SupportBundleError
from .update_system import UpdateError, UpdateManager, installed_version

# The distribution owns the friendly public versions of these commands. Keep
# the lower-level commands available in the internal component CLI only.
app.registered_commands[:] = [
    command for command in app.registered_commands if command.name not in {"doctor", "open"}
]

service_app = typer.Typer(
    help="Manage the local Villani Service.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(service_app, name="service")
update_app = typer.Typer(
    help="Check, preview, install, and roll back user-controlled updates.",
    no_args_is_help=True,
    add_completion=False,
)
license_app = typer.Typer(
    help="Inspect and install offline Villani entitlements.",
    no_args_is_help=True,
    add_completion=False,
)
support_app = typer.Typer(
    help="Preview and create a privacy-preserving local support archive.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(update_app, name="update")
app.add_typer(license_app, name="license")
app.add_typer(support_app, name="support")


def _self_service_error(error: Exception) -> None:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(2) from error


@app.command("version")
def version_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Print the exact canonical product version."""

    value = {
        "schema_version": "villani.version.v1",
        "version": installed_version(),
        "components_share_version": True,
    }
    typer.echo(json.dumps(value, sort_keys=True) if json_output else f"Villani {value['version']}")


@update_app.command("status")
def update_status_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Read update state without making a network request."""

    try:
        state = UpdateManager(villani_home()).status()
    except UpdateError as error:
        _self_service_error(error)
    value = state.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True))
        return
    typer.echo(f"Installed: {state.installed_version}")
    typer.echo(f"Channel: {state.policy.channel}")
    typer.echo(f"State: {state.status}")
    typer.echo("Automatic updates: disabled (updates are always user controlled)")
    if state.available_version:
        typer.echo(f"Available: {state.available_version}")
    if state.release_notes:
        typer.echo(f"Release notes: {state.release_notes}")


@update_app.command("channel")
def update_channel_command(
    channel: str = typer.Argument(..., help="stable, beta, or pinned"),
    pinned_version: str | None = typer.Option(None, "--version"),
    feed: str | None = typer.Option(None, "--feed"),
    checks_enabled: bool | None = typer.Option(
        None, "--checks/--no-checks", help="Enable or disable explicit update checks."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Select a channel; this never checks or installs automatically."""

    try:
        policy = UpdateManager(villani_home()).set_policy(
            channel,
            pinned_version=pinned_version,
            feed_url=feed,
            checks_enabled=checks_enabled,
        )
    except UpdateError as error:
        _self_service_error(error)
    value = policy.model_dump(mode="json")
    typer.echo(
        json.dumps(value, sort_keys=True)
        if json_output
        else f"Update channel set to {policy.channel}. No update was checked or installed."
    )


@update_app.command("check")
def update_check_command(
    feed: str | None = typer.Option(None, "--feed"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Fetch only release metadata; no source, prompts, or repository data is sent."""

    try:
        state = UpdateManager(villani_home()).check(feed_url=feed)
    except UpdateError as error:
        _self_service_error(error)
    value = state.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True))
        return
    typer.echo(f"Villani {state.available_version} is {state.status}. No update was installed.")
    if state.release_notes:
        typer.echo(state.release_notes)


@update_app.command("preview")
def update_preview_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Preview compatibility and data migrations without changing anything."""

    try:
        manager = UpdateManager(villani_home())
        state = manager.status()
        migration = manager.migration_preview()
    except UpdateError as error:
        _self_service_error(error)
    value = {
        "schema_version": "villani.update_preview.v1",
        "installed_version": state.installed_version,
        "available_version": state.available_version,
        "channel": state.policy.channel,
        "release_notes": state.release_notes,
        "migration": migration.model_dump(mode="json"),
        "artifact_verification_required": True,
        "atomic_switch": True,
        "rollback_available": bool(state.previous_installation),
        "repositories_modified": False,
        "source_uploaded": False,
    }
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True))
        return
    typer.echo(f"Installed: {state.installed_version}")
    typer.echo(f"Candidate: {state.available_version or 'not checked'}")
    typer.echo("Repositories modified: no")
    typer.echo("Source uploaded: no")
    typer.echo(
        "Migrations: " + ("; ".join(migration.actions) if migration.actions else "none required")
    )


def _install_update(
    *,
    artifact: Path | None,
    sha256: str | None,
    json_output: bool,
) -> None:
    try:
        manager = UpdateManager(villani_home())
        if artifact is None:
            if sha256 is not None:
                raise UpdateError("--sha256 requires --artifact")
            state = manager.install_checked_update()
        else:
            if sha256 is None:
                raise UpdateError("offline artifact installation requires --sha256")
            if not artifact.expanduser().is_file():
                raise UpdateError(f"artifact does not exist: {artifact}")
            state = manager.install_artifact(str(artifact.expanduser().resolve()), sha256.lower())
    except UpdateError as error:
        _self_service_error(error)
    value = state.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True))
    else:
        typer.echo(f"Villani {state.installed_version} was verified and activated atomically.")
        typer.echo(f"Launchers: {villani_home() / 'bin'}")
        typer.echo("Rollback: villani update rollback")


@update_app.command("install")
def update_install_command(
    artifact: Path | None = typer.Option(None, "--artifact"),
    sha256: str | None = typer.Option(None, "--sha256"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Explicitly install the checked release or an offline verified archive."""

    _install_update(artifact=artifact, sha256=sha256, json_output=json_output)


@app.command("install")
def install_command(
    artifact: Path = typer.Option(..., "--artifact"),
    sha256: str = typer.Option(..., "--sha256"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Install a standalone offline artifact into the managed per-user location."""

    _install_update(artifact=artifact, sha256=sha256, json_output=json_output)


@update_app.command("rollback")
def update_rollback_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Atomically restore the previous verified installation and configuration backup."""

    try:
        state = UpdateManager(villani_home()).rollback()
    except UpdateError as error:
        _self_service_error(error)
    value = state.model_dump(mode="json")
    typer.echo(
        json.dumps(value, sort_keys=True)
        if json_output
        else f"Rolled back to Villani {state.installed_version}; startup and doctor verification passed."
    )


@license_app.command("status")
def license_status_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Check the local entitlement without contacting a licensing service."""

    state = load_entitlement(villani_home())
    value = state.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True))
        return
    typer.echo(f"Tier: {state.tier.title()}")
    typer.echo(f"Status: {state.status}")
    typer.echo("Licensing network used: no")
    typer.echo("Source data shared: no")
    typer.echo("Recorded evidence remains readable: yes")
    if state.repair_action:
        typer.echo(state.repair_action)


@license_app.command("install")
def license_install_command(
    source: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Verify and atomically install a signed offline license file."""

    try:
        state = install_license(source, villani_home())
    except EntitlementError as error:
        _self_service_error(error)
    value = state.model_dump(mode="json")
    typer.echo(
        json.dumps(value, sort_keys=True)
        if json_output
        else f"Villani {state.tier.title()} license installed locally ({state.status})."
    )


@license_app.command("development")
def license_development_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Install the signed development fixture when explicitly enabled by environment."""

    if os.environ.get("VILLANI_ALLOW_DEVELOPMENT_LICENSE") != "1":
        _self_service_error(
            EntitlementError(
                "development fixtures are disabled; set VILLANI_ALLOW_DEVELOPMENT_LICENSE=1 only in development"
            )
        )
    home = villani_home()
    source = home / f"development-license-{secrets.token_hex(4)}.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.write_bytes(development_license_bytes())
        state = install_license(source, home)
    except (OSError, EntitlementError) as error:
        _self_service_error(error)
    finally:
        source.unlink(missing_ok=True)
    value = state.model_dump(mode="json")
    typer.echo(
        json.dumps(value, sort_keys=True)
        if json_output
        else "Signed development Pro fixture installed; production use remains disabled."
    )


def _support_preview(run_ids: Sequence[str]):
    report = run_doctor()
    return SupportBundleBuilder(villani_home()).preview(
        run_ids=run_ids,
        doctor=report.as_dict(),
    )


@support_app.command("preview")
def support_preview_command(
    run_id: list[str] | None = typer.Option(None, "--run"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show exactly what a support archive would include; create nothing."""

    try:
        manifest = _support_preview(run_id or [])
    except SupportBundleError as error:
        _self_service_error(error)
    value = manifest.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True))
        return
    typer.echo("Support bundle preview (no archive created)")
    for item in manifest.items:
        typer.echo(
            f"- {'include' if item.included else 'exclude'}: {item.logical_name} — {item.reason}"
        )
    typer.echo("Upload: never automatic")


@support_app.command("create")
def support_create_command(
    run_id: list[str] | None = typer.Option(None, "--run"),
    confirm_manifest: bool = typer.Option(
        False,
        "--confirm-manifest",
        help="Confirm that the manifest preview was reviewed.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Create one local archive only after explicit manifest confirmation."""

    selected_runs = run_id or []
    try:
        report = run_doctor()
        builder = SupportBundleBuilder(villani_home())
        preview = builder.preview(run_ids=selected_runs, doctor=report.as_dict())
        if not confirm_manifest:
            typer.echo(json.dumps(preview.model_dump(mode="json"), sort_keys=True))
            raise SupportBundleError(
                "review the manifest above, then rerun with --confirm-manifest"
            )
        archive, manifest = builder.create(
            run_ids=selected_runs,
            doctor=report.as_dict(),
        )
    except SupportBundleError as error:
        _self_service_error(error)
    value = {
        "archive": str(archive),
        "manifest": manifest.model_dump(mode="json"),
    }
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True))
    else:
        typer.echo(f"Support archive created locally: {archive}")
        typer.echo(f"SHA-256: {manifest.archive_sha256}")
        typer.echo("Upload: not performed")


@app.command("cleanup")
def cleanup_command(
    apply: bool = typer.Option(
        False, "--apply", help="Apply the displayed cache/log retention plan."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Preview stale cache cleanup; run bundles and evidence are never selected."""

    try:
        report = cleanup(villani_home(), apply=apply)
    except CleanupError as error:
        _self_service_error(error)
    value = report.as_dict()
    if json_output:
        typer.echo(json.dumps(value, sort_keys=True))
        return
    typer.echo(f"Cleanup {'applied' if apply else 'dry run'}: {len(report.items)} item(s)")
    typer.echo(f"Eligible bytes: {report.reclaimed_bytes}")
    typer.echo("Run bundles deleted: 0")
    if not apply:
        typer.echo("Run with --apply to remove only the listed caches and retained logs.")


@app.command("install-service", hidden=True)
def install_service_command() -> None:
    """Legacy compatibility alias for installing Villani Service."""

    result = install_service()
    typer.echo(json.dumps(result.as_dict(), sort_keys=True))


@service_app.command("status")
def service_status_command(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show Villani Service state, log path, and last error."""

    result = service_status()
    if json_output:
        typer.echo(json.dumps(result.as_dict(), sort_keys=True))
        return
    state = "running" if result.running else "stopped"
    typer.echo(f"Villani Service is {state}.")
    typer.echo(f"Automatic startup: {'enabled' if result.automatic_start else 'disabled'}")
    typer.echo(f"Log: {result.log_path}")
    if result.last_error:
        typer.echo(f"Last error: {result.last_error}")


@service_app.command("start")
def service_start_command(
    automatic: bool = typer.Option(
        False,
        "--automatic/--no-automatic",
        help="Also start Villani Service automatically when you sign in.",
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Start Villani Service safely; repeated starts do not create duplicates."""

    result = start_service(automatic_start=automatic)
    if json_output:
        typer.echo(json.dumps(result.as_dict(), sort_keys=True))
    else:
        typer.echo("Villani Service is running.")
        typer.echo(f"Log: {result.log_path}")


@service_app.command("stop")
def service_stop_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Stop Villani Service with a bounded shutdown."""

    result = stop_service()
    if json_output:
        typer.echo(json.dumps(result.as_dict(), sort_keys=True))
    else:
        typer.echo("Villani Service is stopped.")
        typer.echo(f"Log: {result.log_path}")


@service_app.command("restart")
def service_restart_command(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Restart Villani Service, retaining its automatic-start setting."""

    result = restart_service()
    if json_output:
        typer.echo(json.dumps(result.as_dict(), sort_keys=True))
    else:
        typer.echo("Villani Service restarted and is running.")
        typer.echo(f"Log: {result.log_path}")


@app.command("uninstall-service", hidden=True)
def uninstall_service_command(
    delete_data: bool = typer.Option(
        False, "--delete-data", help="Also delete local configuration, runs, and spool."
    ),
    confirm_delete_data: bool = typer.Option(
        False,
        "--confirm-delete-data",
        help="Required confirmation when --delete-data is supplied.",
    ),
) -> None:
    """Remove the user service, preserving all local data by default."""

    result = uninstall_service(
        delete_data=delete_data,
        confirm_delete_data=confirm_delete_data,
    )
    typer.echo(json.dumps(result.as_dict(), sort_keys=True))


def _select_detected_model(
    detections: Sequence[ProviderDetection],
    *,
    requested_model: str | None,
    assume_defaults: bool,
) -> tuple[ProviderDetection, str]:
    choices = [
        (detection, model)
        for detection in detections
        if detection.usable
        for model in detection.available_models
    ]
    if requested_model:
        matching = [item for item in choices if item[1] == requested_model]
        if not matching:
            raise SetupError(f"requested model {requested_model!r} was not detected")
        return matching[0]
    recommended = recommend_backend(detections)
    if recommended is None:
        raise SetupError("no provider with an available model was detected")
    if assume_defaults or len(choices) == 1:
        return recommended
    typer.echo("")
    typer.echo("Available models:")
    recommended_key = (recommended[0].detected_endpoint, recommended[1])
    default_index = 1
    for index, (detection, model) in enumerate(choices, start=1):
        suffix = " (recommended)" if (detection.detected_endpoint, model) == recommended_key else ""
        if suffix:
            default_index = index
        typer.echo(f"  {index}. {model} — {detection.display_name}{suffix}")
    while True:
        selection = typer.prompt("Select a model", default=default_index, type=int)
        if 1 <= selection <= len(choices):
            return choices[selection - 1]
        typer.echo(f"Enter a number from 1 to {len(choices)}.")


def _optional_approval(
    selected: bool | None,
    *,
    assume_defaults: bool,
    prompt: str,
    default: bool,
) -> bool:
    if selected is not None:
        return selected
    if assume_defaults:
        return default
    return typer.confirm(prompt, default=default)


def _probe_console(url: str) -> bool:
    request = urllib.request.Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=3
        ) as response:
            body = response.read(65_536).decode("utf-8", errors="replace")
            return response.status == 200 and "Villani Console" in body
    except (OSError, urllib.error.URLError):
        return False


def _open_console(*, launch_browser: bool = True) -> str:
    status = service_status()
    if not status.running or not status.console_url:
        raise ServiceError("Villani Service is stopped. Run: villani service start")
    if not _probe_console(status.console_url):
        raise ServiceError("Villani Console is not responding. Run: villani service restart")
    if launch_browser:
        webbrowser.open(status.console_url, new=2)
    return status.console_url


def _setup_cli_execution(
    *,
    home: Path,
    config_path: Path,
    repository: Path | None,
    mode: str,
    codex_model: str | None,
    claude_model: str | None,
    understand_system: str | None,
    write_system: str | None,
    verify_system: str | None,
    choose_system: str | None,
    assume_defaults: bool,
    start: bool | None,
    automatic: bool | None,
    open_console: bool | None,
    sample: bool | None,
) -> None:
    evidence_path = home / "diagnostics" / "agent-systems" / "setup-detect.json"
    detected = detect_cli_agent_systems(evidence_path=str(evidence_path))
    write_management_evidence(evidence_path, detected)
    typer.echo("Detected CLI agent systems:")
    for item in detected.systems:
        typer.echo(
            f"- {item.display_name}: {item.status.value}; "
            f"version={item.exact_version or 'not detected'}; "
            f"auth={item.authentication_status}; next={item.exact_next_action}"
        )

    by_driver = {item.driver: item for item in detected.systems}
    selected_models = {
        "codex": (codex_model or "").strip(),
        "claude_code": (claude_model or "").strip(),
    }
    if not any(selected_models.values()) and not assume_defaults:
        ready = [item for item in detected.systems if item.status == DoctorStatus.READY]
        if not ready:
            raise SetupError(
                "no ready CLI was detected; run the exact repair action above, then rerun setup"
            )
        for item in ready:
            if typer.confirm(f"Configure {item.display_name}", default=True):
                selected_models[item.driver] = typer.prompt(
                    f"Model string for {item.display_name}"
                ).strip()
    if not any(selected_models.values()):
        raise SetupError(
            "CLI setup needs --codex-model or --claude-model; model names are never guessed"
        )
    for driver, selected_model in selected_models.items():
        if not selected_model:
            continue
        diagnostic = by_driver[driver]
        if diagnostic.status != DoctorStatus.READY:
            raise SetupError(
                f"{diagnostic.display_name} is not ready: {diagnostic.what_failed or diagnostic.status.value}. "
                f"Run: {diagnostic.exact_next_action}"
            )

    explicit = (understand_system, write_system, verify_system, choose_system)
    role_assignments = None
    if mode == "custom" or any(value is not None for value in explicit):
        if not all(isinstance(value, str) and value.strip() for value in explicit):
            raise SetupError(
                "custom setup requires --understand-system, --write-system, "
                "--verify-system, and --choose-system"
            )
        role_assignments = {
            "classification": str(understand_system),
            "coding": str(write_system),
            "verification": str(verify_system),
            "selection": str(choose_system),
        }
    profile_id = "custom" if mode == "custom" else "cli"
    configuration = build_cli_configuration(
        repository=repository,
        codex_model=selected_models["codex"] or None,
        claude_model=selected_models["claude_code"] or None,
        role_assignments=role_assignments,
        profile_id=profile_id,
    )
    configured_backends = validate_configuration(configuration)
    configured_registry = build_agent_system_registry(configuration, configured_backends)
    bindings = configured_registry.resolve_profile(profile_id)
    configured_registry.require_profile_runnable(bindings)
    model_validations = []
    for system in configured_registry.list_configured():
        if not isinstance(system, CliAgentSystemConfig):
            continue
        validation = validate_cli_model(
            system,
            evidence_root=home / "diagnostics" / "agent-systems" / "model-probes",
        )
        model_validations.append(validation)
        typer.echo(
            f"- {system.id} model probe: {validation.status}; evidence={validation.evidence_path}"
        )
        if validation.status != "PASS":
            raise SetupError(
                f"model {system.model!r} was not proved available for {system.id}: "
                f"{validation.reason}. Run: {validation.exact_next_action}"
            )
    write_result = write_configuration_atomic(config_path, configuration)
    typer.echo(f"Active execution profile: {profile_id}")
    for role, system_id in bindings.bindings.items():
        labels = {
            "classification": "Understand task",
            "coding": "Write code",
            "verification": "Verify result",
            "selection": "Choose candidate",
        }
        typer.echo(f"- {labels[role.value]}: {system_id}")
    typer.echo(f"Configuration saved atomically: {write_result.path}")

    before_service = service_status()
    should_start = _optional_approval(
        start,
        assume_defaults=assume_defaults,
        prompt="Start Villani Service now",
        default=True,
    )
    service_result = before_service
    if should_start:
        automatic_start = _optional_approval(
            automatic,
            assume_defaults=assume_defaults,
            prompt="Start Villani Service automatically when you sign in",
            default=False,
        )
        service_result = start_service(automatic_start=automatic_start)
        typer.echo("Villani Service is running.")
    should_open = _optional_approval(
        open_console,
        assume_defaults=assume_defaults,
        prompt="Open Villani Console now",
        default=False if assume_defaults else True,
    )
    console_url = _open_console() if should_open else None
    if console_url:
        typer.echo(f"Villani Console: {console_url}")
    should_sample = _optional_approval(
        sample,
        assume_defaults=assume_defaults,
        prompt="Create and run a safe sample task in a disposable repository",
        default=False,
    )
    sample_result = create_sample_repository() if should_sample else None
    sample_exit_code = run_sample_task(sample_result) if sample_result else None
    if sample_result:
        typer.echo(f"Sample repository: {sample_result.path}")
        typer.echo(
            "Sample task completed successfully."
            if sample_exit_code == 0
            else f"Sample task failed with exit code {sample_exit_code}."
        )
    write_setup_record(
        home / "setup-record.json",
        {
            "schema_version": "villani.setup_record.v1",
            "repository": str(repository) if repository else None,
            "execution_mode": mode,
            "active_execution_profile": profile_id,
            "role_bindings": {
                role.value: system_id for role, system_id in bindings.bindings.items()
            },
            "agent_systems": [
                item.model_dump(mode="json") for item in configured_registry.list_configured()
            ],
            "detection_evidence": str(evidence_path),
            "model_validations": [item.model_dump(mode="json") for item in model_validations],
            "configuration": write_result.as_dict(),
            "service": service_result.as_dict(),
            "console_url": console_url,
            "sample": sample_result.as_dict() if sample_result else None,
            "sample_exit_code": sample_exit_code,
        },
    )
    typer.echo("Setup complete. Run `villani doctor` at any time.")
    if sample_exit_code not in {None, 0}:
        raise typer.Exit(1)


@app.command("setup")
def setup_command(
    reset: bool = typer.Option(
        False, "--reset", help="Replace existing configuration after explicit confirmation."
    ),
    assume_defaults: bool = typer.Option(
        False, "--yes", "-y", help="Use the recommended model and safe noninteractive defaults."
    ),
    endpoint: str | None = typer.Option(
        None, "--endpoint", help="Also inspect this OpenAI-compatible endpoint."
    ),
    model: str | None = typer.Option(None, "--model", help="Select a detected model by name."),
    execution_mode: str = typer.Option(
        "api",
        "--execution-mode",
        help="Simple setup mode: api, cli, or custom.",
    ),
    codex_model: str | None = typer.Option(
        None, "--codex-model", help="Configured Codex CLI model string."
    ),
    claude_model: str | None = typer.Option(
        None, "--claude-model", help="Configured Claude Code model string."
    ),
    understand_system: str | None = typer.Option(
        None, "--understand-system", help="Custom agent-system ID for Understand task."
    ),
    write_system: str | None = typer.Option(
        None, "--write-system", help="Custom agent-system ID for Write code."
    ),
    verify_system: str | None = typer.Option(
        None, "--verify-system", help="Custom agent-system ID for Verify result."
    ),
    choose_system: str | None = typer.Option(
        None, "--choose-system", help="Custom agent-system ID for Choose candidate."
    ),
    coding_system: str = typer.Option(
        "auto",
        "--coding-system",
        help="auto, villani-code, codex, or claude-code",
    ),
    start: bool | None = typer.Option(
        None, "--start/--no-start", help="Start Villani Service after configuration."
    ),
    automatic: bool | None = typer.Option(
        None,
        "--automatic/--no-automatic",
        help="Enable user-level automatic startup when starting the service.",
    ),
    open_console: bool | None = typer.Option(
        None, "--open/--no-open", help="Open Villani Console when setup completes."
    ),
    sample: bool | None = typer.Option(
        None, "--sample/--no-sample", help="Create and run a task in a disposable repository."
    ),
) -> None:
    """Interactively detect a model and create the first runnable configuration."""

    home = villani_home()
    config_path = home / "config.yaml"
    typer.echo("Villani guided setup")
    typer.echo("====================")
    if reset:
        confirmed = assume_defaults or typer.confirm(
            "Reset the active configuration? A backup will be created", default=False
        )
        if not confirmed:
            typer.echo("Configuration was not changed.")
            return
    elif config_path.is_file():
        try:
            validate_configuration(load_configuration(config_path))
            valid_existing = True
        except SetupError as error:
            valid_existing = False
            typer.echo(f"Existing configuration needs repair: {error}")
        if valid_existing and assume_defaults:
            typer.echo(f"Configuration is already valid at {config_path}.")
            typer.echo("Run `villani setup --reset` to replace it.")
            return
        prompt = "Reconfigure Villani now" if valid_existing else "Repair the configuration now"
        if not typer.confirm(prompt, default=not valid_existing):
            typer.echo("Configuration was not changed.")
            return

    selected_execution_mode = execution_mode.strip().lower()
    if selected_execution_mode not in {"api", "cli", "custom"}:
        raise SetupError("--execution-mode must be api, cli, or custom")

    repository = detect_repository()
    if repository:
        typer.echo(f"Repository detected: {repository}")
    else:
        typer.echo("No Git repository detected in the current directory.")

    if selected_execution_mode in {"cli", "custom"}:
        _setup_cli_execution(
            home=home,
            config_path=config_path,
            repository=repository,
            mode=selected_execution_mode,
            codex_model=codex_model,
            claude_model=claude_model,
            understand_system=understand_system,
            write_system=write_system,
            verify_system=verify_system,
            choose_system=choose_system,
            assume_defaults=assume_defaults,
            start=start,
            automatic=automatic,
            open_console=open_console,
            sample=sample,
        )
        return

    typer.echo("Detecting supported model providers on this computer...")
    detections = detect_providers(explicit_endpoint=endpoint)
    for detection in detections:
        typer.echo(f"- {detection.display_name}: {detection.diagnostic_message}")

    sessions = detect_session_sources()
    installed_sessions = [item for item in sessions if item.installed]
    typer.echo(
        f"Coding-session history sources detected: {len(installed_sessions)}"
        if installed_sessions
        else "No supported coding-session history sources were detected."
    )
    harnesses = discover_agent_harnesses()
    typer.echo("Coding systems:")
    for harness in harnesses:
        readiness = harness.readiness
        typer.echo(
            f"- {harness.display_name}: installed={readiness.installed}; "
            f"version={readiness.exact_version or 'not detected'}; "
            f"auth={readiness.authentication_status}; "
            f"protocol={readiness.conformance_status}; "
            f"qualification={readiness.qualification_state}; "
            f"repair={readiness.repair_action}"
        )
    before_service = service_status()
    typer.echo(
        "Villani Service is running."
        if before_service.running
        else "Villani Service is currently stopped."
    )

    if recommend_backend(detections) is None and endpoint is None and not assume_defaults:
        if typer.confirm(
            "No loaded model was found. Enter an OpenAI-compatible endpoint", default=True
        ):
            manual = typer.prompt("Endpoint URL")
            detections = detect_providers(explicit_endpoint=manual)
            for detection in detections:
                if detection.detected_endpoint.rstrip("/") == manual.rstrip("/"):
                    typer.echo(f"- {detection.display_name}: {detection.diagnostic_message}")
                    break
    selected, selected_model = _select_detected_model(
        detections, requested_model=model, assume_defaults=assume_defaults
    )
    typer.echo(f"Selected default model: {selected_model} ({selected.display_name})")

    connection_probe = test_backend(selected, selected_model)
    typer.echo(connection_probe.diagnostic_message)
    if not connection_probe.succeeded:
        raise SetupError("the selected backend failed its connection test")
    capability_probe = run_capability_probe(selected, selected_model)
    typer.echo(capability_probe.diagnostic_message)
    if not capability_probe.succeeded:
        raise SetupError("the selected backend failed the small capability probe")

    requested_coding_system = coding_system.strip().lower()
    if requested_coding_system == "auto":
        # The bundled Villani Code route has acceptance-grade conformance and
        # works with every detected provider. External systems stay visible and
        # selectable but are never silently promoted from provisional evidence.
        requested_coding_system = "villani-code"
    if requested_coding_system not in {"villani-code", "codex", "claude-code"}:
        raise SetupError("--coding-system must be auto, villani-code, codex, or claude-code")
    selected_harness = next(
        item for item in harnesses if item.harness_id == requested_coding_system
    )
    readiness = selected_harness.readiness
    if not readiness.installed:
        raise SetupError(readiness.repair_action)
    if readiness.version_supported is False:
        raise SetupError(readiness.repair_action)
    if readiness.authentication_status not in {"ready", "not_applicable"}:
        raise SetupError(readiness.repair_action)
    if requested_coding_system in {"codex", "claude-code"} and not (
        readiness.details.get("protocol_probe") == "passed"
        or readiness.conformance_status == "passed"
    ):
        raise SetupError(readiness.repair_action)
    if requested_coding_system == "claude-code" and not readiness.details.get(
        "strict_sandbox_available"
    ):
        raise SetupError(readiness.repair_action)
    if requested_coding_system == "codex" and not os.environ.get("OPENAI_API_KEY"):
        raise SetupError(
            "Codex setup requires OPENAI_API_KEY for the configured provider identity; "
            "set it, then run: villani setup --coding-system codex"
        )
    typer.echo(f"Selected coding system: {selected_harness.display_name}")

    configuration = build_configuration(
        selected,
        selected_model,
        repository=repository,
        session_sources=sessions,
        coding_system=requested_coding_system,
        coding_command=readiness.command_identity,
    )
    configured_backends = validate_configuration(configuration)
    configured_registry = build_agent_system_registry(configuration, configured_backends)
    write_result = write_configuration_atomic(config_path, configuration)
    typer.echo(f"Configuration saved atomically: {write_result.path}")
    if write_result.backup_path:
        typer.echo(f"Previous configuration backed up: {write_result.backup_path}")

    should_start = _optional_approval(
        start,
        assume_defaults=assume_defaults,
        prompt="Start Villani Service now",
        default=True,
    )
    service_result = before_service
    if should_start:
        automatic_start = _optional_approval(
            automatic,
            assume_defaults=assume_defaults,
            prompt="Start Villani Service automatically when you sign in",
            default=False,
        )
        service_result = start_service(automatic_start=automatic_start)
        typer.echo("Villani Service is running.")

    should_open = _optional_approval(
        open_console,
        assume_defaults=assume_defaults,
        prompt="Open Villani Console now",
        default=False if assume_defaults else True,
    )
    console_url: str | None = None
    if should_open:
        console_url = _open_console()
        typer.echo(f"Villani Console: {console_url}")

    should_sample = _optional_approval(
        sample,
        assume_defaults=assume_defaults,
        prompt="Create and run a safe sample task in a disposable repository",
        default=False,
    )
    sample_result = None
    sample_exit_code: int | None = None
    if should_sample:
        sample_result = create_sample_repository()
        typer.echo(f"Sample repository: {sample_result.path}")
        sample_exit_code = run_sample_task(sample_result)
        if sample_exit_code == 0:
            typer.echo("Sample task completed successfully.")
        else:
            typer.echo(f"Sample task failed with exit code {sample_exit_code}.", err=True)

    record = {
        "schema_version": "villani.setup_record.v1",
        "repository": str(repository) if repository else None,
        "provider": selected.as_dict(),
        "selected_model": selected_model,
        "session_sources": [item.as_dict() for item in sessions],
        "harnesses": [item.model_dump(mode="json") for item in harnesses],
        "selected_coding_system": requested_coding_system,
        "configured_agent_systems": [
            item.model_dump(mode="json") for item in configured_registry.list()
        ],
        "connection_probe": connection_probe.as_dict(),
        "capability_probe": capability_probe.as_dict(),
        "configuration": write_result.as_dict(),
        "service": service_result.as_dict(),
        "console_url": console_url,
        "sample": sample_result.as_dict() if sample_result else None,
        "sample_exit_code": sample_exit_code,
    }
    write_setup_record(home / "setup-record.json", record)
    typer.echo("Setup complete. Run `villani doctor` at any time.")
    if sample_exit_code not in {None, 0}:
        raise typer.Exit(1)


@app.command("doctor")
def doctor_command(
    repo: Path | None = typer.Option(
        None, "--repo", help="Repository to inspect without mutation."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable diagnostics."),
    installation_only: bool = typer.Option(
        False,
        "--installation-only",
        help="Verify only package, version, migration, update, entitlement, and storage health.",
    ),
) -> None:
    """Check configuration, model, service, storage, repository, and console health."""

    try:
        report = run_doctor(repository=repo, installation_only=installation_only)
    except RepositoryDiagnosticError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from error
    typer.echo(render_json(report) if json_output else render_human(report))
    if not report.healthy:
        raise typer.Exit(1)


@app.command("open")
def open_command(
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Validate and print the Console URL without launching a browser.",
    ),
) -> None:
    """Open the single local Villani Console."""

    try:
        url = _open_console(launch_browser=not print_only)
    except ServiceError as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(2) from error
    typer.echo(f"Villani Console: {url}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments in (["--version"], ["-V"]):
            print(f"Villani {installed_version()}")
            return 0
        if arguments and arguments[0] not in {
            "--help",
            "-h",
            "setup",
            "doctor",
            "version",
            "update",
            "install",
            "license",
            "support",
            "cleanup",
        }:
            check_upgrade(villani_home(), apply=True)
        app(args=arguments, prog_name="villani", standalone_mode=True)
    except (
        CleanupError,
        EntitlementError,
        MigrationError,
        ServiceError,
        SetupError,
        SupportBundleError,
        UpdateError,
        OSError,
    ) as error:
        print(f"villani: {error}", file=sys.stderr)
        return 2
    return 0
