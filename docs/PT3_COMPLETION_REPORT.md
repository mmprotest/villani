# PT3 completion report — Founder Thesis Lab

Milestone status: **INSUFFICIENT_EVIDENCE**  
Implementation status: **COMPLETE**  
Gate B: **INSUFFICIENT_EVIDENCE**  
Date: 2026-07-17

The Founder Thesis Lab implementation is complete and every PT3 software acceptance criterion is covered. The milestone is not reported as complete evidence because no real founder task corpus or paired founder trials were supplied. Gate B has 0 of 30 required paired real tasks across 0 of 2 required repositories. Synthetic fixtures were deliberately excluded; no significance, cost saving, or reliability advantage is claimed.

The machine-readable completion record is `docs/PT3_COMPLETION_REPORT.json`. The machine-readable Gate B record is `docs/PT3_GATE_B.json`.

## Acceptance results

| Criterion | Result | Evidence |
|---|---|---|
| Real code tasks can be packaged reproducibly | PASS | CLI capture/freeze/export test restores and freezes a content-addressed Git archive containing actual allowed code |
| Both arms use identical baselines | PASS | Direct and Villani trial records assert the same immutable and restored digest after the source changes |
| Future solutions cannot leak | PASS | Runner payload, verifier input, and portable-export non-leakage assertions |
| Human review is first-class | PASS | Blinded queue plus append-only reviews and amendments |
| Reliability, cost, time, and supervision are measured | PASS | Raw counts, intervals, paired deltas, accounting, attempt/escalation/disagreement tests |
| No passive monitoring exists | PASS | Explicit-command workflow; validation returns `passive_monitoring=false` |
| No external harness is added | PASS | Existing `VillaniCodeRunner` and public Villani CLI are reused; validation returns `external_harness=false` |
| No later milestone is started | PASS | PT4 behavior is absent |

There is no PT3 acceptance failure. Gate B remains insufficient because the required real evidence does not exist in the workspace.

## User-facing behavior

Before PT3, Villani could evaluate replay/capability evidence offline, but it had no reproducible founder-task capture, paired direct-versus-Villani runner, blinded human review ledger, thesis report, or Founder Gate command.

After PT3, `villani eval` provides:

- `init`, `import-baseline`, `add-task`, `validate`, `freeze`, and `export` for content-addressed task capture;
- `run --arms direct,villani --repetitions N` for randomized, sequential, resumable paired trials;
- `review-queue` and append-only `review` amendments;
- JSON, Markdown, and linked HTML `report` output;
- a `gate` command returning `PASS`, `FAIL`, or `INSUFFICIENT_EVIDENCE` with exit 0, 1, or 2.

The source repository is never modified. Provider/local cost remains unknown unless actual usage/configuration or measured power/runtime/electricity inputs exist.

## Architecture and product decisions

- The five strict v1 Pydantic contracts generate normative and packaged schemas and have matching TypeScript models.
- Baselines are exact Git commits captured as deterministic regular-file ZIP archives. Paths are normalized, symlinks/submodules are excluded, executable mode is preserved, likely secrets fail closed, archives are hashed, and restoration is proved.
- Future context and hidden checks live only in evaluator storage. Runner payloads omit task identity, future/expected-solution facts, hidden material, arm, route, harness, cost, and competing candidates. Portable exports use opaque task slots.
- The direct arm selects the strongest enabled coding backend and invokes `VillaniCodeRunner` once. It receives the same runner-visible task, criteria, permissions, and validation context, but no Villani retry, selection, or corrective verifier feedback.
- The Villani arm uses the normal performance-mode product path with mandatory verification and non-delivery approval. Both arms get fresh restored isolation.
- One separate arm-blind verifier restores the baseline again, applies either patch, enforces file-change policy, and runs the same authoritative commands. Its input contains no arm, harness, route, cost, or competitor identity.
- A persisted deterministic randomized plan, deterministic trial IDs, an atomic suite lock, and terminal-trial skipping prevent duplicates. Interrupted trials resume under the same ID.
- Cost derives only from captured provider telemetry plus configured billing facts and optional measured local power/runtime/electricity. Unknown remains null; incompatible currencies cannot be combined or compared.
- Human labels are separate append-only records. An amendment references an earlier record and never rewrites it.
- Reports always retain raw counts. Proportions use Wilson intervals and medians use deterministic bootstrap intervals. Binary proof has no probability, so calibration is reported undefined.
- Gate B excludes synthetic fixtures and never claims statistical significance automatically.

