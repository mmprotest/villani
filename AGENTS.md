# Villani closed-loop product

## Product objective

Build one local-first coding-agent control loop:

task submission
-> task classification
-> execution-policy selection
-> isolated coding attempt
-> evidence collection
-> candidate verification
-> stop, retry, or escalate
-> candidate selection
-> patch materialization
-> replay and audit

The public product is Villani. Villani Code, Villani Ops, and Villani Flight Recorder are internal components.

## Architectural invariants

- There is one canonical run identity across controller, worker, verifier, selector, storage, and console.
- Task classification must complete before the controller selects the coding backend.
- The controller owns retry, escalation, stopping, selection, and finalization.
- The controller must be a deterministic state machine.
- LLMs may classify, investigate, review, and verify, but may not directly mutate controller state.
- Only verifier outcomes with acceptance-grade evidence may authorize acceptance.
- Never automatically apply an accepted_unverified or best-effort candidate.
- Reuse the Villani Ops verifier-parallel candidate, verifier, selection, and materialization primitives.
- Do not use the adaptive agentic orchestrator as the primary path.
- Every state transition must be persisted and emitted as a canonical event.
- Every attempt must run in an isolated worktree.
- Every completed run must remain inspectable after process termination.
- All generated patches must exclude Villani internal state and artifacts.

## Scope exclusions for closed-loop-v1

Do not implement:

- task decomposition
- multi-agent decomposition
- scheduling
- team or SaaS features
- additional coding-agent runners
- benchmark-specific behaviour
- task-name-specific logic
- language-specific routing logic
- operating-system-specific routing logic
- a learned router
- natural-language interrogation of runs
- additional UI pages outside run search, run detail, candidate evidence, and replay

## Compatibility

- Preserve existing component commands unless the milestone explicitly replaces them.
- Avoid broad rewrites.
- Prefer extracting and adapting existing working functions.
- Do not change tests merely to hide regressions.
- Do not add production dependencies unless necessary and documented.
- Python code must support Python 3.11.
- Runtime behaviour must remain cross-platform.

## Required validation

Villani Code:

    python -m pytest -q

Villani Ops:

    python -m pytest -q

Flight Recorder:

    npm test
    npm run typecheck
    npm run build
    npm run format:check

Closed-loop integration:

    python -m pytest tests/closed_loop -q

## Completion reporting

At the end of every milestone, report:

- files changed
- architectural decisions made
- tests run
- exact test results
- remaining failures
- assumptions
- known risks
- confirmation that the next milestone was not started

## Durable repository rules

1. Read root `PLANS.md` before editing.
2. Implement only the milestone named by the current user prompt.
3. Do not begin a later milestone.
4. Preserve unrelated user changes.
5. `components/villani-code` owns coding execution.
6. `components/villani-ops` owns the deterministic closed-loop controller and public CLI.
7. `components/villani-flight-recorder` is a read-only observability consumer.
8. Root `schemas` is the normative cross-component wire contract.
9. Classification must happen before coding backend selection.
10. Verifier errors, unclear verdicts, missing evidence, and malformed output are never acceptance eligible.
11. Selection receives only acceptance-eligible candidates.
12. Unknown cost is `null` plus an accounting status, never numeric zero.
13. Only the selected recorded patch may be materialized.
14. Never write API keys or secrets to logs, fixtures, run bundles, or reports.
15. Run the component-specific verification commands for every edited component.
16. Update only the progress section of `PLANS.md` at the end of a pass.
