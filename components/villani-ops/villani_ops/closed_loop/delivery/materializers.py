"""Idempotent delivery materializers for one exactly verified patch."""

from __future__ import annotations
import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol
from pydantic import BaseModel, ConfigDict, Field
from ..durable_io import write_json_atomic
from ..interfaces import (
    DependencyFailure,
    Materialization,
    MaterializationContext,
    Materializer,
    Selection,
)
from ..plugins.builtins import MATERIALIZER_MANIFEST
from .provenance import ProvenanceSigner, build_statement, record_digest


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "villani.delivery_receipt.v1"
    idempotency_key: str
    materialization_type: str
    materializer_name: str
    materializer_version: str
    patch_sha256: str
    artifact: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = {}


class GitProvider(Protocol):
    def create_or_get_pull_request(
        self,
        *,
        idempotency_key: str,
        repository: str,
        branch: str,
        title: str,
        patch_sha256: str,
    ) -> dict[str, Any]: ...


class FakeGitProvider:
    def __init__(self) -> None:
        self.requests: dict[str, dict[str, Any]] = {}

    def create_or_get_pull_request(self, **request: str) -> dict[str, Any]:
        key = request["idempotency_key"]
        self.requests.setdefault(
            key, {**request, "url": f"fake://pull/{len(self.requests) + 1}"}
        )
        return self.requests[key]


