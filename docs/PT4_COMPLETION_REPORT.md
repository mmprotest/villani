# Villani Product Transformation PT4 completion report

Milestone status: **INSUFFICIENT_EVIDENCE**

The evidence-bounded PT4 pass is finished, but the milestone is not claimed complete. Founder Gate
B has 0 paired real tasks across 0 repositories, real-baseline integrity is unresolved, and there
are 0 human-labelled founder outcomes. No real-task before/after population exists. The required
performance-hardening acceptance evidence therefore does not exist.

PT5 is not authorized and was not started.

## First-action evidence decision

The Founder Gate path was exercised before PT4 production behavior changed. An empty real-founder
workspace truthfully failed validation with `no_tasks`, could not be frozen, and could not generate
a report. A frozen synthetic suite was then used only to execute the installed Gate B path. Its one
task is structurally ineligible and counted as zero founder evidence.

- execution suite: `.test-temp/pt4-prechange-synthetic-20260717`
- suite digest: `6254c99f1149eaaae07e72e783147ca7b8c9ed4758419b9307547deb80105d49`
- Gate B result before changes: `INSUFFICIENT_EVIDENCE`, process exit 2
- Gate B result after changes: `INSUFFICIENT_EVIDENCE`, process exit 2
- PT4 threshold: 20 paired real tasks with valid baselines and materially complete labels
- actual: 0 paired real tasks, 0 real repositories, 0 eligible trials, 0 reviews
- Gate B threshold: 30 paired real tasks across at least 2 repositories

The exact missing evidence and next experiments are recorded in
[`product/FOUNDER_EVIDENCE_INSUFFICIENT.md`](product/FOUNDER_EVIDENCE_INSUFFICIENT.md).

## Exact files

Added:

- `components/villani-ops/villani_ops/evaluation_lab/hardening.py`
- `components/villani-ops/villani_ops/tests/test_founder_hardening.py`
- `integration/fixtures/evaluation/pt4/unknown-duration-accounting.json`
- `docs/product/FOUNDER_EVIDENCE_INSUFFICIENT.md`
- `docs/product/PT4_FOUNDER_EVIDENCE_SUFFICIENCY.json`
- `docs/product/PT4_HARDENING_ANALYSIS.md`
- `docs/product/PT4_HARDENING_ANALYSIS.json`
- `docs/PT4_COMPLETION_REPORT.md`
- `docs/PT4_COMPLETION_REPORT.json`

Changed:

- `components/villani-ops/villani_ops/evaluation_lab/reporting.py`
- `docs/FOUNDER_THESIS_LAB.md`
- `PLANS.md` progress section only

Deleted: none.

Migrated: none.

Unrelated PT0-PT3 and user working-tree changes were preserved.

## Architecture and product decisions

- PT4 analysis is isolated in `evaluation_lab.hardening`; it is not imported by controller, policy,
  semantic verification, selection, or delivery code.
- The analysis implements all 19 required failure classes. It accepts only explicit linked
  observations marked `real_founder_work`; synthetic observations cannot affect counts or ranks.
- Clusters expose count, opaque repositories, task classes, agent systems, truthful cost/review
  accounting, acceptance impact, diagnostic confidence, artifacts, and generic-fix availability.
- Ranking uses exactly `frequency x recoverable accepted-change loss x average cost or supervision
  burden x diagnostic confidence`. Unknown incomparable burden and non-repeated mechanisms are not
  ranked. This workspace produces an empty ranking.
- Paired evidence requires both arms to share task identity, repetition, task digest, and immutable
  baseline digest. Mismatched trials do not count.
- Infrastructure exclusions are disclosed separately and do not distort the human-labelled
  verifier matrix. Precision, recall, specificity, and F1 remain undefined when denominators are
  absent. Binary verification never gains a fabricated calibration probability.
- Exact frozen before/after comparison rejects task, baseline, arm, repetition, or population drift
  and discloses tool-version changes.
- A founder-proof certificate can be generated only for Gate B `PASS` with zero known false
  acceptance. Repository identities and exclusion reasons are digested. No certificate was issued.
- The sole behavior correction is an undeniable accounting defect: unknown trial duration is no
  longer coerced through `value_ms or 0`. It stays `null` with `accounting_status: unknown`.
- No routing, retry, escalation, acceptance, selection, delivery, verifier authority, or repository
  mutation behavior changed.

## Schema and configuration migrations