## Contracts and migrations

Added, without changing an existing public contract:

- `villani.evaluation_suite.v1`
- `villani.evaluation_task.v1`
- `villani.evaluation_trial.v1`
- `villani.human_review.v1`
- `villani.evaluation_report.v1`

The root schemas remain normative and the wheel carries byte-identical packaged copies. The Python registry, TypeScript package, and durable fixtures were updated together. No configuration migration, run-bundle migration, data rewrite, or generated-asset migration was introduced. Existing acceptance, routing, verification, selection, and delivery behavior is unchanged.

## Exact file inventory

### Added

```text
components/villani-ops/villani_ops/evaluation_lab/__init__.py
components/villani-ops/villani_ops/evaluation_lab/models.py
components/villani-ops/villani_ops/evaluation_lab/workspace.py
components/villani-ops/villani_ops/evaluation_lab/runner.py
components/villani-ops/villani_ops/evaluation_lab/reviews.py
components/villani-ops/villani_ops/evaluation_lab/reporting.py
components/villani-ops/villani_ops/schemas/v1/evaluation-suite.schema.json
components/villani-ops/villani_ops/schemas/v1/evaluation-task.schema.json
components/villani-ops/villani_ops/schemas/v1/evaluation-trial.schema.json
components/villani-ops/villani_ops/schemas/v1/human-review.schema.json
components/villani-ops/villani_ops/schemas/v1/evaluation-report.schema.json
components/villani-ops/villani_ops/tests/test_founder_thesis_lab.py
components/villani-run-model/src/evaluation.ts
components/villani-run-model/dist/evaluation.js
components/villani-run-model/dist/evaluation.d.ts
components/villani-run-model/test/evaluation.test.ts
schemas/v1/evaluation-suite.schema.json
schemas/v1/evaluation-task.schema.json
schemas/v1/evaluation-trial.schema.json
schemas/v1/human-review.schema.json
schemas/v1/evaluation-report.schema.json
integration/fixtures/protocol/v1/valid_run/evaluation-suite.json
integration/fixtures/protocol/v1/valid_run/evaluation-task.json
integration/fixtures/protocol/v1/valid_run/evaluation-trial.json
integration/fixtures/protocol/v1/valid_run/human-review.json
integration/fixtures/protocol/v1/valid_run/evaluation-report.json
scripts/generate-evaluation-schemas.py
docs/FOUNDER_THESIS_LAB.md
docs/PT3_GATE_B.json
docs/PT3_COMPLETION_REPORT.json
docs/PT3_COMPLETION_REPORT.md
```

### Changed

```text
PLANS.md
README.md
components/villani-ops/README.md
components/villani-ops/villani_ops/cli/unified.py
components/villani-ops/villani_ops/closed_loop/schema_validation.py
components/villani-ops/villani_ops/tests/closed_loop/test_protocol.py
components/villani-run-model/src/index.ts
components/villani-run-model/dist/index.js
components/villani-run-model/dist/index.d.ts
```

### Deleted or migrated

None.

## Tests added

Fifteen Python tests cover immutable capture, baseline equality, randomized-order persistence, interrupted resume, concurrent duplicate lockout, direct-arm isolation, future/hidden non-leakage, actual-code export, secret exclusion, secret-bearing patch rejection, setup contamination, append-only review, metrics/accounting, Gate B states, report redaction/structure, external paths, complete CLI capture, passive-monitoring absence, external-harness absence, and strongest backend selection.

