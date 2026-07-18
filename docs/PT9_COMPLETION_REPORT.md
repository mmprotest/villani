# PT9 completion report

Status: **INSUFFICIENT_EVIDENCE**  
Implementation status: complete  
Acceptance status: not proven  
Assessment date: 2026-07-18

PT9’s adaptive-verification implementation is complete and the deterministic validation surface passes. The milestone is not reported COMPLETE: the frozen founder evaluation contains zero eligible matched cases, so lower supervision, zero observed false acceptance, and Gate D PASS cannot be established without fabricating evidence.

The exact path inventory and command log are in [PT9_COMPLETION_REPORT.json](PT9_COMPLETION_REPORT.json). Durable Gate D evidence is in [PT9_FROZEN_GATE_D_INPUT.json](PT9_FROZEN_GATE_D_INPUT.json) and [PT9_GATE_D.json](PT9_GATE_D.json).

## Outcome

Villani now builds a deterministic, versioned verification plan for each candidate. The plan uses task and criteria, explicit risk, generic repository policy, candidate diff scope, qualification uncertainty, and historical verification failure modes. It selects among repository validation, changed-test execution, static checks, diff integrity, generated-artifact exclusion, requirement mapping, focused probes, semantic verification, independent verification for critical risk, and exact-decision manual review.

Conclusive deterministic failure rejects before a redundant semantic call. Acceptance still always requires semantic verification. Critical changes also require an independent verifier. Unclear, malformed, error, missing, disagreeing, or incomplete evidence always normalizes to decision 0.

Ordinary proved changes receive a compact Ready to apply package with task, summary, files, requirements, checks, risk, known cost/time, why Villani trusts the result, and a full-evidence link. Unproved changes receive Needs review and the exact unresolved decision.

## Architecture and product decisions

- Risk tiers are standard, elevated, and critical. Signals are generic: explicit risk, security/destructive/migration implications, diff scope, configured sensitive paths, qualification uncertainty, and historical disagreement or false cases. No framework, language, operating-system, benchmark, repository, or task-name route rule was added.
- Repository validation comes from repository-discovered/configured argv commands. Changed-test and static-check nodes require explicit generic verification-kind tags.
- Focused probes execute in the candidate environment. Temporary inputs cannot escape or overwrite the candidate, are removed in a finally block, and persist path/hash/size/cleanup evidence without content in the probe report. Behavior failure and infrastructure failure remain separate; only infrastructure failure is retryable.
- The semantic-verifier context is allowlisted. Harness identity, route, cost, controller qualification identity, and competing candidates are excluded. Restricted provenance remains outside semantic context as an identity digest and artifact pointer.
- Binary authority records decision 1 or 0, reason code, requirements proved and not proved, blockers, infrastructure state, plan-node results, restricted provenance, and verifier cost. Semantic unclear/error/not-invoked states cannot represent acceptance.
- Human feedback is explicit, optional, append-only, and local-first. Accepted as-is, corrected before use, reverted, reopened defect, false acceptance, and false rejection are supported. Later commits or issues are linked only through explicit import; no repository or employee monitoring exists.
- Missing cost, duration, review minutes, or full-trace use remains null with explicit accounting. It is never silently treated as zero.
- Gate D compares matched strongest-only, accepted-change optimizer, and optimizer-plus-adaptive arms. Empty or unmatched evidence cannot pass.

## Contracts and migration

Six normative and packaged v1 contracts were added:

- villani.adaptive_verification_plan.v1
- villani.binary_verification_decision.v1
- villani.review_package.v1
- villani.human_outcome.v1
- villani.supervision_metrics.v1
- villani.gate_d.v1

Matching strict Python and TypeScript models, valid/invalid fixtures, Flight Recorder validation, and UI projections were added. Run manifests gained optional adaptive-verification, human-outcome, and supervision-metric pointers. Product runs gained an optional proof package. Existing configuration and run bundles remain readable; absent PT9 data remains unavailable rather than synthesized.

New configuration receives the additive adaptive_verification_v1 defaults. Existing configuration without the block uses conservative runtime defaults and requires no destructive migration.

## Files

Added:

- The eight-file adaptive_verification package under components/villani-ops/villani_ops/closed_loop
- Six schemas under both schemas/v1 and the packaged Ops schema directory
- PT9 Python and Run Model tests plus src/adaptiveVerification.ts and generated JavaScript/declarations
- Six valid top-level PT9 fixtures, three attempt-local proof fixtures, and four invalid semantic fixtures
- scripts/generate-pt9-schemas.py and scripts/generate-pt9-fixtures.py
- docs/ADAPTIVE_VERIFICATION.md, Gate D input/result, and both completion reports
- The verified Web/Agentd production asset index-DvFaI5_1.js

Changed:

- Ops verifier, controller, focused probes, verification evidence/graph/routing, product projection, protocol/schema maps, CLI/default configuration, README, and related tests
- Run Model product/index types
- Flight Recorder protocol types, semantic validation, generated output, and tests
- Single-task Web proof presentation, tests, production HTML, and synchronized Agentd asset manifest
- Product-run/run-manifest schemas and the shared valid-run manifest
- Root protocol and installed-CLI E2E tests
- PLANS.md progress only

Replaced:

- The prior PT8 Web and Agentd index-CfriGpez.js bundles were replaced by the verified PT9 bundle. No source or durable evidence was deleted.

The machine report contains the exact path-by-path lists. The worktree also contains the immediately preceding user-authorized PT6, PT7, and PT8 work; their reports remain authoritative for those milestones.

## Tests added

Thirteen PT9 Python tests cover tier selection, configured sensitive paths, deterministic planning, verifier blindness, probe isolation/cleanup, behavior versus infrastructure failure, binary normalization, critical independent verification, compact review packages, explicit local feedback, unknown review time, false-acceptance quarantine, Gate D states, migration, and CLI behavior.

Two Run Model tests cover all six protocol identities, binary authority, and unknown review-time parity. Existing verifier-routing, controller, delivery, protocol, Web, Flight Recorder, and installed-CLI tests were expanded for redundant-call avoidance, cascade escalation, no unclear/error acceptance, proof presentation, invalid fixtures, and the structured semantic-verifier lifecycle.

## Validation

Final applicable results:

- Villani Ops: 1278 passed, 2 skipped, 116 deselected in 381.27 seconds.
- PT9 plus verifier routing: 23 passed in 1.00 second after final formatting.
- Villani Code: 686 passed, 1 skipped, 27 warnings.
- Agentd: 86 passed.
- Root closed loop: 11 passed with 2 non-failing warnings in 62.67 seconds.
- Structured installed-CLI E2E: 3 passed with 1 pytest-cache warning in 43.75 seconds.
- Root protocol: 2 passed with 1 pytest-cache warning.
- Run Model: 6 files/17 tests passed; typecheck and build passed.
- Flight Recorder: 21 files/118 tests passed; typecheck, build, and format passed.
- Web: 4 files/25 tests passed; typecheck, production build, and format passed. The production build required approved execution outside the filesystem sandbox because esbuild could not read an ancestor directory inside it.
- Ruff lint passed; all 12 scoped files are formatted. Python 3.11 compilation passed. Isolated mypy found no issues in 8 PT9 production modules.
- Twenty-seven generated outputs were deterministic across two runs; all 6 normative/package schema pairs were byte-identical.
- The 13-root credential scan found 0 findings. Source audits found 0 passive-monitor implementations and 0 forbidden semantic-plan identity fields.
- Packaged console parity verified all 3 files. Final git diff checking passed with only line-ending conversion warnings.

Preliminary failures were fixed and are preserved in the machine report: six initial Ops regressions, three installed-CLI E2E failures from deterministic-only verification, Web and Ruff formatting findings, three fixture-generator import warnings, and a naive mypy traversal that surfaced the existing non-PT9 repository baseline. The final scoped and complete gates above pass.

## Gate D

The live command returned exit code 2 and [PT9_GATE_D.json](PT9_GATE_D.json) records INSUFFICIENT_EVIDENCE with next_milestone_permitted=false.

Explainability and safe fallback pass. The other five checks are insufficient because all three frozen arms contain zero eligible cases, known cost/duration is unavailable, and explicit review minutes are unknown. The empty evaluation cannot prove no accepted-as-is regression, zero false acceptance, lower cost/time, or lower review burden.

## End-to-end evidence and screenshots

End-to-end evidence includes the frozen Gate D input/result, valid plan/decision/review/outcome/metrics fixtures, attempt-local proof artifacts, the built Single Task UI, synchronized Agentd assets, and a direct product-run projection that emitted Ready to apply with a Compact proof evidence link.

No screenshot was captured. The required in-app browser returned “Browser is not available: iab,” and the browser workflow prohibits substituting another browser. UI evidence is the passing Web suite, typecheck, production build, format check, direct product projection, and packaged-asset parity.

## Known failures, skips, and risks

- Gate D remains INSUFFICIENT_EVIDENCE. No eligible matched founder trial exists for the three required policy arms.
- Two existing Ops host-capability tests skip on native Windows because Unix-domain socket/FIFO facilities are unavailable. One Villani Code opt-in external smoke test is skipped. No paid external run occurred.
- Task, diff, command, review, and evidence artifacts can contain repository data and are not encrypted. Redaction is bounded; exported artifacts require operator review.
- Worktree/process isolation is not a kernel sandbox. Commands are shell-free and workspace scoped, but hostile repositories require a stronger configured execution provider.
- Redacted raw verifier artifacts can contain repository context. Probe reports omit temporary-file content and retain only path/hash/size/cleanup evidence.
- Explicit adverse feedback can quarantine an exact qualification profile. History is append-only, so incorrect feedback is correctable only through later explicit evidence, not deletion.
- Old bundles lack compact proof and supervision fields; consumers show them as unavailable. No user source, durable evidence, vendor configuration, or session directory was deleted.

## User-visible behavior

Before PT9, Villani exposed verifier evidence and a candidate verdict but no versioned adaptive proof plan, compact Ready/Needs review package, explicit later-outcome import, supervision accounting, or Gate D.

After PT9, Villani uses the minimum deterministic proof graph warranted by risk and repository policy while retaining mandatory semantic proof. Single Task shows WHY VILLANI TRUSTS IT, risk, checks, and the full evidence link, or the exact unresolved decision. The CLI adds verification plan, feedback-import, feedback, metrics, and gate-d commands.

## Scope boundary

PT10 was not started.
