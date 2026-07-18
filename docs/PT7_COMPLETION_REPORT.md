# PT7 completion report

Status: **INSUFFICIENT_EVIDENCE**  
Implementation status: complete  
Acceptance status: not proven  
Assessment date: 2026-07-18

PT7's repository-specific qualification implementation is complete and its deterministic suites pass. The milestone is not reported `COMPLETE`, because the live Gate C artifact is `INSUFFICIENT_EVIDENCE`: Villani Code, Codex, and Claude Code each have zero eligible frozen founder-suite observations for this repository. Gate C correctly exits 2 and no system is automatically eligible.

The machine-readable report is [`PT7_COMPLETION_REPORT.json`](PT7_COMPLETION_REPORT.json), and the live scorecards are [`PT7_GATE_C.json`](PT7_GATE_C.json).

## Outcome

PT7 adds an append-only repository qualification ledger, conservative evidence evaluation, statistical scorecards, drift invalidation, routing eligibility, CLI commands, Agents UI presentation, normative schemas, cross-component models, fixtures, migration behavior, and Gate C.

The live repository-specific states are:

| System | Exact installed identity | State | Eligible sample | Automatic route | Reason |
| --- | --- | ---: | ---: | ---: | --- |
| Villani Code | `villani-code@0.1.0rc1`, provider `local`, model `default` | Experimental | 0 | No | Conformance passed, but no eligible repository evidence exists. |
| Codex | `codex@0.144.5`, provider `openai`, model `default` | Unsupported | 0 | No | Authentication is not ready and the route is not production-enabled. |
| Claude Code | `claude-code@2.1.138`, provider `anthropic`, model `default` | Unsupported | 0 | No | Strict isolation is unavailable on native Windows and the route is not production-enabled. |

No ranking is emitted because the samples are empty and materially unmatched.

## Architecture and product decisions

- Qualification history is append-only. Observations, exclusions, amendments, and invalidations remain auditable; derived snapshots are content-addressed.
- Repository identity and Git lineage are combined with task category, difficulty, risk, required capabilities, complete system identity, execution environment, verification-policy version, software versions, and time window.
- Backoff is exact repository/task profile → repository/category → repository-wide system → explicitly compatible repository cohort. Language and framework metadata never pool evidence.
- Eligible evidence must come from a frozen `real_founder_work` trial with a valid baseline, complete candidate artifacts, authoritative binary verification, resolved infrastructure, required human review, and no corruption, secret, or direct-target mutation. Exclusions are persisted by reason.
- Policy `repository_qualification_v1` uses the requested initial thresholds: zero eligible observations is experimental; 1–19 and zero false acceptance is provisional; qualified requires at least 20 approved observations, zero known false acceptance, Wilson lower bound above the task-risk threshold, valid exact conformance, and no severe drift.
- Cost is grouped by authoritative currency. Unknown cost, duration, and review time remain null with explicit accounting; zero is never fabricated.
- Harness, model, provider/serving engine, material environment, verification policy, lineage, capability, recency, reliability, or false-acceptance changes safely downgrade current state without deleting history.
- Automatic routing uses qualified systems only. If no qualified system exists, it may use one strongest evidence-backed configured provisional route with an explicit fallback marker. Experimental requires a visible same-run manual override; unsupported cannot be overridden.
- Qualification gates backend selection. Semantic verification remains blind to harness identity, route, cost, qualification, and competing candidates.
- Gate C requires all three named scorecards and checks evidence parity, exact identity, conformance, isolation, unsupported behavior, qualification correctness, and routing eligibility. Empty or partial arms cannot pass.

## Contracts and migration

Added normative and packaged v1 schemas:

- `villani.qualification_observation.v1`
- `villani.qualification_invalidation.v1`
- `villani.qualification_snapshot.v1`
- `villani.gate_c.v1`

Matching strict Python and TypeScript models, shared valid fixtures, and a semantic invalid fixture for fabricated zero cost were added. The default configuration gained an additive versioned qualification policy. Existing bundles/configuration remain readable. Legacy non-repository capability snapshots are retained only as excluded history and cannot silently qualify a route. Unknown policy fields fail closed.

## Files

Added:

- `components/villani-ops/villani_ops/closed_loop/qualification/{__init__,evaluation,gate,models,policy,repository,scoring,store}.py`
- `schemas/v1/{gate-c,qualification-invalidation,qualification-observation,qualification-snapshot}.schema.json`
- Matching four packaged schemas under `components/villani-ops/villani_ops/schemas/v1/`
- `components/villani-ops/villani_ops/tests/test_pt7_repository_qualification.py`
- `components/villani-run-model/src/qualification.ts`, generated `dist/qualification.{d.ts,js}`, and `test/qualification.test.ts`
- Four valid PT7 protocol fixtures and `integration/fixtures/protocol/v1/invalid/qualification_unknown_cost_as_zero.json`
- `docs/REPOSITORY_QUALIFICATION.md`, `docs/PT7_GATE_C.json`, and both PT7 completion reports
- `components/villani-web/dist/assets/index-BDVFjkQh.js`
- Four synchronized packaged-console files under `components/villani-agentd/villani_agentd/console_assets/`

Changed:

- `PLANS.md`
- `components/villani-agentd/villani_agentd/console.py`
- Flight Recorder protocol/model validation and tests: `src/providers/villaniProtocol.ts`, `src/providers/villaniSchemaValidation.ts`, generated `dist/providers/villaniSchemaValidation.js`, and `test/villaniProtocol.test.ts`
- Ops CLI, docs, schema registry, qualification-aware inventory/registry/attempt routing, and protocol tests
- Run Model index exports and generated index outputs
- Web Agents page, console API, tests, built index, and production HTML
- `docs/AGENT_SYSTEMS.md`, `scripts/generate-harness-schemas.py`, and the two root closed-loop contract/integration tests

Deleted/replaced:

- `components/villani-web/dist/assets/index-Db_ajSzJ.js` was replaced by the verified PT7 production bundle.

The worktree also contains the immediately preceding user-authorized PT6 changes listed in `docs/PT6_COMPLETION_REPORT.json`; those changes were preserved and are not misreported as new PT7 work.

## Tests added

The nine Python PT7 tests cover state transitions, evidence inclusion/exclusion, Wilson math, hierarchical backoff, no language/framework pooling, false-acceptance downgrade, drift, repository lineage, append-only invalidation, manual override, automatic eligibility, Gate C, migration, CLI commands, and fail-closed policy parsing. Three Run Model tests cover contract versions, unknown handling, and explicit eligibility. Existing Web, Flight Recorder, schema, and root integration tests now cover the public presentation and protocol paths.

## Validation

Final results:

- Focused PT5/PT6/PT7/protocol: `69 passed, 2 deselected in 27.44s`.
- Villani Ops: `1241 passed, 2 skipped, 116 deselected in 327.14s`.
- Villani Code: `686 passed, 1 skipped, 27 warnings in 131.20s`.
- Agentd: `86 passed in 23.18s`.
- Root closed-loop integration: `11 passed, 2 warnings in 48.92s`.
- Run Model: 4 files/12 tests passed in 518 ms; typecheck and build passed.
- Flight Recorder: 21 files/113 tests passed in 6.91 s; typecheck, build, and format check passed.
- Web: 4 files/25 tests passed in 2.15 s; typecheck, 58-module build, and format check passed.
- Ruff passed; 14 files were already formatted. Focused mypy found no issues in 7 source files. Python 3.11 compilation passed.
- Secret scan: 18 roots, 0 findings. All four generated schemas were idempotent and byte-identical to packaged copies. Packaged console parity: 3 files verified.
- Post-type-fix PT7/protocol regression: `27 passed in 18.85s`.
- Real-harness opt-in gate: `2 skipped, 29 deselected in 0.62s`; exact reason: `VILLANI_REAL_HARNESS_TESTS is not set to 1.` No paid execution occurred.
- Gate C passed both the normative protocol-schema validator and strict Python model validation. The completion report parsed as JSON, `git diff --check` passed, and all eight verified PT7 cleanup targets were absent.

The exact command-by-command log, including all remediated preliminary failures, is in the machine-readable report. The notable preliminary failures were a circular import, missing semantic accounting keyword, stale zero-evidence quickstart expectations, formatting/lint/type findings, and Vite/esbuild sandbox access. Each was corrected or rerun under the approved environment, and the corresponding final suites pass.

## End-to-end artifacts and screenshots

End-to-end evidence includes the live Gate C JSON, all shared PT7 fixtures, the built Agents UI, and its synchronized Agentd console manifest. No screenshot was captured: the in-app browser reported no attached target (`targets=[]`), and its required workflow prohibits substituting an unrelated browser. UI evidence is therefore the 25 passing Web tests, typecheck, production build, format check, and packaged-asset parity.

## Known failures, skips, and risks

- Gate C remains `INSUFFICIENT_EVIDENCE`; this is the acceptance blocker, not a coding failure.
- No existing frozen founder suite contains eligible separate arms for Villani Code, Codex, and Claude Code. Codex also lacks authentication, and native Windows cannot prove Claude Code's strict isolation.
- Two existing Ops host-capability tests and one Villani Code opt-in external smoke test were skipped for documented environment reasons.
- Qualification imports reject secret-shaped ledger data, but patches and run artifacts can still contain repository data. Redaction is bounded and local artifacts are not encrypted.
- Worktree/process isolation is not a kernel sandbox. Environment mismatch and unsupported routes fail closed.
- Repository identity and lineage are local evidence metadata and may be sensitive when exported.
- Evidence is append-only and delivery still materializes only the selected recorded patch. No durable evidence or user source was deleted. Reproducible test/config temp directories were removed after exact-path verification.
- Older bundles and configuration remain readable, but material identity, environment, policy, lineage, capability, or reliability changes intentionally downgrade historical qualification.

## User-visible behavior

Before PT7, Agents could show installation, readiness, and coarse harness status, but not repository/task-specific reliability. After PT7, it shows exact system/model identity, repository state, observed acceptance, known median accepted-change cost, duration, sample count, last tested time, caveat, Doctor, and View evidence. Users have evidence-backed `qualify`, `status`, `evidence`, `invalidate`, and `gate-c` commands. Experimental manual runs are visibly labeled and cannot manufacture qualification.

## Scope boundary

PT8 was not started.
