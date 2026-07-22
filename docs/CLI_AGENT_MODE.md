# CLI Agent Mode

Villani has one deterministic controller and four independently bound roles:

| Product label | Role ID | Purpose |
| --- | --- | --- |
| Understand task | `classification` | Classify difficulty, risk, uncertainty, and required capabilities before routing. |
| Write code | `coding` | Work in a fresh candidate worktree and return a recorded patch. |
| Verify result | `verification` | Review one candidate from a separate blind, read-only workspace and session. |
| Choose candidate | `selection` | Compare only acceptance-eligible candidates when deterministic evidence cannot choose one. |

An **agent system** is one configured API backend or installed Codex/Claude CLI plus an exact model
string and role policy. An **execution profile** is only a saved map from these four roles to agent
systems. Profiles do not create another controller or relax deterministic acceptance and delivery
rules.

## API, CLI, and hybrid modes

- **API mode** preserves the existing Villani behavior. Provider requests use configured API/local
  backends, and existing users migrate to the `api` profile.
- **CLI mode** launches a fresh non-resuming Codex CLI or Claude Code process for each bound role.
  A single installed CLI may fill all four roles, but every invocation is still a separate process,
  session, and role workspace.
- **Hybrid mode** mixes role bindings. For example, an API classifier can route a Claude Code coding
  attempt that a Codex verifier checks, while the deterministic selector remains in charge.

The setup flow does not require YAML:

```console
villani setup
villani agents detect
villani agents list
villani agents inspect codex-coder
villani agents doctor codex-coder
villani profiles list
villani profiles set-role hybrid coding codex-coder
villani profiles set-role hybrid verification claude-verifier
villani profiles set-role hybrid selection deterministic
villani profiles activate hybrid
```

Use the exact model string exposed by the installed CLI, or enter one and let Villani validate it
with a bounded read-only probe. Villani does not recommend a provider based on prestige and does not
silently replace an unavailable role with another system.

## Authentication and accounting

Authentication remains provider-owned. Sign in with the Codex or Claude Code command itself, then
run `villani agents doctor <system-id>`. Detection and Doctor do not start login, read credential
files, update the executable, or expose secrets.

Villani records usage or cost only when the CLI authoritatively reports it. Otherwise the value is
`null` with accounting status `unknown`. Villani has no subscription quota management, reset
tracking, billing control, or automatic account switching. CLI mode is not a quota bypass or a
promise of free inference.

## Isolation and independent verification

Each coding candidate receives its own Git worktree. Classification, verification, and selection
receive separate Villani-controlled read-only workspaces and never write the target or candidate
repositories. Codex read-only roles use a scoped permission profile; Claude read-only roles expose
only the role's permitted tools, with editing, ambient project instructions, plugins, hooks, MCP,
auto-memory, and session persistence disabled.

The verifier receives only the verbatim task and criteria, clean original repository
representation, one candidate patch and changed-file manifest, authoritative validation evidence,
and permitted debug facts. Provider/model/driver identity, order/rank, cost, tokens, duration,
competitors, coder transcript, and selector output are excluded. A verifier `1` is semantic evidence
only; deterministic validation, infrastructure health, eligibility, and delivery gates still apply.

Villani's worktree, provider permission, and process-tree controls are not a general kernel sandbox
for an arbitrarily malicious executable. Use a stronger configured container/VM boundary for
hostile repositories or untrusted CLI binaries.

## Supported-version policy

Support is capability-gated, not launch-gated. Codex must expose ephemeral JSONL execution,
schema-constrained output, non-interactive approvals, controlled configuration, and workspace
selection. Codex read-only roles require scoped permission profiles (`0.138.0` or newer) in addition
to the probed flags. Claude Code is currently bounded to `>=2.1.138,<2.2.0` and must expose print
mode, stream JSON, JSON Schema, no-session persistence, permission/tool restriction, and controlled
empty ambient configuration. A version that launches but fails any role requirement is
`UNSUPPORTED`, not production-ready.

## Troubleshooting

Run the role-specific doctor first:

```console
villani agents doctor <system-id>
```

Every non-ready result identifies affected roles, whether a repository was modified, an evidence
path, and one exact next action. Runtime infrastructure failure is shown separately from semantic
rejection and records the stage/role, agent system, safe summary, target-mutation fact,
partial-patch preservation, fallback fact, repair action, and evidence path.

Common outcomes:

- `ACTION_REQUIRED`: complete the exact provider-owned login or model action, then rerun Doctor.
- `UNSUPPORTED`: install a version exposing every listed role capability; Villani will not use an
  unsafe fallback flag.
- timeout/cancellation: inspect the linked process evidence. Partial candidate patches are retained
  when safely captured, descendants are terminated, and the candidate remains acceptance-ineligible.
- target drift or a dirty target: restore/review the target state and start a new delivery attempt;
  Villani will not apply a stale recorded patch.

## Deterministic release gate and real smoke

The CLI Agent Mode phase runs automatically inside the packaged release gate and can also run by
itself:

```console
python release-verification/run_cli_agent_gate.py --artifacts /tmp/villani-cli-gate
```

Its 30-scenario fake-executable suite exercises production argument construction and stream parsing
without provider calls. It writes a conformance matrix, resource bounds, secret scan, exact command
report, and digest evidence index even on failure or interruption.

Real-provider smoke is never part of ordinary unit tests. It creates disposable repositories only,
uses tiny generic tasks, preserves evidence, and requires explicit consent plus exact configured
model strings:

```console
python release-verification/run_cli_agent_smoke.py --detect-only
python release-verification/run_cli_agent_smoke.py --consent
```

The command states that calls may consume provider usage. Without `--consent` (or the documented
exact consent environment value), no model call is made. A skipped provider yields `PARTIAL`, never
full certification. `--detect-only` performs bounded executable/version/auth/capability probes and
returns without any model call.
