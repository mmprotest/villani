# PT8 completion report

Status: **INSUFFICIENT_EVIDENCE**  
Implementation status: complete  
Acceptance status: not proven  
Assessment date: 2026-07-18

PT8's accepted-change economics implementation is complete and its deterministic validation passes. The milestone is not reported `COMPLETE`: the frozen founder replay has zero eligible cases, the four policy scorecards therefore have unknown economics, safe publication is refused, and Gate C remains `INSUFFICIENT_EVIDENCE`.

The machine-readable command log and exact file inventory are in [`PT8_COMPLETION_REPORT.json`](PT8_COMPLETION_REPORT.json). Durable evaluation evidence is in [`PT8_POLICY_EVALUATION.json`](PT8_POLICY_EVALUATION.json), [`PT8_FROZEN_POLICY_REPLAY_INPUT.json`](PT8_FROZEN_POLICY_REPLAY_INPUT.json), and [`PT8_GATE_C.json`](PT8_GATE_C.json).

## Outcome

Villani now creates a deterministic, versioned route plan after classification and before execution. It evaluates eligible systems using:

`(execution + verification + expected review + retry/escalation + latency penalty) / conservative probability of acceptance without correction`

Unknown inputs stay unknown. A route with partial accounting is labeled partial and is not silently compared as if missing cost, review, time, or probability were zero.

Automatic routing considers qualified systems only. When evidence is sparse, it falls back to the strongest eligible qualified evidence, or one explicitly labeled provisional route when no system is qualified. Experimental systems are never automatic. Privacy, provider, permission, local-only, Local first, exclusion, maximum-known-cost, forced-system, and strongest-only constraints are recorded in the plan.

## Architecture and product decisions

- Classification still precedes routing. Inputs are limited to pre-run task assessment, repository profile, exact qualified-system profiles, conservative probability, component economics, availability, budgets, privacy, permissions, provider, environment, qualification, and drift. Task names, benchmark IDs, future outcomes, hidden patches, language rules, and operating-system quality heuristics are not route inputs.
- Complete, same-currency objectives use conservative accepted-change economics. Sparse or materially unmatched inputs use strongest evidence and emit every unknown/rejection instead of fabricating a total.
- Route plans are content-addressed and persist systems considered, qualification, probability, costs, duration, unknowns, rejection reasons, first route, ordered fallbacks, reserves, explanation, and policy/input digests.
- Sequential retry remains bounded by evidence: retry the same system only with credible progress and an actionable correction; escalate on capability failure or no credible progress; preserve verification and final-validation reserves. PT8 adds no arbitrary retry loop.
- Finalization can project an eligible canonical outcome into append-only qualification and economics ledgers only when the new configuration explicitly enables it. Updates affect future routes only. Infrastructure exclusions and unverified outcomes do not train economics; forced choices are excluded from automatic-policy metrics; false acceptance appends an immediate qualification invalidation.
- Frozen policy replay is point-in-time. It compares strongest-only, cheapest-qualified, accepted-change optimizer, and forced strategies without using future evidence. Publication is immutable and fail closed unless conservative reliability does not decrease and false-acceptance exposure does not increase. Rollback changes only the active pointer. No LLM can publish controller policy.
- Semantic verification remains blind to harness identity, route, cost, qualification, and competing candidates.

## Contracts and migration

Seven normative and packaged v1 contracts were added:

- `villani.economics_observation.v1`
- `villani.economics_snapshot.v1`
- `villani.online_evidence_update.v1`
- `villani.route_plan.v1`
- `villani.route_policy.v1`
- `villani.route_policy_evaluation.v1`
- `villani.route_policy_publication.v1`

Matching strict Python and TypeScript models, generated fixtures, Flight Recorder validation, and a semantic invalid fixture for fabricated zero cost were added. Run manifests gained optional `route_plans` and `economics_update` pointers. New initializations receive `villani.accepted_change_economics_configuration.v1`; existing configuration remains readable and does not silently gain online-update side effects. Legacy bundles without PT8 fields remain readable.

## Files

Added:

- `components/villani-ops/villani_ops/closed_loop/economics/{__init__,evaluation,models,online,planner,publication,runtime_update,store}.py`
- Seven PT8 schemas under both `schemas/v1/` and `components/villani-ops/villani_ops/schemas/v1/`
- `components/villani-ops/villani_ops/tests/test_pt8_accepted_change_economics.py`
- `components/villani-run-model/src/economics.ts`, generated `dist/economics.{d.ts,js}`, and `test/economics.test.ts`
- Seven valid PT8 protocol documents and `integration/fixtures/protocol/v1/invalid/economics_unknown_cost_as_zero.json`
- `scripts/generate-pt8-fixtures.py`
- `docs/ACCEPTED_CHANGE_ROUTING.md`, three durable evidence inputs/results, and both completion reports
- Web/Agentd production asset `index-CfriGpez.js`

Changed:

- `PLANS.md`
- Ops controller, CLI, policy/presets/preview, inventory, protocol/run manifest, schema validation, qualification source kind, docs, and controller/protocol/CLI tests
- Run Model index and qualification source-kind model
- Flight Recorder protocol types, schema validation, generated output, and fixture tests
- Agents Web page, API types, tests, production HTML, and synchronized Agentd console assets
- Root run-manifest schema, schema generator, protocol contract test, and `docs/AGENT_SYSTEMS.md`

