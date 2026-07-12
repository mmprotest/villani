from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import secrets
import time
from villani_ops.isolation.copy_git import (
    create_git_baselined_copy,
    capture_candidate_patch,
    remove_tree,
)
from villani_ops.runners import runner_for_name
from villani_ops.storage.files import FileStorage
from villani_ops.core.task import Task
from villani_ops.core.backend import Backend
from villani_ops.git_ops import safe_apply
from villani_ops.verifier.service import (
    debug_resolution as _debug_resolution,
    execute_verifier,
    resolve_verifier_debug_dir as resolve_verifier_debug_dir,
)
from .selection import (
    select_winner,
    POLICY,
    LLM_COMPARE_POLICY,
    select_success_with_llm_comparison,
    build_candidate_evidence_matrix,
    write_candidate_evidence_matrix,
    write_selection_report,
    finalize_evidence_reasons,
    rank_candidates_by_evidence,
)


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(p: Path, o):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, indent=2, default=str), encoding="utf-8")


def append_jsonl(p: Path, o):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.open("a", encoding="utf-8").write(json.dumps(o, default=str) + "\n")


def wire_path(value):
    return Path(value).as_posix() if value else None


def build_verifier_parallel_candidate_task(
    task: str, success_criteria: str | None, candidate_id: str
) -> str:
    parts = [
        f"Verifier-parallel candidate: {candidate_id}",
        "",
        "Original task (preserved verbatim):",
        task,
    ]
    if success_criteria:
        parts += ["", "Success criteria:", success_criteria]
    parts += [
        "",
        "Before finishing, run or describe validation that exercises the riskiest requirement in the task, not only the happy path. If the task involves abnormal-path behavior such as interruption, cancellation, cleanup, rollback, timeout, failure handling, concurrency, persistence, recovery, edge cases, or resource management, include validation that targets that behavior. State what was validated and what remains unvalidated.",
    ]
    return "\n".join(parts)


@dataclass
class VerifierParallelConfig:
    repo: Path
    task: str
    success_criteria: str | None = None
    candidates: int = 5
    parallelism: int | None = None
    seed: int | None = None
    workspace: Path = Path(".villani-ops")
    agent: str = "villani-code"
    backend: str | None = None
    verifier_backend: str | None = None
    candidate_timeout_seconds: int | None = None
    verifier_timeout_seconds: int = 180
    verifier_max_tool_calls: int = 12
    on_all_fail: str = "fail"
    keep_worktrees: bool = False
    out: Path | None = None


@dataclass
class CandidateResult:
    candidate_id: str
    worktree_path: Path
    run_status: str = "pending"
    debug_root: Path | None = None
    debug_dir: Path | None = None
    resolved_trace_dir: Path | None = None
    debug_resolution_status: str | None = None
    debug_resolution_reason: str | None = None
    verifier_result: dict | None = None
    verifier_trace_dir: Path | None = None
    error: str | None = None
    artifacts_dir: Path | None = None
    started_at: str | None = None
    completed_at: str | None = None
    exit_code: int | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    duration_seconds: float | None = None
    changed_files: list[str] | None = None
    patch_path: Path | None = None
    patch_status: str | None = None


