"""Command-line interface for the Villani local daemon."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .client import ClientError, LocalClient
from .config import AgentdPaths, Limits, ServerConfig
from .lifecycle import doctor, run_foreground_service, start_background, stop_background
from .wrapper import wrap_adapter
from .adapters import ADAPTERS


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
    except (ClientError, OSError, RuntimeError, ValueError) as error:
        print(f"villani-agentd: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
