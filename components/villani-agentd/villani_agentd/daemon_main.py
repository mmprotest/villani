"""Private foreground bootstrap used by the lifecycle manager."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .cli import _add_limits, _limits
from .config import AgentdPaths, ServerConfig
from .server import serve


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="villani-agentd-internal")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--insecure-development", action="store_true")
    _add_limits(parser)
    args = parser.parse_args(argv)
    paths = AgentdPaths(args.root)
    token = paths.token.read_text(encoding="utf-8").strip()
    serve(
        ServerConfig(host=args.host, port=args.port, limits=_limits(args)),
        paths,
        token,
        insecure_development=args.insecure_development,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
