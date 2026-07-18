# Villani Ops

Villani Ops is a terminal-first orchestration layer for AI coding agents.

It runs multiple independent coding attempts in parallel, captures evidence from each run, compares the candidates, and materializes the best patch with a full audit trail.

## What it does

Villani Ops takes one repository task and runs a candidate tournament:

```text
1. Start from the original task and success criteria.
2. Launch independent Villani Code attempts in isolated worktrees.
3. Run attempts in parallel up to the backend's max_parallel setting.
4. Capture patches, telemetry, debug artifacts, and command evidence.
5. Review and compare candidates.
6. Select the strongest materializable candidate.
7. Apply the selected patch.
8. Write an auditable run report.
```

Candidate generation is intentionally clean: each candidate receives the original task and success criteria, not previous candidate failures, review notes, comparison notes, or hidden-test guesses.

## Install

From the repository root (install both public Python entry points so the
`villani` runner dependency is available):

```bash
pip install -e components/villani-code -e components/villani-ops
```

If `villani-code` is not installed, `villani run` stops before the first coding
attempt with an installation message. The local installer performs this paired
installation automatically.

## Quick start

Initialize a workspace:

```bash
villani-ops init
```

Add a backend. This example uses an OpenAI-compatible local endpoint:

```bash
villani-ops backend add qwen35b \
  --provider openai-compatible \
  --base-url http://127.0.0.1:1234/v1 \
  --model villanis/models/qwen3.6-35b-a3b-ud-iq4_xs.gguf \
  --api-key dummy \
  --input-cost 0.14 \
  --output-cost 1.00 \
  --roles coding,classification,review,policy,investigation,selection \
  --capability-score 32 \
  --max-tokens 50000 \
  --max-parallel 4
```

Run a tournament (equivalent one-line form: `villani-ops run --mode performance --repo ./repo --task "Fix the failing tests"`):

```bash
villani-ops run \
  --repo ./repo \
  --task "Fix the failing tests" \
  --success-criteria "Tests pass and the diff is minimal." \
  --mode performance \
  --runner villani-code \
  --candidate-attempts 4 \
  --orchestrator adaptive \
  --non-interactive
```

The canonical public CLI accepts either a short positional task or one UTF-8 task file; from inside
a Git repository the repository defaults to the current repository:

```powershell
villani run 'Fix the slug-generation bug'

villani run `
    --task-file 'C:\tasks\pytest-fix.md' `
    --repo 'C:\repos\pytest' `
    --delivery suggest
```

Use `--task-file` for multiline instructions, long Markdown tasks, PowerShell automation,
generated task specifications, and reproducible runs. Internal whitespace and line breaks are
preserved. Interrupted runs can be resumed with `villani resume <run_id>` or
`villani resume --latest`. Use `villani rerun <run_id>` to create a new, lineage-linked run with
fresh cost accounting and optional policy or budget overrides.

A bare public `villani run` uses the Performance preset and strongest eligible configured coding
route, requires verification, sets no wall-time budget, and waits for a non-destructive delivery
decision after a proved result. Success criteria are optional when the task itself is explicit;
`--success-criteria`, `--task-file`, advanced policy/budget flags, and `--json` remain available.
Default terminal output projects the four product stages and one final verdict from
`villani.product_run.v1`, while `--verbose` retains the technical stream. Use
`villani evidence <run_id>` for the recorded evidence index. Unknown cost or duration remains
unknown rather than numeric zero.

Accepted patches have an explicit delivery state. The public choices are:

```bash
villani run "Fix the failing tests" --delivery suggest
villani run "Fix the failing tests" --delivery approve
villani run "Fix the failing tests" --delivery apply
villani run "Fix the failing tests" --delivery branch
villani run "Fix the failing tests" --delivery pull-request
```

Suggest preserves the patch without repository mutation. Apply with approval pauses in the
persisted `AWAITING_APPROVAL` controller state and resumes through `villani approve <run_id>`;
`villani reject`, `villani request-rerun`, and `villani choose-candidate` record the other audited
review outcomes. Automatic apply, branch, and pull-request delivery fail closed unless configured
authority permits them. Branch delivery uses an isolated delivery worktree so the original branch
is never switched. GitHub, GitLab, and local-only provider adapters sit behind a provider-neutral
contract, and all delivery failures preserve the exact selected patch.

New configuration created by `villani init` explicitly defaults a bare `villani run` to automatic
apply for low-risk, acceptance-eligible results. That compatibility default is still authority
gated: absent, disabled, or insufficient delivery authority fails closed, and an explicit
`--delivery` choice takes precedence.

Normal model and policy setup does not require capability scores or YAML editing:

```bash
villani models detect
villani models add local-qwen --model qwen --provider local \
  --endpoint http://127.0.0.1:1234/v1 --default
