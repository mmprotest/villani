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