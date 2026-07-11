"""Private foreground bootstrap used by the lifecycle manager."""

from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Sequence

from .cli import _add_limits, _limits
from .config import AgentdPaths, ServerConfig, SyncConfig
from .uploader import SynchronizationWorker
from .remote_worker import RemoteExecutionWorker
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
    limits = _limits(args)
    stop = threading.Event()
    sync_config = SyncConfig.load(paths.sync_config)
    worker_threads: list[threading.Thread] = []
    if sync_config is not None:
        worker = SynchronizationWorker(paths, sync_config, limits)
        worker_threads.append(threading.Thread(target=worker.run, args=(stop,), daemon=True))
        if sync_config.remote_execution_enabled:
            remote = RemoteExecutionWorker(paths, sync_config, limits)
            worker_threads.append(threading.Thread(target=remote.run, args=(stop,), daemon=True))
        for worker_thread in worker_threads:
            worker_thread.start()
    try:
        serve(
            ServerConfig(host=args.host, port=args.port, limits=limits),
            paths,
            token,
            insecure_development=args.insecure_development,
        )
    finally:
        stop.set()
        for worker_thread in worker_threads:
            worker_thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