Two TypeScript tests cover the five exact schema identities, evaluator-only expected-patch absence, and explicit unknown cost units. Shared protocol tests now validate five additional fixtures and 18 total v1 schemas.

The mandatory test mapping is recorded in full in `docs/PT3_COMPLETION_REPORT.json`.

## Validation commands and exact results

Commands are shown from the stated working directory. Intermediate failures and environment limits are retained.

### PT3 and shared protocol

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\test_founder_thesis_lab.py villani_ops\tests\closed_loop\test_protocol.py --basetemp .test-temp\pt3-focused
```

Initial result: **FAIL** — 22 passed, 4 failed in 7.97 seconds. All four failures were Windows cleanup of read-only Git objects. After the cleanup fix, the same command passed 26 tests in 12.45 seconds.

The final expanded command was:

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\test_founder_thesis_lab.py villani_ops\tests\closed_loop\test_protocol.py --basetemp .test-temp\pt3-focused-lock
```

Final result: **PASS** — 32 passed in 14.12 seconds.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp .test-temp\pt3-full
```

Result: command timeout after 121 seconds at 30%; no failure had appeared.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp .test-temp\pt3-full-rerun
```

Result: **PASS** — 1,178 passed, 2 skipped, 114 deselected in 236.08 seconds.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp .test-temp\pt3-full-final
```

Final result: **PASS** — 1,178 passed, 2 skipped, 114 deselected in 244.81 seconds.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m pytest -q villani_ops\tests\test_hardened_execution_environment.py -rs --basetemp .test-temp\pt3-skip-reasons
```

Result: **PASS** — 23 passed, 2 skipped in 3.35 seconds. The host Python lacks Unix-domain socket and FIFO creation support.

### Lint and type checks

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m ruff check villani_ops\evaluation_lab villani_ops\tests\test_founder_thesis_lab.py villani_ops\closed_loop\schema_validation.py villani_ops\cli\unified.py
```

Initial result: four unused imports. Final result: **PASS** — all checks passed.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m mypy villani_ops\evaluation_lab villani_ops\closed_loop\schema_validation.py
```

Result: broad followed-import **FAIL** — 206 existing errors in 43 files while checking 7 source files, primarily outside PT3.

```powershell
# cwd: components/villani-ops
& ..\..\.venv\Scripts\python.exe -m mypy --follow-imports=skip --ignore-missing-imports villani_ops\evaluation_lab
```

Final result: **PASS** — no issues in 6 PT3 source files.

### Shared TypeScript model

```powershell
# cwd: components/villani-run-model
npm test
npm run typecheck
```

Environment result: both commands failed because this standalone checkout has no local `vitest` or `tsc` executable.

```powershell
# cwd: components/villani-run-model
& ..\villani-web\node_modules\.bin\vitest.cmd run
& ..\villani-web\node_modules\.bin\tsc.cmd --noEmit -p tsconfig.json
& ..\villani-web\node_modules\.bin\tsc.cmd -p tsconfig.json
& ..\villani-web\node_modules\.bin\prettier.cmd --check src\evaluation.ts test\evaluation.test.ts src\index.ts
```

Final results: **PASS** — 2 files/7 tests; no-emit typecheck; emitted build; and formatting. The first Prettier check identified `evaluation.ts` and `index.ts`; after formatting, all matched files passed.

