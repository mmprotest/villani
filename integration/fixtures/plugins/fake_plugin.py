"""Dependency-free fake used by the Villani plugin conformance kit."""

from __future__ import annotations

import json
import struct
import sys
import time


def read_message(transport: str) -> dict:
    if transport == "jsonl":
        return json.loads(sys.stdin.buffer.readline())
    header = sys.stdin.buffer.read(4)
    size = struct.unpack(">I", header)[0]
    return json.loads(sys.stdin.buffer.read(size))


def write_message(value: dict, transport: str) -> None:
    payload = json.dumps(value, separators=(",", ":")).encode()
    if transport == "jsonl":
        sys.stdout.buffer.write(payload + b"\n")
    else:
        sys.stdout.buffer.write(struct.pack(">I", len(payload)) + payload)
    sys.stdout.buffer.flush()


def main() -> int:
    transport = sys.argv[1] if len(sys.argv) > 1 else "length-prefixed-json"
    behavior = sys.argv[2] if len(sys.argv) > 2 else "echo"
    request = read_message(transport)
    if behavior == "crash":
        print("fake crash diagnostic", file=sys.stderr)
        return 17
    if behavior == "timeout":
        time.sleep(60)
    if behavior == "malformed":
        sys.stdout.buffer.write(b"not-json\n" if transport == "jsonl" else b"\x00\x00\x00\x08not-json")
        return 0
    if behavior == "oversized":
        sys.stdout.buffer.write((10_000_000).to_bytes(4, "big") if transport != "jsonl" else b"{" + b"x" * 10_000_000)
        return 0
    protocol = request["protocol_version"]
    if behavior == "mismatch":
        protocol = "villani.unsupported.v999"
    write_message(
        {
            "schema_version": "villani.plugin.rpc.v1",
            "request_id": request["request_id"],
            "protocol_version": protocol,
            "status": "ok",
            "result": {"echo": request["payload"], "secret_names": sorted(request["secrets"])},
            "error": None,
        },
        transport,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