There is no public wire-contract or configuration migration. No root schema, packaged schema,
Python public protocol model, TypeScript model, CLI command, UI route, or configuration key changed.
The PT4 analysis JSON is a milestone evidence artifact, not a replacement for the versioned PT3
evaluation contracts. Existing run bundles, evaluation suites, trials, reviews, reports, and
configuration remain readable.

## Tests added

`test_founder_hardening.py` adds nine generic tests:

1. complete failure taxonomy, required cluster fields, opaque identities, and synthetic exclusion;
2. exact prioritization math and refusal to rank unknown burden;
3. human-labelled verifier metrics, false cases, requirement errors, evidence correlations,
   semantic/deterministic disagreement, and infrastructure exclusion;
4. exact frozen before/after identity, deltas, duplicate prevention, and tool-version disclosure;
5. same-repetition, same-task-digest, same-baseline paired-population enforcement;
6. identifier-free unknown-duration regression fixture and truthful unknown accounting;
7. Gate B recalculation and fail-closed insufficient-evidence behavior;
8. PASS-only content-addressed certificate redaction and false-acceptance rejection;
9. exact identifier scan over production rules, including a positive canary.

## Validation commands and exact results

Unless a different directory is shown, commands ran from the repository root.

### Evidence gate and reports

- `& .venv\Scripts\villani.exe eval init .test-temp\pt4-prechange-evidence-20260717 --title "PT4 pre-change founder evidence audit"`
  - first attempt: exit 2, `[WinError 5] Access is denied: C:\Users\Simon\.villani`;
    no repository file changed
  - rerun with workspace-local `VILLANI_HOME`: exit 0; empty real-founder draft created
- `& .venv\Scripts\villani.exe eval validate .test-temp\pt4-prechange-evidence-20260717 --json`
  - exit 1; `valid: false`, `task_count: 0`, issue `no_tasks`
- `& .venv\Scripts\villani.exe eval freeze .test-temp\pt4-prechange-evidence-20260717 --disclosure-complete`
  - exit 2; freeze rejected because the suite contains no tasks
- `& .venv\Scripts\villani.exe eval report .test-temp\pt4-prechange-evidence-20260717 --json-output .test-temp\pt4-empty-real-report\evaluation-report.json --markdown-output .test-temp\pt4-empty-real-report\evaluation-report.md --html-output .test-temp\pt4-empty-real-report\evaluation-report.html`
  - exit 2; report rejected because the real suite is not frozen
- `& .venv\Scripts\villani.exe eval validate .test-temp\pt4-prechange-synthetic-20260717 --json`
  - exit 0; `valid: true`, `task_count: 1`, no issues, no passive monitoring, no external harness
- `& .venv\Scripts\villani.exe eval gate .test-temp\pt4-prechange-synthetic-20260717 --json`
  - pre-change and final runs both returned Gate B `INSUFFICIENT_EVIDENCE`; direct process exit 2
  - checks: 0/30 paired tasks, 0/2 repositories, valid synthetic baseline, insufficient reviewed
    false-acceptance population, unknown accepted rates/review delta/cost delta/configuration rate,
    complete synthetic disclosure, and synthetic evidence excluded
- `& .venv\Scripts\villani.exe eval report .test-temp\pt4-prechange-synthetic-20260717 --json-output .test-temp\pt4-postchange-report\evaluation-report.json --markdown-output .test-temp\pt4-postchange-report\evaluation-report.md --html-output .test-temp\pt4-postchange-report\evaluation-report.html`
  - exit 0; JSON, Markdown, and HTML written; Gate B `INSUFFICIENT_EVIDENCE`

### PT4 and Villani Ops

- `& .venv\Scripts\python.exe -m py_compile components\villani-ops\villani_ops\evaluation_lab\hardening.py components\villani-ops\villani_ops\evaluation_lab\reporting.py`
  - exit 0
- `& .venv\Scripts\python.exe -m ruff check components\villani-ops\villani_ops\evaluation_lab\hardening.py components\villani-ops\villani_ops\evaluation_lab\reporting.py`
  - exit 0; all checks passed
- first combined PT3/PT4 focused run:
  `& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\test_founder_hardening.py villani_ops\tests\test_founder_thesis_lab.py --basetemp .test-temp\pt4-focused`
  from `components/villani-ops`
  - exit 1; 22 passed, 1 failed in 14.14s
  - the new test used an invalid empty frozen synthetic fixture, which made the existing Gate return
    `FAIL`; the fixture was replaced with a valid frozen synthetic suite and production behavior was
    not weakened
