"""Command-line interface for the Villani local daemon."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from typing import Sequence

from .client import ClientError, LocalClient
from .config import AgentdPaths, Limits, ServerConfig, SyncConfig, villani_home
from .credentials import InstallationCredentialStore
from .uploader import ControlPlaneClient, RemoteError, SynchronizationWorker
from .remote_worker import RemoteExecutionWorker
from .lifecycle import doctor, run_foreground_service, start_background, stop_background
from .wrapper import wrap_adapter
from .adapters import ADAPTERS
from .local_import import LocalRunImporter


def _add_limits(parser: argparse.ArgumentParser) -> None:
    defaults = Limits()
    for name, value in defaults.as_dict().items():
        parser.add_argument(f"--{name.replace('_', '-')}", type=int, default=value)


def _limits(namespace: argparse.Namespace) -> Limits:
    return Limits(**{name: getattr(namespace, name) for name in Limits().as_dict()})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="villani-agentd", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start the local daemon")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=0)
    start.add_argument("--insecure-development", action="store_true")
    _add_limits(start)

    subparsers.add_parser("status", help="show daemon status")
    subparsers.add_parser("stop", help="stop the local daemon")
    subparsers.add_parser("doctor", help="check local daemon storage and configuration")
    enroll = subparsers.add_parser("enroll", help="enroll this daemon with a control plane")
    enroll.add_argument("--control-plane", required=True)
    enroll.add_argument("--token", required=True)
    enroll.add_argument("--installation-id", required=True)
    enroll.add_argument("--agent-name", default="villani-agentd")
    enroll.add_argument("--agent-version", default="0.1.0")
    enroll.add_argument("--batch-size", type=int, default=250)
    enroll.add_argument("--concurrency", type=int, default=2)
    subparsers.add_parser("sync-once", help="synchronize pending data once")
    backfill = subparsers.add_parser(
        "backfill", help="import canonical runs created while agentd was absent"
    )
    backfill.add_argument("--batch-size", type=int, default=100)
    subparsers.add_parser("rotate-credential", help="rotate this installation credential")
    worker_enable = subparsers.add_parser(
        "worker-enable", help="explicitly enable pull-based controlled remote execution"
    )
    worker_enable.add_argument("--worker-id", required=True)
    worker_enable.add_argument("--residency", action="append", required=True)
    worker_enable.add_argument("--network-class", required=True)
    worker_enable.add_argument("--reachable-model", action="append", default=[])
    worker_enable.add_argument("--reachable-runtime", action="append", default=[])
    subparsers.add_parser("worker-disable", help="disable remote task pulling")
    subparsers.add_parser("worker-once", help="heartbeat and pull at most one remote task")
    service_run = subparsers.add_parser(
        "service-run", help="run the foreground daemon under a user service manager"
    )
    _add_limits(service_run)

    wrap = subparsers.add_parser("wrap", help="run a command through a local adapter")
    wrap.add_argument("--adapter", choices=sorted(ADAPTERS), required=True)
    wrap.add_argument("wrapped_command", nargs=argparse.REMAINDER)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = AgentdPaths.default()
    try:
        if args.command == "start":
            if not 0 <= args.port <= 65535:
                raise ValueError("port must be between 0 and 65535")
            endpoint = start_background(
                ServerConfig(host=args.host, port=args.port, limits=_limits(args)),
                paths,
                insecure_development=args.insecure_development,
            )
            print(json.dumps(endpoint, sort_keys=True))
            return 0
        if args.command == "status":
            print(json.dumps(LocalClient.from_files(paths).status(), sort_keys=True))
            return 0
        if args.command == "stop":
            stopped = stop_background(paths)
            print(json.dumps({"stopped": stopped}, sort_keys=True))
            return 0
        if args.command == "doctor":
            healthy, report = doctor(paths)
            print(json.dumps(report, sort_keys=True))
            return 0 if healthy else 1
        if args.command == "enroll":
            response = ControlPlaneClient(args.control_plane).request(
                "POST",
                "/v1/installations/enroll",
                {
                    "enrollment_token": args.token,
                    "installation_id": args.installation_id,
                    "agent_name": args.agent_name,
                    "agent_version": args.agent_version,
                },
                auth=False,
            )
            storage = InstallationCredentialStore(paths).set(
                args.installation_id, str(response["credential"])
            )
            SyncConfig(
                endpoint=args.control_plane,
                installation_id=args.installation_id,
                batch_size=args.batch_size,
                concurrency=args.concurrency,
            ).save(paths.sync_config)
            print(json.dumps({"enrolled": True, "credential_storage": storage}, sort_keys=True))
            return 0
        if args.command == "sync-once":
            config = SyncConfig.load(paths.sync_config)
            if config is None:
                raise RuntimeError("daemon is not enrolled; local-only mode remains active")
            print(
                json.dumps(
                    SynchronizationWorker(paths, config, Limits()).sync_once(), sort_keys=True
                )
            )
            return 0
        if args.command == "backfill":
            print(
                json.dumps(
                    LocalRunImporter(paths, Limits(), batch_size=args.batch_size).run_once(),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "rotate-credential":
            config = SyncConfig.load(paths.sync_config)
            if config is None:
                raise RuntimeError("daemon is not enrolled")
            credentials = InstallationCredentialStore(paths)
            response = ControlPlaneClient(
                config.endpoint, credentials.get(config.installation_id)
            ).request(
                "POST",
                f"/v1/installations/{config.installation_id}/credentials/rotate",
                {},
            )
            storage = credentials.set(config.installation_id, str(response["credential"]))
            print(
                json.dumps(
                    {
                        "credential_storage": storage,
                        "credential_version": response["credential_version"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "worker-enable":
            config = SyncConfig.load(paths.sync_config)
            if config is None:
                raise RuntimeError("daemon must be enrolled before remote execution is enabled")
            if not (villani_home() / "config.yaml").is_file():
                raise RuntimeError("Villani configuration is required before enabling a worker")
            replace(
                config,
                remote_execution_enabled=True,
                worker_id=args.worker_id,
                network_class=args.network_class,
                data_residency_labels=tuple(args.residency),
                reachable_models=tuple(args.reachable_model),
                reachable_runtimes=tuple(args.reachable_runtime),
            ).save(paths.sync_config)
            print(json.dumps({"remote_execution_enabled": True, "worker_id": args.worker_id}))
            return 0
        if args.command == "worker-disable":
            config = SyncConfig.load(paths.sync_config)
            if config is None:
                raise RuntimeError("daemon is not enrolled")
            replace(config, remote_execution_enabled=False).save(paths.sync_config)
            print(json.dumps({"remote_execution_enabled": False}))
            return 0
        if args.command == "worker-once":
            config = SyncConfig.load(paths.sync_config)
            if config is None or not config.remote_execution_enabled:
                raise RuntimeError("remote execution is not enabled")
            print(
                json.dumps({"claimed": RemoteExecutionWorker(paths, config, Limits()).run_once()})
            )
            return 0
        if args.command == "service-run":
            run_foreground_service(paths, _limits(args))
            return 0
        if args.command == "wrap":
            command = list(args.wrapped_command)
            if command and command[0] == "--":
                command.pop(0)
            endpoint = json.loads(paths.endpoint.read_text(encoding="utf-8"))
            limits = Limits(**endpoint.get("limits", {}))
            return wrap_adapter(args.adapter, command, LocalClient.from_files(paths), limits)
    except (ClientError, OSError, RemoteError, RuntimeError, ValueError) as error:
        print(f"villani-agentd: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
