from __future__ import annotations

import json
import sys
from typing import Sequence

import typer

from villani_ops.cli.unified import app

from .migrations import MigrationError, check_upgrade
from .services import (
    ServiceError,
    install_service,
    service_status,
    uninstall_service,
    villani_home,
)

service_app = typer.Typer(
    help="Manage the user-level Villani daemon service.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(service_app, name="service")


@app.command("install-service")
def install_service_command() -> None:
    """Install and start the daemon for the current user."""

    result = install_service()
    typer.echo(json.dumps(result.as_dict(), sort_keys=True))


@service_app.command("status")
def service_status_command() -> None:
    """Show the current user's daemon service state."""

    typer.echo(json.dumps(service_status().as_dict(), sort_keys=True))


@app.command("uninstall-service")
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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] not in {"--help", "-h"}:
            check_upgrade(villani_home(), apply=True)
        app(args=arguments, prog_name="villani", standalone_mode=True)
    except (MigrationError, ServiceError, OSError) as error:
        print(f"villani: {error}", file=sys.stderr)
        return 2
    return 0