villani models test
villani policy use balanced
villani policy explain "Fix the failing tests"
```

Models move through `UNRATED`, `BOOTSTRAP`, `OBSERVED`, `QUALIFIED`, and `DISABLED` without
presenting bootstrap choices or sparse observations as measured capability. Qualification requires
the configured sample minimum and Wilson confidence bound. Reliable, Balanced, Local first,
Cheapest acceptable, and Custom are the public presets; internal controller modes are Advanced
compatibility controls. `villani policy simulate --preset <preset>` evaluates recorded routing
decisions without changing live policy or claiming causal savings.

A coding route is recorded as a complete, content-addressed agent system. Inspect the migrated
identity, qualification, capability evidence, permissions, and harness diagnostics with:

```bash
villani agents list
villani agents inspect <route-or-system-id>
villani agents doctor [route-or-system-id]
```

Villani Code is the only production-enabled harness in PT5. External harness identities may be
configured for inspection, but remain disabled and cannot be selected until their contract and
qualification evidence are proven. See `docs/AGENT_SYSTEMS.md` for the lifecycle, evidence, and
compatibility contract.

At composition time the public CLI detects an installed, running `villani-agentd` and attaches the
closed-loop event sink. Local canonical events are committed before daemon delivery, daemon
absence or delivery degradation is diagnostic-only, and resume retains the original run ID and
event sequence. The controller depends only on the closed-loop sink contract and does not import
agentd.

`--candidate-attempts 4` means Villani Ops will try to run four independent candidate attempts. Parallelism is bounded by the backend's `--max-parallel` value.

## Main command

```bash
villani-ops run \
  --repo <path-to-repo> \
  --task "<task>" \
  --success-criteria "<success criteria>" \
  --mode performance \
  --runner villani-code \
  --candidate-attempts 4 \
  --orchestrator adaptive \
  --non-interactive
```

Recommended demo defaults:

| Option | Value | Purpose |
|---|---:|---|
| `--mode` | `performance` | Use the strongest enabled backend for orchestration and coding roles. |
| `--runner` | `villani-code` | Use Villani Code as the coding runner. |
| `--orchestrator` | `adaptive` | Use the candidate tournament orchestrator. |
| `--candidate-attempts` | `4` | Run multiple independent candidates. |
| `--timeout-seconds` | `1500` | Default run timeout if not explicitly set. |
| backend `--max-parallel` | `4` | Allow parallel candidate execution when capacity exists. |

## Legacy compatibility

The previous cost-policy runner remains available as a legacy compatibility command via `villani-ops cost-run` for older YAML policy workflows. New runs should use `villani-ops run --mode performance`.

## Adaptive tournament mode

`adaptive` is the main path.

In adaptive mode, Villani Ops uses parallel independent candidate generation plus comparative selection:

```text
Candidate generation:
  clean task prompt
  isolated worktree
  no feedback from other candidates

Candidate evaluation:
  evidence packet per candidate
  risk review
  pairwise comparison
  tournament ranking

Finalization:
  selected candidate materialized
  artifacts written to the run directory
```

Adaptive mode does not use decomposition as the primary demo path.

## Run output

Each run writes a directory under:

```text
.villani-ops/runs/<run-id>/
```

Important artifacts include:

```text
state.json
runtime_events.jsonl
cost_summary.json
candidates/<candidate_id>/patch.diff
candidates/<candidate_id>/evidence.json
candidates/<candidate_id>/runner_summary.json
reviews/<candidate_id>.json
comparisons/pairwise.json
comparisons/ranking.json
comparisons/agreement.json
selection.json
final_report.md
viewer/index.html
```

The run directory is the audit trail. It should show what each candidate did, why the winner was selected, what evidence was available, and what risks remained.

## Inspecting results

The CLI prints the run directory at the end of a run:

```text
Run directory: .villani-ops/runs/<run-id>
```

Start with:

```text
final_report.md
selection.json
comparisons/ranking.json
comparisons/pairwise.json
candidates/*/evidence.json
```

These files are the main product surface for now.

## Applying the result

The selected patch is materialized automatically when the run finishes accepted.

You can also use the helper commands exposed by the CLI:

```bash
villani-ops apply <run-id>
villani-ops branch <run-id> --name villani-ops/<run-id>
villani-ops pr <run-id> --title "Villani Ops changes"
```

## Backend roles

For the demo path, one strong backend can handle every role:

```text
coding
classification
review
policy
investigation
selection
```

The important setting for parallel attempts is:

```text
--max-parallel <N>
```

Villani Ops will not exceed the backend's configured parallelism.

## Current limitations

Villani Ops is alpha software.

Known limitations:

- Candidate selection is still experimental.
- If no authoritative validation exists, selection may be best-effort.

The current release is for testing the orchestration loop, candidate tournament, artifact trail, and local-first workflow.

## Founder Thesis Lab

The public `villani eval` command group captures content-addressed real-task suites, runs randomized direct-versus-Villani trials from identical restored baselines, records append-only human review, and generates JSON, Markdown, and HTML evidence reports plus Founder Gate B. It performs no passive monitoring and never applies trial patches to the source repository.

See the [Founder Thesis Lab guide](../../docs/FOUNDER_THESIS_LAB.md) for PowerShell and POSIX capture, run, review, reporting, and gate examples.

## Test suites

The default pytest command is the fast development suite. It excludes slow,
integration, and end-to-end tests so normal local validation stays responsive:

```bash
python -m pytest -q
```

Run the excluded suites explicitly when validating orchestration flows or slower
process behavior:

```bash
python -m pytest -m slow -q
python -m pytest -m integration -q
python -m pytest -m e2e -q
python -m pytest -m "slow or integration or e2e" -q
```

Slow/integration/e2e tests cover full agentic orchestration, real runner process
flows, long subprocess cleanup scenarios, and larger scenario fixtures. Fast
viewer, storage, CLI fake-backend, usage-normalization, graph-rendering, and
bounded subprocess unit tests remain in the default suite.
