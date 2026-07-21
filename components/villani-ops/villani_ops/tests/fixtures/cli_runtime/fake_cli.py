"""Cross-platform fake executable for the provider-neutral CLI runtime tests."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def _signal(name: str):
    return getattr(signal, name, None)


def _install_ignore_handlers() -> None:
    for candidate in (_signal("SIGTERM"), _signal("SIGBREAK")):
        if candidate is not None:
            signal.signal(candidate, lambda *_args: None)


def _install_graceful_handlers() -> None:
    def shutdown(*_args: object) -> None:
        os.write(2, b"graceful-shutdown\n")
        raise SystemExit(0)

    for candidate in (_signal("SIGTERM"), _signal("SIGBREAK")):
        if candidate is not None:
            signal.signal(candidate, shutdown)


def _write_many(descriptor: int, byte: bytes, count: int) -> None:
    remaining = count
    chunk = byte * 32_768
    while remaining:
        value = chunk[:remaining]
        os.write(descriptor, value)
        remaining -= len(value)


def _write_line(value: str, descriptor: int = 1) -> None:
    os.write(descriptor, value.encode("utf-8") + b"\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--message", default="ok")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--bytes", type=int, default=1_000_000)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--path")
    parser.add_argument("--child-pid-path")
    parser.add_argument("--environment-name", action="append", default=[])
    parser.add_argument("--value", action="append", default=[])
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    mode = arguments.mode
    if mode == "success":
        _write_line(arguments.message)
        return arguments.exit_code
    if mode == "arguments":
        _write_line(
            json.dumps(
                {"values": arguments.value, "cwd": str(Path.cwd())},
                ensure_ascii=False,
            )
        )
        return 0
    if mode == "stdin":
        payload = sys.stdin.buffer.read()
        os.write(1, payload)
        return 0
    if mode == "valid-jsonl":
        for index in range(3):
            _write_line(
                json.dumps({"sequence": index + 1, "message": arguments.message})
            )
        return 0
    if mode == "malformed-jsonl":
        _write_line('{"sequence":1}')
        _write_line('{"sequence":')
        return 0
    if mode == "oversized-jsonl":
        _write_line(json.dumps({"payload": "x" * arguments.bytes}))
        return 0
    if mode == "dual-output":
        threads = [
            threading.Thread(target=_write_many, args=(1, b"o", arguments.bytes)),
            threading.Thread(target=_write_many, args=(2, b"e", arguments.bytes)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return 0
    if mode == "sleep":
        time.sleep(arguments.seconds)
        return arguments.exit_code
    if mode == "graceful":
        _install_graceful_handlers()
        _write_line("ready")
        time.sleep(arguments.seconds)
        return 0
    if mode == "ignore-termination":
        _install_ignore_handlers()
        _write_line("ready")
        time.sleep(arguments.seconds)
        return 0
    if mode == "output-until-killed":
        _install_ignore_handlers()
        while True:
            os.write(1, b"output-after-cancellation\n")
            time.sleep(0.005)
    if mode == "child-sleep":
        _install_ignore_handlers()
        time.sleep(arguments.seconds)
        return 0
    if mode == "spawn-child":
        _install_ignore_handlers()
        child = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--mode",
                "child-sleep",
                "--seconds",
                str(arguments.seconds),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if arguments.child_pid_path:
            Path(arguments.child_pid_path).write_text(str(child.pid), encoding="ascii")
        _write_line(json.dumps({"child_pid": child.pid}))
        time.sleep(arguments.seconds)
        return 0
    if mode == "final-output":
        if not arguments.path:
            raise ValueError("--path is required")
        Path(arguments.path).write_text(arguments.message, encoding="utf-8")
        return arguments.exit_code
    if mode == "close-stdout":
        os.close(1)
        os.write(2, b"stdout-closed\n")
        time.sleep(min(arguments.seconds, 0.1))
        return arguments.exit_code
    if mode == "partial-crash":
        os.write(1, b"partial-stdout\n")
        os.write(2, b"partial-stderr\n")
        os._exit(arguments.exit_code or 23)
    if mode == "environment-names":
        document = {
            name: {"present": name in os.environ} for name in arguments.environment_name
        }
        _write_line(json.dumps(document, sort_keys=True))
        return 0
    if mode == "emit-environment-value":
        for name in arguments.environment_name:
            os.write(1, os.environ.get(name, "").encode("utf-8") + b"\n")
            os.write(2, os.environ.get(name, "").encode("utf-8") + b"\n")
        return 0
    if mode == "outside-write":
        if not arguments.path:
            raise ValueError("--path is required")
        try:
            Path(arguments.path).write_text(arguments.message, encoding="utf-8")
        except OSError as error:
            _write_line(json.dumps({"written": False, "error": type(error).__name__}))
            return 1
        _write_line(json.dumps({"written": True}))
        return 0
    if mode == "invalid-utf8":
        os.write(1, b"valid-prefix\xffinvalid\n")
        return 0
    if mode == "close-stdin":
        os.close(0)
        time.sleep(min(arguments.seconds, 0.2))
        return 0
    raise ValueError(f"unsupported mode: {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