### Required cross-component validation

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m pytest tests\closed_loop -q --basetemp .test-temp\pt3-closed-loop-final
```

Result: **PASS** — 11 passed, 2 warnings in 43.01 seconds.

```powershell
# cwd: components/villani-code
& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp .test-temp\pt3-code
```

Environment result: the custom base-temp parent did not exist; 212 passed, 1 skipped, and 474 tests errored during setup. After creating the parent, the exact rerun was:

```powershell
# cwd: components/villani-code
& ..\..\.venv\Scripts\python.exe -m pytest -q --basetemp .test-temp\pt3-code-rerun
```

Result: baseline **FAIL** in an untouched component — 74 failed, 612 passed, 1 skipped, 101 warnings in 73.30 seconds. Failures converge on `context_projection.py` slicing mocked `subprocess.run(...).stdout=None`; Windows subprocess-reader decoding warnings are also present. PT3 does not change Villani Code.

```powershell
# cwd: components/villani-flight-recorder
npm test
npm run typecheck
npm run build
npm run format:check
```

Results: **PASS** — build plus 21 files/111 tests; typecheck; build; and all-file Prettier check.

### Schema, packaging, secrets, and repository hygiene

```powershell
# cwd: repository root
& .venv\Scripts\python.exe scripts\generate-evaluation-schemas.py
```

Initial development result: import-cycle failure. Final repeated result: **PASS** and idempotent.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -c "from pathlib import Path; import hashlib; a=Path('schemas/v1'); b=Path('components/villani-ops/villani_ops/schemas/v1'); names=['evaluation-suite.schema.json','evaluation-task.schema.json','evaluation-trial.schema.json','human-review.schema.json','evaluation-report.schema.json']; bad=[n for n in names if hashlib.sha256((a/n).read_bytes()).digest()!=hashlib.sha256((b/n).read_bytes()).digest()]; print(f'PT3 schema parity: {len(names)-len(bad)}/{len(names)} identical'); print('mismatches:',bad); raise SystemExit(bool(bad))"
```

Result: **PASS** — 5/5 identical, no mismatches.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe scripts\check-secrets.py components\villani-ops\villani_ops\evaluation_lab components\villani-ops\villani_ops\cli\unified.py components\villani-ops\villani_ops\closed_loop\schema_validation.py components\villani-ops\villani_ops\tests\test_founder_thesis_lab.py components\villani-ops\villani_ops\tests\closed_loop\test_protocol.py components\villani-run-model\src\evaluation.ts components\villani-run-model\src\index.ts components\villani-run-model\test\evaluation.test.ts components\villani-run-model\dist\evaluation.js components\villani-run-model\dist\evaluation.d.ts components\villani-run-model\dist\index.js components\villani-run-model\dist\index.d.ts docs\FOUNDER_THESIS_LAB.md docs\PT3_COMPLETION_REPORT.md docs\PT3_COMPLETION_REPORT.json docs\PT3_GATE_B.json scripts\generate-evaluation-schemas.py schemas\v1\evaluation-suite.schema.json schemas\v1\evaluation-task.schema.json schemas\v1\evaluation-trial.schema.json schemas\v1\human-review.schema.json schemas\v1\evaluation-report.schema.json components\villani-ops\villani_ops\schemas\v1\evaluation-suite.schema.json components\villani-ops\villani_ops\schemas\v1\evaluation-task.schema.json components\villani-ops\villani_ops\schemas\v1\evaluation-trial.schema.json components\villani-ops\villani_ops\schemas\v1\human-review.schema.json components\villani-ops\villani_ops\schemas\v1\evaluation-report.schema.json integration\fixtures\protocol\v1\valid_run\evaluation-suite.json integration\fixtures\protocol\v1\valid_run\evaluation-task.json integration\fixtures\protocol\v1\valid_run\evaluation-trial.json integration\fixtures\protocol\v1\valid_run\human-review.json integration\fixtures\protocol\v1\valid_run\evaluation-report.json
```

Initial result: two fake credential literals in tests were flagged and changed to runtime construction. Final result: **PASS** — 32 roots, 0 findings.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -m build --wheel --outdir .test-temp\pt3-wheel components\villani-ops
```

Sandbox result: isolated build requirements could not reach the package index. Approved rerun: **PASS** — `villani_ops-0.2.0-py3-none-any.whl` built.

