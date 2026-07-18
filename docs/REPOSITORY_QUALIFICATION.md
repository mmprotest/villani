# Repository-specific qualification

PT7 turns agent-system connectivity into a repository- and task-aware decision. A system is never
qualified because it is installed, authenticated, or able to complete a hello-world task.
Qualification is derived from immutable evidence under the versioned
`repository_qualification_v1` policy.

## States

- `qualified`: at least 20 eligible observations at an approved profile or backoff level, no known
  false acceptance, a Wilson lower bound strictly above the task-risk threshold, exact conformance,
  current evidence, and no severe drift. Only this state is automatically selectable.
- `provisional`: valid matching evidence exists, but its sample, confidence, recency, or approved
  backoff is insufficient. When no qualified route exists, the controller may use only the
  strongest otherwise-eligible provisional route and records that fallback explicitly.
- `experimental`: no eligible matching evidence exists, exact conformance is unproved, or a false
  case or severe drift makes reliability unknown. Automatic routing never selects it.
- `unsupported`: a required capability is absent, conformance failed, the environment is
  prohibited, the version is incompatible, authentication is unavailable, or the route was
  explicitly disabled or invalidated. It cannot be overridden.

The state belongs to a repository, task profile, complete agent-system identity, execution
environment, verification-policy version, evidence time window, and software-version set. There is
no global agent score.

## Profile and backoff

Evidence resolution starts with the exact repository and task profile, then conservatively checks:

1. exact repository, category, difficulty, risk, and required capabilities;
2. repository and category, using observations at least as difficult and risky;
3. repository-wide evidence with compatible difficulty, risk, and capabilities;
4. an explicitly configured and approved compatible-repository cohort; or
5. no evidence.

Repositories are not pooled because they share a language or framework. Repository identity uses a
credential-free digest of the Git origin when present, or a digest of the resolved local path.
Only observations whose commit is an ancestor of the current `HEAD` can qualify the current
lineage. Divergent history is retained and exposed as drift.

## Eligible evidence

`villani agents qualify` imports trials from a frozen `real_founder_work` evaluation suite and
validates the suite, task, immutable restored baseline, candidate artifacts, exact configured
system identity, authoritative binary verification, resolved infrastructure status, append-only
human review, isolation result, artifact integrity, and secret scan. A successful observation must
be both proved acceptable and accepted as-is when review is required.

Cancellation, provider outage, environment mismatch, missing executable, policy denial, verifier
or delivery infrastructure failure, corruption, missing review, and secret findings are persisted
as exclusions. They do not become task failures and do not increase the eligible sample. Unknown
cost or duration remains `null` with an explicit accounting status; it is never numeric zero.
Later rollback, reopened defect, false acceptance, and false rejection are recorded by a newer
append-only projection for the same trial. Earlier history remains inspectable.

Direct JSON import cannot claim eligible evidence. `--evidence` accepts only already-excluded
records with the exact configured identity; eligible observations must be reconstructed locally
from `--suite` and `--trial`.

## Statistics and policy

Each derived profile stores eligible sample, successes, failures, exclusions by reason, acceptance
rate, Wilson lower bound, proved-acceptable and accepted-as-is counts, false cases, known cost by
currency, accepted-change cost, unknown cost counts, duration, review minutes, last evidence time,
software-version diversity, and drift flags. Empty samples have unknown rates rather than zero.

The additive default configuration is:

```yaml
qualification:
  schema_version: villani.repository_qualification_configuration.v1
  policy:
    policy_version: repository_qualification_v1
    minimum_qualified_observations: 20
    provisional_maximum_observations: 19
    wilson_z: 1.959963984540054
    task_wilson_thresholds:
      low: 0.60
      medium: 0.70
      high: 0.80
    maximum_evidence_age_days: 180
    recent_reliability_window: 5
    approved_backoff_levels:
      - exact_repository_task
      - repository_category
      - repository_wide
    compatible_repository_cohorts: {}
    approved_repository_cohorts: []
```

Unknown configuration or policy versions and unknown policy fields fail closed. A cohort is used
only when both its membership and its name are explicitly approved.

## Commands

Record locally verified trial evidence:

```text
villani agents qualify <route> --suite <frozen-suite> --trial <trial-id> \
  --category maintenance --difficulty medium --risk medium
```

Inspect status and immutable evidence:

```text
villani agents status [route] --repo <repository> --category maintenance \
  --difficulty medium --risk medium
villani agents evidence <route> --repo <repository>
```

Persist an invalidation without deleting history:

```text
villani agents invalidate <route> --repo <repository> \
  --reason capability_loss --severity unsupported \
  --detail "Required isolation capability is unavailable." \
  --evidence-reference doctor/conformance.json
```

Run an experimental system only with an explicit, same-run acknowledgement:

```text
villani run --repo <repository> --task "..." --agent-system <route> \
  --allow-experimental
```

The CLI displays `Experimental` before execution and records
`qualification_created: false`. An unsupported route cannot be manually selected.

## Drift and invalidation

Harness or protocol incompatibility, model identity change, provider or serving-engine change,
execution-environment change, verification-policy change, repository lineage divergence, recent
reliability breach, false acceptance, capability loss, and stale evidence downgrade safely. Fresh
eligible evidence for the complete current identity can requalify a route; historical drift remains
visible as a warning rather than being deleted.

The durable store is under `$VILLANI_HOME/qualification/`:

```text
observations.jsonl
invalidations.jsonl
snapshot-v1.json
```

The two JSONL ledgers are append-only and guarded by a process lock. The snapshot is a derived,
content-addressed projection and can be rebuilt. Truncated, duplicate, malformed, or secret-shaped
records fail closed.

## Scorecards and Gate C

```text
villani agents gate-c --repo <repository> --category maintenance \
  --difficulty medium --risk medium --output gate-c.json
```

Gate C emits `PASS`, `FAIL`, or `INSUFFICIENT_EVIDENCE` and exits 0, 1, or 2 respectively. It
requires repository-specific scorecards for Villani Code, Codex, and Claude Code and checks
evidence parity, exact identity, conformance, isolation, unsupported behavior, qualification-state
correctness, and automatic-routing eligibility. Scorecards disclose accepted-as-is,
proved-acceptable, false cases, failures, cost, duration, review time, sample, and backoff. Unknown
or materially unmatched samples are warned and are not ranked.

## Compatibility and privacy

The PT7 schemas and configuration are additive. Existing runs and PT5/PT6 agent-system documents
remain readable. The former capability snapshot is recorded as an excluded migration because it
lacks repository lineage, complete identity, execution fingerprint, and required review; it never
silently creates qualification.

Qualification evidence may contain repository paths, patches, trial output, and review metadata.
Ledgers reject recognized secret shapes and reports redact known credentials, but artifacts are not
encrypted and bounded redaction cannot recognize every secret. Inspect evidence before sharing it.
