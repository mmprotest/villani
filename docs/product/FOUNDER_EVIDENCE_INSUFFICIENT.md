# PT4 founder evidence sufficiency

Status: **INSUFFICIENT_EVIDENCE**  
Gate B: **INSUFFICIENT_EVIDENCE**  
Assessment date: 2026-07-17

PT4 cannot authorize production performance changes. The workspace contains no qualifying paired
real founder tasks, no qualifying repository population, and no human-labelled founder outcomes.
The pre-change Gate B execution therefore stops the milestone at the evidence boundary required by
the PT4 prompt.

## Pre-change gate execution

The gate was executed before any PT4 production behavior changed. A frozen synthetic suite was
used only to exercise the real installed `villani eval gate` path; its task is structurally
ineligible and counted as zero founder evidence.

```powershell
# cwd: repository root
New-Item -ItemType Directory -Force .test-temp\pt4-home | Out-Null
$env:VILLANI_HOME=(Resolve-Path .test-temp\pt4-home).Path
& .venv\Scripts\villani.exe eval gate .test-temp\pt4-prechange-synthetic-20260717 --json
```

Process result: exit code 2. Gate result: `INSUFFICIENT_EVIDENCE`.

The frozen execution fixture has suite digest
`6254c99f1149eaaae07e72e783147ca7b8c9ed4758419b9307547deb80105d49`, evidence kind
`synthetic_fixture`, one synthetic task, zero trials, and zero human reviews. The fixture is not a
substitute for founder evidence and is ignored by every count below.

## Exact missing evidence

| Evidence | Actual | PT4 hardening threshold | Gate B threshold | Status |
| --- | ---: | ---: | ---: | --- |
| Paired real tasks | 0 | 20 | 30 | Missing |
| Source repositories | 0 | 2 | 2 | Missing |
| Valid immutable real-task baselines | 0 established | Every task | Every task | Unresolved |
| Completed paired real trials | 0 | Both arms for every task | Both arms for every task | Missing |
| Latest human reviews | 0 | Complete enough to diagnose clusters | Complete for every eligible trial | Missing |
| Known false acceptances | Undefined population | No increase permitted | Exactly 0 | Unresolved |
| Accepted-as-is rates | Direct: unknown; Villani: unknown | Required for before/after | Villani must not be lower | Missing |
| Median review-time delta | Unknown | Required for prioritization | At least 30% lower, or cost criterion | Missing |
| Total cost per accepted change delta | Unknown | Required for prioritization | At least 25% lower, or review criterion | Missing |
| Automatic configuration rate | Unknown | Required for friction diagnosis | At least 80% | Missing |
| Verifier confusion matrix | 0 labelled cases | Required for verifier changes | Zero known false acceptance | Missing |
| Failure clusters | 0 real linked artifacts | Repeated mechanisms required | Not independently sufficient | Missing |
| Exact before/after frozen-task identity | 0 pairs | Every changed cluster | Required for supported claims | Missing |
| Complete unknown/exclusion disclosure | Attested only for the synthetic execution fixture | Required for real suite | Required for real suite | Unresolved for founder work |

The earlier durable Gate B record at `docs/PT3_GATE_B.json` independently reports the same founder
population: 0 eligible real tasks, 0 paired real tasks, 0 repositories, 0 eligible trials, and 0
completed human reviews.

## Consequence for PT4

- Do not change navigation, compaction, patch-quality policy, classification, validation discovery,
  focused probes, verifier normalization, retry, escalation, selection, accounting, or failure
  presentation based on this empty population.
- Do not rank speculative fixes: frequency, recoverable accepted-change loss, cost/supervision
  burden, and diagnostic confidence are all unobserved.
- Do not generate a founder-proof certificate. Certificates are permitted only when recalculated
  Gate B passes.
- Changes in this PT4 pass may cover evidence analysis, fail-closed insufficiency behavior, static
  safety checks, documentation, and an undeniable correctness defect proven by an individual
  artifact. Source inspection established one such defect outside the founder-performance gate:
  unknown trial duration was coerced to numeric zero in evaluation metrics. The identifier-free
  regression fixture proves that unknown now remains `null` with `accounting_status: unknown`.

The complete taxonomy, empty verifier population, prioritization refusal, before/after evidence
state, and certificate decision are recorded in
[`PT4_HARDENING_ANALYSIS.md`](PT4_HARDENING_ANALYSIS.md) and its machine-readable JSON companion.

## Next experiments

1. Capture and freeze at least 20 real tasks from the founder's work before considering any PT4
   hardening change; capture at least 30 across two repositories to make Gate B decidable.
2. Preserve one exact immutable baseline per task and prove restoration before either arm runs.
3. Run the direct and Villani arms from that identical baseline, with persisted randomized order,
   complete agent/environment versions, and the same arm-blind final verification.
4. Complete blinded append-only human review for every eligible trial, including review minutes,
   correction outcome, false acceptance/rejection, and later rollback or reopened-defect facts.
5. Record actual execution and verification cost where known; retain null plus accounting status
   where unknown. Record measured local power/runtime/electricity price only when configured.
6. Generate the failure taxonomy over linked real artifacts. Treat a cluster as repeated only when
   at least two independent real task identities exhibit the same generic mechanism.
7. Rank only repeated fixable clusters using
   `frequency x recoverable accepted-change loss x average cost or supervision burden x diagnostic confidence`.
8. For the highest-ranked supported cluster, derive an identifier-free regression fixture, apply a
   generic fix, and rerun every member on the exact frozen snapshots with tool/model version changes
   disclosed.
9. Recalculate verifier diagnostics and Gate B. Stop if any reviewed false acceptance is introduced.
10. Issue a redacted content-addressed founder-proof certificate only after Gate B returns `PASS`.

PT5 is not authorized by this evidence state and was not started.