- initial focused mypy:
  `& .venv\Scripts\python.exe -m mypy --follow-imports=skip --ignore-missing-imports components\villani-ops\villani_ops\evaluation_lab\hardening.py components\villani-ops\villani_ops\evaluation_lab\reporting.py`
  - exit 1; one `Counter` index annotation error; annotation corrected
- final Ruff commands:
  `& .venv\Scripts\python.exe -m ruff format --check components\villani-ops\villani_ops\evaluation_lab\hardening.py components\villani-ops\villani_ops\evaluation_lab\reporting.py components\villani-ops\villani_ops\tests\test_founder_hardening.py`
  and the corresponding `ruff check`
  - exit 0; 3 files formatted; all checks passed
- final focused mypy command shown above
  - exit 0; no issues in 2 source files
- `& .venv\Scripts\python.exe -m pytest -q components\villani-ops\villani_ops\tests\test_founder_hardening.py --basetemp .test-temp\pt4-focused-final`
  - exit 0; 9 passed in 1.36s
- `& .venv\Scripts\python.exe -m pytest -q components\villani-ops\villani_ops\tests\test_founder_hardening.py components\villani-ops\villani_ops\tests\test_founder_thesis_lab.py --basetemp .test-temp\pt4-combined-final`
  - exit 0; 24 passed in 14.43s
- first full Ops invocation with a 1-second command timeout
  - harness exit 124 after 2.017s; no test result; immediately rerun without the accidental timeout
- `& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp .test-temp\pt4-full-final`
  from `components/villani-ops`
  - exit 0; 1,188 passed, 2 skipped, 114 deselected in 240.82s
- `& .venv\Scripts\python.exe -m pytest -q components\villani-ops\villani_ops\tests\test_cli_orchestrator_default.py components\villani-ops\villani_ops\tests\test_hardened_execution_environment.py components\villani-ops\villani_ops\tests\test_verifier_tool_loop.py -rs --basetemp .test-temp\pt4-skip-reasons`
  - exit 0; 39 passed, 2 skipped in 12.78s
  - skip reasons: host Python lacks Unix-domain sockets; host Python lacks FIFO creation

### Required cross-component checks

- `& .venv\Scripts\python.exe -m pytest tests\closed_loop -q --basetemp .test-temp\pt4-closed-loop`
  - exit 0; 11 passed, 2 warnings in 39.90s
  - warnings: Starlette `httpx` deprecation and denied root pytest-cache creation
- `& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp .test-temp\pt4-full`
  from `components/villani-code`
  - exit 1; 74 failed, 612 passed, 1 skipped, 101 warnings in 73.28s
  - this exactly reproduces the documented untouched baseline; PT4 changed no Villani Code file
- `npm test` from `components/villani-flight-recorder`
  - exit 0; 21 test files and 111 tests passed
- `npm run typecheck` from `components/villani-flight-recorder`
  - exit 0
- `npm run build` from `components/villani-flight-recorder`
  - exit 0
- `npm run format:check` from `components/villani-flight-recorder`
  - exit 0; all matched files use Prettier style

### Evidence integrity and security checks

- production identifier scan via
  `scan_production_for_evidence_identifiers` over `evaluation_lab`, `closed_loop`, and `cli`
  - exit 0; 2 identifiers, 3 roots, 0 violations
- JSON parse and `canonical_digest` verification over the sufficiency report, hardening analysis,
  and regression fixture
  - exit 0; 3 JSON documents valid; claimed, recalculated, and linked analysis digests all equal
    `4f5f4c70fa194831f6f249bc905f73413889c96f3f4f528a01701a8c3bb5fcce`
- fresh final-code hardening generation compared with the durable report after removing only
  generation timestamp and self-digest
  - exit 0; semantically equal; 0 paired tasks, 0 ranked fixes, no certificate, PT5 not started
- `& .venv\Scripts\python.exe scripts\check-secrets.py components\villani-ops\villani_ops\evaluation_lab\hardening.py components\villani-ops\villani_ops\evaluation_lab\reporting.py components\villani-ops\villani_ops\tests\test_founder_hardening.py integration\fixtures\evaluation\pt4 docs\product docs\FOUNDER_THESIS_LAB.md`
  - exit 0; 6 roots, 0 findings
- final expanded secret scan adding both completion reports and `PLANS.md`
  - exit 0; 9 roots, 0 findings
- completion-manifest parse and listed-file existence check
  - exit 0; 4 JSON documents valid, no listed file missing, status
    `INSUFFICIENT_EVIDENCE`, no certificate, and PT5 unauthorized/not started
