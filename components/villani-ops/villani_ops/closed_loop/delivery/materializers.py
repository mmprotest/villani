"""Idempotent delivery of one selected, acceptance-eligible patch.

The controller selects the candidate and decides whether delivery is allowed.
This module performs the requested Git operation without changing those rules.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..adapters.git_isolation import repository_identity
from ..durable_io import read_jsonl_tolerant, write_json_atomic
from ..event_writer import redact_data, redact_message
from ..interfaces import (
    DependencyFailure,
    Materialization,
    MaterializationContext,
    Materializer,
    Selection,
)
from ..plugins.builtins import MATERIALIZER_MANIFEST
from .provenance import ProvenanceSigner, build_statement, record_digest


class DeliveryError(RuntimeError):
    """A safe, user-actionable delivery failure."""

    def __init__(
        self, code: str, message: str, *, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


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
    metadata: dict[str, Any] = Field(default_factory=dict)


class GitHostAdapter(Protocol):
    """Provider-neutral boundary used by pull-request delivery."""

    provider_name: str

    def push_branch(
        self,
        *,
        repository: Path,
        branch: str,
        remote: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def create_or_get_pull_request(
        self,
        *,
        idempotency_key: str,
        repository: str,
        branch: str,
        title: str,
        body_path: str | None = None,
        base_branch: str | None = None,
        patch_sha256: str,
    ) -> dict[str, Any]: ...


# Compatibility name retained for integrations that imported the v1 protocol.
GitProvider = GitHostAdapter


def _run(
    command: list[str],
    *,
    cwd: Path,
    error_code: str,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
        )
    except FileNotFoundError as error:
        raise DeliveryError(
            "provider_tool_unavailable",
            f"required provider tool is unavailable: {command[0]}",
        ) from error
    if result.returncode == 0:
        return result
    message = redact_message(
        result.stderr.strip() or result.stdout.strip() or error_code
    )
    lowered = message.lower()
    if any(
        marker in lowered
        for marker in (
            "authentication failed",
            "permission denied",
            "could not read username",
            "not logged in",
            "unauthorized",
            "http 401",
            "http 403",
        )
    ):
        code = "authentication_failure"
    elif any(
        marker in lowered
        for marker in (
            "could not resolve host",
            "unable to access",
            "connection refused",
            "network is unreachable",
            "could not read from remote repository",
        )
    ):
        code = "remote_unavailable"
    elif any(
        marker in lowered for marker in ("non-fast-forward", "fetch first", "rejected")
    ):
        code = "push_rejected"
    else:
        code = error_code
    raise DeliveryError(code, message, details={"exit_code": result.returncode})


class _CommandGitHostAdapter:
    provider_name = "generic"
    executable = ""

    def push_branch(
        self,
        *,
        repository: Path,
        branch: str,
        remote: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        del idempotency_key
        _run(
            ["git", "push", "--set-upstream", remote, f"HEAD:{branch}"],
            cwd=repository,
            error_code="push_failed",
        )
        return {"remote": remote, "branch": branch, "pushed": True}


class GitHubGitHostAdapter(_CommandGitHostAdapter):
    provider_name = "github"
    executable = "gh"

    def create_or_get_pull_request(
        self,
        *,
        idempotency_key: str,
        repository: str,
        branch: str,
        title: str,
        body_path: str | None = None,
        base_branch: str | None = None,
        patch_sha256: str,
    ) -> dict[str, Any]:
        del idempotency_key, patch_sha256
        cwd = Path(repository)
        try:
            existing = subprocess.run(
                ["gh", "pr", "view", branch, "--json", "url,number"],
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as error:
            raise DeliveryError(
                "provider_tool_unavailable",
                "required provider tool is unavailable: gh",
            ) from error
        if existing.returncode == 0:
            try:
                value = json.loads(existing.stdout)
            except json.JSONDecodeError:
                value = {}
            if isinstance(value, dict) and value.get("url"):
                return {
                    "provider": self.provider_name,
                    "url": str(value["url"]),
                    "number": value.get("number"),
                    "existing": True,
                }
        command = ["gh", "pr", "create", "--head", branch, "--title", title]
        if body_path:
            command.extend(["--body-file", body_path])
        if base_branch:
            command.extend(["--base", base_branch])
        result = _run(command, cwd=cwd, error_code="pull_request_creation_failed")
        url = next(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if line.startswith("http")
            ),
            result.stdout.strip(),
        )
        return {"provider": self.provider_name, "url": url, "existing": False}


class GitLabGitHostAdapter(_CommandGitHostAdapter):
    provider_name = "gitlab"
    executable = "glab"

    def create_or_get_pull_request(
        self,
        *,
        idempotency_key: str,
        repository: str,
        branch: str,
        title: str,
        body_path: str | None = None,
        base_branch: str | None = None,
        patch_sha256: str,
    ) -> dict[str, Any]:
        del idempotency_key, patch_sha256
        cwd = Path(repository)
        command = [
            "glab",
            "mr",
            "create",
            "--source-branch",
            branch,
            "--title",
            title,
            "--yes",
        ]
        if body_path:
            command.extend(["--description-file", body_path])
        if base_branch:
            command.extend(["--target-branch", base_branch])
        result = _run(command, cwd=cwd, error_code="pull_request_creation_failed")
        url = next(
            (
                line.strip()
                for line in result.stdout.splitlines()
                if line.startswith("http")
            ),
            result.stdout.strip(),
        )
        return {"provider": self.provider_name, "url": url, "existing": False}


class LocalOnlyGitHostAdapter:
    """Fail-closed fallback when no connected Git host is configured."""

    provider_name = "local-only"

    def push_branch(self, **_: Any) -> dict[str, Any]:
        raise DeliveryError(
            "remote_unavailable",
            "no Git-host provider is configured; the local branch and patch were preserved",
        )

    def create_or_get_pull_request(self, **_: Any) -> dict[str, Any]:
        raise DeliveryError(
            "provider_adapter_unavailable",
            "no Git-host provider adapter is configured",
        )


class FakeGitProvider:
    """Local fixture adapter used by integration tests and offline demos."""

    provider_name = "local-fixture"

    def __init__(self, *, push_error: DeliveryError | None = None) -> None:
        self.requests: dict[str, dict[str, Any]] = {}
        self.pushes: dict[str, dict[str, Any]] = {}
        self.push_error = push_error

    def push_branch(self, **request: Any) -> dict[str, Any]:
        if self.push_error is not None:
            raise self.push_error
        key = str(request["idempotency_key"])
        value = {
            "provider": self.provider_name,
            "remote": str(request.get("remote") or "fixture"),
            "branch": str(request["branch"]),
            "pushed": True,
        }
        self.pushes.setdefault(key, value)
        return self.pushes[key]

    def create_or_get_pull_request(self, **request: Any) -> dict[str, Any]:
        key = str(request["idempotency_key"])
        safe_request = {
            name: str(value) if isinstance(value, Path) else value
            for name, value in request.items()
        }
        self.requests.setdefault(
            key,
            {
                **safe_request,
                "provider": self.provider_name,
                "url": f"fixture://pull/{len(self.requests) + 1}",
            },
        )
        return self.requests[key]


def build_git_host_adapter(
    configuration: Mapping[str, Any], repository: str | Path | None = None
) -> GitHostAdapter:
    """Resolve a provider adapter without leaking provider logic into the controller."""

    delivery_value = configuration.get("delivery")
    delivery = dict(delivery_value) if isinstance(delivery_value, Mapping) else {}
    provider = str(delivery.get("provider") or "auto").strip().lower()
    if provider in {"fixture", "fake", "local-fixture"}:
        return FakeGitProvider()
    if provider == "github":
        return GitHubGitHostAdapter()
    if provider == "gitlab":
        return GitLabGitHostAdapter()
    if provider in {"local", "local-only", "none"}:
        return LocalOnlyGitHostAdapter()
    remote_url = ""
    if repository is not None:
        result = subprocess.run(
            ["git", "remote", "get-url", str(delivery.get("remote") or "origin")],
            cwd=Path(repository),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            remote_url = result.stdout.strip().lower()
    if "github.com" in remote_url and shutil.which("gh"):
        return GitHubGitHostAdapter()
    if "gitlab" in remote_url and shutil.which("glab"):
        return GitLabGitHostAdapter()
    return LocalOnlyGitHostAdapter()


def _safe_string(value: Any, *, fallback: str = "Unknown") -> str:
    redacted = redact_data(str(value))
    text = str(redacted).strip()
    return text or fallback


def build_pull_request_body(
    context: MaterializationContext,
    selection: Selection,
    config: Mapping[str, Any],
) -> str:
    """Build a curated, redacted evidence summary; never dump internal records."""

    run_directory = Path(context.run_directory)
    task: dict[str, Any] = {}
    delivery: dict[str, Any] = {}
    for path, target in (
        (run_directory / "task.json", task),
        (run_directory / "delivery.json", delivery),
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            value = {}
        if isinstance(value, dict):
            target.update(value)
    review_value = delivery.get("review")
    review = review_value if isinstance(review_value, dict) else {}
    changed_value = review.get("files_changed")
    changed = (
        [str(item) for item in changed_value]
        if isinstance(changed_value, list)
        else list(DeliveryMaterializerAdapter._captured_changed_files(context))
    )
    validation_value = review.get("validation_evidence")
    validation = validation_value if isinstance(validation_value, list) else []
    validation_lines = [
        f"- {_safe_string(item.get('summary'))}"
        for item in validation
        if isinstance(item, dict)
    ] or ["- No validation summary was available in the delivery record."]
    event_types: list[str] = []
    events_path = run_directory / "events.jsonl"
    if events_path.is_file():
        try:
            event_types = [
                str(item.get("event_type")) for item in read_jsonl_tolerant(events_path)
            ]
        except (OSError, ValueError, json.JSONDecodeError):
            event_types = []
    attempt_count = (
        len([path for path in (run_directory / "attempts").glob("*") if path.is_dir()])
        if (run_directory / "attempts").is_dir()
        else 0
    )
    recovery = [
        name.replace("_", " ")
        for name in event_types
        if name
        in {
            "policy_escalated",
            "verification_escalated",
            "attempt_rejected",
            "recovery_resumed",
        }
    ]
    cost_value = review.get("cost")
    cost = cost_value if isinstance(cost_value, dict) else {}
    amount = cost.get("value")
    cost_text = (
        f"{amount} {cost.get('currency') or 'USD'}"
        if amount is not None
        else f"Unknown ({cost.get('accounting_status') or 'unavailable'})"
    )
    replay = config.get("replay_link")
    replay_line = f"\n- Replay: {_safe_string(replay)}" if replay else ""
    changed_lines = [f"- `{_safe_string(path)}`" for path in changed] or ["- None"]
    recovery_lines = [f"- {_safe_string(item)}" for item in recovery] or ["- None"]
    return "\n".join(
        [
            "## Task",
            "",
            _safe_string(task.get("instruction"), fallback="Task unavailable"),
            "",
            "## Summary",
            "",
            _safe_string(config.get("summary") or selection.reason),
            "",
            "## Changed files",
            "",
            *changed_lines,
            "",
            "## Validation",
            "",
            *validation_lines,
            "",
            "## Verifier authority",
            "",
            _safe_string(review.get("verifier_authority")),
            "",
            "## Attempts and recovery",
            "",
            f"- Attempts: {attempt_count}",
            *recovery_lines,
            "",
            "## Cost",
            "",
            f"- {cost_text}{replay_line}",
            "",
            "---",
            "This patch was generated by an agent and selected by Villani using "
            "the evidence summarized above.",
            "",
        ]
    )


class DeliveryMaterializerAdapter:
    plugin_manifest = MATERIALIZER_MANIFEST
    version = "2.0.0"

    def __init__(
        self,
        *,
        local_apply: Materializer | None = None,
        git_provider: GitHostAdapter | None = None,
        provenance_signer: ProvenanceSigner | None = None,
    ) -> None:
        self.local_apply = local_apply
        self.git_provider = git_provider
        self.provenance_signer = provenance_signer

    def _validated_patch(
        self, selection: Selection, context: MaterializationContext
    ) -> tuple[str, str]:
        candidate = context.selected_candidate
        if selection.selected_attempt_id != candidate.attempt.attempt_id:
            raise DeliveryError(
                "selection_mismatch", "selection does not match candidate"
            )
        patch = candidate.patch
        digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        if not patch or digest != candidate.attempt.patch_sha256:
            raise DeliveryError(
                "patch_integrity_failure",
                "delivery patch does not match the verified snapshot digest",
            )
        return patch, digest

    @staticmethod
    def _captured_changed_files(context: MaterializationContext) -> tuple[str, ...]:
        metadata = getattr(context.selected_candidate.attempt, "metadata", {})
        captured = (
            metadata.get("changed_files") if isinstance(metadata, Mapping) else None
        )
        if captured is None:
            return ()
        if not isinstance(captured, (list, tuple)) or not all(
            isinstance(item, str)
            and item
            and not Path(item).is_absolute()
            and ".." not in Path(item).parts
            for item in captured
        ):
            raise DeliveryError(
                "unsafe_changed_file_capture",
                "candidate changed-file capture is malformed or unsafe",
            )
        return tuple(sorted(set(captured)))

    def materialize(
        self, selection: Selection, context: MaterializationContext
    ) -> Materialization:
        try:
            patch, digest = self._validated_patch(selection, context)
            changed_files = self._captured_changed_files(context)
            config_value = context.policy_configuration.get("delivery", {})
            config = dict(config_value) if isinstance(config_value, Mapping) else {}
            kind = str(config.get("materialization_type") or "local_patch_apply")
            if (
                "verification_graph" in context.policy_configuration
                and self.provenance_signer is None
            ):
                raise DeliveryError(
                    "provenance_signer_unavailable",
                    "verification-graph delivery requires a provenance signer",
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
                receipt = DeliveryReceipt.model_validate_json(
                    receipt_path.read_text(encoding="utf-8")
                )
                if (
                    receipt.patch_sha256 != digest
                    or receipt.materialization_type != kind
                ):
                    raise DeliveryError(
                        "idempotency_conflict",
                        "idempotency key was reused for a different delivery",
                    )
                return Materialization(
                    status="succeeded",
                    final_patch=patch,
                    final_report="Delivery already completed; no cost was repeated.",
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
            safe_metadata = redact_data(metadata)
            if not isinstance(safe_metadata, dict):
                safe_metadata = {}
            receipt = DeliveryReceipt(
                idempotency_key=key,
                materialization_type=kind,
                materializer_name="villani.delivery",
                materializer_version=self.version,
                patch_sha256=digest,
                artifact=artifact,
                changed_files=list(changed_files),
                metadata=safe_metadata,
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
            # The receipt is the commit marker and is written only after delivery.
            write_json_atomic(receipt_path, receipt.model_dump(mode="json"))
            return Materialization(
                status="succeeded",
                final_patch=patch,
                final_report=f"Delivered exact patch digest {digest} using {kind}.",
                changed_files=changed_files,
                metadata=result_metadata,
            )
        except Exception as error:
            code = error.code if isinstance(error, DeliveryError) else "delivery_failed"
            details = error.details if isinstance(error, DeliveryError) else {}
            message = redact_message(str(error))
            return Materialization(
                status="failed",
                final_patch=None,
                final_report=f"Delivery failed: {message}",
                failure=DependencyFailure(
                    code=code,
                    message=message,
                    details={**details, "patch_preserved": True},
                ),
            )

    @staticmethod
    def _source_patch(patch: str, digest: str, context: MaterializationContext) -> Path:
        source = Path(context.run_directory) / "delivery" / f"{digest}.patch"
        source.parent.mkdir(parents=True, exist_ok=True)
        exact = patch.encode("utf-8")
        if source.exists() and source.read_bytes() != exact:
            raise DeliveryError(
                "patch_integrity_failure",
                "exact delivery patch artifact has conflicting content",
            )
        if not source.exists():
            source.write_bytes(exact)
        return source

    @staticmethod
    def _baseline(context: MaterializationContext) -> dict[str, Any]:
        metadata = getattr(context.selected_candidate.attempt, "metadata", {})
        worktree = metadata.get("worktree") if isinstance(metadata, Mapping) else None
        baseline = (
            worktree.get("source_repository") if isinstance(worktree, Mapping) else None
        )
        if not isinstance(baseline, dict):
            raise DeliveryError(
                "repository_identity_missing",
                "selected candidate lacks repository identity evidence",
            )
        return baseline

    @staticmethod
    def _validate_mutation_target(
        context: MaterializationContext,
    ) -> tuple[Path, dict[str, Any], dict[str, Any], str]:
        repo = Path(context.repository_path).resolve()
        baseline = DeliveryMaterializerAdapter._baseline(context)
        baseline_path = Path(str(baseline.get("repository_path") or "")).resolve()
        if repo != baseline_path:
            raise DeliveryError(
                "repository_moved",
                "target repository moved after candidate execution",
            )
        if not repo.is_dir():
            raise DeliveryError(
                "repository_moved",
                "target repository is missing or moved after candidate execution",
            )
        current = repository_identity(repo)
        if not current.get("is_git_repository"):
            raise DeliveryError(
                "invalid_repository", "target is no longer a Git repository"
            )
        if current.get("git_root") != baseline.get("git_root"):
            raise DeliveryError(
                "repository_moved", "target Git root no longer matches the run baseline"
            )
        if current.get("head") != baseline.get("head"):
            raise DeliveryError(
                "target_branch_changed",
                "target branch changed after candidate execution",
            )
        branch_result = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if branch_result.returncode != 0:
            raise DeliveryError(
                "detached_head", "target repository is on a detached HEAD"
            )
        return repo, baseline, current, branch_result.stdout.strip()

    @staticmethod
    def _require_unchanged_clean_worktree(
        baseline: Mapping[str, Any], current: Mapping[str, Any]
    ) -> None:
        current_status = str(current.get("status_porcelain") or "")
        baseline_status = str(baseline.get("status_porcelain") or "")
        if current_status != baseline_status:
            raise DeliveryError(
                "dirty_repository",
                "target working tree changed after candidate execution",
            )
        if current_status.strip():
            raise DeliveryError(
                "dirty_repository",
                "delivery requires a clean target working tree",
            )

    @staticmethod
    def _valid_branch(repo: Path, branch: str) -> None:
        if not branch or any(character.isspace() for character in branch):
            raise DeliveryError("invalid_branch", "delivery branch name is invalid")
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise DeliveryError("invalid_branch", "delivery branch name is invalid")

    def _branch_delivery(
        self,
        *,
        patch: str,
        digest: str,
        config: Mapping[str, Any],
        context: MaterializationContext,
        require_commit: bool,
    ) -> tuple[str, dict[str, Any], Path]:
        repo, baseline, current, original_branch = self._validate_mutation_target(
            context
        )
        self._require_unchanged_clean_worktree(baseline, current)
        branch = str(config.get("branch") or f"villani/{context.run_id}")
        self._valid_branch(repo, branch)
        source = self._source_patch(patch, digest, context)
        delivery_root = Path(context.run_directory) / "delivery"
        worktree = (
            delivery_root
            / "worktrees"
            / hashlib.sha256(branch.encode()).hexdigest()[:16]
        )
        state_path = delivery_root / "branch-state.json"
        state: dict[str, Any] = {}
        if state_path.is_file():
            try:
                value = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise DeliveryError(
                    "delivery_synchronization_failure",
                    "delivery branch state is malformed",
                ) from error
            if not isinstance(value, dict):
                raise DeliveryError(
                    "delivery_synchronization_failure",
                    "delivery branch state is malformed",
                )
            state = value
            if (
                state.get("branch") != branch
                or state.get("patch_sha256") != digest
                or state.get("baseline_head") != baseline.get("head")
            ):
                raise DeliveryError(
                    "branch_already_exists",
                    "delivery branch state does not match this selected patch",
                )
        branch_exists = (
            subprocess.run(
                ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )
        if not state:
            if branch_exists:
                raise DeliveryError(
                    "branch_already_exists",
                    f"branch already exists: {branch}",
                )
            state = {
                "schema_version": "villani.delivery_branch.v1",
                "branch": branch,
                "patch_sha256": digest,
                "baseline_head": baseline.get("head"),
                "worktree": str(worktree),
                "stage": "planned",
            }
            write_json_atomic(state_path, state)
        if not branch_exists and worktree.exists():
            raise DeliveryError(
                "delivery_synchronization_failure",
                "delivery worktree exists without its recorded branch",
            )
        if not branch_exists:
            worktree.parent.mkdir(parents=True, exist_ok=True)
            _run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    str(baseline["head"]),
                ],
                cwd=repo,
                error_code="branch_creation_failed",
            )
            state["stage"] = "branch_created"
            write_json_atomic(state_path, state)
        if not worktree.is_dir():
            raise DeliveryError(
                "delivery_synchronization_failure",
                "recorded delivery worktree is unavailable",
            )
        reverse = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(source)],
            cwd=worktree,
            check=False,
            capture_output=True,
            text=True,
        )
        if reverse.returncode != 0:
            check = subprocess.run(
                ["git", "apply", "--check", str(source)],
                cwd=worktree,
                check=False,
                capture_output=True,
                text=True,
            )
            if check.returncode != 0:
                raise DeliveryError(
                    "patch_conflict",
                    redact_message(check.stderr.strip() or "selected patch conflicts"),
                )
            _run(
                ["git", "apply", "--index", str(source)],
                cwd=worktree,
                error_code="patch_conflict",
            )
            state["stage"] = "patch_applied"
            write_json_atomic(state_path, state)
        commit: str | None = None
        if require_commit:
            message_result = subprocess.run(
                ["git", "log", "-1", "--format=%B"],
                cwd=worktree,
                check=False,
                capture_output=True,
                text=True,
            )
            if (
                message_result.returncode == 0
                and f"Villani-Patch-SHA256: {digest}" in message_result.stdout
            ):
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            else:
                commit_message = _safe_string(
                    config.get("commit_message") or "Apply Villani candidate"
                )
                _run(
                    [
                        "git",
                        "commit",
                        "-m",
                        f"{commit_message}\n\nVillani-Patch-SHA256: {digest}",
                    ],
                    cwd=worktree,
                    error_code="commit_failed",
                )
                commit = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=worktree,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            state.update({"stage": "committed", "commit": commit})
            write_json_atomic(state_path, state)
        after = repository_identity(repo)
        branch_after = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if (
            after.get("head") != current.get("head")
            or after.get("status_porcelain") != current.get("status_porcelain")
            or branch_after != original_branch
        ):
            raise DeliveryError(
                "original_worktree_changed",
                "delivery changed the original branch or working tree",
            )
        return (
            commit or branch,
            {
                "branch": branch,
                "commit": commit,
                "delivery_worktree": str(worktree),
                "original_branch": original_branch,
                "original_head": current.get("head"),
                "original_worktree_unchanged": True,
                "patch_preserved": True,
            },
            worktree,
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
                raise DeliveryError(
                    "materializer_unavailable",
                    "local patch apply materializer is not configured",
                )
            repo, baseline, current, _ = self._validate_mutation_target(context)
            source_value = context.selected_candidate.attempt.patch_path
            source = Path(context.run_directory) / str(source_value)
            if (
                not source.is_file()
                or source.read_text(encoding="utf-8", errors="strict") != patch
            ):
                raise DeliveryError(
                    "patch_integrity_failure",
                    "recorded patch no longer matches verified content",
                )
            already_applied = subprocess.run(
                ["git", "apply", "--reverse", "--check", str(source)],
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
            )
            if already_applied.returncode == 0:
                return "final.patch", {
                    "recovered_exact_patch": True,
                    "patch_preserved": True,
                }
            self._require_unchanged_clean_worktree(baseline, current)
            check = subprocess.run(
                ["git", "apply", "--check", str(source)],
                cwd=repo,
                check=False,
                capture_output=True,
                text=True,
            )
            if check.returncode != 0:
                raise DeliveryError(
                    "patch_conflict",
                    redact_message(check.stderr.strip() or "selected patch conflicts"),
                )
            result = self.local_apply.materialize(selection, context)
            if result.status != "succeeded" or result.final_patch != patch:
                failure = result.failure
                raise DeliveryError(
                    getattr(failure, "code", "safe_apply_failed"),
                    getattr(
                        failure,
                        "message",
                        "local apply did not deliver the exact patch",
                    ),
                )
            return "final.patch", {**dict(result.metadata), "patch_preserved": True}
        if kind == "patch_export":
            destination = Path(
                str(
                    config.get("destination")
                    or Path(context.run_directory) / "delivery" / "selected.patch"
                )
            ).resolve()
            target_repository = Path(context.repository_path).resolve()
            if config.get(
                "workflow_version"
            ) == "villani.delivery_workflow.v1" and destination.is_relative_to(
                target_repository
            ):
                raise DeliveryError(
                    "unsafe_patch_destination",
                    "suggest mode cannot write its patch inside the target repository",
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            patch_bytes = patch.encode("utf-8")
            if destination.exists() and destination.read_bytes() != patch_bytes:
                raise DeliveryError(
                    "patch_export_conflict",
                    "patch export destination already has different content",
                )
            if not destination.exists():
                destination.write_bytes(patch_bytes)
            return str(destination), {
                "repository_modified": False,
                "patch_preserved": True,
            }
        if kind in {"local_branch", "local_branch_commit"}:
            commit_requested = kind == "local_branch_commit" or bool(
                config.get("commit", False)
            )
            artifact, metadata, _ = self._branch_delivery(
                patch=patch,
                digest=digest,
                config=config,
                context=context,
                require_commit=commit_requested,
            )
            return artifact, metadata
        if kind == "pull_request":
            artifact, branch_metadata, worktree = self._branch_delivery(
                patch=patch,
                digest=digest,
                config=config,
                context=context,
                require_commit=True,
            )
            provider = self.git_provider or build_git_host_adapter(
                context.policy_configuration, context.repository_path
            )
            branch = str(branch_metadata["branch"])
            remote = str(config.get("remote") or "origin")
            push = provider.push_branch(
                repository=worktree,
                branch=branch,
                remote=remote,
                idempotency_key=key,
            )
            body = build_pull_request_body(context, selection, config)
            body_path = (
                Path(context.run_directory) / "delivery" / "pull-request-body.md"
            )
            body_path.write_text(body, encoding="utf-8", newline="\n")
            response = provider.create_or_get_pull_request(
                idempotency_key=key,
                repository=str(worktree),
                branch=branch,
                title=_safe_string(config.get("title") or "Villani candidate"),
                body_path=str(body_path),
                base_branch=(
                    str(config["base_branch"]) if config.get("base_branch") else None
                ),
                patch_sha256=digest,
            )
            safe_response = redact_data(response)
            if not isinstance(safe_response, dict):
                safe_response = {}
            return str(safe_response.get("url") or artifact), {
                **branch_metadata,
                "provider": provider.provider_name,
                "push": redact_data(push),
                "pull_request": safe_response,
                "pull_request_body": "delivery/pull-request-body.md",
            }
        raise DeliveryError(
            "unsupported_delivery_mode", f"unsupported materialization type: {kind}"
        )
