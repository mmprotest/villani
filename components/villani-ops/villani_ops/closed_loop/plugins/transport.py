"""Fail-closed subprocess transport for untrusted plugins."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Iterable, Mapping

from jsonschema import ValidationError, validate

from .models import (
    PROTOCOL_VERSIONS,
    PluginCallRequest,
    PluginCallResponse,
    PluginExecutionError,
    PluginFailure,
    PluginManifest,
)
from .discovery import artifact_digest


def encode_message(document: Mapping[str, object], transport: str) -> bytes:
    payload = json.dumps(
        dict(document), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if transport == "jsonl":
        return payload + b"\n"
    return len(payload).to_bytes(4, "big") + payload


def decode_message(data: bytes, transport: str, maximum: int) -> dict[str, object]:
    if transport == "jsonl":
        if len(data) > maximum:
            raise PluginExecutionError(
                PluginFailure(
                    classification="oversized_message",
                    message="plugin response exceeded maximum message size",
                )
            )
        lines = data.splitlines()
        if len(lines) != 1:
            raise ValueError("JSONL response must contain exactly one line")
        payload = lines[0]
    else:
        if len(data) < 4:
            raise ValueError("length-prefixed response is missing its header")
        declared = int.from_bytes(data[:4], "big")
        if declared > maximum:
            raise PluginExecutionError(
                PluginFailure(
                    classification="oversized_message",
                    message="plugin response exceeded maximum message size",
                    details={"declared_bytes": declared},
                )
            )
        if len(data) != declared + 4:
            raise ValueError("length-prefixed response size does not match its header")
        payload = data[4:]
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("plugin response must be a JSON object")
    return value


class SubprocessPluginClient:
    def __init__(
        self,
        manifest: PluginManifest,
        *,
        base_directory: Path,
        allowed_digests: Iterable[str],
        timeout_seconds: float = 300.0,
        maximum_message_bytes: int = 8 * 1024 * 1024,
        diagnostic_bytes: int = 64 * 1024,
    ) -> None:
        if manifest.transport == "in-process":
            raise ValueError("SubprocessPluginClient cannot execute in-process plugins")
        if not manifest.enabled:
            raise ValueError("disabled plugin manifests cannot be executed")
        if manifest.digest not in set(allowed_digests):
            raise ValueError("plugin digest is not allowlisted for execution")
        self.manifest = manifest
        self.base_directory = base_directory.resolve()
        self.timeout_seconds = timeout_seconds
        self.maximum_message_bytes = maximum_message_bytes
        self.diagnostic_bytes = diagnostic_bytes
        artifact = (self.base_directory / str(manifest.artifact_path)).resolve()
        if (
            not artifact.is_relative_to(self.base_directory)
            or not artifact.is_file()
            or artifact_digest(artifact) != manifest.digest
        ):
            raise ValueError(
                "plugin artifact is unavailable, outside its directory, or has a digest mismatch"
            )
        assert manifest.entrypoint
        referenced = False
        for value in manifest.entrypoint:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = self.base_directory / candidate
            if candidate.resolve() == artifact:
                referenced = True
        if not referenced:
            raise ValueError(
                "plugin entrypoint does not reference its digest-verified artifact"
            )

    def _command(self) -> list[str]:
        assert self.manifest.entrypoint
        command = list(self.manifest.entrypoint)
        for index, value in enumerate(command):
            candidate = Path(value)
            if (
                not candidate.is_absolute()
                and (self.base_directory / candidate).exists()
            ):
                command[index] = str((self.base_directory / candidate).resolve())
        return command

    def call(
        self,
        operation: str,
        payload: Mapping[str, object],
        *,
        configuration: Mapping[str, object] | None = None,
        available_secrets: Mapping[str, str] | None = None,
        cancellation: threading.Event | None = None,
    ) -> dict[str, object]:
        protocol = PROTOCOL_VERSIONS[self.manifest.kind]
        plugin_configuration = dict(configuration or {})
        try:
            validate(
                instance=plugin_configuration, schema=self.manifest.configuration_schema
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise PluginExecutionError(
                PluginFailure(
                    classification="configuration_error",
                    message="plugin configuration does not match its declared schema",
                    details={
                        "error": error.message
                        if isinstance(error, ValidationError)
                        else str(error)
                    },
                )
            ) from error
        available = dict(available_secrets or {})
        # The manifest is the complete authority for secret names. Ambient environment
        # and caller-supplied unknown names are never forwarded.
        secrets = {
            name: available[name]
            for name in self.manifest.required_secrets
            if name in available
        }
        missing = [
            name for name in self.manifest.required_secrets if name not in secrets
        ]
        if missing:
            raise PluginExecutionError(
                PluginFailure(
                    classification="configuration_error",
                    message="required plugin secrets are unavailable",
                    details={"missing_secret_names": missing},
                )
            )
        request = PluginCallRequest(
            request_id=f"req_{uuid.uuid4().hex}",
            protocol_version=protocol,
            operation=operation,
            payload=dict(payload),
            configuration=plugin_configuration,
            secrets=secrets,
        )
        encoded = encode_message(
            request.model_dump(mode="json"), self.manifest.transport
        )
        if len(encoded) > self.maximum_message_bytes + 4:
            raise PluginExecutionError(
                PluginFailure(
                    classification="oversized_message",
                    message="plugin request exceeded maximum message size",
                )
            )
        process = subprocess.Popen(
            self._command(),
            cwd=self.base_directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            },
        )
        assert process.stdin and process.stdout and process.stderr
        stdout = bytearray()
        stderr = bytearray()
        oversized = threading.Event()

        def drain(stream: object, target: bytearray, limit: int, enforce: bool) -> None:
            while True:
                chunk = stream.read(65_536)  # type: ignore[attr-defined]
                if not chunk:
                    return
                remaining = max(0, limit + 1 - len(target))
                target.extend(chunk[:remaining])
                if enforce and len(target) > limit:
                    oversized.set()

        threads = [
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, self.maximum_message_bytes + 4, True),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr, self.diagnostic_bytes, False),
                daemon=True,
            ),
        ]
        for thread in threads:
            thread.start()
        process.stdin.write(encoded)
        process.stdin.close()
        started = time.monotonic()
        failure: PluginFailure | None = None
        while process.poll() is None:
            if cancellation is not None and cancellation.is_set():
                failure = PluginFailure(
                    classification="cancelled", message="plugin call was cancelled"
                )
                break
            if oversized.is_set():
                failure = PluginFailure(
                    classification="oversized_message",
                    message="plugin response exceeded maximum message size",
                )
                break
            if time.monotonic() - started > self.timeout_seconds:
                failure = PluginFailure(
                    classification="timeout", message="plugin call timed out"
                )
                break
            time.sleep(0.01)
        if failure is not None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        else:
            process.wait()
        for thread in threads:
            thread.join(timeout=2)
        diagnostic = bytes(stderr).decode("utf-8", errors="replace")
        for secret in secrets.values():
            if secret:
                diagnostic = diagnostic.replace(secret, "[REDACTED]")
        if failure is not None:
            failure.details["stderr"] = diagnostic
            raise PluginExecutionError(failure)
        if process.returncode != 0:
            raise PluginExecutionError(
                PluginFailure(
                    classification="crash",
                    message=f"plugin exited with code {process.returncode}",
                    details={"exit_code": process.returncode, "stderr": diagnostic},
                )
            )
        try:
            document = decode_message(
                bytes(stdout), self.manifest.transport, self.maximum_message_bytes
            )
            response = PluginCallResponse.model_validate(document)
        except PluginExecutionError:
            raise
        except Exception as error:
            raise PluginExecutionError(
                PluginFailure(
                    classification="malformed_response",
                    message="plugin returned malformed output",
                    details={"error": str(error), "stderr": diagnostic},
                )
            ) from error
        if (
            response.request_id != request.request_id
            or response.protocol_version != protocol
        ):
            raise PluginExecutionError(
                PluginFailure(
                    classification="protocol_mismatch",
                    message="plugin response identity or protocol does not match request",
                )
            )
        if response.status == "error":
            assert response.error is not None
            raise PluginExecutionError(response.error)
        assert response.result is not None
        return dict(response.result)