- `git diff --check`
  - exit 0; no whitespace error; Git emitted line-ending normalization warnings across the existing
    dirty working tree
- UTF-8 text scan over the PT4 Markdown reports and `PLANS.md`
  - exit 0; 0 mojibake findings

## End-to-end artifacts and screenshots

Artifacts:

- `.test-temp/pt4-prechange-evidence-20260717/`: empty real-founder audit workspace retained as
  evidence of `no_tasks`
- `.test-temp/pt4-prechange-synthetic-20260717/`: frozen ineligible Gate execution suite
- `.test-temp/pt4-postchange-report/evaluation-report.{json,md,html}`: installed CLI report outputs
- `.test-temp/pt4-generated-final/PT4_HARDENING_ANALYSIS.{json,md}`: fresh final-code comparison
- `docs/product/PT4_HARDENING_ANALYSIS.{json,md}`: durable analysis
- `docs/product/PT4_FOUNDER_EVIDENCE_SUFFICIENCY.json`: machine-readable first-action decision

Screenshots: none. PT4 changed no UI and the insufficient-evidence branch does not authorize a new
visual surface; no screenshot was fabricated.

Founder-proof certificate: not generated because Gate B did not pass.

## Known failures, skipped tests, and reasons

- Villani Code: 74 existing failures, 612 passes, 1 skip. The leading mechanism is mocked
  `subprocess.run(...).stdout` being `None` in `context_projection.py`; Windows subprocess reader
  decoding also emits warnings. This component was not edited, and the result matches the PT3
  documented baseline.
- Villani Ops: 2 host-capability skips, for unavailable Unix-domain sockets and FIFO creation.
- Villani Ops: 114 slow/integration/e2e tests are intentionally deselected by the component's
  default `-m 'not slow and not integration and not e2e'`; root closed-loop E2E coverage passed.
- The initial default-home command failed because the sandbox denied `C:\Users\Simon\.villani`;
  all evaluation work then used workspace-local `VILLANI_HOME`.
- The first new focused test and initial mypy run failed for test-fixture/type-annotation defects;
  both were corrected and their final superseding runs pass.

## Security, privacy, data-loss, and compatibility risks

- Security: no execution, route, verifier, acceptance, selection, or delivery authority changed.
  Certificate generation rejects known false acceptance. The scoped secret scan reports 0 findings.
- Privacy: task/repository identities are opaque in clusters and certificates. Trial artifact links
  remain local relative paths. Evaluation bundles can still contain actual confidential source code
  and are not encrypted; operators must control exports.
- Data loss: analysis is read-only. No target repository was mutated, no run bundle was rewritten,
  and no migration or deletion occurred.
- Compatibility: existing bundles and configuration remain readable. Consumers that previously
  misinterpreted missing duration as zero will now see truthful null/unknown accounting; this is the
  intended correction and the only observable compatibility change.
- Isolation: PT4 did not alter the existing process-isolation model. The synthetic Gate fixture was
  created in a dedicated temporary Git repository.

## User-facing behavior before and after

Before:

- an accepted-as-is evaluation trial with unknown duration could contribute numeric `0` and a
  falsely complete elapsed-time metric;
- no PT4 failure-taxonomy/sufficiency artifact existed;
- exact PT4 pairing and certificate-redaction behavior had no dedicated regression contract.

After:

- unknown duration remains unknown and cannot fabricate a speed claim;
- PT4 reports exactly why performance hardening is unauthorized and which experiments are next;
- synthetic evidence is excluded, mismatched trial identities cannot count, unsupported clusters
  are not ranked, verifier statistics remain undefined without labels, and no certificate is issued.

No end-user task execution, acceptance, routing, retry, selection, or delivery behavior changed.

## Acceptance disposition

- Every production change is linked to repeated real-task evidence or an undeniable defect:
  **PASS for the actual change set**; the only behavior correction is the identifier-free accounting
  defect.
- Before/after founder-task metrics exist: **NOT SATISFIED**; there are 0 eligible frozen pairs.
- No false acceptance introduced: **PASS / not applicable**; verifier behavior did not change.
- Gate B truthfully returns PASS, FAIL, or INSUFFICIENT_EVIDENCE: **PASS**; current result is
  `INSUFFICIENT_EVIDENCE`.
- The next milestone cannot start unless Gate B passes: **PASS**; PT5 authorization is false.

Because a required acceptance criterion lacks evidence, the milestone status is
`INSUFFICIENT_EVIDENCE`, not `COMPLETE`.

**Explicit confirmation: PT5 was not started.**
