from __future__ import annotations

import hashlib
import json
import random
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .config import AgentdPaths, Limits, SyncConfig
from .credentials import InstallationCredentialStore
from .spool import SQLiteSpool


class RemoteError(RuntimeError):
    def __init__(self, status: int | None, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def permanent(self) -> bool:
        return (
            self.status is not None
            and 400 <= self.status < 500
            and self.status
            not in {
                408,
                425,
                429,
            }
        )


class ControlPlaneClient:
    def __init__(self, endpoint: str, credential: str | None = None) -> None:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("control-plane endpoint must be HTTP(S)")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("non-loopback control-plane endpoints require HTTPS")
        self.endpoint = endpoint.rstrip("/")
        self.credential = credential
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self, method: str, path: str, body: dict[str, Any], *, auth: bool = True
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            if not self.credential:
                raise RemoteError(None, "installation credential is missing")
            headers["Authorization"] = f"Bearer {self.credential}"
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=json.dumps(body, separators=(",", ":")).encode(),
            headers=headers,
            method=method,
        )
        return self._open_json(request)

    def upload(self, instruction: dict[str, Any], path: Path) -> None:
        headers = dict(instruction.get("headers") or {})
        request = urllib.request.Request(
            instruction["url"],
            data=path.read_bytes(),
            headers=headers,
            method=instruction["method"],
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                response.read()
        except urllib.error.HTTPError as error:
            if error.code == 412:
                return
            raise self._http_error(error) from error
        except OSError as error:
            raise RemoteError(None, str(error)) from error

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with self.opener.open(request, timeout=30) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise self._http_error(error) from error
        except (OSError, json.JSONDecodeError) as error:
            raise RemoteError(None, str(error)) from error
        if not isinstance(value, dict):
            raise RemoteError(None, "control plane returned non-object JSON")
        return value

    @staticmethod
    def _http_error(error: urllib.error.HTTPError) -> RemoteError:
        retry = error.headers.get("Retry-After")
        retry_after = None
        if retry:
            try:
                retry_after = max(0.0, float(retry))
            except ValueError:
                try:
                    value = parsedate_to_datetime(retry)
                    retry_after = max(0.0, (value - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return RemoteError(error.code, error.read().decode(errors="replace"), retry_after)


def utc_text(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


class SynchronizationWorker:
    def __init__(
        self,
        paths: AgentdPaths,
        config: SyncConfig,
        limits: Limits,
        *,
        random_source: random.Random | None = None,
    ) -> None:
        self.paths = paths
        self.config = config
        self.spool = SQLiteSpool(paths, limits)
        credential = InstallationCredentialStore(paths).get(config.installation_id)
        self.client = ControlPlaneClient(config.endpoint, credential)
        self.random = random_source or random.Random()

    def _delay(self, retries: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        ceiling = min(
            self.config.max_backoff_seconds,
            self.config.base_backoff_seconds * (2 ** min(retries, 16)),
        )
        return self.random.uniform(0, ceiling)

    def sync_once(self) -> dict[str, int]:
        now = utc_text()
        event_rows = self.spool.pending_events(self.config.batch_size, now)
        event_result = 0
        if event_rows:
            ids = [row["event_id"] for row in event_rows]
            batch_id = "agentd:" + hashlib.sha256("\n".join(ids).encode()).hexdigest()
            try:
                self.client.request(
                    "POST",
                    "/v1/ingest/batches",
                    {"batch_id": batch_id, "events": [row["document"] for row in event_rows]},
                )
            except RemoteError as error:
                if error.permanent:
                    self.spool.dead_letter_events(ids, now, str(error))
                else:
                    delay = self._delay(
                        max(int(row["retry_count"]) for row in event_rows) + 1,
                        error.retry_after,
                    )
                    self.spool.retry_events(
                        ids,
                        utc_text(datetime.now(timezone.utc) + timedelta(seconds=delay)),
                        str(error),
                    )
            else:
                self.spool.acknowledge_events(ids)
                event_result = len(ids)

        outcome_rows = self.spool.pending_finalizations(self.config.batch_size, now)
        outcome_result = 0
        for row in outcome_rows:
            try:
                self.client.request("POST", "/v1/outcomes", row["payload"]["outcome"])
            except RemoteError as error:
                if error.permanent:
                    self.spool.dead_letter_finalization(row["run_id"], now, str(error))
                else:
                    delay = self._delay(int(row["retry_count"]) + 1, error.retry_after)
                    self.spool.retry_finalization(
                        row["run_id"],
                        utc_text(datetime.now(timezone.utc) + timedelta(seconds=delay)),
                        str(error),
                    )
            else:
                self.spool.acknowledge_finalization(row["run_id"])
                outcome_result += 1

        artifacts = self.spool.pending_artifacts(self.config.concurrency, now)
        artifact_result = 0
        if artifacts:
            with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
                artifact_result = sum(executor.map(self._sync_artifact, artifacts))
        return {
            "events": event_result,
            "artifacts": artifact_result,
            "outcomes": outcome_result,
        }

    def _sync_artifact(self, row: dict[str, Any]) -> int:
        now = utc_text()
        try:
            registration = self.client.request(
                "POST",
                "/v1/artifacts/descriptors",
                {"run_id": row["run_id"], "descriptor": row["descriptor"]},
            )
            if registration["status"] != "already_present":
                local_path = self.paths.root / "artifacts" / row["storage_reference"]
                self.client.upload(registration["upload_instruction"], local_path)
                self.client.request(
                    "POST",
                    f"/v1/artifact-uploads/{registration['upload_id']}/complete",
                    {},
                )
        except RemoteError as error:
            if error.permanent:
                self.spool.dead_letter_artifact(row["artifact_id"], now, str(error))
            else:
                delay = self._delay(int(row["retry_count"]) + 1, error.retry_after)
                self.spool.retry_artifact(
                    row["artifact_id"],
                    utc_text(datetime.now(timezone.utc) + timedelta(seconds=delay)),
                    str(error),
                )
            return 0
        self.spool.acknowledge_artifact(row["artifact_id"])
        return 1

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.sync_once()
            stop.wait(self.config.poll_seconds)
