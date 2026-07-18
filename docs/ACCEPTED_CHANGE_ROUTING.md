# Accepted-change economics routing

PT8 selects among repository-qualified agent systems using conservative total cost per accepted
change. Qualification remains the safety gate; economics only orders routes that the PT7 policy
allows. An automatic route can use `qualified` systems, or the strongest explicitly labeled
`provisional` fallback when none is qualified. It never auto-selects `experimental` or
`unsupported` systems.

## Objective and unknown inputs

The versioned `total_accepted_change_v1` objective is:

```text
(execution cost
 + verification cost
 + expected human-review cost
 + retry/escalation cost
 + configured latency penalty)
/ conservative probability of acceptance without correction
```

The probability is the applicable repository/task qualification profile's Wilson lower bound,
not its raw acceptance rate. The policy uses conservative cost and duration statistics and keeps
verification and final-validation reserves intact.

Every money or duration component carries an accounting status. A complete objective is comparable
only when all required components, one currency, and the conservative probability are known. A
partial objective reports its known subtotal and the names of missing inputs; it never treats a
missing review, verifier, retry, execution, or latency value as zero. When complete objectives
cannot be compared, routing falls back deterministically to the strongest qualified evidence.

## Route plan

Every decision persists `villani.route_plan.v1` under `route-plans/<decision-id>.json`. It records:

- the repository and generic task profile used before execution;
- exact systems considered, qualification state, probability, cost and duration evidence;
- unknowns and rejection reasons, including privacy, provider, permissions, availability, budget,
  drift, capability, and reserve failures;
- selected first system, ordered qualified fallbacks, and sequence economics;
- forced-choice and automatic-policy-metric eligibility;
- policy version/digest, evidence cutoff, deterministic input digest, and explanation.

No task name, benchmark ID, candidate patch, verifier result, future outcome, language heuristic, or
operating-system quality heuristic is an optimizer input. Semantic verification remains blind to
harness identity, route, cost, and competing candidates.

The default explanation is: **Villani chose the route most likely to produce a proven change at
the lowest total cost.** `villani policy explain <task> --repo <path>` is read-only and does not
start a coding attempt.

## Sequential behavior

After the initial route, the deterministic controller may retry the same system only when recorded
evidence shows credible progress and an actionable correction, and only while downstream reserves
remain valid. Capability failure or lack of credible progress escalates to the next stronger
qualified fallback. The run stops when acceptance is proved or no safe, economically rational
route remains; PT8 does not add a blind retry loop.

## Advanced constraints

The route picker remains hidden by default. Advanced CLI constraints are recorded in the route
plan:

```text
--local-only
--maximum-known-route-cost <USD>
--preferred-provider <provider>
--exclude-system <route-or-system-id>
--strongest-only
--agent-system <route-or-system-id>
```

A forced experimental route still requires explicit experimental acknowledgement. Forced outcomes
remain labeled and are excluded from automatic-policy quality metrics. They cannot create
qualification unless the normal evidence policy independently permits it.

## Future-only evidence updates

When the new configuration explicitly enables `economics.online_update.enabled`, a finalized run
appends a qualification observation and an economics observation only after checking baseline,
candidate completeness, authoritative verification, infrastructure resolution, required review,
isolation, corruption, and secret safety. Excluded and unverified outcomes are retained with a
reason but do not update a profile. New observations affect later route decisions only. A known
false acceptance immediately creates a severe qualification invalidation and quarantines the
profile; history is never deleted.

The run stores a validated `villani.online_evidence_update.v1` receipt in
`economics-update.json`. Legacy configurations do not gain this write side effect, and older run
manifests remain valid because the new artifact paths are optional.

## Safe policy lifecycle

Controller policy is deterministic data, never LLM-authored code. A proposed policy must replay
frozen historical cases point-in-time, using only evidence available at each original decision.
Publication fails closed unless conservative reliability is non-decreasing and false-acceptance
exposure is non-increasing.

```text
villani policy economics-evaluate --cases cases.json --proposed-policy policy.json --output evaluation.json
villani policy economics-publish --policy policy.json --evaluation evaluation.json
villani policy economics-status
villani policy economics-rollback [--to <published-version>]
```

Published versions are immutable and content-addressed. Activation changes one durable pointer, and
rollback moves that pointer to an earlier evaluated publication. Connectivity, execution, or a
small unmatched sample is not enough evidence to publish a policy safely.

## Evaluation scorecards

`villani.route_policy_evaluation.v1` compares `strongest_only`, `cheapest_qualified`,
`accepted_change_optimizer`, and `forced` policies. Each scorecard reports accepted-as-is, proved
acceptable, false acceptance, failures, known total cost, elapsed duration, review minutes,
escalation, measurable regret, unmatched outcomes, and unknown-input rate. Unknown or materially
unmatched samples remain warned and are not ranked as if equivalent.