```powershell
# cwd: repository root; approved read of the already-built wheel
& .venv\Scripts\python.exe -c "from pathlib import Path; import zipfile; p=next(Path('.test-temp/pt3-wheel').glob('*.whl')); required={'villani_ops/evaluation_lab/__init__.py','villani_ops/evaluation_lab/models.py','villani_ops/evaluation_lab/workspace.py','villani_ops/evaluation_lab/runner.py','villani_ops/evaluation_lab/reviews.py','villani_ops/evaluation_lab/reporting.py','villani_ops/schemas/v1/evaluation-suite.schema.json','villani_ops/schemas/v1/evaluation-task.schema.json','villani_ops/schemas/v1/evaluation-trial.schema.json','villani_ops/schemas/v1/human-review.schema.json','villani_ops/schemas/v1/evaluation-report.schema.json'}; names=set(zipfile.ZipFile(p).namelist()); missing=sorted(required-names); print(f'pt3_wheel_required={len(required)} present={len(required)-len(missing)} missing={missing}'); assert not missing"
```

Result: **PASS** — 11/11 required PT3 modules/schema files present, none missing.

```powershell
# cwd: repository root
& .venv\Scripts\python.exe -c "import json,pathlib; files=['docs/PT3_COMPLETION_REPORT.json','docs/PT3_GATE_B.json']; [json.loads(pathlib.Path(f).read_text(encoding='utf-8')) for f in files]; print('completion_json=valid; gate_json=valid')"
```

Result: **PASS** — both machine-readable reports parse as JSON.

```powershell
# cwd: repository root
git diff --check
```

Result: **PASS** — no whitespace errors; existing LF-to-CRLF working-copy warnings only.

## End-to-end artifacts and screenshots

- Five normative and five packaged schemas.
- Five shared protocol fixtures.
- CLI capture/freeze/export and report artifacts generated and inspected in isolated tests.
- JSON, Markdown, and linked HTML report output verified for structure and redaction.
- Ignored wheel artifact `.test-temp/pt3-wheel/villani_ops-0.2.0-py3-none-any.whl`, with 11/11 PT3 files verified.
- `docs/PT3_GATE_B.json` records the actual insufficient-evidence state.

No screenshots were created: PT3 adds a CLI/evidence workflow, not a new browser surface, and this milestone specified no screenshot contract.

## Known failures and skipped tests

- Gate B is `INSUFFICIENT_EVIDENCE`: there are no eligible real founder trials. This is not replaced with synthetic data.
- The untouched Villani Code full suite has 74 baseline failures, 612 passes, and one opt-in Claude Code smoke skip.
- Villani Ops skips two host-capability tests because the current Python host lacks Unix-domain sockets and FIFO creation. Its default configuration deselects 114 slow/integration/e2e tests by declared markers.
- Broad followed-import mypy retains 206 transitive repository errors. All six direct PT3 modules pass focused mypy.
- The standalone Run Model has no local npm tools; pinned repository binaries pass all model checks.

## Security, privacy, data-loss, and compatibility risks

- High-confidence secret scanning and bounded output redaction cannot recognize every credential format. Operators must use exclusions and inspect bundles before sharing.
- Portable bundles contain actual source code; reports identify products, harnesses, models, providers, serving engines, and environment fingerprints. Confidentiality is marked but files are not encrypted.
- Setup, coding, and validation commands execute with configured process permissions. Fresh Git isolation is not a kernel sandbox; hostile tasks should use the existing container/devcontainer provider.
- Capture reads the source commit and trials never apply to the source repository. The lab only creates evaluation workspace, review, report, and artifact data; it performs no migration or deletion.
- A hard-killed process can leave `evaluation-run.lock`; subsequent execution fails closed and prints the exact manual recovery action.
- Review blinding is procedural rather than cryptographic. Reviewers must not open `run-plan.json` or `trial.json` until labeling is complete.
- All contracts are additive. Existing bundles and configuration remain readable.

## Assumptions and next action

No founder task corpus, provider credentials, measured outcomes, or authorization to incur provider charges was supplied. The lab therefore stops before a real paid trial.

To resolve Gate B, capture and freeze at least 30 real tasks from at least two founder repositories, run both arms, complete blinded reviews, generate the suite report, and execute:

```console
villani eval gate <suite> --json
```

## Milestone boundary

PT4 was not started.