class VerifierParallelOrchestrator:
    def __init__(
        self,
        config: VerifierParallelConfig,
        runner=None,
        verifier=None,
        integrator=None,
    ):
        self.config = config
        self.runner = runner
        self.verifier = verifier
        self.integrator = integrator

    def _backend_obj(self):
        s = FileStorage(self.config.workspace)
        s.init_workspace()
        backs = s.load_backends()
        if self.config.backend:
            if self.config.backend not in backs:
                raise ValueError(f"missing backend: {self.config.backend}")
            return backs[self.config.backend]
        elig = [b for b in backs.values() if b.enabled]
        if not elig:
            raise ValueError("missing backend")
        return sorted(elig, key=lambda b: b.capability_score, reverse=True)[0]

    def _run_candidate(self, cid, odir, backend: Backend):
        cdir = odir / "candidates" / cid
        rdir = cdir / "run"
        rdir.mkdir(parents=True, exist_ok=True)
        cr = CandidateResult(
            cid, cdir / "worktree", "running", artifacts_dir=rdir, started_at=now()
        )
        start = time.time()
        try:
            copied = create_git_baselined_copy(self.config.repo, cdir)
            cr.worktree_path = copied.worktree_path
            cr.patch_path = copied.patch_path
        except Exception as e:
            cr.error = f"candidate isolation setup failed: {e}"
            cr.run_status = "failed"
            cr.patch_status = "failed"
            (rdir / "stderr.txt").write_text(cr.error, encoding="utf-8")
            cr.completed_at = now()
            cr.duration_seconds = time.time() - start
            return cr
        try:
            run = self.runner or runner_for_name(self.config.agent or "villani-code")
            res = run.run_task(
                repo_path=cr.worktree_path,
                task=build_verifier_parallel_candidate_task(
                    self.config.task, self.config.success_criteria, cid
                ),
                success_criteria=self.config.success_criteria,
                backend_name=backend.name,
                backend_config=backend,
                timeout_seconds=self.config.candidate_timeout_seconds
                or backend.timeout_seconds
                or 1200,
                context={"attempt_id": cid},
                artifacts_dir=rdir,
            )
            (rdir / "stdout.txt").write_text(res.stdout or "", encoding="utf-8")
            (rdir / "stderr.txt").write_text(res.stderr or "", encoding="utf-8")
            cr.exit_code = res.exit_code
            cr.stdout_path = rdir / "stdout.txt"
            cr.stderr_path = rdir / "stderr.txt"
            cr.debug_root = (
                Path(res.debug_artifact_dir) if res.debug_artifact_dir else None
            )
            cr.resolved_trace_dir = (
                Path(res.resolved_trace_dir) if res.resolved_trace_dir else None
            )
            cr.debug_dir, cr.debug_resolution_status, cr.debug_resolution_reason = (
                _debug_resolution(cr.debug_root, cr.resolved_trace_dir)
            )
            cr.run_status = "completed" if res.exit_code == 0 else "failed"
        except Exception as e:
            cr.error = f"agent runner failed: {e}"
            cr.run_status = "failed"
            (rdir / "stderr.txt").write_text(cr.error, encoding="utf-8")
        try:
            cap = capture_candidate_patch(
                cr.worktree_path, cr.patch_path or (cdir / "diff.patch")
            )
            cr.changed_files = cap.changed_files
            cr.patch_path = (
                Path(cap.patch_path) if cap.patch_path else (cdir / "diff.patch")
            )
            cr.patch_status = (
                "captured"
                if cap.patch_path
                else ("failed" if cap.failure_reason else "empty")
            )
            if cap.failure_reason:
                cr.error = (
                    cr.error + "; " if cr.error else ""
                ) + f"patch capture failed: {cap.failure_reason}"
        except Exception as e:
            cr.patch_status = "failed"
            cr.error = (
                cr.error + "; " if cr.error else ""
            ) + f"patch capture failed: {e}"
        cr.completed_at = now()
        cr.duration_seconds = time.time() - start
        return cr

    def _run_verifier(self, cr: CandidateResult, odir: Path):
        vdir = odir / "candidates" / cr.candidate_id / "verifier"
        vdir.mkdir(parents=True, exist_ok=True)
        out = vdir / "verifier-result.json"
        execution = execute_verifier(
            debug_root=cr.debug_root,
            resolved_trace_dir=cr.resolved_trace_dir or cr.debug_dir,
            repo_dir=cr.worktree_path,
            workspace=self.config.workspace,
            backend=self.config.verifier_backend,
            out=out,
            trace_dir=vdir / "trace",
            timeout_seconds=self.config.verifier_timeout_seconds,
            max_tool_calls=self.config.verifier_max_tool_calls,
            verifier=self.verifier,
            invocation="subprocess",
            stdout_path=vdir / "stdout.txt",
            stderr_path=vdir / "stderr.txt",
        )
        cr.debug_dir = execution.debug_dir
        cr.debug_resolution_status = execution.resolution_status
        cr.debug_resolution_reason = execution.resolution_reason
        cr.verifier_result = execution.result
        cr.verifier_trace_dir = (
            Path(cr.verifier_result.get("traceDir"))
            if cr.verifier_result.get("traceDir")
            else None
        )
        return cr

    def _record_candidate(self, cr, p):
        v = cr.verifier_result or {}
        verifier_trace = wire_path(cr.verifier_trace_dir or v.get("traceDir"))
        return {
            "candidateId": cr.candidate_id,
            "worktreePath": str(cr.worktree_path),
            "status": "verified" if v else cr.run_status,
            "agent": self.config.agent,
            "backend": self.config.backend,
            "startedAt": cr.started_at,
            "completedAt": cr.completed_at,
            "debugRoot": str(cr.debug_root) if cr.debug_root else None,
            "debugDir": str(cr.debug_dir) if cr.debug_dir else None,
            "candidateDebugDir": wire_path(cr.debug_dir),
            "resolvedTraceDir": str(cr.resolved_trace_dir)
            if cr.resolved_trace_dir
            else None,
            "debugResolutionStatus": cr.debug_resolution_status,
            "debugResolutionReason": cr.debug_resolution_reason,
            "patchPath": str(cr.patch_path) if cr.patch_path else None,
            "patchStatus": cr.patch_status,
            "verifierResultPath": str(
                p / "candidates" / cr.candidate_id / "verifier" / "verifier-result.json"
            ),
            "verifierTraceDir": verifier_trace,
            "traceDir": verifier_trace,
            "result": v.get("result"),
            "verdict": v.get("verdict"),
            "confidence": v.get("confidence"),
            "recommendedAction": v.get("recommendedAction"),
            "error": cr.error
            or (v.get("reason") if v.get("verdict") == "error" else None),
        }

    def _integrate(self, odir, winner):
        rec = {
            "schemaVersion": "villani-ops-verifier-parallel-integration-v1",
            "winnerCandidateId": winner.candidate_id if winner else None,
            "sourceWorktree": str(winner.worktree_path) if winner else None,
            "targetRepo": str(self.config.repo),
            "patchPath": str(winner.patch_path)
            if winner and winner.patch_path
            else None,
            "status": "skipped",
            "changedFiles": [],
            "error": None,
        }
        if not winner:
            write_json(odir / "integration.json", rec)
            return rec
        write_json(
            odir / "task.json",
            Task(
                repo_path=str(self.config.repo),
                objective=self.config.task,
                success_criteria=self.config.success_criteria,
            ).model_dump(mode="json"),
        )
        write_json(
            odir / "decision.json",
            {
                "accepted": True,
                "winning_patch_path": str(winner.patch_path),
                "winning_attempt_id": winner.candidate_id,
            },
        )
        try:
            art = (
                self.integrator(odir, winner)
                if self.integrator
                else safe_apply(odir, artifact_name="integration-apply.json")
            )
            rec.update(
                {
                    "status": "integrated",
                    "changedFiles": winner.changed_files or [],
                    "apply": art,
                }
            )
        except Exception as e:
            rec.update({"status": "failed", "error": str(e)})
        write_json(odir / "integration.json", rec)
        return rec

    def _materialization_record(self, odir, source, selection, integ, winner):
        v = (winner.verifier_result or {}) if winner else {}
        fallback = bool(selection.get("fallback") or selection.get("fallbackWinner"))
        status = (
            "selected"
            if winner
            and (
                integ.get("status") in {"integrated", "skipped"}
                or fallback
                or selection.get("winnerResult") == 1
            )
            else ("no_winner" if not winner else "integration_failed")
        )
        rec = {
            "schemaVersion": "villani-ops-materializable-selection-v1",
            "source": source,
            "orchestrationId": odir.name,
            "orchestrationDir": str(odir),
            "winnerCandidateId": winner.candidate_id if winner else None,
            "winnerResult": selection.get("winnerResult"),
            "winnerVerdict": v.get("verdict"),
            "winnerConfidence": v.get("confidence"),
            "targetRepo": str(self.config.repo),
            "patchPath": str(winner.patch_path)
            if winner and winner.patch_path
            else None,
            "selectionPath": str(odir / "selection.json"),
            "integrationPath": str(odir / "integration.json"),
            "createdAt": now(),
            "status": status,
            "fallbackWinner": fallback,
            "selectionPolicy": selection.get("selectionPolicy"),
            "materializationPath": str(odir / "materialization.json"),
            "candidateDebugDir": wire_path(winner.debug_dir) if winner else None,
            "verifierTraceDir": wire_path(
                winner.verifier_trace_dir or v.get("traceDir")
            )
            if winner
            else None,
        }
        write_json(odir / "materialization.json", rec)
        if winner:
            sel2 = dict(selection)
            sel2.update(
                {
                    "winnerPatchPath": rec["patchPath"],
                    "materializationPath": rec["materializationPath"],
                    "candidateDebugDir": rec["candidateDebugDir"],
                    "verifierTraceDir": rec["verifierTraceDir"],
                    "traceDir": rec["verifierTraceDir"],
                }
            )
            write_json(odir / "selection.json", sel2)
            write_json(
                odir / "task.json",
                Task(
                    repo_path=str(self.config.repo),
                    objective=self.config.task,
                    success_criteria=self.config.success_criteria,
                ).model_dump(mode="json"),
            )
            write_json(
                odir / "decision.json",
                {
                    "accepted": True,
                    "winning_patch_path": rec["patchPath"],
                    "winning_attempt_id": winner.candidate_id,
                },
            )
        return rec

    def run(self):
        cfg = self.config
        cfg.repo = Path(cfg.repo).resolve()
        cfg.workspace = Path(cfg.workspace).resolve()
        cfg.parallelism = cfg.parallelism or cfg.candidates
        cfg.seed = cfg.seed if cfg.seed is not None else secrets.randbelow(2**31)
        if (
            cfg.candidates < 1
            or cfg.parallelism < 1
            or cfg.parallelism > cfg.candidates
        ):
            raise ValueError("invalid candidates/parallelism")
        oid = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + secrets.token_hex(3)
        )
        odir = cfg.workspace / "orchestrations" / oid
        odir.mkdir(parents=True)
        backend = self._backend_obj()
        cfg.backend = cfg.backend or backend.name
        selector_backend = backend
        if cfg.verifier_backend and cfg.verifier_backend != backend.name:
            backs = FileStorage(cfg.workspace).load_backends()
            selector_backend = backs.get(cfg.verifier_backend) or backend
        candidates = []
        with ThreadPoolExecutor(max_workers=cfg.parallelism) as ex:
            futs = [
                ex.submit(self._run_candidate, f"candidate-{i:03d}", odir, backend)
                for i in range(1, cfg.candidates + 1)
            ]
            for f in as_completed(futs):
                candidates.append(self._run_verifier(f.result(), odir))
        candidates = sorted(candidates, key=lambda c: c.candidate_id)
        for cr in candidates:
            append_jsonl(
                odir / "candidate-runs.jsonl",
                {
                    "candidateId": cr.candidate_id,
                    "status": cr.run_status,
                    "exitCode": cr.exit_code,
                    "durationSeconds": cr.duration_seconds,
                    "stdoutPath": str(cr.stdout_path) if cr.stdout_path else None,
                    "stderrPath": str(cr.stderr_path) if cr.stderr_path else None,
                    "debugRoot": str(cr.debug_root) if cr.debug_root else None,
                    "debugDir": str(cr.debug_dir) if cr.debug_dir else None,
                    "resolvedTraceDir": str(cr.resolved_trace_dir)
                    if cr.resolved_trace_dir
                    else None,
                },
            )
            v = cr.verifier_result or {}
            verifier_trace = wire_path(v.get("traceDir"))
            append_jsonl(
                odir / "verifier-results.jsonl",
                {
                    "candidateId": cr.candidate_id,
                    "result": v.get("result"),
                    "verdict": v.get("verdict"),
                    "confidence": v.get("confidence"),
                    "recommendedAction": v.get("recommendedAction"),
                    "debugDir": str(cr.debug_dir) if cr.debug_dir else None,
                    "candidateDebugDir": wire_path(cr.debug_dir),
                    "verifierTraceDir": verifier_trace,
                    "traceDir": verifier_trace,
                    "verifierResultPath": str(
                        odir
                        / "candidates"
                        / cr.candidate_id
                        / "verifier"
                        / "verifier-result.json"
                    ),
                },
            )
            append_jsonl(odir / "candidates.jsonl", self._record_candidate(cr, odir))
        sel = select_winner(candidates, cfg.seed, cfg.on_all_fail)
        successes = [
            c for c in candidates if (c.verifier_result or {}).get("result") == 1
        ]
        llm_advisory = None
        if successes:
            ranked_successes = rank_candidates_by_evidence(successes)
            final_winner_id = (
                ranked_successes[0]["candidate_id"] if ranked_successes else None
            )
            if final_winner_id:
                sel.winnerCandidateId = final_winner_id
                sel.winnerResult = 1
                sel.selectionPolicy = POLICY
                sel.reason = (
                    ranked_successes[0].get("final_selection_reason")
                    or f"Selected {final_winner_id} by deterministic evidence-ranked selection among verifier-success candidates."
                )
                sel.candidatePool = [r["candidate_id"] for r in ranked_successes]
                sel.tieBreak = len(sel.candidatePool) > 1
        if len(successes) > 1:
            meta = {
                "policyName": LLM_COMPARE_POLICY,
                "eligibleCandidateIds": [c.candidate_id for c in successes],
                "selectedCandidateId": None,
                "comparisonReason": None,
                "fallbackUsed": False,
                "fallbackReason": None,
                "usedForFinalDecision": False,
                "evidenceRankedWinnerId": sel.winnerCandidateId,
                "disagreedWithEvidenceSelector": False,
                "advisoryNote": None,
            }
            try:
                cmp = select_success_with_llm_comparison(
                    task=cfg.task,
                    success_criteria=cfg.success_criteria,
                    candidates=successes,
                    model=selector_backend.model,
                    base_url=selector_backend.base_url,
                    provider=selector_backend.provider,
                    api_key=selector_backend.api_key,
                    timeout_s=cfg.verifier_timeout_seconds,
                )
                if (
                    cmp
                    and cmp.get("selectedCandidateId") in meta["eligibleCandidateIds"]
                ):
                    meta["selectedCandidateId"] = cmp["selectedCandidateId"]
                    meta["comparisonReason"] = cmp.get("reason")
                    if meta["selectedCandidateId"] != sel.winnerCandidateId:
                        meta["disagreedWithEvidenceSelector"] = True
                        meta["advisoryNote"] = (
                            f"LLM comparison recommended {meta['selectedCandidateId']}, but evidence-ranked selector selected {sel.winnerCandidateId} because {sel.reason}"
                        )
                else:
                    meta["fallbackUsed"] = True
                    meta["fallbackReason"] = "LLM comparative selector unavailable"
            except Exception as e:
                meta["fallbackUsed"] = True
                meta["fallbackReason"] = str(e)
                sel.reason += f" LLM comparative selection failed; used deterministic evidence-ranked selector: {e}"
            llm_advisory = meta
            sel.llmComparison = meta
        evidence_matrix = finalize_evidence_reasons(
            build_candidate_evidence_matrix(candidates, sel.winnerCandidateId),
            sel.winnerCandidateId,
        )
        selected_row = next(
            (
                row
                for row in evidence_matrix
                if row.get("selection_status") == "selected"
            ),
            None,
        )
        if selected_row and selected_row.get("final_selection_reason"):
            sel.reason = selected_row["final_selection_reason"]
        if llm_advisory:
            for row in evidence_matrix:
                recommended = row["candidate_id"] == llm_advisory.get(
                    "selectedCandidateId"
                )
                row["llm_comparison_recommended"] = recommended
                row["llm_comparison_reason"] = (
                    llm_advisory.get("comparisonReason") if recommended else None
                )
                row["llm_disagreement_with_evidence_selector"] = bool(
                    recommended and llm_advisory.get("disagreedWithEvidenceSelector")
                )
                if recommended and llm_advisory.get("advisoryNote"):
                    row["llm_comparison_advisory_note"] = llm_advisory["advisoryNote"]
        write_json(odir / "selection.json", sel.to_dict())
        write_candidate_evidence_matrix(
            odir / "candidate_evidence_matrix.json", evidence_matrix
        )
        write_selection_report(
            odir / "selection_report.md", evidence_matrix, sel.winnerCandidateId
        )
        winner = next(
            (c for c in candidates if c.candidate_id == sel.winnerCandidateId), None
        )
        integ = self._integrate(odir, winner)
        status = (
            "completed"
            if integ["status"] in {"integrated", "skipped"}
            and (winner or cfg.on_all_fail == "fail")
            else "failed"
        )
        if sel.winnerCandidateId is None and cfg.on_all_fail == "fail":
            status = "failed"
        mat = self._materialization_record(
            odir, "verifier-parallel", sel.to_dict(), integ, winner
        )
        orch = {
            "schemaVersion": "villani-ops-verifier-parallel-orchestration-v1",
            "orchestrationId": oid,
            "mode": "verifier-parallel",
            "createdAt": oid[:16],
            "completedAt": now(),
            "status": status,
            "repo": str(cfg.repo),
            "workspace": str(cfg.workspace),
            "taskPreview": cfg.task[:120],
            "candidates": cfg.candidates,
            "parallelism": cfg.parallelism,
            "seed": cfg.seed,
            "agent": cfg.agent,
            "backend": cfg.backend,
            "verifierBackend": cfg.verifier_backend,
            "onAllFail": cfg.on_all_fail,
            "winnerCandidateId": sel.winnerCandidateId,
            "selectionPolicy": sel.selectionPolicy,
            "materializationPath": str(odir / "materialization.json"),
            "winnerPatchPath": mat.get("patchPath"),
        }
        write_json(odir / "orchestration.json", orch)
        self._transcript(odir, candidates, sel, integ)
        if not cfg.keep_worktrees and integ["status"] == "integrated":
            for cr in candidates:
                if cr.worktree_path and cr.worktree_path.exists():
                    remove_tree(cr.worktree_path)
        out = {
            "schemaVersion": "villani-ops-verifier-parallel-output-v1",
            "orchestrationId": oid,
            "status": status,
            "winnerCandidateId": sel.winnerCandidateId,
            "winnerResult": sel.winnerResult,
            "selectionPath": str(odir / "selection.json"),
            "integrationPath": str(odir / "integration.json"),
            "materializationPath": str(odir / "materialization.json"),
            "winnerPatchPath": mat.get("patchPath"),
            "candidateDebugDir": mat.get("candidateDebugDir"),
            "verifierTraceDir": mat.get("verifierTraceDir"),
            "orchestrationDir": str(odir),
            "candidates": sel.allCandidates,
        }
        if cfg.out:
            write_json(cfg.out, out)
        return out

    def _transcript(self, odir, cands, sel, integ):
        rows = "\n".join(
            f"| {c.candidate_id} | {c.run_status} | {c.debug_root or ''} | {c.debug_dir or ''} | {(c.verifier_result or {}).get('result')} | {(c.verifier_result or {}).get('confidence')} | {(c.verifier_result or {}).get('traceDir')} |"
            for c in cands
        )
        (odir / "transcript.md").write_text(
            f"# Verifier Parallel Orchestration\n\n## Summary\n- Repo: {self.config.repo}\n- Candidates: {len(cands)}\n- Winner: {sel.winnerCandidateId}\n- Selection policy: {POLICY}\n- Seed: {self.config.seed}\n\n## Task\n\n{self.config.task}\n\n## Candidate Results\n\n| Candidate | Run Status | Debug Root | Debug Dir | Verifier Result | Confidence | Trace |\n|---|---|---|---|---:|---:|---|\n{rows}\n\n## Selection\n\n{sel.reason}\n\n## Integration\n\n{integ.get('status')} {integ.get('error') or ''}\n",
            encoding="utf-8",
        )
