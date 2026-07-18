# Founder Thesis Lab

The Founder Thesis Lab runs paired, reproducible trials against real repository snapshots. One arm invokes the strongest configured coding system exactly once. The other uses Villani's normal performance-mode control loop. Both start from the same frozen code archive and pass through the same independent final validation.

The lab is explicit and local: it does not watch background usage, collect passive telemetry, add an external harness, or modify the source repository. Every trial restores a fresh isolated baseline. Unknown cost or duration remains unknown rather than being reported as numeric zero.

## Capture a suite

Use an exact Git commit whenever possible. Baseline capture includes tracked regular files, applies built-in and operator exclusions, rejects high-confidence secret material, writes a deterministic code archive, and proves that the archive restores to the recorded digest.

PowerShell:

```powershell
villani eval init .villani-evaluation\founder --title "Founder paired tasks" --measured-power-watts 62 --electricity-price-per-kwh 0.32 --currency AUD
$baseline = (villani eval import-baseline .villani-evaluation\founder --repo C:\work\product --commit 9f6c2ab --json | ConvertFrom-Json).baseline_digest
villani eval add-task .villani-evaluation\founder "Keep the multiline task exactly as written." --baseline $baseline --success-criteria "The requested behavior is covered." --validation-command "python -m pytest -q" --category maintenance --risk medium
villani eval validate .villani-evaluation\founder --json
villani eval freeze .villani-evaluation\founder --disclosure-complete
villani eval export .villani-evaluation\founder --output founder-runner-bundle.zip
```

POSIX shell:

```sh
villani eval init .villani-evaluation/founder --title "Founder paired tasks" --measured-power-watts 62 --electricity-price-per-kwh 0.32 --currency AUD
baseline=$(villani eval import-baseline .villani-evaluation/founder --repo /work/product --commit 9f6c2ab --json | python -c "import json,sys; print(json.load(sys.stdin)['baseline_digest'])")
villani eval add-task .villani-evaluation/founder "Keep the multiline task exactly as written." --baseline "$baseline" --success-criteria "The requested behavior is covered." --validation-command "python -m pytest -q" --category maintenance --risk medium
villani eval validate .villani-evaluation/founder --json
villani eval freeze .villani-evaluation/founder --disclosure-complete
villani eval export .villani-evaluation/founder --output founder-runner-bundle.zip
```

Use `--task-file` when the task is multiline. Repeat `--success-criteria`, `--validation-command`, `--setup-command`, `--include`, and `--exclude` as needed. `--hidden-validation-command`, `--hidden-check-file`, and `--future-context-file` place evaluator facts outside the runner payload. The portable export contains the actual allowed code and runner-visible task contract, but never task identity, future material, hidden checks, or an expected patch.

Do not put credentials in task text, command arguments, review notes, or source files. Refer to configured environment-variable names instead. Capture and trial writing fail closed when high-confidence credential patterns are found.

## Run paired trials

```console
villani eval run .villani-evaluation/founder --arms direct,villani --repetitions 3
```

Arm order is deterministically randomized per task and repetition, then persisted in `run-plan.json`. Trials run sequentially by default. Re-running the same command skips completed and excluded identities; an interrupted identity resumes without creating a duplicate.

The direct arm gets the frozen baseline, verbatim task, criteria, permissions, and runner-visible validation context. It gets one coding-system invocation and no Villani retry, selection, or verifier correction. The Villani arm uses normal mandatory verification, retry, escalation, and selection. Neither arm delivers into the source repository. The same arm-blind final verifier restores another copy of the baseline, applies the captured patch, enforces file-change policy, and runs every authoritative command.

Local compute cost is calculated only when measured watts, elapsed runtime, electricity price, and currency were configured. The lab does not invent a cloud-equivalent local price. Provider costs depend on captured usage and configured billing facts; missing data stays `null` with an accounting status.

## Blinded review

```console
villani eval review-queue .villani-evaluation/founder --json
villani eval review .villani-evaluation/founder TRIAL_ID --reviewer founder --outcome accepted_as_is --review-minutes 6
villani eval review .villani-evaluation/founder TRIAL_ID --reviewer founder --outcome accepted_after_correction --review-minutes 11 --correction-summary "Handled the uncovered edge case." --severity medium --amend REVIEW_ID
```

The queue omits arm identity and links only to the patch and final validation result. Keep `run-plan.json` and `trial.json` away from a blinded reviewer until labels are complete. Outcomes are `accepted_as_is`, `accepted_after_correction`, or `rejected`. Later rollback and reopened-defect facts are optional amendment flags. Every label and amendment is appended to `human-reviews.jsonl`; prior records are never overwritten.

## Reports and Gate B

```console
villani eval report .villani-evaluation/founder
villani eval gate .villani-evaluation/founder --json
```

The report command writes `evaluation-report.json`, `evaluation-report.md`, and `evaluation-report.html`. Reports lead with reliability, review time, cost, false acceptance, task classes, and failure modes; retain raw counts and confidence intervals; disclose unknowns and exclusions; and link every trial bundle. Binary verification has no calibrated probability, so the report marks calibration undefined instead of inventing one.

Gate B returns:

- `PASS` (exit 0) only with at least 30 paired real tasks across two repositories, valid baselines, complete human review, zero known false acceptance, no lower accepted-as-is rate, the required review-time or cost improvement, at least 80% automatic configuration, and complete disclosure.
- `FAIL` (exit 1) when sufficient evidence violates a gate or a fail-closed integrity/disclosure requirement fails.
- `INSUFFICIENT_EVIDENCE` (exit 2) when the evidence volume or required measurements are incomplete.

Synthetic fixtures are useful for testing mechanics but are structurally ineligible for Founder Gate evidence. Small samples never produce an automatic significance claim.
