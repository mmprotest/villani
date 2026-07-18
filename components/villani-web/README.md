# Villani Console

Villani Console consumes the shared monochrome, light design tokens and primitives from
`components/villani-ui`. Release packaging rebuilds the application and validates every local
`index.html` asset reference.

The same application supports local-only and connected workspaces. Agentd exposes
structured local history and replay endpoints backed by Flight Recorder's parsing
and indexing engine; the browser never reads arbitrary filesystem paths. Connected
run detail is projected through `@villani/run-model`. Explicit API fields are
authoritative and recorded events are a backward-compatible fallback. Activity, candidates,
costs, files, policy, and timelines therefore share run identity, aggregate accounting,
verification evidence, selection reasoning, and redaction state.

The shared `ProductShell` exposes exactly four default destinations: New task, Activity, Agents,
and Settings. New task is the root route; legacy Home, Run, and History links redirect without
breaking bookmarked run and replay pages. Models, Policies, Replay, Fleet, Tasks, Costs, Alerts,
and Audit remain available from Settings or direct links. Team is deliberately not shown before a
later enrolment milestone.

Agents renders complete `villani.agent_system.v1` identities: harness and exact version, model and
provider, protocol, execution and permission policy, qualification, tri-state capabilities,
billing knowledge, redaction status, and content-addressed system ID. Legacy model-only inventory
remains a display fallback for older services.

Healthy infrastructure has no permanent status strip. The shell shows a compact notice only when
setup, service, storage, synchronization, credentials, migration, or page recovery needs action.
The same shell and shared controls render the resumable Repository, Agent connection,
Verification, and Ready onboarding stages. Keyboard-visible focus, associated form errors,
reduced motion, and responsive layouts from 320px upward are part of the browser contract.
Unavailable cost and duration remain explicitly unknown rather than being synthesized.

New task has two always-visible fields—repository and multiline task—plus one optional Details
disclosure. Unsent text is restored locally without normalizing whitespace. A high-confidence
repository check is preselected; an uncertain check asks one plain-language confirmation, while
the absence of a repository check does not block execution or create proof. The primary action is
`Run safely`; it submits once with Performance/strongest-eligible routing, mandatory verification,
no default wall-time limit, and a non-destructive post-result delivery decision.

Running and result screens consume `villani.product_run.v1` from Agentd. Canonical events produce
exactly Understanding, Working, Checking, and Ready—never browser timers or fake percentages.
Final verdicts are exactly Ready to apply, Needs review, Could not prove, or Cancelled. Only Ready
to apply can expose Apply change, Create branch, or Open pull request. Refresh reconnects through
the persisted run identity; closing the browser does not cancel server-side work.

Development requires Node 20. Run `npm ci`, `npm test`, `npm run typecheck`, `npm run build`,
`npm run format:check`, and `npm run e2e`.
