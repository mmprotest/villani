# Protocol/schema fixture evaluation

Command:

```console
python evaluation/final_gate.py --output evaluation/results/final-foundation.json
```

The locked fixture environment uses `deterministic-fixture-agent@1`, cheap/strong fixture models
at version 1, `closed-loop-system@v1`, `verification-graph@v1`, Python 3.11, and protocol v2.
There are 20 deterministic observations per strategy. Results are fixture measurements, not live
provider performance. It invokes no model, attempts no coding task, and supports no routing-quality
or cost-savings conclusion.

| Strategy | Verified success (95% Wilson CI) | False accept / reject | Cost per accepted | Wall ms | Attempts / escalations | Verifier cost |
| --- | --- | --- | --- | --- | --- | --- |
| Strong-only | 16/20, 80% (58.40–91.93%) | 0 / 1 | 2.5000 | 18,000 | 20 / 0 | 4.00 |
| Cheap-only | 10/20, 50% (29.93–70.07%) | 1 / 3 | 0.8000 | 10,000 | 20 / 0 | 2.40 |
| Cheap-first escalation | 16/20, 80% (58.40–91.93%) | 0 / 1 | 1.5625 | 22,000 | 32 / 12 | 5.00 |
| Strong-first | 16/20, 80% (58.40–91.93%) | 0 / 1 | 2.5000 | 18,400 | 20 / 0 | 4.00 |
| Adaptive candidate | 15/20, 75% (53.13–88.81%) | 0 / 2 | 1.4667 | 17,000 | 28 / 8 | 4.40 |

Cheap-first escalation produced six more fixture acceptances than cheap-only for 17.0 incremental
fixture cost units. The locked minimum for a savings claim is 30 observations per strategy, so
**no savings claim is supported**. Raw `fixture://` run references and all confidence intervals are
in the JSON report. No benchmark-specific production routing logic was added.

An opt-in live evaluation is intentionally separate. Copy and version the example manifest, then
execute the real public path explicitly:

```console
python evaluation/live_evaluation.py --manifest evaluation/live-task-manifest.example.json --output evaluation/results/live.json --execute
```

The live entry point supports `strong-only`, `cheap-only`, `cheap-first-escalation`,
`strong-first`, and `adaptive`; records real run IDs plus model/provider/prompt/verifier,
environment, and repository revisions; and computes verified success, false acceptance/rejection,
costs, attempts, escalation value, wall time, and confidence intervals. It refuses to claim
savings below the manifest's minimum sample size and never changes production routing. Live paid
evaluation is not part of ordinary pull-request CI and no live/paid measurement is claimed here.
