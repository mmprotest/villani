from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import webbrowser
from typing import Sequence

import typer

from villani_ops.cli.unified import app

from .migrations import MigrationError, check_upgrade
from .diagnostics import render_human, render_json, run_doctor
from .onboarding import (
    ProviderDetection,
    SetupError,
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

    repository = detect_repository()
    if repository:
        typer.echo(f"Repository detected: {repository}")
    else:
        typer.echo("No Git repository detected in the current directory.")

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
    before_service = service_status()
    typer.echo(
        "Villani Service is running."
        if before_service.running
        else "Villani Service is currently stopped."
    )

    if recommend_backend(detections) is None and endpoint is None and not assume_defaults:
        if typer.confirm("No loaded model was found. Enter an OpenAI-compatible endpoint", default=True):
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

    configuration = build_configuration(
        selected,
        selected_model,
        repository=repository,
        session_sources=sessions,
    )
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
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable diagnostics."),
) -> None:
    """Check configuration, model, service, storage, repository, and console health."""

    report = run_doctor()
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
        if arguments and arguments[0] not in {"--help", "-h", "setup", "doctor"}:
            check_upgrade(villani_home(), apply=True)
        app(args=arguments, prog_name="villani", standalone_mode=True)
    except (MigrationError, ServiceError, SetupError, OSError) as error:
        print(f"villani: {error}", file=sys.stderr)
        return 2
    return 0