class DeliveryMaterializerAdapter:
    plugin_manifest = MATERIALIZER_MANIFEST
    version = "1.0.0"

    def __init__(
        self,
        *,
        local_apply: Materializer | None = None,
        git_provider: GitProvider | None = None,
        provenance_signer: ProvenanceSigner | None = None,
    ) -> None:
        self.local_apply, self.git_provider, self.provenance_signer = (
            local_apply,
            git_provider or FakeGitProvider(),
            provenance_signer,
        )

    def _validated_patch(
        self, selection: Selection, context: MaterializationContext
    ) -> tuple[str, str]:
        candidate = context.selected_candidate
        if selection.selected_attempt_id != candidate.attempt.attempt_id:
            raise ValueError("selection does not match candidate")
        patch = candidate.patch
        digest = hashlib.sha256(patch.encode()).hexdigest()
        if not patch or digest != candidate.attempt.patch_sha256:
            raise ValueError("delivery patch does not match verified snapshot digest")
        return patch, digest

    @staticmethod
    def _captured_changed_files(context: MaterializationContext) -> tuple[str, ...]:
        metadata = getattr(context.selected_candidate.attempt, "metadata", {})
        captured = metadata.get("changed_files") if isinstance(metadata, Mapping) else None
        if captured is None:
            return ()
        if not isinstance(captured, (list, tuple)) or not all(
            isinstance(item, str)
            and item
            and not Path(item).is_absolute()
            and ".." not in Path(item).parts
            for item in captured
        ):
            raise ValueError("candidate changed-file capture is malformed or unsafe")
        return tuple(sorted(set(captured)))

    def materialize(
        self, selection: Selection, context: MaterializationContext
    ) -> Materialization:
        try:
            patch, digest = self._validated_patch(selection, context)
            changed_files = self._captured_changed_files(context)
            config = context.policy_configuration.get("delivery", {})
            config = config if isinstance(config, dict) else {}
            kind = str(config.get("materialization_type") or "local_patch_apply")
            if (
                "verification_graph" in context.policy_configuration
                and self.provenance_signer is None
            ):
                raise ValueError(
                    "verification-graph delivery requires a provenance signer"
                )
            key = str(
                config.get("idempotency_key")
                or f"{context.run_id}:{selection.selected_attempt_id}:{kind}:{digest}"
            )
            receipt_path = (
                Path(context.run_directory)
                / "delivery"
                / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
            )
            if receipt_path.is_file():
                receipt = DeliveryReceipt.model_validate(
                    json.loads(receipt_path.read_text(encoding="utf-8"))
                )
                if (
                    receipt.patch_sha256 != digest
                    or receipt.materialization_type != kind
                ):
                    raise ValueError("idempotency key reused for a different delivery")
                return Materialization(
                    status="succeeded",
                    final_patch=patch,
                    final_report="Delivery already completed.",
                    changed_files=tuple(receipt.changed_files),
                    metadata={
                        "delivery_receipt": receipt.model_dump(mode="json"),
                        "idempotent_replay": True,
                        **(
                            {"final_provenance": "final_provenance.json"}
                            if (
                                Path(context.run_directory) / "final_provenance.json"
                            ).is_file()
                            else {}
                        ),
                    },
                )
            artifact, metadata = self._deliver(
                kind, patch, digest, key, config, context, selection
            )
            receipt = DeliveryReceipt(
                idempotency_key=key,
                materialization_type=kind,
                materializer_name="villani.delivery",
                materializer_version=self.version,
                patch_sha256=digest,
                artifact=artifact,
                changed_files=list(changed_files),
                metadata=metadata,
            )
            result_metadata: dict[str, Any] = {
                "delivery_receipt": receipt.model_dump(mode="json"),
                "idempotent_replay": False,
            }
            if "verification_graph" in context.policy_configuration:
                assert self.provenance_signer is not None
                verification = context.selected_candidate.verification
                evidence = tuple(
                    digest_value
                    for item in (
                        *verification.success_evidence,
                        *verification.failure_evidence,
                        *verification.missing_evidence,
                    )
                    if isinstance(
                        digest_value := getattr(item, "digest_sha256", None), str
                    )
                )
                approvals = context.policy_configuration.get("approval_records", ())
                approval_digests = (
                    tuple(record_digest(item) for item in approvals)
                    if isinstance(approvals, (list, tuple))
                    else ()
                )
                statement = build_statement(
                    run_id=context.run_id,
                    attempt_id=context.selected_candidate.attempt.attempt_id,
                    patch_sha256=digest,
                    graph_id=str(verification.metadata.get("verification_graph_id")),
                    graph_version=str(
                        verification.metadata.get("verification_graph_version")
                    ),
                    evidence_digests=evidence,
                    approval_digests=approval_digests,
                    materializer_name="villani.delivery",
                    materializer_version=self.version,
                    materialization_type=kind,
                    key_id=self.provenance_signer.key_id,
                )
                signed = self.provenance_signer.sign(statement)
                provenance_path = Path(context.run_directory) / "final_provenance.json"
                write_json_atomic(provenance_path, signed.model_dump(mode="json"))
                result_metadata["final_provenance"] = provenance_path.name
                result_metadata["provenance_signature"] = signed.signature
            # The receipt is the commit marker.  It is written only after all
            # required provenance artifacts so recovery cannot claim a partial delivery.
            write_json_atomic(receipt_path, receipt.model_dump(mode="json"))
            return Materialization(
                status="succeeded",
                final_patch=patch,
                final_report=f"Delivered exact patch digest {digest} using {kind}.",
                changed_files=changed_files,
                metadata=result_metadata,
            )
        except Exception as error:
            return Materialization(
                status="failed",
                final_patch=None,
                final_report=f"Delivery failed: {error}",
                failure=DependencyFailure(code="delivery_failed", message=str(error)),
            )

    def _deliver(
        self,
        kind: str,
        patch: str,
        digest: str,
        key: str,
        config: dict[str, Any],
        context: MaterializationContext,
        selection: Selection,
    ) -> tuple[str | None, dict[str, Any]]:
        if kind == "local_patch_apply":
            if self.local_apply is None:
                raise ValueError("local patch apply materializer is not configured")
            source = Path(context.run_directory) / str(
                context.selected_candidate.attempt.patch_path
            )
            if (
                not source.is_file()
                or source.read_text(encoding="utf-8", errors="strict") != patch
            ):
                raise ValueError("recorded patch no longer matches verified content")
            already_applied = subprocess.run(
                ["git", "apply", "--reverse", "--check", str(source)],
                cwd=context.repository_path,
                check=False,
                capture_output=True,
            )
            if already_applied.returncode == 0:
                return "final.patch", {"recovered_exact_patch": True}
            result = self.local_apply.materialize(selection, context)
            if result.status != "succeeded" or result.final_patch != patch:
                raise RuntimeError(
                    "local apply did not deliver the exact verified patch"
                )
            return "final.patch", dict(result.metadata)
        if kind == "patch_export":
            destination = Path(
                str(
                    config.get("destination")
                    or Path(context.run_directory) / "delivery" / "selected.patch"
                )
            ).resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            patch_bytes = patch.encode("utf-8")
            if destination.exists() and destination.read_bytes() != patch_bytes:
                raise ValueError(
                    "patch export destination already has different content"
                )
            destination.write_bytes(patch_bytes)
            return str(destination), {}
        if kind == "local_branch_commit":
            repo, branch = (
                Path(context.repository_path),
                str(config.get("branch") or f"villani/{context.run_id}"),
            )
            source = Path(context.run_directory) / "delivery" / f"{digest}.patch"
            source.parent.mkdir(parents=True, exist_ok=True)
            exact_bytes = patch.encode("utf-8")
            if source.exists() and source.read_bytes() != exact_bytes:
                raise ValueError(
                    "exact delivery patch artifact has conflicting content"
                )
            source.write_bytes(exact_bytes)
            exists = (
                subprocess.run(
                    ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
                    cwd=repo,
                    check=False,
                    capture_output=True,
                ).returncode
                == 0
            )
            subprocess.run(
                ["git", "checkout", branch]
                if exists
                else ["git", "checkout", "-b", branch],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            if exists:
                message = subprocess.run(
                    ["git", "log", "-1", "--format=%B"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if f"Villani-Patch-SHA256: {digest}" in message:
                    return commit, {
                        "branch": branch,
                        "commit": commit,
                        "recovered_exact_patch": True,
                    }
                raise ValueError(
                    "delivery branch already exists without the expected patch digest"
                )
            subprocess.run(
                ["git", "apply", "--index", str(source)],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    str(config.get("commit_message") or "Apply Villani candidate")
                    + f"\n\nVillani-Patch-SHA256: {digest}",
                ],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            return commit, {"branch": branch, "commit": commit}
        if kind == "pull_request":
            branch = str(config.get("branch") or f"villani/{context.run_id}")
            response = self.git_provider.create_or_get_pull_request(
                idempotency_key=key,
                repository=context.repository_path,
                branch=branch,
                title=str(config.get("title") or "Villani candidate"),
                patch_sha256=digest,
            )
            return str(response.get("url")), response
        raise ValueError(f"unsupported materialization type: {kind}")