Deleted/replaced:

- The prior Web JavaScript bundles were replaced by the verified `index-CfriGpez.js` production artifact. The base worktree reports `index-Db_ajSzJ.js` deleted; the uncommitted PT7 `index-BDVFjkQh.js` was also superseded.

The exact path-by-path lists are in the machine report. The worktree also contains the immediately preceding, user-authorized PT6 and PT7 changes; their reports remain the source of truth for those milestones.

## Tests added

Sixteen PT8 Python tests cover full/partial accounting, conservative probability, determinism, sparse fallback, no experimental automatic selection, qualification boundaries, privacy/provider/cost/permission/local controls, forced metrics, reserves, point-in-time replay, publication/rollback, false-acceptance quarantine, online future-only learning, route explanation, no task-name leakage, migration, and CLI lifecycle. Two controller tests cover route-plan and economics-update artifacts. Three Run Model tests cover versions, partial objectives, forced-metric exclusion, and the online-update contract. Existing Web and protocol tests now cover repository economics and unknown handling.

## Validation

Final applicable results:

- Villani Ops: `1260 passed, 2 skipped, 116 deselected in 354.57s (0:05:54)` on the final post-format run.
- PT8 mandatory tests: `16 passed in 1.30s`; controller artifact/update tests: `2 passed in 1.66s`; protocol: `19 passed in 1.37s`.
- Villani Code: `686 passed, 1 skipped, 27 warnings in 135.38s`.
- Agentd: `86 passed in 18.52s`.
- Root closed loop: `11 passed, 2 warnings in 61.73s`.
- Run Model: 5 files/15 tests passed in 868 ms; typecheck and build passed.
- Flight Recorder: 21 files/114 tests passed in 6.87 s; typecheck, build, and format passed.
- Web: 4 files/25 tests passed in 2.26 s; typecheck, 59-module production build, and format passed. The test/build commands required approved execution outside the filesystem sandbox because esbuild could not read an ancestor directory inside it.
- Ruff lint passed; Ruff reports 16 PT8-touched files formatted. Isolated mypy reports no issues in 8 economics modules. Python 3.11 compilation passed.
- Seven schemas are idempotent and byte-identical to packaged copies; eight fixtures are idempotent; 16-root secret scan found 0 findings; packaged console parity verified 3 files.
- Gate C and policy-evaluation artifacts pass normative schema and strict Python-model validation.
- The opt-in real-harness gate skipped exactly 2 tests because `VILLANI_REAL_HARNESS_TESTS is not set to 1`; 29 tests were deselected and no paid run occurred.

The machine report records each validation command, including remediated preliminary failures: three initial Ops regressions, wrong component-relative focused paths, an invalid TEMP path, Web sandbox access, controller JSON serialization, protocol accounting semantics, formatting/lint/type findings, and safe policy-publication refusal.

## End-to-end evidence and screenshots

End-to-end artifacts include the frozen replay input, four-strategy policy evaluation, Gate C, valid route-plan and online-update fixtures, built Agents UI, and synchronized Agentd asset manifest.

No screenshot was captured. The in-app browser inventory was empty (`[]`), and its required workflow prohibits substituting an unrelated browser. UI evidence is the 25 passing Web tests, typecheck, 59-module production build, format check, and console-asset parity. The verified Vite preview process was terminated, port 4173 had no remaining listener, and all 6 disposable PT8 validation paths were verified absent after cleanup.

## Known failures, skips, and risks

- Gate C remains `INSUFFICIENT_EVIDENCE`. The durable Villani Code, Codex, and Claude Code scorecards have zero eligible founder observations, and the isolated PT8 Gate run has no configured scorecards.
- The frozen PT8 input contains zero cases. Cost, elapsed time, review, escalation, regret, reliability, and false-acceptance comparisons remain unknown; policy publication correctly refuses activation.
- Two existing Ops host-capability cases skip on native Windows; Villani Code has one opt-in external smoke skip. Two real-harness cases skipped because the explicit opt-in was absent.
- Route/run artifacts can contain repository, system, provider, cost, and review metadata and are not encrypted. Redaction is bounded. Worktree/process isolation is not a kernel sandbox.
- Evidence stores are append-only and delivery remains limited to the selected recorded patch. No user source, durable evidence, vendor configuration, or session directory was deleted.
- Existing bundles and configuration remain readable. Exact identity, environment, policy, lineage, capability, currency, or reliability drift can intentionally make old economics ineligible and produce a safer fallback or no route.

## User-visible behavior

Before PT8, qualification could gate systems but routing did not expose or persist a total accepted-change objective. After PT8, Villani chooses the route most likely to produce a proven change at the lowest conservatively known total cost, or explains its evidence fallback when inputs are sparse. Agents shows repository-specific conservative probability, known component economics, sample scope, and honest `Unknown` values. Advanced controls stay hidden by default. `policy explain` is read-only, and economics evaluation, publication, status, and rollback are deterministic and versioned.

## Scope boundary

PT9 was not started.
